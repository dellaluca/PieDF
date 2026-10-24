import bisect
import math
from collections import OrderedDict
from enum import Enum

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QWheelEvent
from PySide6.QtWidgets import QAbstractScrollArea

from piedf.renderer import Renderer

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


class FitMode(Enum):
    NONE = 0
    WIDTH = 1
    PAGE = 2


class PdfView(QAbstractScrollArea):
    """Vista a scorrimento continuo che chiede al renderer solo le pagine visibili."""

    page_changed = Signal(int)  # indice da 0
    zoom_changed = Signal(float, FitMode)

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

    def set_document(self, gen: int, page_sizes: list[tuple[float, float]]) -> None:
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

    def clear(self) -> None:
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
        painter.end()

        self._request_visible()

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        self.viewport().update()
        self._update_current_page()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._sizes and self._fit != FitMode.NONE:
            self._apply_fit()
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
        if event.key() == Qt.Key.Key_Home:
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
