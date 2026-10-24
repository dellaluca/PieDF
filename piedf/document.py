from dataclasses import dataclass

import pymupdf

A4 = (595.0, 842.0)


class OpenError(Exception):
    """Il file non si può aprire (non è un PDF, è danneggiato oltre la riparazione, non esiste…)."""


class PasswordRequired(Exception):
    def __init__(self, wrong: bool) -> None:
        super().__init__("Password errata" if wrong else "Password richiesta")
        self.wrong = wrong


@dataclass
class PdfDocument:
    path: str
    password: str | None
    # Dimensioni delle pagine in punti PDF, con la rotazione della pagina già applicata.
    page_sizes: list[tuple[float, float]]
    repaired: bool


def open_document(path: str, password: str | None = None) -> PdfDocument:
    """Apre il file solo per leggerne la struttura; il render avviene nel processo dedicato."""
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        raise OpenError(str(e)) from e

    try:
        if doc.needs_pass:
            if password is None:
                raise PasswordRequired(wrong=False)
            if not doc.authenticate(password):
                raise PasswordRequired(wrong=True)
        if doc.page_count == 0:
            raise OpenError("Il documento non contiene pagine.")
        sizes = []
        broken = 0
        for i in range(doc.page_count):
            try:
                rect = doc[i].rect
                sizes.append((rect.width, rect.height))
            except Exception:
                # Una pagina illeggibile non deve impedire di leggere le altre:
                # si tiene il posto con la misura della precedente (o A4).
                broken += 1
                sizes.append(sizes[-1] if sizes else A4)
        if broken == doc.page_count:
            raise OpenError("Nessuna pagina leggibile.")
        return PdfDocument(path, password, sizes, doc.is_repaired or broken > 0)
    finally:
        # Chiudere subito evita di tenere il file bloccato su Windows.
        doc.close()
