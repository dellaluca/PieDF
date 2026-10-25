"""Processo di render.

PyMuPDF tiene il GIL durante il render, quindi un thread non basterebbe a tenere
fluida l'interfaccia: le pagine si disegnano in un processo separato. Questo modulo
non importa Qt, così il processo figlio parte in fretta.

Messaggi in ingresso:
    ("open", gen, path, password)        path None chiude il documento
    ("want", gen, [(page, scale), ...])   sostituisce la lista precedente
    ("links", gen, page)                  link della pagina, risposta subito
    ("field", gen, page, xref, value)     valore di un campo modulo, solo nel documento in memoria
    ("search", gen, id, text, case, start)  cerca in tutte le pagine da `start` (poi da capo);
                                          sostituisce la ricerca in corso, testo vuoto = ferma
    ("quit",)
Messaggi in uscita:
    ("page", gen, page, scale, width, height, stride, samples_rgb)
    ("links", gen, page, [{kind, rect, page, to, uri}, ...])
    ("error", gen, message)
    ("closed", gen)                       documento chiuso: il file non è più aperto qui
    ("field", gen, page)                  campo applicato: i render precedenti della pagina sono vecchi
    ("hits", gen, id, page, [rect, ...])  risultati di una pagina (solo le pagine con risultati)
    ("search_done", gen, id, has_text)    fine della ricerca; has_text False = nessuna pagina ha testo

Link: `rect` (x0, y0, x1, y1) e `to` (x, y) sono in punti nello spazio della pagina come
si vede (dopo /Rotate), come `page.rect`. `kind` è "goto" (interno: `page` e `to`, con
None per le coordinate che la destinazione non indica) oppure "uri" (esterno: `uri`
così com'è, anche file:, launch e link ad altri PDF; decide la GUI cosa seguire).

Ricerca (#14): una pagina alla volta, e solo quando non ci sono pagine da disegnare: il
render delle pagine visibili ha sempre la precedenza. `search_for` di MuPDF ignora le
maiuscole: con `case` si tengono solo i risultati il cui testo coincide anche nelle maiuscole.
I rettangoli sono nello spazio della pagina come si vede, come quelli dei link
(`search_for` li dà nella pagina non ruotata: verificato con /Rotate 90).

Le annotazioni di PieDF (caselle di testo) le disegna la vista dal suo modello: qui si
cancellano dal documento in memoria prima del primo render di ogni pagina, così non
compaiono due volte. Il file non si tocca.

La stampa (#25) usa un secondo processo, `print_pages`, che non tocca quello delle pagine
visibili e stampa il file così com'è, annotazioni di PieDF comprese.
Le miniature di "Organizza pagine" (#26) un altro ancora, `thumbnails`, che apre anche i
file inseriti e li disegna così come sono, annotazioni di PieDF comprese.
"""

import math
import queue


def run(requests, results) -> None:
    import pymupdf

    from piedf.document import set_field_value

    doc = None
    gen = 0
    wanted: list[tuple[int, float]] = []
    cleaned: set[int] = set()  # pagine già senza le annotazioni di PieDF
    search = None  # ricerca in corso: [id, testo, maiuscole, pagine da cercare, testo trovato]

    while True:
        # Senza lavoro si aspetta; altrimenti si leggono solo i messaggi già arrivati,
        # così le richieste vecchie (scroll veloce, zoom) vengono scartate.
        msg = requests.get() if not wanted and search is None else _get_nowait(requests)
        while msg is not None:
            kind = msg[0]
            if kind == "quit":
                return
            if kind == "open":
                _, gen, path, password = msg
                wanted = []
                cleaned = set()
                search = None
                if doc is not None:
                    doc.close()
                doc = None
                if path is None:  # documento chiuso
                    results.put(("closed", gen))
                    msg = _get_nowait(requests)
                    continue
                try:
                    doc = pymupdf.open(path)
                    if doc.needs_pass and not doc.authenticate(password or ""):
                        raise ValueError("Password errata")
                except Exception as e:
                    doc = None
                    results.put(("error", gen, str(e)))
            elif kind == "search" and msg[1] == gen:
                _, _, sid, text, case, start = msg
                search = None
                if text.strip() and doc is not None:
                    n = doc.page_count
                    start = max(0, min(start, n - 1))
                    search = [sid, text, case, list(range(start, n)) + list(range(start)), False]
            elif kind == "want" and msg[1] == gen:
                wanted = list(msg[2])
            elif kind == "field" and msg[1] == gen and doc is not None:
                _, _, page, xref, value = msg
                try:
                    set_field_value(doc[page], xref, value)
                except Exception as e:
                    results.put(("error", gen, f"Campo {xref}: {e}"))
                results.put(("field", gen, page))
            elif kind == "links" and msg[1] == gen and doc is not None:
                page = msg[2]
                try:
                    results.put(("links", gen, page, _links(doc, page)))
                except Exception as e:
                    results.put(("links", gen, page, []))
                    results.put(("error", gen, f"Link di pagina {page + 1}: {e}"))
            msg = _get_nowait(requests)

        if doc is None:
            wanted, search = [], None
            continue
        if not wanted:
            if search is not None:
                sid, text, case, pages, has_text = search
                page = pages.pop(0)
                try:
                    rects, page_text = _search(doc[page], text, case, not has_text)
                    search[4] = has_text or page_text
                    if rects:
                        results.put(("hits", gen, sid, page, rects))
                except Exception as e:
                    results.put(("error", gen, f"Ricerca a pagina {page + 1}: {e}"))
                if not pages:
                    results.put(("search_done", gen, sid, search[4]))
                    search = None
            continue

        page, scale = wanted.pop(0)
        try:
            if page not in cleaned:
                _remove_piedf_annots(doc[page])
                cleaned.add(page)
            pix = doc[page].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            results.put(("page", gen, page, scale, pix.width, pix.height, pix.stride, pix.samples))
        except Exception as e:
            results.put(("error", gen, f"Pagina {page + 1}: {e}"))


# Pagine per la stampa già pronte e non ancora confermate dalla GUI: a 600 dpi un A4 occupa
# circa 100 MB, quindi non si va avanti più di così. È anche il numero di slot di memoria condivisa.
PRINT_AHEAD = 2


def _lower_priority() -> None:
    """Priorità sotto il normale: con pochi core il thread che disegna sul QPrinter, che tiene
    il GIL, non deve aspettare il processo di render per tornare in esecuzione (#34)."""
    import os
    import sys

    try:
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(5)
    except (ImportError, OSError, AttributeError):
        pass


def print_pages(path, password, items, slots, acks, results) -> None:
    """Processo di stampa: disegna `items` [(pagina, scala), ...] in ordine, uno alla volta.

    I pixel RGB dell'n-esimo elemento vanno nella memoria condivisa `slots[n % PRINT_AHEAD]`
    (nomi di `SharedMemory` creati dalla GUI): passare ~100 MB per pagina nella coda teneva
    il GIL della GUI per decine di millisecondi (#34). Messaggi in uscita:
    ("page", n, width, height, stride, secondi_di_render), ("error", messaggio).
    Ogni pagina disegnata dalla GUI arriva confermata su `acks`: solo allora il suo slot
    si riusa. Per annullare la GUI chiude il processo.
    """
    import time
    from multiprocessing import shared_memory

    import pymupdf

    _lower_priority()
    try:
        doc = pymupdf.open(path)
        if doc.needs_pass and not doc.authenticate(password or ""):
            raise ValueError("Password errata")
    except Exception as e:
        results.put(("error", str(e)))
        return
    shms = [shared_memory.SharedMemory(name) for name in slots]
    acked = 0
    for n, (page, scale) in enumerate(items):
        while n - acked >= PRINT_AHEAD:
            acks.get()
            acked += 1
        t = time.perf_counter()
        try:
            pix = doc[page].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            buf = shms[n % PRINT_AHEAD].buf
            size = pix.stride * pix.height
            if size > len(buf):
                raise ValueError("pagina più grande del previsto")
            buf[:size] = pix.samples_mv
        except Exception as e:
            results.put(("error", f"Pagina {page + 1}: {e}"))
            return
        results.put(("page", n, pix.width, pix.height, pix.stride, time.perf_counter() - t))
        del pix, buf
    for shm in shms:
        shm.close()
    doc.close()


def _remove_piedf_annots(page) -> None:
    from piedf.document import piedf_annots

    try:
        for annot in piedf_annots(page):
            page.delete_annot(annot)
    except Exception:
        pass  # meglio un doppione che una pagina bianca


def thumbnails(requests, results) -> None:
    """Processo delle miniature di "Organizza pagine" (#26): pagine anche di altri file.

    Messaggi in ingresso: ("source", id, path, password), ("want", [(id, page, scale), ...])
    che sostituisce la lista precedente (si disegna solo quello che si vede), ("quit",).
    In uscita: ("thumb", id, page, width, height, stride, samples_rgb), ("error", id, page, messaggio).
    """
    import pymupdf

    sources = {}
    docs = {}
    wanted: list[tuple[int, int, float]] = []
    while True:
        msg = requests.get() if not wanted else _get_nowait(requests)
        while msg is not None:
            if msg[0] == "quit":
                for doc in docs.values():
                    doc.close()
                # Le miniature non ancora lette non servono più: senza questo il processo
                # aspetterebbe all'uscita che la GUI le legga.
                results.cancel_join_thread()
                return
            if msg[0] == "source":
                sources[msg[1]] = (msg[2], msg[3])
            elif msg[0] == "want":
                wanted = list(msg[1])
            msg = _get_nowait(requests)
        if not wanted:
            continue
        sid, page, scale = wanted.pop(0)
        try:
            doc = docs.get(sid)
            if doc is None:
                path, password = sources[sid]
                doc = pymupdf.open(path)
                if doc.needs_pass and not doc.authenticate(password or ""):
                    raise ValueError("Password errata")
                docs[sid] = doc
            pix = doc[page].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False, annots=True)
            results.put(("thumb", sid, page, pix.width, pix.height, pix.stride, pix.samples))
        except Exception as e:
            results.put(("error", sid, page, str(e)))


def _search(page, text: str, case: bool, check_text: bool) -> tuple[list[tuple], bool]:
    """Risultati di `text` nella pagina, nello spazio come si vede, e se la pagina ha testo
    (controllato solo con `check_text`: basta una pagina per saperlo)."""
    textpage = page.get_textpage()
    has_text = check_text and bool(textpage.extractText().strip())
    rects = page.search_for(text, textpage=textpage)
    if case:
        wanted = " ".join(text.split())
        rects = [r for r in rects if wanted in " ".join(page.get_textbox(r, textpage=textpage).split())]
    m = page.rotation_matrix
    rects = [r * m for r in rects]
    return [(r.x0, r.y0, r.x1, r.y1) for r in rects], has_text


def _links(doc, index: int) -> list[dict]:
    # I link grezzi di MuPDF invece di get_links(): `doc.resolve_link` dà la destinazione
    # già nello spazio della pagina di arrivo come si vede, anche per le destinazioni con
    # nome (LINK_NAMED) e per le pagine ruotate; get_links() lascia le destinazioni con
    # nome nelle coordinate PDF grezze.
    links = []
    page = doc[index]  # va tenuta viva: i link la referenziano in modo debole
    link = page.first_link
    while link:
        uri = link.uri or ""
        r = link.rect  # già nello spazio della pagina come si vede, come get_links()["from"]
        item = {"kind": None, "rect": (r.x0, r.y0, r.x1, r.y1), "page": -1, "to": (None, None), "uri": uri}
        if link.is_external:
            item["kind"] = "uri"
        elif uri.startswith("#"):
            target, x, y = doc.resolve_link(uri)
            if target >= 0:
                item.update(kind="goto", page=target, to=(_coord(x), _coord(y)))
        if item["kind"] is not None and not r.is_empty:
            links.append(item)
        link = link.next
    return links


def _coord(v: float) -> float | None:
    return None if math.isnan(v) else v


def _get_nowait(q):
    try:
        return q.get_nowait()
    except queue.Empty:
        return None
