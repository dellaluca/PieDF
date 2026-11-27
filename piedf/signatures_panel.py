"""Scheda «Firme» del pannello laterale (#73): le firme digitali del PDF, in ordine di revisione.

I risultati arrivano dal processo della verifica (`SealCheckJob`, lo stesso della barra del
sigillo): finché non arrivano la scheda dice «Controllo delle firme…». Ogni firma è una
scheda (`SignatureCard`, testo che va a capo); clic su una firma visibile = salto al suo campo;
menu contestuale: versione firmata e dettagli del certificato.
"""

from html import escape

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QMenu, QScrollArea, QVBoxLayout, QWidget,
)

from piedf import sealing

STATE_COLOR = {
    sealing.INTACT: "#1e8e3e",
    sealing.ALLOWED: "#1e8e3e",
    sealing.MODIFIED: "#e37400",
    sealing.INVALID: "#d93025",
}
SAVE_TEXT = "Salva la versione firmata…"
DETAILS_TEXT = "Dettagli del certificato…"
CARD_STYLE = ("#signature_card { background: palette(base); border: 1px solid palette(mid); border-radius: 4px; }"
              "#signature_card:hover { border-color: palette(highlight); }")


def state_pixmap(state: str) -> QPixmap:
    """Pallino del colore dello stato."""
    pixmap = QPixmap(14, 14)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(STATE_COLOR.get(state, STATE_COLOR[sealing.INVALID])))
    painter.drawEllipse(1, 1, 12, 12)
    painter.end()
    return pixmap


class SignatureCard(QFrame):
    """Una firma: pallino dello stato e righe di `sealing.signature_lines`."""

    clicked = Signal()
    menu_requested = Signal(QPoint)  # posizione globale

    def __init__(self, info: sealing.SignatureInfo, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("signature_card")
        self.setStyleSheet(CARD_STYLE)
        self.lines = sealing.signature_lines(info)
        name, date, kind, state, identity = (escape(line) for line in self.lines)
        self.icon = QLabel()
        self.icon.setPixmap(state_pixmap(info.state))
        self.icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        color = STATE_COLOR.get(info.state, STATE_COLOR[sealing.INVALID])
        self.text = QLabel(f"<b>{name}</b><br>{date}<br>{kind}<br>"
                           f"<span style='color:{color}; font-weight:600'>{state}</span><br>{identity}")
        self.text.setTextFormat(Qt.TextFormat.RichText)
        self.text.setWordWrap(True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.addWidget(self.icon)
        layout.addWidget(self.text, 1)
        self.visible_field = info.page >= 0
        tip = "\n".join(self.lines)
        if self.visible_field:
            tip += f"\n\nClic per andare al campo della firma (pagina {info.page + 1})"
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(tip)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        self.menu_requested.emit(event.globalPos())


class SignaturesPanel(QWidget):
    """Elenco delle firme con lo stato della verifica e la nota sulla revoca."""

    signature_clicked = Signal(int)  # indice della firma (solo firme visibili)
    save_requested = Signal(int)  # «Salva la versione firmata…»

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.signatures: list[sealing.SignatureInfo] | None = None
        self.cards: list[SignatureCard] = []
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setMargin(12)
        self.message.setEnabled(False)  # testo grigio
        self._content = QWidget()
        self._cards = QVBoxLayout(self._content)
        self._cards.setContentsMargins(6, 6, 6, 6)
        self._cards.setSpacing(6)
        self._cards.addWidget(self.message)
        self._cards.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(self._content)
        self.note = QLabel(sealing.REVOCATION_NOTE)
        self.note.setWordWrap(True)
        self.note.setMargin(8)
        self.note.setEnabled(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.scroll, 1)
        layout.addWidget(self.note)
        self.set_checking()

    def _clear(self) -> None:
        for card in self.cards:
            self._cards.removeWidget(card)
            card.deleteLater()
        self.cards = []

    def set_checking(self) -> None:
        """Verifica in corso: solo il messaggio."""
        self.signatures = None
        self._clear()
        self.message.setText(sealing.SIGNATURES_CHECKING)
        self.message.show()

    def set_signatures(self, signatures: list[sealing.SignatureInfo] | None) -> None:
        """Risultato della verifica; None o vuoto = le firme non si leggono."""
        self.signatures = signatures
        self._clear()
        if not signatures:
            self.message.setText(sealing.SIGNATURES_FAILED)
            self.message.show()
            return
        self.message.hide()
        for i, info in enumerate(signatures):
            card = SignatureCard(info, self._content)
            card.clicked.connect(lambda i=i: self._on_clicked(i))
            card.menu_requested.connect(lambda pos, i=i: self._show_menu(i, pos))
            self._cards.insertWidget(self._cards.count() - 1, card)  # prima dello spazio in fondo
            self.cards.append(card)

    def rows(self) -> list[str]:
        """Testo delle firme, una riga per voce (per i controlli)."""
        return ["\n".join(card.lines) for card in self.cards]

    def _on_clicked(self, index: int) -> None:
        if self.signatures and self.signatures[index].page >= 0:
            self.signature_clicked.emit(index)

    def context_menu(self, index: int) -> QMenu:
        """Menu contestuale della firma `index`."""
        menu = QMenu(self)
        menu.addAction(SAVE_TEXT, lambda: self.save_requested.emit(index))
        menu.addAction(DETAILS_TEXT, lambda: self.show_details(index))
        return menu

    def _show_menu(self, index: int, pos: QPoint) -> None:
        menu = self.context_menu(index)
        menu.exec(pos)
        menu.deleteLater()

    def show_details(self, index: int) -> None:
        dialog = CertificateDetailsDialog(self.signatures[index], self)
        dialog.exec()
        dialog.deleteLater()


class CertificateDetailsDialog(QDialog):
    """Dettagli del certificato di una firma, sola lettura."""

    def __init__(self, info: sealing.SignatureInfo, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Dettagli del certificato")
        form = QFormLayout()
        self.values: dict[str, str] = {}
        for label, value in info.details:
            text = QLabel(value)
            text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            form.addRow(f"{label}:", text)
            self.values[label] = value
        if not info.details:
            form.addRow(QLabel("Il certificato di questa firma non si legge."))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Chiudi")
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.setMinimumWidth(480)
