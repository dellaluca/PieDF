"""Processo di render.

PyMuPDF tiene il GIL durante il render, quindi un thread non basterebbe a tenere
fluida l'interfaccia: le pagine si disegnano in un processo separato. Questo modulo
non importa Qt, così il processo figlio parte in fretta.

Messaggi in ingresso:
    ("open", gen, path, password)        path None chiude il documento
    ("want", gen, [(page, scale), ...])   sostituisce la lista precedente
    ("links", gen, page)                  link della pagina, risposta subito
    ("field", gen, page, xref, value)     valore di un campo modulo, solo nel documento in memoria
    ("quit",)
Messaggi in uscita:
    ("page", gen, page, scale, width, height, stride, samples_rgb)
    ("links", gen, page, [{kind, rect, page, to, uri}, ...])
    ("error", gen, message)
    ("closed", gen)                       documento chiuso: il file non è più aperto qui
    ("field", gen, page)                  campo applicato: i render precedenti della pagina sono vecchi

Link: `rect` (x0, y0, x1, y1) e `to` (x, y) sono in punti nello spazio della pagina come
si vede (dopo /Rotate), come `page.rect`. `kind` è "goto" (interno: `page` e `to`, con
None per le coordinate che la destinazione non indica) oppure "uri" (esterno: `uri`
così com'è, anche file:, launch e link ad altri PDF; decide la GUI cosa seguire).
"""

import math
import queue


def run(requests, results) -> None:
    import pymupdf

    from piedf.document import set_field_value

    doc = None
    gen = 0
    wanted: list[tuple[int, float]] = []

    while True:
        # Senza lavoro si aspetta; altrimenti si leggono solo i messaggi già arrivati,
        # così le richieste vecchie (scroll veloce, zoom) vengono scartate.
        msg = requests.get() if not wanted else _get_nowait(requests)
        while msg is not None:
            kind = msg[0]
            if kind == "quit":
                return
            if kind == "open":
                _, gen, path, password = msg
                wanted = []
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

        if doc is None or not wanted:
            wanted = []
            continue

        page, scale = wanted.pop(0)
        try:
            pix = doc[page].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            results.put(("page", gen, page, scale, pix.width, pix.height, pix.stride, pix.samples))
        except Exception as e:
            results.put(("error", gen, f"Pagina {page + 1}: {e}"))


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
