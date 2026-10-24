"""Firme piazzate sul documento aperto, ancora in memoria (le scrive nel PDF `writer.py`).

I rettangoli sono in punti PDF, nelle coordinate della pagina come si vede (dopo /Rotate),
origine in alto a sinistra: lo stesso spazio di `page.rect` in PyMuPDF e di
`PdfDocument.page_sizes`. Il modello non dipende dalla vista.
"""

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QBuffer, QObject, QPointF, QRectF, Signal
from PySide6.QtGui import QImage, QUndoCommand, QUndoStack

from piedf.signatures import Kind

DEFAULT_WIDTH = {Kind.SIGNATURE: 150.0, Kind.INITIALS: 50.0}  # punti
MAX_PAGE_FRACTION = 0.45  # su pagine strette la firma non supera questa frazione della larghezza
MIN_HEIGHT = 8.0  # punti


@dataclass(eq=False)  # confronto per identità: due firme uguali restano distinte
class Placement:
    page: int  # indice da 0
    rect: QRectF  # punti PDF, pagina come si vede, origine in alto a sinistra
    image_path: Path  # PNG della firma (signatures.Signature.path)
    kind: Kind


def default_rect(kind: Kind, image: QImage, page_size: tuple[float, float], center: QPointF) -> QRectF:
    """Rettangolo alla dimensione predefinita centrato su `center`, tenuto dentro la pagina."""
    page_w, page_h = page_size
    width = min(DEFAULT_WIDTH[kind], page_w * MAX_PAGE_FRACTION)
    ratio = image.height() / image.width() if not image.isNull() and image.width() else 0.5
    height = width * ratio
    if height > page_h:
        height = page_h
        width = height / ratio
    rect = QRectF(0, 0, width, height)
    rect.moveCenter(center)
    return clamp_rect(rect, page_size)


def clamp_rect(rect: QRectF, page_size: tuple[float, float]) -> QRectF:
    """Sposta il rettangolo (senza ridimensionarlo) perché resti dentro la pagina."""
    page_w, page_h = page_size
    x = max(0.0, min(rect.x(), page_w - rect.width()))
    y = max(0.0, min(rect.y(), page_h - rect.height()))
    return QRectF(x, y, rect.width(), rect.height())


class Placements(QObject):
    """Elenco delle firme piazzate, selezione e annulla/ripeti."""

    changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.items: list[Placement] = []
        self.selected: Placement | None = None
        self.undo_stack = QUndoStack(self)
        self.undo_stack.cleanChanged.connect(self.changed)
        # Le immagini restano in memoria: se il PNG viene eliminato la firma piazzata resta.
        self._images: dict[Path, QImage] = {}

    @property
    def modified(self) -> bool:
        return not self.undo_stack.isClean()

    def image(self, path: Path) -> QImage:
        image = self._images.get(path)
        if image is None:
            image = QImage(str(path))
            self._images[path] = image
        return image

    def missing_pngs(self) -> dict[Path, bytes]:
        """PNG (dalla memoria) delle firme piazzate il cui file è stato eliminato."""
        streams = {}
        for path in {p.image_path for p in self.items if not p.image_path.exists()}:
            buffer = QBuffer()
            buffer.open(QBuffer.OpenModeFlag.WriteOnly)
            self.image(path).save(buffer, "PNG")
            streams[path] = bytes(buffer.data())
        return streams

    def on_page(self, page: int) -> list[Placement]:
        return [p for p in self.items if p.page == page]

    def select(self, placement: Placement | None) -> None:
        if placement is not self.selected:
            self.selected = placement
            self.changed.emit()

    def clear(self) -> None:
        self.items.clear()
        self.selected = None
        self.undo_stack.clear()
        self.changed.emit()

    def mark_saved(self) -> None:
        """Toglie l'asterisco dal titolo senza svuotare la storia."""
        self.undo_stack.setClean()

    # --- Modifiche (ognuna è un passo di annulla) --------------------------------------

    def add(self, placement: Placement) -> None:
        self.image(placement.image_path)
        self.undo_stack.push(_AddCommand(self, placement))

    def remove(self, placement: Placement) -> None:
        self.undo_stack.push(_RemoveCommand(self, placement))

    def set_rect(self, placement: Placement, old: QRectF, new: QRectF, text: str) -> None:
        """Registra un cambio di rettangolo già applicato dal vivo (trascinamento)."""
        if old != new and placement in self.items:
            self.undo_stack.push(_RectCommand(self, placement, old, new, text))

    def preview_rect(self, placement: Placement, rect: QRectF) -> None:
        """Cambia il rettangolo durante un trascinamento, senza passo di annulla."""
        placement.rect = QRectF(rect)
        self.changed.emit()

    # --- Usati dai comandi -------------------------------------------------------------

    def _insert(self, index: int, placement: Placement) -> None:
        self.items.insert(index, placement)
        self.selected = placement
        self.changed.emit()

    def _take(self, placement: Placement) -> int:
        index = self.items.index(placement)
        del self.items[index]
        if self.selected is placement:
            self.selected = None
        self.changed.emit()
        return index


class _AddCommand(QUndoCommand):
    def __init__(self, model: Placements, placement: Placement) -> None:
        super().__init__("posizionamento firma")
        self._model, self._placement = model, placement
        self._index = len(model.items)

    def redo(self) -> None:
        self._model._insert(self._index, self._placement)

    def undo(self) -> None:
        self._model._take(self._placement)


class _RemoveCommand(QUndoCommand):
    def __init__(self, model: Placements, placement: Placement) -> None:
        super().__init__("eliminazione firma")
        self._model, self._placement = model, placement
        self._index = model.items.index(placement)

    def redo(self) -> None:
        self._index = self._model._take(self._placement)

    def undo(self) -> None:
        self._model._insert(self._index, self._placement)


class _RectCommand(QUndoCommand):
    def __init__(self, model: Placements, placement: Placement, old: QRectF, new: QRectF, text: str) -> None:
        super().__init__(text)
        self._model, self._placement = model, placement
        self._old, self._new = QRectF(old), QRectF(new)

    def redo(self) -> None:
        self._placement.rect = QRectF(self._new)
        self._model.changed.emit()

    def undo(self) -> None:
        self._placement.rect = QRectF(self._old)
        self._model.selected = self._placement
        self._model.changed.emit()
