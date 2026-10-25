"""Annotazioni modificabili di PieDF (caselle di testo; poi il disegno, #22), in memoria.

A differenza delle firme non si "cuociono" nel PDF: si salvano come annotazioni con titolo
"PieDF" e alla riapertura tornano nel modello (`PdfDocument.annotations`), modificabili.
Il processo di render le toglie dal suo documento in memoria e le disegna la vista dal modello.

Il modello condivide il `QUndoStack` di `Placements`, come `FormValues`. I rettangoli sono
tuple (x0, y0, x1, y1) in punti PDF, pagina come si vede, come in `document.Annotation`.
"""

from dataclasses import fields as dataclass_fields

import pymupdf
from PySide6.QtCore import QObject, QRectF, Signal
from PySide6.QtGui import QImage, QUndoCommand, QUndoStack

from piedf.document import FONTS, Annotation, TextBox, add_text_box, text_box_height

FONT_LABELS = {"Helv": "Helvetica", "TiRo": "Times", "Cour": "Courier"}
COLORS = {"#000000": "Nero", "#1a3fbf": "Blu", "#c00000": "Rosso"}
MIN_SIZE, MAX_SIZE = 8, 36
DEFAULT_STYLE = {"font": FONTS[0], "size": 11.0, "color": "#000000", "align": 0}
STYLE_KEYS = tuple(DEFAULT_STYLE)
NEW_BOX_WIDTH = 180.0  # punti
MIN_BOX_WIDTH = 20.0  # punti
IMAGE_CACHE = 200  # immagini delle caselle tenute in memoria


def to_qrect(rect: tuple[float, float, float, float]) -> QRectF:
    x0, y0, x1, y1 = rect
    return QRectF(x0, y0, x1 - x0, y1 - y0)


def to_tuple(rect: QRectF) -> tuple[float, float, float, float]:
    return (rect.left(), rect.top(), rect.right(), rect.bottom())


def fitted_rect(box: TextBox, rect: QRectF, page_size: tuple[float, float] | None = None, **changes) -> QRectF:
    """Il rettangolo con l'altezza che segue il testo (con le modifiche `changes`), dentro la pagina."""
    text = changes.get("text", box.text)
    font, size = changes.get("font", box.font), changes.get("size", box.size)
    height = text_box_height(text or " ", font, size, rect.width())
    rect = QRectF(rect.x(), rect.y(), rect.width(), height)
    if page_size is not None:
        page_w, page_h = page_size
        rect.moveLeft(max(0.0, min(rect.x(), page_w - rect.width())))
        rect.moveTop(max(0.0, min(rect.y(), page_h - rect.height())))
    return rect


def _state(item: Annotation) -> tuple:
    return tuple(getattr(item, f.name) for f in dataclass_fields(item))


class Annotations(QObject):
    """Annotazioni di PieDF del documento aperto, selezione e annulla/ripeti."""

    changed = Signal()

    def __init__(self, undo_stack: QUndoStack, parent=None) -> None:
        super().__init__(parent)
        self.undo_stack = undo_stack
        self.items: list[Annotation] = []
        self.selected: Annotation | None = None
        self._original: list[tuple[int, int]] = []  # (pagina, xref) di quelle lette dal file
        self._loaded: dict[int, tuple] = {}  # id → stato letto dal file
        self._images: dict[tuple, QImage] = {}

    def set_items(self, items: list[Annotation]) -> None:
        """Nuovo documento (o nessuno): si riparte da quelle del file."""
        self.items = list(items)
        self.selected = None
        self._original = [(a.page, a.xref) for a in items]
        self._loaded = {id(a): _state(a) for a in items}
        self.changed.emit()

    def on_page(self, page: int) -> list[Annotation]:
        return [a for a in self.items if a.page == page]

    def select(self, item: Annotation | None) -> None:
        if item is not self.selected:
            self.selected = item
            self.changed.emit()

    def pending(self) -> tuple[list[tuple[int, int]], list[Annotation]] | None:
        """(pagina, xref) delle originali da togliere e annotazioni da scrivere; None se niente è cambiato."""
        same = len(self.items) == len(self._loaded) and all(
            self._loaded.get(id(a)) == _state(a) for a in self.items
        )
        return None if same else (list(self._original), list(self.items))

    def image(self, box: TextBox, scale: float) -> QImage:
        """Aspetto della casella com'è nel PDF salvato (lo disegna MuPDF), alla scala data."""
        x0, y0, x1, y1 = box.rect
        key = (x1 - x0, y1 - y0, box.text, box.font, box.size, box.color, box.align, round(scale, 4))
        image = self._images.get(key)
        if image is None:
            if len(self._images) >= IMAGE_CACHE:
                self._images.clear()
            image = _render(box, scale)
            self._images[key] = image
        return image

    # --- Modifiche (ognuna è un passo di annulla) --------------------------------------

    def add(self, item: Annotation) -> None:
        self.undo_stack.push(_AddCommand(self, item))

    def remove(self, item: Annotation) -> None:
        self.undo_stack.push(_RemoveCommand(self, item))

    def update(self, item: Annotation, label: str, **changes) -> None:
        """Cambia attributi (testo, rettangolo, formato) in un solo passo di annulla, chiamato `label`."""
        old = {k: getattr(item, k) for k in changes}
        if old != changes and item in self.items:
            self.undo_stack.push(_ChangeCommand(self, item, old, changes, label))

    def set_rect(self, item: Annotation, old: QRectF, new: QRectF, text: str) -> None:
        """Registra un cambio di rettangolo già applicato dal vivo (trascinamento)."""
        if old != new and item in self.items:
            self.undo_stack.push(_ChangeCommand(self, item, {"rect": to_tuple(old)}, {"rect": to_tuple(new)}, text))

    def preview_rect(self, item: Annotation, rect: QRectF) -> None:
        """Cambia il rettangolo durante un trascinamento, senza passo di annulla."""
        item.rect = to_tuple(rect)
        self.changed.emit()

    # --- Usati dai comandi -------------------------------------------------------------

    def _insert(self, index: int, item: Annotation) -> None:
        self.items.insert(index, item)
        self.selected = item
        self.changed.emit()

    def _take(self, item: Annotation) -> int:
        index = self.items.index(item)
        del self.items[index]
        if self.selected is item:
            self.selected = None
        self.changed.emit()
        return index

    def _apply(self, item: Annotation, values: dict) -> None:
        for key, value in values.items():
            setattr(item, key, value)
        self.selected = item
        self.changed.emit()


def _render(box: TextBox, scale: float) -> QImage:
    # Una pagina grande quanto la casella, con la sola FreeText: pochi decimi di millisecondo.
    x0, y0, x1, y1 = box.rect
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=max(x1 - x0, 1.0), height=max(y1 - y0, 1.0))
        add_text_box(page, TextBox(0, (0, 0, page.rect.width, page.rect.height), text=box.text,
                                   font=box.font, size=box.size, color=box.color, align=box.align))
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=True)
        # Le pixmap di MuPDF con alfa sono premoltiplicate.
        return QImage(pix.samples, pix.width, pix.height, pix.stride,
                      QImage.Format.Format_RGBA8888_Premultiplied).copy()
    finally:
        doc.close()


def _label(item: Annotation) -> str:
    return "casella di testo" if isinstance(item, TextBox) else "annotazione"


class _AddCommand(QUndoCommand):
    def __init__(self, model: Annotations, item: Annotation) -> None:
        super().__init__(f"inserimento {_label(item)}")
        self._model, self._item = model, item
        self._index = len(model.items)

    def redo(self) -> None:
        self._model._insert(self._index, self._item)

    def undo(self) -> None:
        self._model._take(self._item)


class _RemoveCommand(QUndoCommand):
    def __init__(self, model: Annotations, item: Annotation) -> None:
        super().__init__(f"eliminazione {_label(item)}")
        self._model, self._item = model, item
        self._index = model.items.index(item)

    def redo(self) -> None:
        self._index = self._model._take(self._item)

    def undo(self) -> None:
        self._model._insert(self._index, self._item)


class _ChangeCommand(QUndoCommand):
    def __init__(self, model: Annotations, item: Annotation, old: dict, new: dict, text: str) -> None:
        super().__init__(text)
        self._model, self._item = model, item
        self._old, self._new = dict(old), dict(new)

    def redo(self) -> None:
        self._model._apply(self._item, self._new)

    def undo(self) -> None:
        self._model._apply(self._item, self._old)
