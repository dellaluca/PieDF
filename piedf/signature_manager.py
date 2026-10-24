from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from piedf.signature_dialog import SignatureDialog
from piedf.signatures import Kind, Signature, SignatureStore

THUMB = QSize(220, 80)


def thumbnail_pixmap(image: QImage, size: QSize = THUMB) -> QPixmap:
    """Miniatura su fondo bianco con proporzioni fisse, così l'elenco resta allineato."""
    pixmap = QPixmap(size)
    pixmap.fill(QColor("#ffffff"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    margin = size.height() // 10
    scaled = image.scaled(size - QSize(2 * margin, 2 * margin), Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
    painter.drawImage((size.width() - scaled.width()) // 2, (size.height() - scaled.height()) // 2, scaled)
    painter.end()
    return pixmap


def thumbnail(image: QImage) -> QIcon:
    return QIcon(thumbnail_pixmap(image))


class SignatureManager(QDialog):
    """Elenco delle firme e iniziali salvate: crea, elimina."""

    def __init__(self, store: SignatureStore, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Le mie firme")
        self._store = store
        self.lists: dict[Kind, QListWidget] = {}

        layout = QVBoxLayout(self)
        for kind in Kind:
            header = QHBoxLayout()
            title = QLabel(f"<b>{kind.label}</b>")
            new = QPushButton("Nuova firma…" if kind is Kind.SIGNATURE else "Nuove iniziali…")
            new.clicked.connect(lambda _=False, k=kind: self.create(k))
            header.addWidget(title)
            header.addStretch()
            header.addWidget(new)
            layout.addLayout(header)

            widget = QListWidget()
            widget.setViewMode(QListWidget.ViewMode.IconMode)
            widget.setIconSize(THUMB)
            widget.setMovement(QListWidget.Movement.Static)
            widget.setResizeMode(QListWidget.ResizeMode.Adjust)
            widget.setSpacing(6)
            widget.setFixedHeight(THUMB.height() + 36)
            widget.setFlow(QListWidget.Flow.LeftToRight)
            widget.setWrapping(False)
            widget.itemSelectionChanged.connect(lambda k=kind: self._selection_changed(k))
            self.lists[kind] = widget
            layout.addWidget(widget)

        self.delete_button = QPushButton("Elimina")
        self.delete_button.clicked.connect(self.delete_selected)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Chiudi")
        buttons.addButton(self.delete_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.resize(560, 420)
        self.refresh()

    def refresh(self) -> None:
        for kind, widget in self.lists.items():
            widget.clear()
            signatures = self._store.list(kind)
            for signature in signatures:
                item = QListWidgetItem(thumbnail(signature.image()), "")
                item.setData(Qt.ItemDataRole.UserRole, signature)
                item.setToolTip(signature.path.name)
                widget.addItem(item)
            if not signatures:
                placeholder = QListWidgetItem("Nessuna ancora salvata")
                placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
                widget.addItem(placeholder)
        self.delete_button.setEnabled(False)

    def _selection_changed(self, kind: Kind) -> None:
        # Una sola selezione tra i due elenchi.
        if self.lists[kind].selectedItems():
            for other, widget in self.lists.items():
                if other is not kind:
                    widget.blockSignals(True)
                    widget.clearSelection()
                    widget.blockSignals(False)
        self.delete_button.setEnabled(self.selected() is not None)

    def selected(self) -> Signature | None:
        for widget in self.lists.values():
            items = widget.selectedItems()
            if items:
                return items[0].data(Qt.ItemDataRole.UserRole)
        return None

    def create(self, kind: Kind) -> Signature | None:
        dialog = SignatureDialog(self._store, kind, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()
            return dialog.saved
        return None

    def delete_selected(self) -> None:
        signature = self.selected()
        if signature is None:
            return
        answer = QMessageBox.question(
            self, "Elimina",
            "Eliminare questa firma?" if signature.kind is Kind.SIGNATURE else "Eliminare queste iniziali?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._store.delete(signature)
            self.refresh()
