"""Pannello laterale (#6): miniature delle pagine e indice (segnalibri) del PDF.

Le miniature le disegna il processo di render della vista (`Renderer.request_thumbs`), che ha
già il documento aperto, con una priorità più bassa delle pagine della vista: si chiedono solo
quelle delle celle visibili (più qualcuna sotto). Modello e disegno delle celle sono quelli di
"Organizza pagine" (`PageModel`, `PageDelegate`), con la rotazione della vista come rotazione.
"""

from PySide6.QtCore import QModelIndex, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDockWidget,
    QLabel,
    QListView,
    QStackedWidget,
    QTabWidget,
    QTreeView,
)

from piedf.document import TocEntry
from piedf.page_ops import MAIN, PageRef
from piedf.page_organizer import PageDelegate, PageModel
from piedf.renderer import Renderer

THUMB = 160  # lato massimo della miniatura, px logici: un A4 verticale è largo ~113 px
AHEAD = 4  # miniature da preparare oltre quelle visibili
REQUEST_MS = 30  # le richieste si raccolgono: lo scorrimento veloce non ne manda una per evento
NO_TOC = "Questo documento non ha un indice"
PAGE_ROLE = Qt.ItemDataRole.UserRole + 1  # voce dell'indice: (pagina, y o None)
PAGES_TAB, TOC_TAB = 0, 1


class Sidebar(QDockWidget):
    """Pannello a sinistra con le schede Pagine e Indice."""

    page_clicked = Signal(int)  # indice da 0
    toc_clicked = Signal(int, object)  # pagina da 0, y in punti nella pagina come si vede (o None)

    def __init__(self, renderer: Renderer, parent=None) -> None:
        super().__init__("Pannello laterale", parent)
        self.setObjectName("sidebar")
        # Non flottante né chiudibile dalla sua barra: si mostra e nasconde solo con F4 (o il menu).
        self.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea)
        self.setTitleBarWidget(QLabel())  # niente barra del titolo: le schede bastano
        self._renderer = renderer
        renderer.thumb_ready.connect(self._on_thumb)
        renderer.field_applied.connect(self._on_field_applied)
        self._gen: int | None = None
        self._sizes: list[tuple[float, float]] = []
        self._rotation = 0
        self._last_want: list[tuple[int, float]] | None = None

        self.pages = QListView()
        self.pages.setUniformItemSizes(True)
        self.pages.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.pages.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.pages.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.pages.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.pages.setStyleSheet("QListView { background: #e8e8e8; }")  # le pagine bianche si staccano dallo sfondo
        self.model = PageModel([], self)
        self.pages.setModel(self.model)
        self.pages.setItemDelegate(PageDelegate(self.model, self._schedule, self.pages, thumb=THUMB))
        self.pages.clicked.connect(lambda index: self.page_clicked.emit(index.row()))
        self.pages.activated.connect(lambda index: self.page_clicked.emit(index.row()))
        self.pages.verticalScrollBar().valueChanged.connect(self._schedule)
        self.pages.setMinimumWidth(THUMB + 50)

        self.toc_model = QStandardItemModel(self)
        self.toc = QTreeView()
        self.toc.setModel(self.toc_model)
        self.toc.setHeaderHidden(True)
        self.toc.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.toc.setUniformRowHeights(True)
        self.toc.clicked.connect(self._on_toc_clicked)
        self.toc.activated.connect(self._on_toc_clicked)
        self.no_toc = QLabel(NO_TOC)
        self.no_toc.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.no_toc.setWordWrap(True)
        self.no_toc.setMargin(12)
        self.no_toc.setEnabled(False)  # testo grigio
        self.toc_stack = QStackedWidget()
        self.toc_stack.addWidget(self.no_toc)
        self.toc_stack.addWidget(self.toc)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self.pages, "Pagine")
        self.tabs.addTab(self.toc_stack, "Indice")
        self.tabs.currentChanged.connect(self._schedule)
        self.setWidget(self.tabs)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(REQUEST_MS)
        self._timer.timeout.connect(self._request_thumbs)

    # --- Documento --------------------------------------------------------------------

    def set_document(self, gen: int, page_sizes: list[tuple[float, float]], toc: list[TocEntry]) -> None:
        self._gen = gen
        self._sizes = list(page_sizes)
        self._rotation = 0
        self._last_want = None
        self.model.clear_thumbs()
        self.model.set_refs(self._refs())
        self._fill_toc(toc)
        self._schedule()

    def clear(self) -> None:
        self.set_document(-1, [], [])
        self._gen = None

    def set_current(self, page: int) -> None:
        """Evidenzia la miniatura della pagina corrente e la tiene in vista."""
        if not 0 <= page < self.model.rowCount():
            return
        index = self.model.index(page)
        if self.pages.currentIndex() != index:
            self.pages.setCurrentIndex(index)
        self.pages.scrollTo(index, QAbstractItemView.ScrollHint.EnsureVisible)

    def set_rotation(self, rotation: int) -> None:
        """Le miniature seguono la rotazione della vista."""
        if rotation == self._rotation:
            return
        self._rotation = rotation
        current = self.pages.currentIndex().row()
        self.model.set_refs(self._refs())
        self.set_current(current)

    def _refs(self) -> list[PageRef]:
        return [PageRef(MAIN, i, size, self._rotation) for i, size in enumerate(self._sizes)]

    # --- Indice -----------------------------------------------------------------------

    def _fill_toc(self, toc: list[TocEntry]) -> None:
        self.toc_model.clear()
        parents = [self.toc_model.invisibleRootItem()]
        for entry in toc:
            item = QStandardItem(entry.title)
            item.setToolTip(entry.title)
            item.setData((entry.page, entry.y), PAGE_ROLE)
            # Un livello che salta (1 → 3) si attacca all'ultimo che c'è.
            level = max(1, min(entry.level, len(parents)))
            del parents[level:]
            parents[-1].appendRow(item)
            parents.append(item)
        self.toc.expandToDepth(0)
        self.toc_stack.setCurrentWidget(self.toc if toc else self.no_toc)

    def _on_toc_clicked(self, index: QModelIndex) -> None:
        target = index.data(PAGE_ROLE)
        if target is not None and target[0] >= 0:
            self.toc_clicked.emit(target[0], target[1])

    # --- Miniature --------------------------------------------------------------------

    def _schedule(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def visible_rows(self) -> range:
        """Righe delle celle visibili (tutte alte uguali: bastano la prima e l'ultima)."""
        count = self.model.rowCount()
        if not count:
            return range(0)
        area = self.pages.viewport().rect()
        x = area.width() // 2
        first = self.pages.indexAt(QPoint(x, area.top() + 1)).row()
        last = self.pages.indexAt(QPoint(x, area.bottom() - 1)).row()
        first = max(first, 0)
        last = count - 1 if last < 0 else last
        return range(first, last + 1)

    def _request_thumbs(self) -> None:
        if self._gen is None or not self.isVisible() or self.tabs.currentIndex() != PAGES_TAB:
            return
        rows = self.visible_rows()
        if rows:
            rows = range(rows.start, min(rows.stop + AHEAD, self.model.rowCount()))
        dpr = self.devicePixelRatioF()
        want = []
        for row in rows:
            if (MAIN, row) not in self.model.thumbs:
                w, h = self._sizes[row]
                want.append((row, round(THUMB * dpr / max(w, h, 1), 4)))
        if want != self._last_want:
            self._last_want = want
            self._renderer.request_thumbs(self._gen, want)

    def _on_thumb(self, gen: int, page: int, image: QImage) -> None:
        if gen != self._gen or page >= len(self._sizes):
            return
        self.model.set_thumb(MAIN, page, image)
        if self._last_want:
            self._last_want = [w for w in self._last_want if w[0] != page]

    def _on_field_applied(self, gen: int, page: int) -> None:
        """Un campo cambiato nel documento del processo di render: la miniatura è vecchia."""
        if gen != self._gen or (MAIN, page) not in self.model.thumbs:
            return
        self.model.drop_thumb(MAIN, page)
        self.model.dataChanged.emit(self.model.index(page), self.model.index(page))
        self._last_want = None
        self._schedule()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._schedule()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._schedule()
