"""Annotazioni modificabili di PieDF (caselle di testo e tratti del disegno, #22), in memoria.

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

from piedf.document import FONTS, Annotation, Ink, TextBox, add_text_box, ink_rect, longest_word, text_box_height
from piedf.ink import bounds, stroke_distance, translated

FONT_LABELS = {"Helv": "Helvetica", "TiRo": "Times", "Cour": "Courier"}
COLORS = {"#000000": "Nero", "#1a3fbf": "Blu", "#c00000": "Rosso"}
MIN_SIZE, MAX_SIZE = 8, 36
DEFAULT_STYLE = {"font": FONTS[0], "size": 11.0, "color": "#000000", "align": 0}
NEW_BOX_WIDTH = 180.0  # punti
MIN_BOX_WIDTH = 20.0  # punti
IMAGE_CACHE = 200  # immagini delle caselle tenute in memoria

# Disegno (#22): strumenti, colori e spessori.
PEN, HIGHLIGHTER, ERASER = "pen", "highlighter", "eraser"
TOOL_LABELS = {PEN: "Penna", HIGHLIGHTER: "Evidenziatore", ERASER: "Gomma"}
PEN_COLORS = {"#000000": "Nero", "#1a3fbf": "Blu", "#c00000": "Rosso", "#1e8c3a": "Verde"}
PEN_WIDTHS = {1.0: "Sottile", 2.0: "Medio", 4.0: "Spesso"}
HIGHLIGHTER_COLORS = {"#ffe600": "Giallo", "#5fe05f": "Verde", "#ff7ac0": "Rosa"}
HIGHLIGHTER_WIDTH = 12.0
HIGHLIGHTER_OPACITY = 0.35
DEFAULT_DRAW = {"tool": PEN, "pen_color": "#000000", "pen_width": 2.0, "highlighter_color": "#ffe600"}


def to_qrect(rect: tuple[float, float, float, float]) -> QRectF:
    x0, y0, x1, y1 = rect
    return QRectF(x0, y0, x1 - x0, y1 - y0)


def to_tuple(rect: QRectF) -> tuple[float, float, float, float]:
    return (rect.left(), rect.top(), rect.right(), rect.bottom())


def fitted_rect(box: TextBox, rect: QRectF, page_size: tuple[float, float] | None = None, **changes) -> QRectF:
    """Il rettangolo con l'altezza che segue il testo (con le modifiche `changes`), dentro la pagina.

    MuPDF non spezza le parole: la casella non è mai più stretta della parola più lunga.
    """
    text = changes.get("text", box.text)
    font, size = changes.get("font", box.font), changes.get("size", box.size)
    width = max(rect.width(), MIN_BOX_WIDTH, longest_word(text, font, size))
    if page_size is not None:
        width = min(width, page_size[0])
    height = text_box_height(text or " ", font, size, width)
    rect = QRectF(rect.x(), rect.y(), width, height)
    if page_size is not None:
        page_w, page_h = page_size
        rect.moveLeft(max(0.0, min(rect.x(), page_w - rect.width())))
        rect.moveTop(max(0.0, min(rect.y(), page_h - rect.height())))
    return rect


def moved_ink(strokes, width: float, dx: float, dy: float, page_size: tuple[float, float]) -> dict:
    """Tratti e rettangolo spostati di (dx, dy), con i punti dentro la pagina."""
    x0, y0, x1, y1 = bounds(strokes)
    page_w, page_h = page_size
    dx = max(-x0, min(dx, page_w - x1)) if x1 - x0 <= page_w else 0.0
    dy = max(-y0, min(dy, page_h - y1)) if y1 - y0 <= page_h else 0.0
    strokes = translated(strokes, dx, dy)
    return {"strokes": strokes, "rect": ink_rect(strokes, width)}


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

    def add(self, item: Annotation, select: bool = True) -> None:
        """`select=False` per i tratti: lo strumento di disegno resta attivo, niente selezione."""
        self.undo_stack.push(_AddCommand(self, item, select))

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

    def preview(self, item: Annotation, **values) -> None:
        """Cambia attributi durante un trascinamento (i punti di un tratto), senza passo di annulla."""
        for key, value in values.items():
            setattr(item, key, value)
        self.changed.emit()

    def set_values(self, item: Annotation, old: dict, label: str) -> None:
        """Registra un cambio già applicato dal vivo: `old` sono i valori di prima."""
        new = {k: getattr(item, k) for k in old}
        if old != new and item in self.items:
            self.undo_stack.push(_ChangeCommand(self, item, old, new, label))

    def ink_at(self, page: int, pt: tuple[float, float], tolerance: float) -> Ink | None:
        """Tratto della pagina sotto il punto (punti PDF): distanza sotto metà spessore o `tolerance`."""
        x, y = pt
        for item in reversed(self.items):  # quello disegnato sopra vince
            if not isinstance(item, Ink) or item.page != page:
                continue
            reach = max(item.width / 2, tolerance)
            x0, y0, x1, y1 = item.rect
            if not (x0 - reach <= x <= x1 + reach and y0 - reach <= y <= y1 + reach):
                continue
            if any(stroke_distance(pt, stroke) <= reach for stroke in item.strokes):
                return item
        return None

    # --- Usati dai comandi -------------------------------------------------------------

    def _insert(self, index: int, item: Annotation, select: bool = True) -> None:
        self.items.insert(index, item)
        if select:
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
    if isinstance(item, TextBox):
        return "casella di testo"
    return "tratto" if isinstance(item, Ink) else "annotazione"


class _AddCommand(QUndoCommand):
    def __init__(self, model: Annotations, item: Annotation, select: bool) -> None:
        super().__init__(f"inserimento {_label(item)}")
        self._model, self._item, self._select = model, item, select
        self._index = len(model.items)

    def redo(self) -> None:
        self._model._insert(self._index, self._item, self._select)

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
        # Un tratto cancellato (spesso con la gomma, a strumento attivo) torna senza selezione.
        self._model._insert(self._index, self._item, not isinstance(self._item, Ink))


class _ChangeCommand(QUndoCommand):
    def __init__(self, model: Annotations, item: Annotation, old: dict, new: dict, text: str) -> None:
        super().__init__(text)
        self._model, self._item = model, item
        self._old, self._new = dict(old), dict(new)

    def redo(self) -> None:
        self._model._apply(self._item, self._new)

    def undo(self) -> None:
        self._model._apply(self._item, self._old)
