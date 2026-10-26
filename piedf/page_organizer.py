"""Finestra "Organizza pagine" (#26): miniature, rotazione, eliminazione, spostamento,
inserimento da altri file, pagine vuote ed estrazione.

La finestra lavora solo su una lista di `PageRef` (con annulla/ripeti); il PDF lo costruisce
`page_ops` in un processo a parte (`PageJob`), così la GUI resta reattiva anche su file grandi.
Le miniature arrivano da un processo dedicato (`render_worker.thumbnails`), solo per le celle
visibili.
"""

import multiprocessing
import os
import queue
import time
from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QMimeData, QModelIndex, QObject, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QDrag, QImage, QKeySequence, QPainter, QPen, QPixmap, QTransform, QUndoCommand, QUndoStack
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QStyle,
    QStyledItemDelegate,
    QToolBar,
    QVBoxLayout,
)

from piedf import page_ops, render_worker
from piedf.document import OpenError, PasswordRequired, PdfDocument, open_document
from piedf.page_ops import BLANK, MAIN, PageRef

THUMB = 140  # lato massimo della miniatura, in pixel
CELL = QSize(THUMB + 30, THUMB + 36)
MIME = "application/x-piedf-pages"
POLL_MS = 30
AHEAD = 12  # miniature da preparare oltre quelle visibili
QUIT_TIMEOUT = 3.0  # secondi concessi al processo delle miniature per uscire da solo
INSERTED = QColor("#f9a825")  # cornice delle pagine aggiunte nella finestra
PAGE_BORDER = QColor("#9e9e9e")
PDF_FILTER = "Documenti PDF (*.pdf)"


def open_with_password(parent, path: str) -> PdfDocument | None:
    """Apre `path` chiedendo la password se serve; None se l'utente annulla o il file non va."""
    name = Path(path).name
    password = None
    while True:
        try:
            return open_document(path, password)
        except PasswordRequired as e:
            prompt = "Password errata, riprova:" if e.wrong else f"«{name}» è protetto da password:"
            password, ok = QInputDialog.getText(parent, "Documento protetto", prompt, QLineEdit.EchoMode.Password)
            if not ok:
                return None
        except OpenError as e:
            QMessageBox.critical(
                parent, "Impossibile aprire il file", f"PieDF non riesce ad aprire «{name}».\n\nDettagli: {e}"
            )
            return None


class PageJob(QObject):
    """Costruzione di un PDF con `page_ops` in un processo a parte."""

    progress = Signal(int, int)  # passi fatti, totale
    finished = Signal(object, str)  # PdfDocument del file scritto (None se fallito o annullato), errore

    def __init__(self, sources, refs, dest: str, password: str | None, parent=None) -> None:
        super().__init__(parent)
        self.dest = dest
        ctx = multiprocessing.get_context("spawn")
        self._results = ctx.Queue()
        self._process = ctx.Process(
            target=page_ops.run, args=(list(sources), list(refs), dest, password, self._results), daemon=True
        )
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._done = False

    def start(self) -> None:
        self._process.start()
        self._timer.start()

    def cancel(self) -> None:
        """Ferma il processo; il file di destinazione non viene toccato (si scrive accanto e poi si sostituisce)."""
        if self._done:
            return
        self._process.terminate()
        self._process.join(5)
        try:
            os.remove(self.dest + page_ops.TMP_SUFFIX)
        except OSError:
            pass
        self._finish(None, "")

    def _poll(self) -> None:
        while not self._done:
            try:
                msg = self._results.get_nowait()
            except queue.Empty:
                if not self._process.is_alive():
                    self._finish(None, "Il processo si è interrotto durante la modifica delle pagine.")
                return
            if msg[0] == "progress":
                self.progress.emit(msg[1], msg[2])
            elif msg[0] == "done":
                self._finish(msg[1], "")
            else:
                self._finish(None, msg[1])

    def _finish(self, doc, error: str) -> None:
        self._done = True
        self._timer.stop()
        self.finished.emit(doc, error)


def run_with_progress(parent, job: PageJob, text: str, on_done) -> None:
    """Avvia `job` con un dialog di avanzamento modale; `on_done(doc, errore)` alla fine."""
    progress = QProgressDialog(text, "Annulla", 0, 0, parent)
    progress.setWindowTitle("Pagine")
    progress.setWindowModality(Qt.WindowModality.WindowModal)
    progress.setAutoClose(False)
    progress.setAutoReset(False)
    progress.setMinimumDuration(0)  # subito modale: niente clic sul documento mentre cambia
    progress.canceled.connect(job.cancel)

    def on_progress(done: int, total: int) -> None:
        progress.setMaximum(total)
        progress.setValue(done)

    def on_finished(doc, error: str) -> None:
        progress.canceled.disconnect(job.cancel)
        progress.close()
        progress.deleteLater()
        job.deleteLater()
        on_done(doc, error)

    job.progress.connect(on_progress)
    job.finished.connect(on_finished)
    job.start()


class PageModel(QAbstractListModel):
    """Le pagine nell'ordine attuale e le loro miniature."""

    def __init__(self, refs: list[PageRef], parent=None) -> None:
        super().__init__(parent)
        self.refs = list(refs)
        self.thumbs: dict[tuple[int, int], QImage] = {}  # (sorgente, pagina) → immagine non ruotata
        self._rotated: dict[tuple[int, int, int], QPixmap] = {}
        self.names: dict[int, str] = {}  # sorgente → nome del file, per il tooltip

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.refs)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return str(index.row() + 1)
        if role == Qt.ItemDataRole.ToolTipRole:
            return self.tooltip(self.refs[index.row()])
        return None

    def flags(self, index: QModelIndex):
        flags = super().flags(index)
        return flags | Qt.ItemFlag.ItemIsDragEnabled if index.isValid() else flags

    def tooltip(self, ref: PageRef) -> str:
        if ref.source == BLANK:
            return "Pagina vuota"
        if ref.source == MAIN:
            return f"Pagina {ref.page + 1} del documento"
        return f"Pagina {ref.page + 1} di «{self.names.get(ref.source, '')}»"

    def pixmap(self, ref: PageRef) -> QPixmap | None:
        """Miniatura già ruotata; None se non è ancora arrivata (o è una pagina vuota)."""
        key = (ref.source, ref.page)
        image = self.thumbs.get(key)
        if image is None:
            return None
        rkey = (ref.source, ref.page, ref.rotate % 360)
        pix = self._rotated.get(rkey)
        if pix is None:
            if ref.rotate % 360:
                image = image.transformed(QTransform().rotate(ref.rotate))
            pix = QPixmap.fromImage(image)
            self._rotated[rkey] = pix
        return pix

    def set_thumb(self, source: int, page: int, image: QImage) -> None:
        self.thumbs[(source, page)] = image
        for rot in (0, 90, 180, 270):
            self._rotated.pop((source, page, rot), None)
        for row, ref in enumerate(self.refs):
            if ref.source == source and ref.page == page:
                index = self.index(row)
                self.dataChanged.emit(index, index)

    def drop_thumb(self, source: int, page: int) -> None:
        """La miniatura è vecchia: si richiede (la cella resta bianca finché arriva)."""
        self.thumbs.pop((source, page), None)
        for rot in (0, 90, 180, 270):
            self._rotated.pop((source, page, rot), None)

    def clear_thumbs(self) -> None:
        self.thumbs.clear()
        self._rotated.clear()

    def set_refs(self, refs: list[PageRef]) -> None:
        self.beginResetModel()
        self.refs = list(refs)
        self.endResetModel()


class PageDelegate(QStyledItemDelegate):
    """Cella di una pagina: miniatura (lato massimo `thumb` px) e numero sotto. La usa anche il
    pannello laterale (#6)."""

    def __init__(self, model: PageModel, on_missing, parent=None, thumb: int = THUMB) -> None:
        super().__init__(parent)
        self._model = model
        self._on_missing = on_missing
        self._thumb = thumb
        self._cell = QSize(thumb + 30, thumb + 36)

    def sizeHint(self, option, index) -> QSize:
        return self._cell

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        ref = self._model.refs[index.row()]
        rect = option.rect
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if option.state & QStyle.StateFlag.State_Selected:
            color = QColor(option.palette.highlight().color())
            color.setAlpha(70)
            painter.fillRect(rect.adjusted(2, 2, -2, -2), color)
        thumb = self._thumb
        w, h = ref.shown_size
        k = thumb / max(w, h, 1)
        tw, th = max(1, round(w * k)), max(1, round(h * k))
        area = QRect(rect.x() + (rect.width() - thumb) // 2, rect.y() + 8, thumb, thumb)
        page = QRect(area.x() + (thumb - tw) // 2, area.y() + (thumb - th) // 2, tw, th)
        painter.fillRect(page, Qt.GlobalColor.white)
        pix = self._model.pixmap(ref) if ref.source != BLANK else None
        if pix is not None:
            painter.drawPixmap(page, pix)
        elif ref.source != BLANK:
            self._on_missing()
        if ref.source != MAIN:
            painter.setPen(QPen(INSERTED, 3))
            painter.drawRect(page.adjusted(-2, -2, 1, 1))
        else:
            painter.setPen(QPen(PAGE_BORDER, 1))
            painter.drawRect(page.adjusted(0, 0, -1, -1))
        painter.setPen(option.palette.text().color())
        label = QRect(rect.x(), area.bottom() + 4, rect.width(), rect.bottom() - area.bottom() - 4)
        painter.drawText(label, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, str(index.row() + 1))
        painter.restore()


class PageGrid(QListView):
    """Griglia delle miniature con trascinamento e indicatore di dove finiranno le pagine."""

    moved = Signal(int)  # le pagine selezionate vanno prima della riga indicata (len = in fondo)
    visible_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setMovement(QListView.Movement.Static)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        self.setSpacing(6)
        self.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(False)
        self.setDragDropMode(QListView.DragDropMode.DragDrop)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setStyleSheet("QListView { background: #e8e8e8; }")  # le pagine bianche si staccano dallo sfondo
        self.drop_row: int | None = None
        self.verticalScrollBar().valueChanged.connect(self.visible_changed)

    def visible_rows(self) -> list[int]:
        area = self.viewport().rect()
        model = self.model()
        return [r for r in range(model.rowCount()) if self.visualRect(model.index(r, 0)).intersects(area)]

    def drop_position(self, pos: QPoint) -> int:
        """Riga prima della quale finiscono le pagine lasciate in `pos` (rowCount = in fondo)."""
        model = self.model()
        count = model.rowCount()
        line = []
        last_bottom = None
        for row in range(count):
            r = self.visualRect(model.index(row, 0))
            last_bottom = r.bottom() if last_bottom is None else max(last_bottom, r.bottom())
            if r.top() <= pos.y() <= r.bottom():
                line.append((row, r))
        if not line:
            return count if last_bottom is not None and pos.y() > last_bottom else 0
        for row, r in line:
            if pos.x() < r.center().x():
                return row
        return line[-1][0] + 1

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.visible_changed.emit()

    def startDrag(self, actions) -> None:
        rows = sorted(i.row() for i in self.selectedIndexes())
        if not rows:
            return
        mime = QMimeData()
        mime.setData(MIME, ",".join(map(str, rows)).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        first = self.visualRect(self.model().index(rows[0], 0))
        drag.setPixmap(self.viewport().grab(first))
        drag.setHotSpot(QPoint(first.width() // 2, first.height() // 2))
        drag.exec(Qt.DropAction.MoveAction)
        self._set_drop_row(None)

    def _accepts(self, event) -> bool:
        return event.source() is self and event.mimeData().hasFormat(MIME)

    def dragEnterEvent(self, event) -> None:
        if self._accepts(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if not self._accepts(event):
            event.ignore()
            return
        self._set_drop_row(self.drop_position(event.position().toPoint()))
        event.acceptProposedAction()
        # Vicino ai bordi si scorre, per portare le pagine lontano.
        margin = self.autoScrollMargin()
        y = event.position().y()
        bar = self.verticalScrollBar()
        if y < margin:
            bar.setValue(bar.value() - CELL.height() // 4)
        elif y > self.viewport().height() - margin:
            bar.setValue(bar.value() + CELL.height() // 4)

    def dragLeaveEvent(self, event) -> None:
        self._set_drop_row(None)

    def dropEvent(self, event) -> None:
        if not self._accepts(event):
            event.ignore()
            return
        row = self.drop_position(event.position().toPoint())
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        self._set_drop_row(None)
        self.moved.emit(row)

    def _set_drop_row(self, row: int | None) -> None:
        if row != self.drop_row:
            self.drop_row = row
            self.viewport().update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.drop_row is None or not self.model().rowCount():
            return
        model = self.model()
        count = model.rowCount()
        if self.drop_row < count:
            r = self.visualRect(model.index(self.drop_row, 0))
            x = r.left() - self.spacing()
        else:
            r = self.visualRect(model.index(count - 1, 0))
            x = r.right() + self.spacing()
        painter = QPainter(self.viewport())
        painter.fillRect(QRect(x - 2, r.top() + 4, 4, r.height() - 8), self.palette().highlight())


class _SetRefs(QUndoCommand):
    def __init__(self, organizer: "PageOrganizer", old, new, old_sel, new_sel, text: str) -> None:
        super().__init__(text)
        self._organizer = organizer
        self._old, self._new = list(old), list(new)
        self._old_sel, self._new_sel = list(old_sel), list(new_sel)

    def redo(self) -> None:
        self._organizer._apply(self._new, self._new_sel)

    def undo(self) -> None:
        self._organizer._apply(self._old, self._old_sel)


class PageOrganizer(QDialog):
    """La finestra sul documento aperto `path`. `shown_path` è il file che l'utente conosce
    (l'originale, se `path` è un temporaneo di modifiche precedenti): titolo e nomi proposti.
    `sources` sono i file da cui vengono le pagine, (percorso, password); il primo è `path`."""

    def __init__(self, path: str, password: str | None, page_sizes, shown_path: str, parent=None) -> None:
        super().__init__(parent)
        self._shown = Path(shown_path)
        self.setWindowTitle(f"Organizza pagine – {self._shown.name}")
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.sources: list[tuple[str, str | None]] = [(path, password)]
        self._original = [PageRef(MAIN, i, tuple(size)) for i, size in enumerate(page_sizes)]
        self.model = PageModel(self._original, self)
        self.undo_stack = QUndoStack(self)
        self.undo_stack.indexChanged.connect(self._update_enabled)

        self.grid = PageGrid()
        self.grid.setModel(self.model)
        self.grid.setItemDelegate(PageDelegate(self.model, self._schedule_thumbs, self.grid))
        self.grid.selectionModel().selectionChanged.connect(self._update_enabled)
        self.grid.moved.connect(self.move_selected)
        self.grid.visible_changed.connect(self._schedule_thumbs)
        self.grid.customContextMenuRequested.connect(self._context_menu)

        # Processo delle miniature: solo le celle visibili, a bassa risoluzione.
        ctx = multiprocessing.get_context("spawn")
        self._thumb_requests = ctx.Queue()
        self._thumb_results = ctx.Queue()
        self._thumb_process = ctx.Process(
            target=render_worker.thumbnails, args=(self._thumb_requests, self._thumb_results), daemon=True
        )
        self._thumb_process.start()
        self._thumb_requests.put(("source", MAIN, path, password))
        self._last_want = None
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.setInterval(20)
        self._thumb_timer.timeout.connect(self._request_thumbs)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_MS)
        self._poll_timer.timeout.connect(self._read_thumbs)
        self._poll_timer.start()
        self._job: PageJob | None = None

        self._build_actions()
        bar = QToolBar()
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        for action in (self.act_undo, self.act_redo, None, self.act_rotate_left, self.act_rotate_right,
                       self.act_delete, None, self.act_insert_file, self.act_insert_blank, None, self.act_extract):
            if action is None:
                bar.addSeparator()
            else:
                bar.addAction(action)

        self.info = QLabel()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel)
        self.apply_button = buttons.button(QDialogButtonBox.StandardButton.Apply)
        self.apply_button.setText("Applica")
        self.apply_button.setDefault(True)
        self.apply_button.clicked.connect(self.accept)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Annulla")
        buttons.rejected.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addWidget(self.info)
        bottom.addStretch()
        bottom.addWidget(buttons)

        layout = QVBoxLayout(self)
        layout.addWidget(bar)
        layout.addWidget(self.grid)
        layout.addLayout(bottom)
        if parent is not None:
            self.resize(max(800, parent.width() - 60), max(600, parent.height() - 60))
        else:
            self.resize(1100, 800)
        self._update_enabled()

    # --- Azioni -----------------------------------------------------------------------

    def _action(self, text: str, slot, shortcuts=()) -> QAction:
        action = QAction(text, self)
        action.setShortcuts([QKeySequence(s) for s in shortcuts])
        action.triggered.connect(slot)
        self.addAction(action)
        return action

    def _build_actions(self) -> None:
        self.act_undo = self.undo_stack.createUndoAction(self, "Annulla")
        self.act_undo.setShortcuts([QKeySequence("Ctrl+Z")])
        self.act_redo = self.undo_stack.createRedoAction(self, "Ripeti")
        self.act_redo.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])
        self.addAction(self.act_undo)
        self.addAction(self.act_redo)
        self.act_rotate_left = self._action("Ruota a sinistra", lambda: self.rotate_selected(-90), ["Ctrl+["])
        self.act_rotate_right = self._action("Ruota a destra", lambda: self.rotate_selected(90), ["Ctrl+]"])
        self.act_delete = self._action("Elimina", self.delete_selected, [QKeySequence.StandardKey.Delete])
        self.act_insert_file = self._action("Inserisci da file…", self.choose_insert_file)
        self.act_insert_blank = self._action("Inserisci pagina vuota", self.insert_blank)
        self.act_extract = self._action("Estrai in un nuovo PDF…", self.choose_extract)

    def _context_menu(self, pos: QPoint) -> None:
        index = self.grid.indexAt(pos)
        if index.isValid() and not self.grid.selectionModel().isSelected(index):
            self.select_rows([index.row()])
        menu = QMenu(self)
        for action in (self.act_rotate_left, self.act_rotate_right, self.act_delete, None,
                       self.act_insert_file, self.act_insert_blank, None, self.act_extract):
            if action is None:
                menu.addSeparator()
            else:
                menu.addAction(action)
        menu.exec(self.grid.viewport().mapToGlobal(pos))

    def _update_enabled(self) -> None:
        rows = self.selected_rows()
        count = len(self.model.refs)
        has = bool(rows)
        self.act_rotate_left.setEnabled(has)
        self.act_rotate_right.setEnabled(has)
        self.act_delete.setEnabled(has and len(rows) < count)  # almeno una pagina deve restare
        self.act_extract.setEnabled(has)
        selected = f" · {len(rows)} selezionate" if len(rows) > 1 else (" · 1 selezionata" if rows else "")
        self.info.setText(f"{count} pagine{selected}")

    @property
    def refs(self) -> list[PageRef]:
        return list(self.model.refs)

    @property
    def changed(self) -> bool:
        """Il risultato è diverso dal documento aperto."""
        refs = self.model.refs
        return len(refs) != len(self._original) or any(
            r.source != MAIN or r.page != i or r.rotate % 360 for i, r in enumerate(refs)
        )

    def selected_rows(self) -> list[int]:
        return sorted(i.row() for i in self.grid.selectionModel().selectedIndexes())

    def select_rows(self, rows: list[int]) -> None:
        sel = self.grid.selectionModel()
        sel.clearSelection()
        Flag = sel.SelectionFlag
        for row in rows:
            sel.select(self.model.index(row), Flag.Select)
        if rows:
            sel.setCurrentIndex(self.model.index(rows[0]), Flag.NoUpdate)
            self.grid.scrollTo(self.model.index(rows[0]))

    # --- Modifiche (ognuna è un passo di annulla) --------------------------------------

    def _push(self, new: list[PageRef], new_sel: list[int], text: str) -> None:
        self.undo_stack.push(_SetRefs(self, self.model.refs, new, self.selected_rows(), new_sel, text))

    def _apply(self, refs: list[PageRef], selection: list[int]) -> None:
        self.model.set_refs(refs)
        self.select_rows(selection)
        self._update_enabled()
        self._schedule_thumbs()

    def rotate_selected(self, delta: int) -> None:
        rows = self.selected_rows()
        if not rows:
            return
        new = list(self.model.refs)
        for row in rows:
            r = new[row]
            new[row] = PageRef(r.source, r.page, r.size, (r.rotate + delta) % 360)
        self._push(new, rows, "rotazione")

    def delete_selected(self) -> None:
        rows = self.selected_rows()
        if not rows or len(rows) >= len(self.model.refs):
            return
        gone = set(rows)
        new = [r for i, r in enumerate(self.model.refs) if i not in gone]
        self._push(new, [min(rows[0], len(new) - 1)], "eliminazione")

    def move_selected(self, target: int) -> None:
        """Sposta le pagine selezionate prima della riga `target` (contata prima dello spostamento)."""
        rows = self.selected_rows()
        if not rows:
            return
        gone = set(rows)
        moving = [self.model.refs[i] for i in rows]
        rest = [r for i, r in enumerate(self.model.refs) if i not in gone]
        at = target - sum(1 for i in rows if i < target)
        new = rest[:at] + moving + rest[at:]
        if new == self.model.refs:
            return
        self._push(new, list(range(at, at + len(moving))), "spostamento")

    def _insert_at(self) -> int:
        rows = self.selected_rows()
        return rows[-1] + 1 if rows else len(self.model.refs)

    def insert_refs(self, refs: list[PageRef], at: int | None = None, text: str = "inserimento") -> None:
        at = self._insert_at() if at is None else at
        new = self.model.refs[:at] + refs + self.model.refs[at:]
        self._push(new, list(range(at, at + len(refs))), text)

    def insert_blank(self) -> None:
        rows = self.selected_rows()
        like = self.model.refs[rows[-1] if rows else -1]
        self.insert_refs([PageRef(BLANK, -1, like.shown_size)], text="pagina vuota")

    def add_document(self, path: str, doc: PdfDocument, at: int | None = None) -> None:
        """Inserisce tutte le pagine di un file già aperto (password nota) dopo la selezione o in `at`."""
        sid = len(self.sources)
        self.sources.append((path, doc.password))
        self.model.names[sid] = Path(path).name
        self._thumb_requests.put(("source", sid, path, doc.password))
        self.insert_refs([PageRef(sid, i, tuple(size)) for i, size in enumerate(doc.page_sizes)], at)

    def choose_insert_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Inserisci pagine da", str(self._shown.parent), PDF_FILTER)
        if not path:
            return
        doc = open_with_password(self, path)
        if doc is not None:
            self.add_document(os.path.abspath(path), doc)

    # --- Estrazione -------------------------------------------------------------------

    def choose_extract(self) -> None:
        rows = self.selected_rows()
        if not rows or self._job is not None:
            return
        proposed = str(self._shown.with_name(f"{self._shown.stem}_estratto.pdf"))
        dest, _ = QFileDialog.getSaveFileName(self, "Estrai in un nuovo PDF", proposed, PDF_FILTER)
        if not dest:
            return
        if not Path(dest).suffix:
            dest += ".pdf"
        self.extract(rows, os.path.abspath(dest))

    def extract(self, rows: list[int], dest: str) -> None:
        """Scrive le pagine `rows` (come sono adesso, ruotate comprese) in `dest`; la finestra resta aperta."""
        key = os.path.normcase(dest)
        if any(os.path.normcase(os.path.abspath(p)) == key for p, _ in self.sources):
            QMessageBox.warning(self, "Estrai", "Scegli un file diverso da quelli usati in questa finestra.")
            return
        refs = [self.model.refs[r] for r in rows]
        self._job = PageJob(self.sources, refs, dest, self.sources[MAIN][1], self)

        def done(doc, error: str) -> None:
            self._job = None
            if error:
                QMessageBox.critical(self, "Estrazione non riuscita", error)
            elif doc is not None:
                n = len(refs)
                self.info.setText(f"{'Estratta 1 pagina' if n == 1 else f'Estratte {n} pagine'} in {Path(dest).name}")

        run_with_progress(self, self._job, "Estrazione delle pagine…", done)

    # --- Miniature --------------------------------------------------------------------

    def _schedule_thumbs(self) -> None:
        if not self._thumb_timer.isActive():
            self._thumb_timer.start()

    def _request_thumbs(self) -> None:
        rows = self.grid.visible_rows()
        if rows:
            rows += range(rows[-1] + 1, min(rows[-1] + 1 + AHEAD, len(self.model.refs)))
        want = []
        seen = set()
        for row in rows:
            ref = self.model.refs[row]
            key = (ref.source, ref.page)
            if ref.source == BLANK or key in self.model.thumbs or key in seen:
                continue
            seen.add(key)
            w, h = ref.size
            want.append((ref.source, ref.page, round(THUMB * self.devicePixelRatioF() / max(w, h, 1), 4)))
        if want != self._last_want:
            self._last_want = want
            self._thumb_requests.put(("want", want))

    def _read_thumbs(self) -> None:
        for _ in range(20):  # al massimo per giro: la GUI non deve fermarsi
            try:
                msg = self._thumb_results.get_nowait()
            except queue.Empty:
                return
            if msg[0] == "thumb":
                _, sid, page, width, height, stride, samples = msg
                image = QImage(samples, width, height, stride, QImage.Format.Format_RGB888).copy()
                self.model.set_thumb(sid, page, image)
                if self._last_want:
                    self._last_want = [w for w in self._last_want if (w[0], w[1]) != (sid, page)]

    def done(self, result: int) -> None:
        if self._job is not None:
            self._job.cancel()
        self._poll_timer.stop()
        self._thumb_timer.stop()
        self._stop_thumbs()
        super().done(result)

    def _stop_thumbs(self) -> None:
        """Chiude il processo delle miniature: messaggio di uscita, attesa, `terminate()` solo come ultima risorsa."""
        process = self._thumb_process
        self._thumb_requests.put(("quit",))
        deadline = time.monotonic() + QUIT_TIMEOUT
        while process.is_alive() and time.monotonic() < deadline:
            # Si svuotano i risultati: un processo che esce aspetta che le miniature
            # già spedite lascino la pipe (su Windows il buffer è piccolo) e non uscirebbe.
            # Si legge solo finché è vivo: un messaggio iniziato lo finisce di scrivere.
            try:
                while True:
                    self._thumb_results.get_nowait()
            except (queue.Empty, OSError, ValueError, EOFError):
                pass
            process.join(0.05)
        if process.is_alive():  # dopo terminate() non si legge più: un messaggio a metà bloccherebbe
            process.terminate()
            process.join(2)
        for q in (self._thumb_requests, self._thumb_results):
            q.close()
            q.cancel_join_thread()
