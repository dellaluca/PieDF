"""Costruzione del PDF riorganizzato (#26), senza Qt: gira in un processo a parte.

Il risultato si descrive con una lista di `PageRef` nell'ordine finale. Le pagine del
documento aperto (sorgente 0) non si copiano: si parte dal documento stesso, si eliminano
quelle tolte e si spostano le altre (`delete_pages` / `move_page`). Così restano
cifratura (salvataggio con `PDF_ENCRYPT_KEEP`, stesse password, anche l'owner), campi,
annotazioni, link e il resto del catalogo. `Document.select()` invece ricostruisce il
catalogo e perde l'AcroForm. Le pagine degli altri file arrivano con `insert_pdf`, quelle
vuote con `new_page`; la rotazione aggiunta si somma a quella della pagina.

L'indice (segnalibri) segue le pagine spostate da solo, perché punta agli oggetti pagina;
le voci delle pagine eliminate si tolgono. I link interni verso pagine eliminate restano
senza destinazione.
"""

import os
from dataclasses import dataclass

import pymupdf

MAIN = 0  # sorgente: il documento aperto
BLANK = -1  # sorgente: pagina vuota di dimensione `size`
TMP_SUFFIX = ".piedf-tmp"


@dataclass(frozen=True, eq=False)
class PageRef:
    source: int  # indice in `sources`, MAIN o BLANK
    page: int  # indice da 0 nel file sorgente (-1 per la pagina vuota)
    # Punti PDF, pagina come si vede con la sua /Rotate, prima di `rotate`.
    size: tuple[float, float]
    rotate: int = 0  # rotazione aggiunta in senso orario: 0, 90, 180, 270

    @property
    def shown_size(self) -> tuple[float, float]:
        """Dimensione come si vedrà, con la rotazione aggiunta."""
        w, h = self.size
        return (h, w) if self.rotate % 180 else (w, h)


def build(sources: list[tuple[str, str | None]], refs: list[PageRef], dest: str, progress=None) -> None:
    """Scrive in `dest` il PDF fatto delle pagine `refs`. `sources[i]` = (percorso, password).

    `progress(fatti, totale)` si chiama dopo ogni pagina; l'ultimo passo è il salvataggio.
    Il file si scrive accanto a `dest` e poi lo sostituisce: mai un file a metà.
    """
    if not refs:
        raise ValueError("Nessuna pagina")
    total = len(refs) + 1
    step = progress or (lambda done, total: None)
    docs = {}
    tmp = dest + TMP_SUFFIX
    try:
        doc = _open(*sources[MAIN])
        docs[MAIN] = doc
        count = doc.page_count
        order = [r.page for r in refs if r.source == MAIN]
        if len(set(order)) != len(order) or any(not 0 <= p < count for p in order):
            raise ValueError("Pagine del documento ripetute o inesistenti")
        # Senza pagine del documento (es. si estraggono solo pagine inserite) se ne tiene una
        # in fondo, da togliere alla fine: serve a mantenere cifratura e catalogo.
        spare = not order
        if spare:
            order = [0]
        deleted = sorted(set(range(count)) - set(order))
        toc = doc.get_toc(simple=False)
        gone = set(deleted)
        dropped = [i for i, item in enumerate(toc) if item[2] - 1 in gone]
        if deleted:
            doc.delete_pages(deleted)

        # Ordine delle pagine rimaste: si porta ognuna al suo posto, dalla prima.
        current = [p for p in range(count) if p not in gone]
        for i, page in enumerate(order):
            at = current.index(page, i)
            if at != i:
                doc.move_page(at, i)
                current.insert(i, current.pop(at))

        # Pagine degli altri file e vuote, nella posizione finale (in ordine crescente,
        # così le pagine del documento finiscono da sole al loro posto).
        done = 0
        pos = 0
        while pos < len(refs):
            ref = refs[pos]
            if ref.source == MAIN:
                pos += 1
            elif ref.source == BLANK:
                doc.new_page(pos, width=ref.size[0], height=ref.size[1])
                pos += 1
            else:
                # Pagine consecutive dello stesso file: una sola insert_pdf (risorse condivise).
                end = pos + 1
                while (end < len(refs) and refs[end].source == ref.source
                       and refs[end].page == refs[end - 1].page + 1):
                    end += 1
                src = docs.get(ref.source)
                if src is None:
                    src = docs[ref.source] = _open(*sources[ref.source])
                doc.insert_pdf(src, from_page=ref.page, to_page=refs[end - 1].page, start_at=pos)
                pos = end
            step(pos, total)
        if spare:
            doc.delete_page(len(refs))
            dropped = sorted(set(dropped) | {i for i, item in enumerate(toc) if item[2] == 1})

        for i, ref in enumerate(refs):
            if ref.rotate % 360:
                page = doc[i]
                page.set_rotation((page.rotation + ref.rotate) % 360)
        if dropped:
            _remove_toc_items(doc, dropped)
        _prune_fields(doc)

        doc.save(tmp, garbage=3, deflate=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
        for d in docs.values():
            d.close()
        docs = {}
        os.replace(tmp, dest)
        step(total, total)
    finally:
        for d in docs.values():
            d.close()
        try:
            os.remove(tmp)
        except OSError:
            pass


def _open(path: str, password: str | None):
    doc = pymupdf.open(path)
    if doc.needs_pass and not doc.authenticate(password or ""):
        doc.close()
        raise ValueError(f"Password errata per «{os.path.basename(path)}»")
    return doc


def _remove_toc_items(doc, dropped: list[int]) -> None:
    """Toglie dall'indice le voci `dropped` (indici in `toc`, letto prima delle modifiche).

    I figli di una voce tolta salgono di livello, perché l'indice resti valido per set_toc.
    """
    # delete_pages lascia le voci senza destinazione, quindi sono ancora tutte e nello stesso ordine.
    gone = set(dropped)
    after = [item for i, item in enumerate(doc.get_toc(simple=False)) if i not in gone]
    fixed = []
    level = 0
    for item in after:
        item = list(item)
        item[0] = min(item[0], level + 1)
        level = item[0]
        fixed.append(item)
    doc.set_toc(fixed)


def _prune_fields(doc) -> None:
    """Toglie da /AcroForm /Fields i campi che non hanno più widget su nessuna pagina."""
    kind, value = doc.xref_get_key(doc.pdf_catalog(), "AcroForm/Fields")
    if kind == "xref":
        holder = int(value.split()[0])
        value = doc.xref_object(holder, compressed=True)
    elif kind == "array":
        holder = None
    else:
        return
    fields = _refs(value)
    if not fields:
        return
    widgets = set()
    for page in doc:
        widgets.update(xref for xref, t, _ in page.annot_xrefs() if t == pymupdf.PDF_ANNOT_WIDGET)
    kept = [f for f in fields if _has_widget(doc, f, widgets, 0)]
    if len(kept) == len(fields):
        return
    array = "[" + " ".join(f"{x} 0 R" for x in kept) + "]"
    if holder is None:
        doc.xref_set_key(doc.pdf_catalog(), "AcroForm/Fields", array)
    else:
        doc.update_object(holder, array)


def _has_widget(doc, xref: int, widgets: set[int], depth: int) -> bool:
    if xref in widgets:
        return True
    if depth > 20:  # /Kids circolari in un file danneggiato
        return False
    kind, value = doc.xref_get_key(xref, "Kids")
    if kind == "xref":
        value = doc.xref_object(int(value.split()[0]), compressed=True)
    elif kind != "array":
        return False
    return any(_has_widget(doc, kid, widgets, depth + 1) for kid in _refs(value))


def _refs(array: str) -> list[int]:
    """Gli xref di un array PDF di riferimenti: "[12 0 R 15 0 R]" → [12, 15]."""
    parts = array.strip().strip("[]").split()
    return [int(parts[i]) for i in range(len(parts) - 2) if parts[i + 2] == "R" and parts[i].isdigit()]


def run(sources, refs, dest, password, results) -> None:
    """Processo: costruisce `dest` e rimanda ("progress", fatti, totale), poi ("done", PdfDocument)
    con la struttura del file nuovo (letta qui, non nella GUI) oppure ("error", messaggio)."""
    from piedf.document import open_document

    try:
        build(sources, refs, dest, lambda done, total: results.put(("progress", done, total)))
        results.put(("done", open_document(dest, password)))
    except Exception as e:
        results.put(("error", str(e)))
