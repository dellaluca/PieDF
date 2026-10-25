import bisect
import math
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QTextOption, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QComboBox,
    QFrame,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QWidget,
)

from piedf.annotations import (
    DEFAULT_STYLE,
    MIN_BOX_WIDTH,
    NEW_BOX_WIDTH,
    Annotations,
    fitted_rect,
    to_qrect,
    to_tuple,
)
from piedf.document import COMBO, LIST, TEXT, FormField, TextBox
from piedf.forms import FormValues
from piedf.placements import MIN_HEIGHT, Placement, Placements, clamp_rect, default_rect
from piedf.renderer import Renderer
from piedf.signatures import Signature

MARGIN = 12  # px logici intorno e tra le pagine
POINTS_TO_PX = 96 / 72  # al 100% una pagina ha le sue dimensioni reali sullo schermo
MIN_ZOOM, MAX_ZOOM = 0.1, 8.0
ZOOM_STEP = 1.25
# Oltre questa soglia la pagina si renderizza più piccola e si ingrandisce in disegno:
# evita immagini da centinaia di MB con pagine enormi o zoom molto alti.
MAX_RENDER_PIXELS = 25_000_000
CACHE_BYTES = 300 * 1024 * 1024

BACKGROUND = QColor("#525659")
PAGE_BORDER = QColor("#3c3f41")
SELECTION = QColor("#1a73e8")
HANDLE = 8  # lato delle maniglie, px logici
PREVIEW_OPACITY = 0.6
FIELD_HIGHLIGHT = QColor(214, 224, 255)  # azzurro chiaro, come in Acrobat (in modalità Multiply)
FIELD_FONT_PT = 12.0  # font dell'editor per i campi a dimensione automatica (massimo)
ALIGNMENTS = [Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignHCenter, Qt.AlignmentFlag.AlignRight]
# Angoli in senso orario da in alto a sinistra: maniglie e cursori corrispondenti.
CORNER_CURSORS = [
    Qt.CursorShape.SizeFDiagCursor, Qt.CursorShape.SizeBDiagCursor,
    Qt.CursorShape.SizeFDiagCursor, Qt.CursorShape.SizeBDiagCursor,
]
# Font dell'editor delle caselle: gli equivalenti di Windows dei font standard PDF (stesse metriche).
BOX_FONTS = {
    "Helv": ("Arial", QFont.StyleHint.SansSerif),
    "TiRo": ("Times New Roman", QFont.StyleHint.Serif),
    "Cour": ("Courier New", QFont.StyleHint.TypeWriter),
}


@dataclass(frozen=True, eq=False)
class PageLink:
    """Link di una pagina, nello spazio della pagina come si vede (vedi render_worker)."""

    rect: QRectF  # punti PDF
    kind: str  # "goto" (interno) o "uri" (esterno, la finestra decide se seguirlo)
    page: int = -1  # destinazione dei link interni
    y: float | None = None  # punti PDF nella pagina di destinazione; None = in cima
    uri: str = ""

    @property
    def tooltip(self) -> str:
        return f"Vai a pagina {self.page + 1}" if self.kind == "goto" else self.uri


class FitMode(Enum):
    NONE = 0
    WIDTH = 1
    PAGE = 2


class PdfView(QAbstractScrollArea):
    """Vista a scorrimento continuo che chiede al renderer solo le pagine visibili."""

    page_changed = Signal(int)  # indice da 0
    zoom_changed = Signal(float, FitMode)
    placing_changed = Signal(bool)  # entrata/uscita dalla modalità posizionamento
    link_goto = Signal(int, float)  # clic su un link interno: pagina, y in punti
    link_uri = Signal(str)  # clic su un link esterno, ancora da validare
    text_tool_changed = Signal(bool)  # entrata/uscita dalla modalità Testo o Data
    # Casella selezionata o in modifica (None se nessuna), anche quando cambia il suo formato.
    text_box_changed = Signal(object)

    def __init__(self, renderer: Renderer, parent=None) -> None:
        super().__init__(parent)
        self._renderer = renderer
        renderer.page_ready.connect(self._on_page_ready)
        renderer.links_ready.connect(self._on_links_ready)
        renderer.field_applied.connect(self._on_field_applied)

        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.verticalScrollBar().setSingleStep(40)
        self.horizontalScrollBar().setSingleStep(40)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._gen = 0
        self._sizes: list[tuple[float, float]] = []
        self._zoom = 1.0
        self._fit = FitMode.WIDTH
        self._tops: list[int] = []
        self._content_w = 0
        self._content_h = 0
        self._current = 0
        self._cache: OrderedDict[int, tuple[float, QImage]] = OrderedDict()
        self._cache_bytes = 0
        self._last_request: list[tuple[int, float]] | None = None
        self._links: dict[int, list[PageLink]] = {}  # pagine i cui link sono già arrivati
        self._links_pending: set[int] = set()

        self._placements = Placements(self)
        self._placements.changed.connect(self.viewport().update)
        self._placements.changed.connect(self._on_selection)
        self._annotations = Annotations(self._placements.undo_stack, self)
        self._connect_annotations()
        self._shown: tuple = (None, None)  # (firma, annotazione) selezionate all'ultimo controllo
        self._active_box: TextBox | None = None  # ultima casella attiva segnalata
        self._placing: Signature | None = None
        self._cursor_pos: QPointF | None = None  # ultima posizione del mouse nella vista
        # Trascinamento in corso: (firma, rettangolo iniziale, punto iniziale in punti, angolo o None).
        self._drag: tuple[Placement, QRectF, QPointF, int | None] | None = None
        self._link_press: tuple[PageLink, QPointF] | None = None  # pressione su un link, non ancora clic
        # Manina: Spazio tenuto premuto e trascinamento in corso (tasto, punto iniziale, barre iniziali).
        self._space_held = False
        self._pan: tuple[Qt.MouseButton, QPointF, int, int] | None = None
        self.viewport().setMouseTracking(True)

        self._forms = FormValues(self._placements.undo_stack, self)
        self._connect_forms()
        self._highlight = True
        self._focus_field: FormField | None = None  # campo attivo (Tab, Spazio), con o senza editor
        self._editing: FormField | None = None
        self._editor: QWidget | None = None  # editor sul posto, figlio del viewport
        self._committing = False

        # Caselle di testo: modalità Testo/Data (None = spenta, "" = Testo, altrimenti il testo
        # della data), casella nell'editor (nuova = non ancora nel modello) e trascinamento
        # (casella, rettangolo iniziale, punto iniziale in punti, lato 0/1 o None).
        self._text_tool: str | None = None
        self._editing_box: TextBox | None = None
        self._new_box = False
        self._box_drag: tuple[TextBox, QRectF, QPointF, int | None] | None = None
        self.text_style = dict(DEFAULT_STYLE)  # formato delle prossime caselle

    # --- API pubblica -----------------------------------------------------------------

    @property
    def page_count(self) -> int:
        return len(self._sizes)

    @property
    def page_sizes(self) -> list[tuple[float, float]]:
        return self._sizes

    @property
    def current_page(self) -> int:
        return self._current

    @property
    def zoom(self) -> float:
        return self._zoom

    @property
    def fit_mode(self) -> FitMode:
        return self._fit

    @property
    def placing(self) -> bool:
        return self._placing is not None

    @property
    def panning(self) -> bool:
        """Manina attiva: Spazio tenuto premuto o trascinamento in corso."""
        return self._space_held or self._pan is not None

    @property
    def editing_field(self) -> FormField | None:
        return self._editing

    @property
    def editing_box(self) -> TextBox | None:
        return self._editing_box

    @property
    def text_tool(self) -> bool:
        return self._text_tool is not None

    @property
    def date_tool(self) -> bool:
        return bool(self._text_tool)

    @property
    def active_text_box(self) -> TextBox | None:
        """Casella in modifica o selezionata: quella su cui agisce la barra delle proprietà."""
        return self._editing_box or (
            self._annotations.selected if isinstance(self._annotations.selected, TextBox) else None
        )

    @property
    def editor(self) -> QWidget | None:
        return self._editor

    @property
    def focus_field(self) -> FormField | None:
        return self._focus_field

    def set_placements(self, placements: Placements) -> None:
        self._placements.changed.disconnect(self.viewport().update)
        self._placements.changed.disconnect(self._on_selection)
        self._placements = placements
        placements.changed.connect(self.viewport().update)
        placements.changed.connect(self._on_selection)

    def set_annotations(self, annotations: Annotations) -> None:
        self._annotations.changed.disconnect(self._on_annotations_changed)
        self._annotations = annotations
        self._connect_annotations()

    def set_forms(self, forms: FormValues) -> None:
        self._forms.values_changed.disconnect(self._on_values_changed)
        self._forms.fields_reset.disconnect(self._on_fields_reset)
        self._forms = forms
        self._connect_forms()

    def set_highlight(self, on: bool) -> None:
        """Sfondo azzurro sui campi compilabili."""
        self._highlight = on
        self.viewport().update()

    def commit_editor(self) -> None:
        """Conferma il testo in corso nell'editor (prima di salvare o chiudere)."""
        self._close_editor(commit=True)

    def start_placing(self, signature: Signature) -> None:
        """Modalità posizionamento: l'anteprima segue il cursore, un clic la piazza."""
        if not self._sizes:
            return
        self._close_editor(commit=True)
        self.cancel_text_tool()
        self._focus_field = None
        self._drag = None
        self._placing = signature
        self._placements.image(signature.path)  # in cache subito, anche se il PNG sparisce
        self._placements.select(None)
        self._annotations.select(None)
        self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        self.setFocus()
        self.viewport().update()
        self.placing_changed.emit(True)

    def cancel_placing(self) -> None:
        if self._placing is None:
            return
        self._placing = None
        self._update_cursor()
        self.viewport().update()
        self.placing_changed.emit(False)

    def start_text_tool(self, date: str | None = None) -> None:
        """Modalità Testo (o Data con `date`): un clic sulla pagina mette una casella."""
        if not self._sizes:
            return
        self._close_editor(commit=True)
        self.cancel_placing()
        self._focus_field = None
        self._text_tool = date or ""
        self._annotations.select(None)
        self._placements.select(None)
        self._update_cursor()
        self.setFocus()
        self.text_tool_changed.emit(True)

    def cancel_text_tool(self) -> None:
        if self._text_tool is None:
            return
        self._text_tool = None
        self._update_cursor()
        self.text_tool_changed.emit(False)

    def set_text_style(self, **changes) -> None:
        """Formato (font, size, color, align) per la casella attiva e per le prossime."""
        self.text_style.update(changes)
        box = self.active_text_box
        if box is None:
            return
        if box is self._editing_box and self._new_box:
            for key, value in changes.items():
                setattr(box, key, value)
        else:
            rect = fitted_rect(box, to_qrect(box.rect), self._sizes[box.page], **changes)
            self._annotations.update(box, "formato casella di testo", rect=to_tuple(rect), **changes)
        if box is self._editing_box:
            self._style_box_editor()
            self._place_editor()
            self._editor.setFocus()
        self._emit_active(force=True)
        self.viewport().update()

    def page_at(self, pos: QPointF | QPoint) -> int | None:
        """Pagina sotto il punto della vista (px logici), o None sullo sfondo."""
        pos = QPointF(pos)
        for page in self._visible_range():
            if self.page_rect_to_viewport(page, QRectF(QPointF(0, 0), QPointF(*self._sizes[page]))).contains(pos):
                return page
        return None

    def to_page_point(self, index: int, pos: QPointF | QPoint) -> QPointF:
        """Punto della vista (px logici) → punti PDF della pagina `index`."""
        pos = QPointF(pos)
        rect = self._page_rect(index)
        s = self._scale()
        return QPointF(
            (pos.x() + self.horizontalScrollBar().value() - rect.x()) / s,
            (pos.y() + self.verticalScrollBar().value() - rect.y()) / s,
        )

    def page_rect_to_viewport(self, index: int, rect_pt: QRectF) -> QRectF:
        """Rettangolo in punti PDF della pagina `index` → coordinate della vista (px logici)."""
        page = self._page_rect(index)
        s = self._scale()
        return QRectF(
            page.x() - self.horizontalScrollBar().value() + rect_pt.x() * s,
            page.y() - self.verticalScrollBar().value() + rect_pt.y() * s,
            rect_pt.width() * s,
            rect_pt.height() * s,
        )

    def link_at(self, pos: QPointF | QPoint) -> PageLink | None:
        """Link sotto il punto della vista (px logici), se i suoi link sono già arrivati."""
        page = self.page_at(pos)
        if page is None:
            return None
        pt = self.to_page_point(page, pos)
        for link in self._links.get(page, ()):
            if link.rect.contains(pt):
                return link
        return None

    def set_document(self, gen: int, page_sizes: list[tuple[float, float]]) -> None:
        self._reset_interaction()
        self._gen = gen
        self._sizes = page_sizes
        self._clear_cache()
        self._current = -1
        # Ogni documento si apre adattato alla larghezza, come in Acrobat.
        self._fit = FitMode.WIDTH
        self._apply_fit()
        self.verticalScrollBar().setValue(0)
        self.horizontalScrollBar().setValue(0)
        self._update_current_page()
        self.zoom_changed.emit(self._zoom, self._fit)
        self.viewport().update()

    def reload(self) -> None:
        """Richiede di nuovo le pagine mancanti (il processo di render ha riaperto il documento)."""
        self._last_request = None
        self._links_pending.clear()
        self.viewport().update()

    def clear(self) -> None:
        self._reset_interaction()
        self._gen += 1
        self._sizes = []
        self._clear_cache()
        self._relayout()
        self.viewport().update()

    def set_zoom(self, zoom: float, anchor: QPoint | None = None) -> None:
        self._fit = FitMode.NONE
        self._zoom_to(zoom, anchor)
        self.zoom_changed.emit(self._zoom, self._fit)

    def zoom_in(self) -> None:
        self.set_zoom(self._zoom * ZOOM_STEP)

    def zoom_out(self) -> None:
        self.set_zoom(self._zoom / ZOOM_STEP)

    def set_fit_mode(self, mode: FitMode) -> None:
        self._fit = mode
        if self._sizes:
            page = self._current
            self._apply_fit()
            if mode == FitMode.PAGE:
                self.go_to_page(page)
        self.zoom_changed.emit(self._zoom, self._fit)

    def go_to_page(self, index: int) -> None:
        if not self._sizes:
            return
        index = max(0, min(index, len(self._sizes) - 1))
        self.verticalScrollBar().setValue(self._tops[index] - MARGIN)
        self._update_current_page(force=index)

    def scroll_position(self) -> tuple[int, float]:
        """Pagina e distanza (punti PDF) dal suo bordo alto al punto in cima alla vista, margine escluso."""
        if not self._sizes:
            return 0, 0.0
        y = self.verticalScrollBar().value() + MARGIN
        page = max(0, min(bisect.bisect_right(self._tops, y) - 1, len(self._sizes) - 1))
        return page, (y - self._tops[page]) / self._scale()

    def set_scroll_position(self, page: int, y_pt: float) -> None:
        """Inverso di `scroll_position`; una pagina oltre la fine vale l'ultima, in cima."""
        if not self._sizes:
            return
        if page >= len(self._sizes):
            page, y_pt = len(self._sizes) - 1, 0.0
        page = max(0, page)
        self.verticalScrollBar().setValue(round(self._tops[page] + y_pt * self._scale()) - MARGIN)
        self._update_current_page()

    # --- Layout -----------------------------------------------------------------------

    def _scale(self) -> float:
        return self._zoom * POINTS_TO_PX

    def _relayout(self) -> None:
        s = self._scale()
        self._tops = []
        y = MARGIN
        max_w = 0
        for w, h in self._sizes:
            self._tops.append(y)
            y += round(h * s) + MARGIN
            max_w = max(max_w, round(w * s))
        self._content_h = y if self._sizes else 0
        self._content_w = max_w + 2 * MARGIN if self._sizes else 0
        self._update_scrollbars()
        self._place_editor()

    def _update_scrollbars(self) -> None:
        vw, vh = self.viewport().width(), self.viewport().height()
        hbar, vbar = self.horizontalScrollBar(), self.verticalScrollBar()
        hbar.setRange(0, max(0, self._content_w - vw))
        hbar.setPageStep(vw)
        vbar.setRange(0, max(0, self._content_h - vh))
        vbar.setPageStep(vh)

    def _page_rect(self, index: int) -> QRect:
        """Rettangolo della pagina in coordinate del contenuto (px logici)."""
        s = self._scale()
        w, h = self._sizes[index]
        pw, ph = round(w * s), round(h * s)
        x = (max(self._content_w, self.viewport().width()) - pw) // 2
        return QRect(x, self._tops[index], pw, ph)

    def _visible_range(self) -> range:
        if not self._sizes:
            return range(0)
        top = self.verticalScrollBar().value()
        bottom = top + self.viewport().height()
        first = max(0, bisect.bisect_right(self._tops, top) - 1)
        last = max(first, bisect.bisect_left(self._tops, bottom) - 1)
        return range(first, min(last, len(self._sizes) - 1) + 1)

    def _fit_zoom(self, mode: FitMode) -> float:
        avail_w = self.viewport().width() - 2 * MARGIN
        max_w = max(w for w, _ in self._sizes)
        zoom = avail_w / (max_w * POINTS_TO_PX)
        if mode == FitMode.PAGE:
            _, h = self._sizes[max(self._current, 0)]
            avail_h = self.viewport().height() - 2 * MARGIN
            zoom = min(zoom, avail_h / (h * POINTS_TO_PX))
        return zoom

    def _apply_fit(self) -> None:
        self._zoom_to(self._fit_zoom(self._fit))

    def _zoom_to(self, zoom: float, anchor: QPoint | None = None) -> None:
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        if not self._sizes:
            self._zoom = zoom
            return
        if anchor is None:
            anchor = QPoint(self.viewport().width() // 2, self.viewport().height() // 2)
        hbar, vbar = self.horizontalScrollBar(), self.verticalScrollBar()
        ratio = zoom / self._zoom
        # Il punto sotto l'ancora (cursore o centro) resta fermo durante lo zoom.
        new_x = (hbar.value() + anchor.x()) * ratio - anchor.x()
        new_y = (vbar.value() + anchor.y()) * ratio - anchor.y()
        self._zoom = zoom
        self._relayout()
        hbar.setValue(round(new_x))
        vbar.setValue(round(new_y))
        self.viewport().update()

    # --- Render e cache ---------------------------------------------------------------

    def _render_scale(self, index: int) -> float:
        w, h = self._sizes[index]
        scale = self._scale() * self.devicePixelRatioF()
        if w * h * scale * scale > MAX_RENDER_PIXELS:
            # MuPDF arrotonda i lati per eccesso: +1 punto per lato tiene il limite.
            return math.floor(math.sqrt(MAX_RENDER_PIXELS / ((w + 1) * (h + 1))) * 10000) / 10000
        return round(scale, 4)

    def _request_visible(self) -> None:
        visible = self._visible_range()
        if not visible:
            return
        # Prima le pagine visibili, poi una per lato da tenere pronta per lo scroll.
        pages = list(visible)
        for extra in (visible.start - 1, visible.stop):
            if 0 <= extra < len(self._sizes):
                pages.append(extra)
        items = []
        for page in pages:
            scale = self._render_scale(page)
            cached = self._cache.get(page)
            if cached is None or cached[0] != scale:
                items.append((page, scale))
        if items != self._last_request:
            self._last_request = items
            self._renderer.request(self._gen, items)
        # I link solo per le pagine visibili: servono solo sotto il mouse.
        for page in visible:
            if page not in self._links and page not in self._links_pending:
                self._links_pending.add(page)
                self._renderer.request_links(self._gen, page)

    def _on_links_ready(self, gen: int, page: int, links: list) -> None:
        if gen != self._gen or page >= len(self._sizes):
            return
        self._links_pending.discard(page)
        self._links[page] = [_page_link(item, self._sizes) for item in links]
        if self._drag is None and self._cursor_pos is not None:
            self._update_cursor()

    def _on_page_ready(self, gen: int, page: int, scale: float, image: QImage) -> None:
        if gen != self._gen or page >= len(self._sizes):
            return
        wanted = self._render_scale(page)
        cached = self._cache.get(page)
        if cached is not None and cached[0] == wanted and scale != wanted:
            return
        self._store(page, scale, image)
        if page in self._visible_range():
            self.viewport().update()

    def _store(self, page: int, scale: float, image: QImage) -> None:
        old = self._cache.pop(page, None)
        if old is not None:
            self._cache_bytes -= old[1].sizeInBytes()
        self._cache[page] = (scale, image)
        self._cache_bytes += image.sizeInBytes()

        visible = self._visible_range()
        for key in list(self._cache):
            if self._cache_bytes <= CACHE_BYTES:
                break
            if key in visible:
                continue
            self._cache_bytes -= self._cache.pop(key)[1].sizeInBytes()
            self._links.pop(key, None)  # i link seguono la cache delle immagini

    def _clear_cache(self) -> None:
        self._cache.clear()
        self._cache_bytes = 0
        self._last_request = None
        self._links.clear()
        self._links_pending.clear()

    # --- Eventi -----------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self.viewport())
        painter.fillRect(self.viewport().rect(), BACKGROUND)

        if not self._sizes:
            painter.setPen(QColor("#d0d0d0"))
            painter.drawText(
                self.viewport().rect(),
                Qt.AlignmentFlag.AlignCenter,
                "Trascina qui un PDF oppure usa File › Apri (Ctrl+O)",
            )
            return

        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        offset = QPoint(self.horizontalScrollBar().value(), self.verticalScrollBar().value())
        dpr = self.devicePixelRatioF()
        for page in self._visible_range():
            rect = self._page_rect(page).translated(-offset)
            painter.fillRect(rect.adjusted(-1, -1, 1, 1), PAGE_BORDER)
            painter.fillRect(rect, Qt.GlobalColor.white)
            cached = self._cache.get(page)
            if cached is None:
                continue
            self._cache.move_to_end(page)
            scale, image = cached
            if scale == round(self._scale() * dpr, 4):
                # Scala esatta: pixel 1:1 per un testo nitido.
                image.setDevicePixelRatio(dpr)
                painter.drawImage(rect.topLeft(), image)
            else:
                # Render vecchio (zoom appena cambiato) o ridotto: si stira finché arriva quello giusto.
                painter.drawImage(QRectF(rect), image)
        self._paint_fields(painter)
        self._paint_placements(painter)
        self._paint_annotations(painter)
        painter.end()

        self._request_visible()

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        self.viewport().update()
        self._update_current_page()
        self._place_editor()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._sizes and self._fit != FitMode.NONE:
            page, y_pt = self.scroll_position()
            self._apply_fit()
            self.set_scroll_position(page, y_pt)
            self.zoom_changed.emit(self._zoom, self._fit)
        else:
            self._update_scrollbars()
        self._place_editor()

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y() / 120
            if steps and self._sizes:
                self.set_zoom(self._zoom * (1.1**steps), event.position().toPoint())
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event) -> None:
        vbar = self.verticalScrollBar()
        selected = self._placements.selected
        box = self._annotations.selected
        focus = self._focus_field
        if event.key() == Qt.Key.Key_Escape and self._placing is not None:
            self.cancel_placing()
        elif event.key() == Qt.Key.Key_Escape and self._text_tool is not None:
            self.cancel_text_tool()
        elif box is not None and self._box_drag is None and event.key() in (
            Qt.Key.Key_Escape, Qt.Key.Key_Delete, Qt.Key.Key_Backspace, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_F2
        ):
            if event.key() == Qt.Key.Key_Escape:
                self._annotations.select(None)
            elif event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                self._annotations.remove(box)
                self._update_cursor()
            else:
                self._open_box_editor(box, new=False)
        elif event.key() == Qt.Key.Key_Space and focus is not None and focus.on_state is not None:
            self._forms.toggle(focus)
        elif event.key() == Qt.Key.Key_Space and self._sizes:
            # Windows ripete la pressione finché si tiene il tasto: conta solo la prima.
            if not event.isAutoRepeat() and not self._space_held:
                self._space_held = True
                self._link_press = None
                self._update_cursor()
                self.viewport().update()  # l'anteprima della firma in posizionamento sparisce
        elif event.key() == Qt.Key.Key_Escape and focus is not None:
            self._focus_field = None
            self.viewport().update()
        elif event.key() == Qt.Key.Key_Escape and selected is not None and self._drag is None:
            self._placements.select(None)
        elif event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and selected is not None and self._drag is None:
            self._placements.remove(selected)
            self._update_cursor()
        elif event.key() == Qt.Key.Key_Home:
            vbar.setValue(0)
        elif event.key() == Qt.Key.Key_End:
            vbar.setValue(vbar.maximum())
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat() and self._space_held:
            # Il trascinamento già iniziato continua fino al rilascio del mouse.
            self._space_held = False
            self._update_cursor()
            self.viewport().update()
            return
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event) -> None:
        # Alt+Tab con lo Spazio premuto: il rilascio arriva a un'altra finestra.
        self._cancel_pan()
        super().focusOutEvent(event)

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.WindowDeactivate:
            self._cancel_pan()
        return super().event(event)

    def _cancel_pan(self) -> None:
        if self._space_held or self._pan is not None:
            self._space_held = False
            self._pan = None
            self._update_cursor()
            self.viewport().update()

    def _update_current_page(self, force: int | None = None) -> None:
        if not self._sizes:
            return
        if force is not None:
            page = force
        else:
            # La pagina corrente è quella che occupa il primo terzo della vista.
            y = self.verticalScrollBar().value() + self.viewport().height() // 3
            page = max(0, bisect.bisect_right(self._tops, y) - 1)
            if self.verticalScrollBar().value() >= self.verticalScrollBar().maximum() > 0:
                page = len(self._sizes) - 1
        if page != self._current:
            self._current = page
            self.page_changed.emit(page)

    # --- Firme piazzate ---------------------------------------------------------------

    def _reset_interaction(self) -> None:
        self._drag = None
        self._box_drag = None
        self._link_press = None
        self._space_held = False
        self._pan = None
        self.cancel_placing()
        self.cancel_text_tool()
        self._close_editor(commit=False)
        self._focus_field = None

    def _placing_rect(self) -> tuple[int, QRectF] | None:
        """Pagina e rettangolo (punti) dove finirebbe la firma in posizionamento."""
        if self._placing is None or self._cursor_pos is None or self.panning:
            return None
        page = self.page_at(self._cursor_pos)
        if page is None:
            return None
        image = self._placements.image(self._placing.path)
        center = self.to_page_point(page, self._cursor_pos)
        return page, default_rect(self._placing.kind, image, self._sizes[page], center)

    def _handle_rects(self, rect: QRectF) -> list[QRectF]:
        corners = [rect.topLeft(), rect.topRight(), rect.bottomRight(), rect.bottomLeft()]
        # Su pixel interi: maniglie nitide.
        return [QRectF(round(c.x() - HANDLE / 2), round(c.y() - HANDLE / 2), HANDLE, HANDLE) for c in corners]

    def _paint_placements(self, painter: QPainter) -> None:
        visible = self._visible_range()
        for placement in self._placements.items:
            if placement.page in visible:
                target = self.page_rect_to_viewport(placement.page, placement.rect)
                painter.drawImage(target, self._placements.image(placement.image_path))

        selected = self._placements.selected
        if selected is not None and selected.page in visible:
            rect = self.page_rect_to_viewport(selected.page, selected.rect)
            pen = QPen(SELECTION, 1, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect)
            painter.setPen(QPen(SELECTION, 1))
            painter.setBrush(Qt.GlobalColor.white)
            for handle in self._handle_rects(rect):
                painter.drawRect(handle)

        preview = self._placing_rect()
        if preview is not None:
            page, rect = preview
            painter.setOpacity(PREVIEW_OPACITY)
            painter.drawImage(self.page_rect_to_viewport(page, rect), self._placements.image(self._placing.path))
            painter.setOpacity(1.0)

    def _hit(self, pos: QPointF) -> tuple[Placement | None, int | None]:
        """Firma sotto il punto e, se è la selezionata, l'angolo della maniglia (0-3)."""
        selected = self._placements.selected
        if selected is not None and selected.page in self._visible_range():
            rect = self.page_rect_to_viewport(selected.page, selected.rect)
            for corner, handle in enumerate(self._handle_rects(rect)):
                if handle.adjusted(-2, -2, 2, 2).contains(pos):
                    return selected, corner
        visible = self._visible_range()
        # Dall'ultima disegnata: quella sopra vince.
        for placement in reversed(self._placements.items):
            if placement.page in visible and self.page_rect_to_viewport(placement.page, placement.rect).contains(pos):
                return placement, None
        return None, None

    def _update_cursor(self) -> None:
        viewport = self.viewport()
        if self._pan is not None or (self._space_held and self._drag is None and self._box_drag is None):
            # La manina vince su tutto: lo strumento di prima riprende rilasciando lo Spazio.
            viewport.setCursor(Qt.CursorShape.ClosedHandCursor if self._pan is not None else Qt.CursorShape.OpenHandCursor)
            if viewport.toolTip():
                viewport.setToolTip("")
        elif self._placing is not None:
            viewport.setCursor(Qt.CursorShape.CrossCursor)
        elif self._text_tool is not None:
            viewport.setCursor(Qt.CursorShape.IBeamCursor if self._text_tool == "" else Qt.CursorShape.CrossCursor)
        elif self._drag is not None:
            corner = self._drag[3]
            viewport.setCursor(Qt.CursorShape.ClosedHandCursor if corner is None else CORNER_CURSORS[corner])
        elif self._box_drag is not None:
            side = self._box_drag[3]
            viewport.setCursor(Qt.CursorShape.ClosedHandCursor if side is None else Qt.CursorShape.SizeHorCursor)
        else:
            placement, corner = self._hit(self._cursor_pos) if self._cursor_pos is not None else (None, None)
            box, side = self._hit_box(self._cursor_pos) if corner is None and self._cursor_pos is not None else (None, None)
            field = None
            if corner is None and box is None and self._cursor_pos is not None:
                field = self._field_at(self._cursor_pos)
            link = None
            if field is None and placement is None and box is None and self._cursor_pos is not None and self._links_active():
                link = self.link_at(self._cursor_pos)
            if corner is not None:
                viewport.setCursor(CORNER_CURSORS[corner])
            elif side is not None:
                viewport.setCursor(Qt.CursorShape.SizeHorCursor)
            elif box is not None:
                viewport.setCursor(Qt.CursorShape.OpenHandCursor)
            elif field is not None:
                text = field.type == TEXT or (field.type == COMBO and field.editable)
                viewport.setCursor(Qt.CursorShape.IBeamCursor if text else Qt.CursorShape.PointingHandCursor)
            elif placement is not None:
                viewport.setCursor(Qt.CursorShape.OpenHandCursor)
            elif link is not None:
                viewport.setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                viewport.unsetCursor()
            tooltip = link.tooltip if link is not None else ""
            if viewport.toolTip() != tooltip:
                viewport.setToolTip(tooltip)

    def _links_active(self) -> bool:
        """I link rispondono al clic solo se nessuno strumento lo usa; un campo modulo sotto il mouse vince sempre (si controlla prima)."""
        return (
            not self._space_held and self._pan is None
            and self._placing is None and self._drag is None and self._placements.selected is None
            and self._text_tool is None and self._box_drag is None and self._editing_box is None
            and self._annotations.selected is None
        )

    def _resized_rect(self, start: QRectF, corner: int, pt: QPointF, page_size: tuple[float, float]) -> QRectF:
        """Ridimensiona da un angolo tenendo fermo quello opposto e le proporzioni."""
        page_w, page_h = page_size
        ratio = start.width() / start.height()
        right, bottom = corner in (1, 2), corner in (2, 3)
        anchor_x = start.left() if right else start.right()
        anchor_y = start.top() if bottom else start.bottom()
        # Estensione verso il cursore, nella direzione dell'angolo trascinato.
        dx = (pt.x() - anchor_x) if right else (anchor_x - pt.x())
        dy = (pt.y() - anchor_y) if bottom else (anchor_y - pt.y())
        width = max(dx, dy * ratio)
        # Dentro la pagina, ma mai sotto l'altezza minima.
        avail_w = (page_w - anchor_x) if right else anchor_x
        avail_h = (page_h - anchor_y) if bottom else anchor_y
        width = min(width, avail_w, avail_h * ratio)
        width = max(width, MIN_HEIGHT * ratio)
        height = width / ratio
        x = anchor_x if right else anchor_x - width
        y = anchor_y if bottom else anchor_y - height
        return QRectF(x, y, width, height)

    def mousePressEvent(self, event) -> None:
        pos = event.position()
        self._cursor_pos = pos
        # Un clic fuori dall'editor conferma quello che si è scritto (l'editor riceve i suoi clic).
        self._close_editor(commit=True)
        # Manina: Spazio + tasto sinistro o tasto centrale. Il clic non fa altro.
        if self._pan is not None or self._space_held or event.button() == Qt.MouseButton.MiddleButton:
            buttons = (Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton)
            if self._pan is None and self._drag is None and self._box_drag is None and event.button() in buttons:
                hbar, vbar = self.horizontalScrollBar(), self.verticalScrollBar()
                self._pan = (event.button(), pos, hbar.value(), vbar.value())
                self._link_press = None
                self._update_cursor()
            event.accept()
            return
        if self._placing is not None:
            if event.button() == Qt.MouseButton.RightButton:
                self.cancel_placing()
            elif event.button() == Qt.MouseButton.LeftButton:
                target = self._placing_rect()
                if target is not None:
                    page, rect = target
                    signature = self._placing
                    self.cancel_placing()
                    self._placements.add(Placement(page, rect, signature.path, signature.kind))
                    self._update_cursor()
            event.accept()
            return
        if self._text_tool is not None:
            if event.button() == Qt.MouseButton.RightButton:
                self.cancel_text_tool()
            elif event.button() == Qt.MouseButton.LeftButton:
                self._text_click(pos)
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton or not self._sizes:
            super().mousePressEvent(event)
            return
        placement, corner = self._hit(pos)
        # Le caselle di testo sono sopra a tutto (tranne le maniglie della firma selezionata),
        # anche sopra i campi; il campo ha la precedenza sulla firma.
        box, side = self._hit_box(pos) if corner is None else (None, None)
        field = self._field_at(pos) if corner is None and box is None else None
        if field is not None:
            self._placements.select(None)
            self._annotations.select(None)
            self._activate_field(field, clicked=True)
            self._update_cursor()
            event.accept()
            return
        if self._focus_field is not None:
            self._focus_field = None
            self.viewport().update()
        if box is not None:
            self._placements.select(None)
            self._annotations.select(box)
            self._box_drag = (box, to_qrect(box.rect), self.to_page_point(box.page, pos), side)
            self._update_cursor()
            event.accept()
            return
        # Con una firma o una casella selezionata il clic la deseleziona e basta, anche sopra un link.
        link = self.link_at(pos) if placement is None and self._links_active() else None
        self._link_press = (link, pos) if link is not None else None
        self._annotations.select(None)
        self._placements.select(placement)
        if placement is not None:
            start = self.to_page_point(placement.page, pos)
            self._drag = (placement, QRectF(placement.rect), start, corner)
        self._update_cursor()
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        self._cursor_pos = pos
        if self._link_press is not None and _moved(self._link_press[1], pos):
            self._link_press = None  # trascinamento: niente clic sul link
        if self._pan is not None:
            # Il documento segue il mouse 1:1; le barre restano da sole nei loro limiti.
            _, start, x, y = self._pan
            delta = pos - start
            self.horizontalScrollBar().setValue(round(x - delta.x()))
            self.verticalScrollBar().setValue(round(y - delta.y()))
        elif self._drag is not None:
            placement, start_rect, start, corner = self._drag
            pt = self.to_page_point(placement.page, pos)
            size = self._sizes[placement.page]
            if corner is None:
                rect = clamp_rect(start_rect.translated(pt - start), size)
            else:
                rect = self._resized_rect(start_rect, corner, pt, size)
            self._placements.preview_rect(placement, rect)
        elif self._box_drag is not None:
            box, start_rect, start, side = self._box_drag
            pt = self.to_page_point(box.page, pos)
            size = self._sizes[box.page]
            if side is None:
                rect = clamp_rect(start_rect.translated(pt - start), size)
            else:
                rect = fitted_rect(box, self._resized_box(start_rect, side, pt, size), size)
                if side == 0:  # non più stretta della parola più lunga: si allarga verso sinistra
                    rect.moveRight(start_rect.right())
            self._annotations.preview_rect(box, rect)
        elif self._placing is not None:
            self.viewport().update()
        else:
            self._update_cursor()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self._cursor_pos = event.position()
        if self._pan is not None:
            if event.button() == self._pan[0]:
                self._pan = None
                self._update_cursor()
        elif self._drag is not None and event.button() == Qt.MouseButton.LeftButton:
            placement, start_rect, _, corner = self._drag
            self._drag = None
            text = "spostamento firma" if corner is None else "ridimensionamento firma"
            # Tutto il trascinamento diventa un solo passo di annulla.
            self._placements.set_rect(placement, start_rect, QRectF(placement.rect), text)
            self._update_cursor()
        elif self._box_drag is not None and event.button() == Qt.MouseButton.LeftButton:
            box, start_rect, _, side = self._box_drag
            self._box_drag = None
            text = "spostamento casella di testo" if side is None else "larghezza casella di testo"
            self._annotations.set_rect(box, start_rect, to_qrect(box.rect), text)
            self._update_cursor()
        elif self._link_press is not None and event.button() == Qt.MouseButton.LeftButton:
            link, start = self._link_press
            self._link_press = None
            if not _moved(start, event.position()) and self.link_at(event.position()) is link:
                self._activate(link)
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        # Doppio clic su una casella: si riapre in modifica (il primo clic l'ha già selezionata).
        pos = event.position()
        if (
            event.button() == Qt.MouseButton.LeftButton and self._placing is None and self._text_tool is None
            and not self.panning
        ):
            box, side = self._hit_box(pos)
            if box is not None and side is None:
                self._box_drag = None
                self._open_box_editor(box, new=False)
                event.accept()
                return
        self.mousePressEvent(event)

    def _activate(self, link: PageLink) -> None:
        if link.kind == "goto":
            self.link_goto.emit(link.page, link.y or 0.0)
        else:
            self.link_uri.emit(link.uri)

    def viewportEvent(self, event) -> bool:
        # Il mouse esce dalla vista: l'anteprima del posizionamento sparisce.
        if event.type() == QEvent.Type.Leave and self._drag is None and self._pan is None:
            self._cursor_pos = None
            if self._placing is not None:
                self.viewport().update()
        return super().viewportEvent(event)

    # --- Campi modulo -----------------------------------------------------------------

    def _connect_forms(self) -> None:
        self._forms.values_changed.connect(self._on_values_changed)
        self._forms.fields_reset.connect(self._on_fields_reset)

    def _on_fields_reset(self) -> None:
        self._close_editor(commit=False)
        self._focus_field = None
        self.viewport().update()

    def _on_values_changed(self, fields: list) -> None:
        for f in fields:
            self._renderer.set_field(self._gen, f.page, f.xref, f.value)
        # Annulla/ripeti su un campo aperto: l'editor mostrerebbe un valore vecchio.
        if self._editing in fields and not self._committing:
            self._close_editor(commit=False)
        self.viewport().update()

    def _on_field_applied(self, gen: int, page: int) -> None:
        """Il campo è nel documento del processo di render: i render già fatti della pagina sono vecchi."""
        if gen != self._gen:
            return
        cached = self._cache.get(page)
        if cached is not None:
            # Si continua a mostrare quello vecchio (niente lampo bianco) finché arriva il nuovo.
            self._cache[page] = (-1.0, cached[1])
        self._last_request = None
        self.viewport().update()

    def _field_rect(self, field: FormField) -> QRectF:
        x0, y0, x1, y1 = field.rect
        return QRectF(x0, y0, x1 - x0, y1 - y0)

    def _field_at(self, pos: QPointF) -> FormField | None:
        """Campo compilabile sotto il punto della vista (i campi firma e i pulsanti non contano)."""
        for page in self._visible_range():
            for f in self._forms.on_page(page):
                if f.fillable and self.page_rect_to_viewport(page, self._field_rect(f)).contains(pos):
                    return f
        return None

    def _paint_fields(self, painter: QPainter) -> None:
        visible = self._visible_range()
        if self._highlight:
            # Moltiplicato: il bianco diventa azzurro, il testo del campo resta scuro.
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
            for page in visible:
                for f in self._forms.on_page(page):
                    if f.fillable:
                        painter.fillRect(self.page_rect_to_viewport(page, self._field_rect(f)), FIELD_HIGHLIGHT)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        focus = self._focus_field
        if focus is not None and self._editor is None and focus.page in visible:
            pen = QPen(SELECTION, 1, Qt.PenStyle.DotLine)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self.page_rect_to_viewport(focus.page, self._field_rect(focus)).adjusted(-2, -2, 2, 2))

    def _activate_field(self, field: FormField, clicked: bool) -> None:
        """Clic o Tab su un campo: caselle e radio si cambiano col clic, gli altri si aprono nell'editor."""
        self._focus_field = field
        if not clicked:
            self._ensure_visible(field)
        if field.type in (TEXT, COMBO, LIST):
            self._open_editor(field, select_all=not clicked)
            if clicked and field.type == COMBO and not field.editable:
                self._editor.showPopup()
        else:
            self.setFocus()
            if clicked:
                self._forms.toggle(field)
        self.viewport().update()

    def _move_focus(self, forward: bool) -> bool:
        """Tab / Maiusc+Tab: campo successivo o precedente, anche sulle pagine dopo."""
        order = self._forms.tab_order
        if not order:
            return False
        self._close_editor(commit=True)
        current = self._focus_field
        if current in order:
            index = (order.index(current) + (1 if forward else -1)) % len(order)
        elif forward:
            index = next((i for i, f in enumerate(order) if f.page >= self._current), 0)
        else:
            index = next((i for i in reversed(range(len(order))) if order[i].page <= self._current), len(order) - 1)
        self._activate_field(order[index], clicked=False)
        return True

    def focusNextPrevChild(self, next: bool) -> bool:
        if self._placing is None and self._sizes and self._move_focus(next):
            return True
        return super().focusNextPrevChild(next)

    def _ensure_visible(self, field: FormField) -> None:
        """Porta il campo in vista (a un terzo dall'alto se era fuori)."""
        rect = self.page_rect_to_viewport(field.page, self._field_rect(field))
        view = QRectF(self.viewport().rect())
        vbar, hbar = self.verticalScrollBar(), self.horizontalScrollBar()
        if rect.top() < 0 or rect.bottom() > view.bottom():
            vbar.setValue(round(vbar.value() + rect.top() - view.height() / 3))
        if rect.left() < 0 or rect.right() > view.right():
            hbar.setValue(round(hbar.value() + rect.center().x() - view.width() / 2))

    def _open_editor(self, field: FormField, select_all: bool) -> None:
        self._close_editor(commit=True)
        parent = self.viewport()
        if field.type == TEXT and field.multiline:
            editor = QPlainTextEdit(field.value, parent)
            if field.max_len:
                editor.textChanged.connect(lambda: self._limit_text(editor, field.max_len))
            editor.moveCursor(editor.textCursor().MoveOperation.End)
            if select_all:
                editor.selectAll()
        elif field.type == TEXT:
            editor = QLineEdit(field.value, parent)
            if field.max_len:
                editor.setMaxLength(field.max_len)
            if field.password:
                editor.setEchoMode(QLineEdit.EchoMode.Password)
            editor.setAlignment(ALIGNMENTS[field.align if 0 <= field.align < 3 else 0])
            if select_all:
                editor.selectAll()
        elif field.type == COMBO:
            editor = QComboBox(parent)
            editor.setEditable(field.editable)
            editor.addItems([text for _, text in field.choices])
            index = next((i for i, (value, _) in enumerate(field.choices) if value == field.value), -1)
            editor.setCurrentIndex(index)
            if field.editable:
                if index < 0:
                    editor.setEditText(field.value)
                editor.lineEdit().installEventFilter(self)
                if select_all:
                    editor.lineEdit().selectAll()
            else:
                # Scegliere una voce la conferma subito; l'editor resta aperto finché si esce dal campo.
                editor.activated.connect(lambda _: self._commit())
        else:
            editor = QListWidget(parent)
            editor.addItems([text for _, text in field.choices])
            editor.setCurrentRow(next((i for i, (value, _) in enumerate(field.choices) if value == field.value), -1))
            editor.itemClicked.connect(lambda _: self._commit())
        editor.installEventFilter(self)
        self._editor, self._editing = editor, field
        self._place_editor()
        editor.show()
        editor.setFocus()

    def _limit_text(self, editor: QPlainTextEdit, max_len: int) -> None:
        text = editor.toPlainText()
        if len(text) > max_len:
            cursor = editor.textCursor()
            position = min(cursor.position(), max_len)
            editor.setPlainText(text[:max_len])
            cursor.setPosition(position)
            editor.setTextCursor(cursor)

    def _place_editor(self) -> None:
        """Segue il campo con scroll e zoom; il font scala con lo zoom."""
        editor, field = self._editor, self._editing
        if editor is None or not self._sizes:
            return
        if self._editing_box is not None:
            self._place_box_editor()
            return
        rect = self.page_rect_to_viewport(field.page, self._field_rect(field)).toAlignedRect()
        size = field.font_size or min(FIELD_FONT_PT, (field.rect[3] - field.rect[1]) * 0.7)
        if field.type in (COMBO, LIST) and not field.font_size:
            size = FIELD_FONT_PT
        font = editor.font()
        font.setPixelSize(max(1, round(size * self._scale())))
        editor.setFont(font)
        editor.setGeometry(rect)

    def _editor_value(self) -> str:
        editor, field = self._editor, self._editing
        if isinstance(editor, QLineEdit):
            return editor.text()
        if isinstance(editor, QPlainTextEdit):
            return editor.toPlainText()
        if isinstance(editor, QComboBox):
            if field.editable:
                text = editor.currentText()
                return next((value for value, shown in field.choices if shown == text), text)
            index = editor.currentIndex()
            return field.choices[index][0] if 0 <= index < len(field.choices) else field.value
        row = editor.currentRow()
        return field.choices[row][0] if 0 <= row < len(field.choices) else field.value

    def _commit(self) -> None:
        if self._editor is None:
            return
        value = self._editor_value()
        if value != self._editing.value:
            self._committing = True
            try:
                self._forms.set_value(self._editing, value)
            finally:
                self._committing = False

    def _close_editor(self, commit: bool) -> None:
        editor = self._editor
        if editor is None:
            return
        if commit:
            self._commit_box() if self._editing_box is not None else self._commit()
        self._editor = self._editing = self._editing_box = None
        focus = editor.window().focusWidget()
        if focus is not None and (focus is editor or editor.isAncestorOf(focus)):
            self.setFocus()
        editor.hide()
        editor.deleteLater()
        self._emit_active()
        self.viewport().update()

    def eventFilter(self, obj, event) -> bool:
        # La scroll area filtra già i suoi eventi anche durante __init__: `_editor` può non esserci ancora.
        if event.type() == QEvent.Type.KeyPress and getattr(self, "_editing_box", None) is not None:
            # Casella di testo: Invio va a capo; Esc, Ctrl+Invio e Tab confermano.
            key, mods = event.key(), event.modifiers()
            ctrl_enter = key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and mods & Qt.KeyboardModifier.ControlModifier
            if key in (Qt.Key.Key_Escape, Qt.Key.Key_Tab, Qt.Key.Key_Backtab) or ctrl_enter:
                self._close_editor(commit=True)
                self.setFocus()
                return True
            return False
        if event.type() == QEvent.Type.KeyPress and getattr(self, "_editor", None) is not None:
            key, mods = event.key(), event.modifiers()
            if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                back = key == Qt.Key.Key_Backtab or bool(mods & Qt.KeyboardModifier.ShiftModifier)
                self._move_focus(not back)
                return True
            if key == Qt.Key.Key_Escape:
                if isinstance(self._editor, QComboBox) and self._editor.view().isVisible():
                    return False  # chiude solo il menu
                self._close_editor(commit=False)
                self.setFocus()
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                # Nel multiriga Invio va a capo, Ctrl+Invio conferma.
                if isinstance(self._editor, QPlainTextEdit) and not mods & Qt.KeyboardModifier.ControlModifier:
                    return False
                self._close_editor(commit=True)
                self.setFocus()
                return True
        return super().eventFilter(obj, event)

    # --- Caselle di testo ---------------------------------------------------------------

    def _connect_annotations(self) -> None:
        self._annotations.changed.connect(self._on_annotations_changed)

    def _on_annotations_changed(self) -> None:
        # Annulla/ripeti ha tolto la casella aperta nell'editor.
        box = self._editing_box
        if box is not None and not self._new_box and not self._committing and box not in self._annotations.items:
            self._close_editor(commit=False)
        self._on_selection()
        self._emit_active(force=True)
        self.viewport().update()

    def _on_selection(self) -> None:
        """Una sola cosa selezionata: tra firma e casella vince quella selezionata per ultima."""
        placement, item = self._placements.selected, self._annotations.selected
        if placement is not None and item is not None:
            if item is self._shown[1]:
                self._annotations.select(None)
            else:
                self._placements.select(None)
            return
        self._shown = (placement, item)
        self._emit_active()

    def _emit_active(self, force: bool = False) -> None:
        box = self.active_text_box
        if force or box is not self._active_box:
            self._active_box = box
            self.text_box_changed.emit(box)

    def _side_handles(self, rect: QRectF) -> list[QRectF]:
        """Maniglie a metà dei lati sinistro e destro: l'altezza segue il testo."""
        y = round(rect.center().y() - HANDLE / 2)
        return [QRectF(round(x - HANDLE / 2), y, HANDLE, HANDLE) for x in (rect.left(), rect.right())]

    def _hit_box(self, pos: QPointF) -> tuple[TextBox | None, int | None]:
        """Casella sotto il punto e, se è la selezionata, il lato della maniglia (0 sinistra, 1 destra)."""
        visible = self._visible_range()
        selected = self._annotations.selected
        if isinstance(selected, TextBox) and selected.page in visible:
            rect = self.page_rect_to_viewport(selected.page, to_qrect(selected.rect))
            for side, handle in enumerate(self._side_handles(rect)):
                if handle.adjusted(-2, -2, 2, 2).contains(pos):
                    return selected, side
        for item in reversed(self._annotations.items):
            if (
                isinstance(item, TextBox) and item.page in visible and item is not self._editing_box
                and self.page_rect_to_viewport(item.page, to_qrect(item.rect)).contains(pos)
            ):
                return item, None
        return None, None

    def _resized_box(self, start: QRectF, side: int, pt: QPointF, page_size: tuple[float, float]) -> QRectF:
        """Nuova larghezza dalla maniglia di un lato, tenendo fermo l'altro (l'altezza la decide il testo)."""
        if side == 1:
            right = max(start.left() + MIN_BOX_WIDTH, min(pt.x(), page_size[0]))
            return QRectF(start.left(), start.top(), right - start.left(), start.height())
        left = min(start.right() - MIN_BOX_WIDTH, max(pt.x(), 0.0))
        return QRectF(left, start.top(), start.right() - left, start.height())

    def _paint_annotations(self, painter: QPainter) -> None:
        visible = self._visible_range()
        dpr = self.devicePixelRatioF()
        scale = self._scale() * dpr
        for item in self._annotations.items:
            if isinstance(item, TextBox) and item.page in visible and item is not self._editing_box:
                target = self.page_rect_to_viewport(item.page, to_qrect(item.rect))
                image = self._annotations.image(item, scale)
                image.setDevicePixelRatio(dpr)
                painter.drawImage(target.topLeft(), image)

        if self._editing_box is not None and self._editor is not None:
            rect, handles = QRectF(self._editor.geometry()), False
        else:
            box = self.active_text_box
            if box is None or box.page not in visible:
                return
            rect, handles = self.page_rect_to_viewport(box.page, to_qrect(box.rect)), True
        pen = QPen(SELECTION, 1, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)
        if handles:
            painter.setPen(QPen(SELECTION, 1))
            painter.setBrush(Qt.GlobalColor.white)
            for handle in self._side_handles(rect):
                painter.drawRect(handle)

    def _text_click(self, pos: QPointF) -> None:
        """Clic in modalità Testo/Data: casella nuova (o, con Testo, quella già lì si riapre)."""
        page = self.page_at(pos)
        if page is None:
            return
        date = self._text_tool
        existing, _ = self._hit_box(pos)
        self.cancel_text_tool()
        if existing is not None and not date:
            self._annotations.select(existing)
            self._open_box_editor(existing, new=False)
            return
        style = self.text_style
        page_w, _ = self._sizes[page]
        # La data è larga quanto il suo testo (fitted_rect la allarga fino alla parola più lunga).
        width = min(MIN_BOX_WIDTH if date else NEW_BOX_WIDTH, page_w)
        pt = self.to_page_point(page, pos)
        box = TextBox(page, (0, 0, 0, 0), text=date, **style)
        # Il clic cade a metà della prima riga.
        rect = QRectF(pt.x(), pt.y() - box.size * 0.6, width, 0)
        box.rect = to_tuple(fitted_rect(box, rect, self._sizes[page]))
        self._placements.select(None)
        if date:
            self._annotations.add(box)  # già confermata
        else:
            self._annotations.select(None)
            self._open_box_editor(box, new=True)
        self._update_cursor()

    def _open_box_editor(self, box: TextBox, new: bool) -> None:
        self._close_editor(commit=True)
        self._box_drag = None
        editor = QPlainTextEdit(box.text, self.viewport())
        editor.setFrameShape(QFrame.Shape.NoFrame)
        editor.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        editor.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        editor.document().setDocumentMargin(0)
        editor.moveCursor(editor.textCursor().MoveOperation.End)
        editor.textChanged.connect(self._place_editor)  # l'altezza segue il testo
        editor.installEventFilter(self)
        self._editor, self._editing_box, self._new_box = editor, box, new
        self._style_box_editor()
        self._place_editor()
        editor.show()
        editor.setFocus()
        self._emit_active()
        self.viewport().update()

    def _style_box_editor(self) -> None:
        editor, box = self._editor, self._editing_box
        # Sfondo trasparente: si vede la pagina sotto, come nel PDF salvato.
        editor.setStyleSheet(f"QPlainTextEdit {{ background: transparent; color: {box.color}; }}")
        option = editor.document().defaultTextOption()
        option.setAlignment(ALIGNMENTS[box.align if 0 <= box.align < 3 else 0])
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        editor.document().setDefaultTextOption(option)

    def _place_box_editor(self) -> None:
        editor, box = self._editor, self._editing_box
        family, hint = BOX_FONTS.get(box.font, BOX_FONTS["Helv"])
        font = QFont(family)
        font.setStyleHint(hint)
        font.setPixelSize(max(1, round(box.size * self._scale())))
        if editor.font() != font:
            editor.setFont(font)
        rect = fitted_rect(box, to_qrect(box.rect), text=editor.toPlainText())
        view_rect = self.page_rect_to_viewport(box.page, rect)
        # Qt può andare a capo in un punto diverso da MuPDF: l'editor non taglia mai una riga.
        lines = editor.document().documentLayout().documentSize().height()
        view_rect.setHeight(max(view_rect.height(), lines * editor.fontMetrics().lineSpacing()))
        editor.setGeometry(view_rect.toAlignedRect())
        self.viewport().update()

    def _commit_box(self) -> None:
        """Conferma il testo: una casella vuota sparisce (o non nasce)."""
        box, text = self._editing_box, self._editor.toPlainText()
        empty = not text.strip()
        self._committing = True
        try:
            if self._new_box:
                if not empty:
                    box.text = text
                    box.rect = to_tuple(fitted_rect(box, to_qrect(box.rect), self._sizes[box.page]))
                    self._annotations.add(box)
            elif empty:
                self._annotations.remove(box)
            elif text != box.text:
                rect = fitted_rect(box, to_qrect(box.rect), self._sizes[box.page], text=text)
                self._annotations.update(box, "modifica testo", text=text, rect=to_tuple(rect))
        finally:
            self._committing = False


def _moved(start: QPointF, pos: QPointF) -> bool:
    return (pos - start).manhattanLength() > QApplication.startDragDistance()


def _page_link(item: dict, sizes: list[tuple[float, float]]) -> PageLink:
    x0, y0, x1, y1 = item["rect"]
    rect = QRectF(QPointF(x0, y0), QPointF(x1, y1)).normalized()
    if item["kind"] != "goto":
        return PageLink(rect, item["kind"], uri=item["uri"])
    page, y = item["page"], item["to"][1]
    if y is not None and 0 <= page < len(sizes):
        y = max(0.0, min(y, sizes[page][1]))
    return PageLink(rect, "goto", page, y)
