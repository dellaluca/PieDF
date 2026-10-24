"""Processo di render.

PyMuPDF tiene il GIL durante il render, quindi un thread non basterebbe a tenere
fluida l'interfaccia: le pagine si disegnano in un processo separato. Questo modulo
non importa Qt, così il processo figlio parte in fretta.

Messaggi in ingresso:
    ("open", gen, path, password)        path None chiude il documento
    ("want", gen, [(page, scale), ...])   sostituisce la lista precedente
    ("field", gen, page, xref, value)     valore di un campo modulo, solo nel documento in memoria
    ("quit",)
Messaggi in uscita:
    ("page", gen, page, scale, width, height, stride, samples_rgb)
    ("error", gen, message)
    ("closed", gen)                       documento chiuso: il file non è più aperto qui
    ("field", gen, page)                  campo applicato: i render precedenti della pagina sono vecchi
"""

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


def _get_nowait(q):
    try:
        return q.get_nowait()
    except queue.Empty:
        return None
