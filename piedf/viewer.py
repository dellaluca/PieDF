import bisect
import math
from collections import OrderedDict
from enum import Enum

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import QAbstractScrollArea

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
# Angoli in senso orario da in alto a sinistra: maniglie e cursori corrispondenti.
CORNER_CURSORS = [
    Qt.CursorShape.SizeFDiagCursor, Qt.CursorShape.SizeBDiagCursor,
    Qt.CursorShape.SizeFDiagCursor, Qt.CursorShape.SizeBDiagCursor,
]


class FitMode(Enum):
    NONE = 0
    WIDTH = 1
    PAGE = 2


class PdfView(QAbstractScrollArea):
    """Vista a scorrimento continuo che chiede al renderer solo le pagine visibili."""

    page_changed = Signal(int)  # indice da 0
    zoom_changed = Signal(float, FitMode)
    placing_changed = Signal(bool)  # entrata/uscita dalla modalità posizionamento

    def __init__(self, renderer: Renderer, parent=None) -> None:
        super().__init__(parent)
        self._renderer = renderer
        renderer.page_ready.connect(self._on_page_ready)

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

        self._placements = Placements(self)
        self._placements.changed.connect(self.viewport().update)
        self._placing: Signature | None = None
        self._cursor_pos: QPointF | None = None  # ultima posizione del mouse nella vista
        # Trascinamento in corso: (firma, rettangolo iniziale, punto iniziale in punti, angolo o None).
        self._drag: tuple[Placement, QRectF, QPointF, int | None] | None = None
        self.viewport().setMouseTracking(True)

    # --- API pubblica -----------------------------------------------------------------

    @property
    def page_count(self) -> int:
        return len(self._sizes)

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

    def set_placements(self, placements: Placements) -> None:
        self._placements.changed.disconnect(self.viewport().update)
        self._placements = placements
        placements.changed.connect(self.viewport().update)

    def start_placing(self, signature: Signature) -> None:
        """Modalità posizionamento: l'anteprima segue il cursore, un clic la piazza."""
        if not self._sizes:
            return
        self._drag = None
        self._placing = signature
        self._placements.image(signature.path)  # in cache subito, anche se il PNG sparisce
        self._placements.select(None)
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

    def _clear_cache(self) -> None:
        self._cache.clear()
        self._cache_bytes = 0
        self._last_request = None

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
        self._paint_placements(painter)
        painter.end()

        self._request_visible()

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        self.viewport().update()
        self._update_current_page()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._sizes and self._fit != FitMode.NONE:
            page, y_pt = self.scroll_position()
            self._apply_fit()
            self.set_scroll_position(page, y_pt)
            self.zoom_changed.emit(self._zoom, self._fit)
        else:
            self._update_scrollbars()

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
        if event.key() == Qt.Key.Key_Escape and self._placing is not None:
            self.cancel_placing()
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
        self.cancel_placing()

    def _placing_rect(self) -> tuple[int, QRectF] | None:
        """Pagina e rettangolo (punti) dove finirebbe la firma in posizionamento."""
        if self._placing is None or self._cursor_pos is None:
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
        if self._placing is not None:
            viewport.setCursor(Qt.CursorShape.CrossCursor)
        elif self._drag is not None:
            corner = self._drag[3]
            viewport.setCursor(Qt.CursorShape.ClosedHandCursor if corner is None else CORNER_CURSORS[corner])
        else:
            placement, corner = self._hit(self._cursor_pos) if self._cursor_pos is not None else (None, None)
            if corner is not None:
                viewport.setCursor(CORNER_CURSORS[corner])
            elif placement is not None:
                viewport.setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                viewport.unsetCursor()

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
        if event.button() != Qt.MouseButton.LeftButton or not self._sizes:
            super().mousePressEvent(event)
            return
        placement, corner = self._hit(pos)
        self._placements.select(placement)
        if placement is not None:
            start = self.to_page_point(placement.page, pos)
            self._drag = (placement, QRectF(placement.rect), start, corner)
        self._update_cursor()
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        self._cursor_pos = pos
        if self._drag is not None:
            placement, start_rect, start, corner = self._drag
            pt = self.to_page_point(placement.page, pos)
            size = self._sizes[placement.page]
            if corner is None:
                rect = clamp_rect(start_rect.translated(pt - start), size)
            else:
                rect = self._resized_rect(start_rect, corner, pt, size)
            self._placements.preview_rect(placement, rect)
        elif self._placing is not None:
            self.viewport().update()
        else:
            self._update_cursor()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self._cursor_pos = event.position()
        if self._drag is not None and event.button() == Qt.MouseButton.LeftButton:
            placement, start_rect, _, corner = self._drag
            self._drag = None
            text = "spostamento firma" if corner is None else "ridimensionamento firma"
            # Tutto il trascinamento diventa un solo passo di annulla.
            self._placements.set_rect(placement, start_rect, QRectF(placement.rect), text)
            self._update_cursor()
        event.accept()

    def viewportEvent(self, event) -> bool:
        # Il mouse esce dalla vista: l'anteprima del posizionamento sparisce.
        if event.type() == QEvent.Type.Leave and self._drag is None:
            self._cursor_pos = None
            if self._placing is not None:
                self.viewport().update()
        return super().viewportEvent(event)
