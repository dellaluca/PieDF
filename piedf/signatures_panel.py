"""Scheda «Firme» del pannello laterale (#73): le firme digitali del PDF, in ordine di revisione.

I risultati arrivano dal processo della verifica (`SealCheckJob`, lo stesso della barra del
sigillo): finché non arrivano la scheda dice «Controllo delle firme…». Clic su una firma
visibile = salto al suo campo; menu contestuale: versione firmata e dettagli del certificato.
"""

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFormLayout, QLabel, QListWidget, QListWidgetItem, QMenu,
    QVBoxLayout, QWidget,
)

from piedf import sealing

INDEX_ROLE = Qt.ItemDataRole.UserRole + 1
STATE_COLOR = {
    sealing.INTACT: "#1e8e3e",
    sealing.ALLOWED: "#1e8e3e",
    sealing.MODIFIED: "#e37400",
    sealing.INVALID: "#d93025",
}
SAVE_TEXT = "Salva la versione firmata…"
DETAILS_TEXT = "Dettagli del certificato…"


def state_icon(state: str) -> QIcon:
    """Pallino del colore dello stato."""
    pixmap = QPixmap(14, 14)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(STATE_COLOR.get(state, STATE_COLOR[sealing.INVALID])))
    painter.drawEllipse(1, 1, 12, 12)
    painter.end()
    return QIcon(pixmap)


class SignaturesPanel(QWidget):
    """Elenco delle firme con lo stato della verifica e la nota sulla revoca."""

    signature_clicked = Signal(int)  # indice della firma (solo firme visibili)
    save_requested = Signal(int)  # «Salva la versione firmata…»

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.signatures: list[sealing.SignatureInfo] | None = None
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setMargin(12)
        self.message.setEnabled(False)  # testo grigio
        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setSpacing(2)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._show_menu)
        self.list.itemClicked.connect(self._on_clicked)
        self.list.itemActivated.connect(self._on_clicked)
        self.note = QLabel(sealing.REVOCATION_NOTE)
        self.note.setWordWrap(True)
        self.note.setMargin(8)
        self.note.setEnabled(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.message)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.note)
        self.set_checking()

    def set_checking(self) -> None:
        """Verifica in corso: messaggio e lista vuota."""
        self.signatures = None
        self.list.clear()
        self.message.setText(sealing.SIGNATURES_CHECKING)
        self.message.show()

    def set_signatures(self, signatures: list[sealing.SignatureInfo] | None) -> None:
        """Risultato della verifica; None = le firme non si leggono."""
        self.signatures = signatures
        self.list.clear()
        if not signatures:
            self.message.setText(sealing.SIGNATURES_FAILED)
            self.message.show()
            return
        self.message.hide()
        for i, info in enumerate(signatures):
            lines = sealing.signature_lines(info)
            item = QListWidgetItem(state_icon(info.state), "\n".join(lines))
            item.setData(INDEX_ROLE, i)
            tip = "\n".join(lines)
            if info.page >= 0:
                tip += f"\n\nClic per andare al campo della firma (pagina {info.page + 1})"
            item.setToolTip(tip)
            self.list.addItem(item)

    def rows(self) -> list[str]:
        """Testo delle righe (per i controlli)."""
        return [self.list.item(i).text() for i in range(self.list.count())]

    def _on_clicked(self, item: QListWidgetItem) -> None:
        index = item.data(INDEX_ROLE)
        if self.signatures and self.signatures[index].page >= 0:
            self.signature_clicked.emit(index)

    def context_menu(self, index: int) -> QMenu:
        """Menu contestuale della firma `index`."""
        menu = QMenu(self)
        menu.addAction(SAVE_TEXT, lambda: self.save_requested.emit(index))
        menu.addAction(DETAILS_TEXT, lambda: self.show_details(index))
        return menu

    def _show_menu(self, pos: QPoint) -> None:
        item = self.list.itemAt(pos)
        if item is None or not self.signatures:
            return
        menu = self.context_menu(item.data(INDEX_ROLE))
        menu.exec(self.list.viewport().mapToGlobal(pos))
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
