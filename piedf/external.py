"""Apertura di indirizzi esterni nel browser, sempre dopo una conferma."""
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox

SAFE_SCHEMES = ("http", "https", "mailto")


def open_external(uri: str, parent=None) -> bool:
    """Chiede conferma e apre `uri` nel browser; `False` se l'indirizzo non è tra quelli sicuri."""
    url = QUrl(uri, QUrl.ParsingMode.StrictMode)
    scheme = url.scheme().lower()
    ok = url.isValid() and scheme in SAFE_SCHEMES and bool(url.path() if scheme == "mailto" else url.host())
    if not ok:
        return False
    # L'indirizzo per intero e codificato (niente caratteri simili Unicode): è quello che si apre
    # davvero, che può essere diverso dal testo visibile nel PDF.
    address = bytes(url.toEncoded()).decode("ascii")
    Button = QMessageBox.StandardButton
    box = QMessageBox(
        QMessageBox.Icon.Question, "Link esterno",
        f"Aprire questo indirizzo nel browser?\n\n{address}",
        Button.Open | Button.Cancel, parent,
    )
    box.button(Button.Open).setText("Apri")
    box.button(Button.Cancel).setText("Annulla")
    box.setDefaultButton(Button.Cancel)
    if box.exec() == Button.Open:
        QDesktopServices.openUrl(url)
    return True
