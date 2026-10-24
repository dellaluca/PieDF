import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QSpinBox,
    QToolBar,
)

from piedf import paths
from piedf.document import OpenError, PasswordRequired, open_document
from piedf.renderer import Renderer
from piedf.signature_dialog import SignatureDialog
from piedf.signature_manager import SignatureManager
from piedf.signatures import Kind, SignatureStore
from piedf.viewer import FitMode, PdfView

MAX_RECENT = 10
FIT_LABELS = {FitMode.WIDTH: "Adatta larghezza", FitMode.PAGE: "Adatta pagina"}
ZOOM_PRESETS = [50, 75, 100, 125, 150, 200, 300, 400]


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.resize(1000, 800)
        self.setAcceptDrops(True)

        self._settings = paths.settings()
        self.signatures = SignatureStore()
        self._path: str | None = None
        self._gen = 0

        self._renderer = Renderer(self)
        self._renderer.failed.connect(self._on_render_failed)
        self.view = PdfView(self._renderer)
        self.view.page_changed.connect(self._on_page_changed)
        self.view.zoom_changed.connect(self._on_zoom_changed)
        self.setCentralWidget(self.view)

        self._build_actions()
        self._build_menu()
        self._build_toolbar()
        self._update_title()
        self._update_enabled()

    # --- Costruzione UI ---------------------------------------------------------------

    def _action(self, text: str, slot, shortcuts=()) -> QAction:
        action = QAction(text, self)
        action.setShortcuts([QKeySequence(s) for s in shortcuts])
        action.triggered.connect(slot)
        self.addAction(action)  # le scorciatoie funzionano anche a toolbar nascosta
        return action

    def _build_actions(self) -> None:
        self.act_open = self._action("&Apri…", self._choose_file, [QKeySequence.StandardKey.Open])
        self.act_close = self._action("&Chiudi", self.close_document, ["Ctrl+W"])
        # StandardKey.Quit non ha una scorciatoia su Windows.
        self.act_quit = self._action("&Esci", self.close, ["Ctrl+Q"])

        self.act_zoom_in = self._action("Zoom &avanti", self.view.zoom_in, ["Ctrl++", "Ctrl+="])
        self.act_zoom_out = self._action("Zoom &indietro", self.view.zoom_out, ["Ctrl+-"])
        self.act_actual = self._action("&Dimensione reale", lambda: self.view.set_zoom(1.0), ["Ctrl+1"])
        self.act_fit_page = self._action(
            "Adatta &pagina", lambda: self.view.set_fit_mode(FitMode.PAGE), ["Ctrl+0"]
        )
        self.act_fit_width = self._action(
            "Adatta &larghezza", lambda: self.view.set_fit_mode(FitMode.WIDTH), ["Ctrl+2"]
        )
        self.act_prev = self._action(
            "Pagina &precedente", lambda: self.view.go_to_page(self.view.current_page - 1), ["Ctrl+PgUp"]
        )
        self.act_next = self._action(
            "Pagina &successiva", lambda: self.view.go_to_page(self.view.current_page + 1), ["Ctrl+PgDown"]
        )
        self.act_goto = self._action("&Vai a pagina…", self._focus_page_box, ["Ctrl+G"])

        self._doc_actions = [
            self.act_close, self.act_zoom_in, self.act_zoom_out, self.act_actual,
            self.act_fit_page, self.act_fit_width, self.act_prev, self.act_next, self.act_goto,
        ]

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(self.act_open)
        self._recent_menu = file_menu.addMenu("File &recenti")
        self._recent_menu.aboutToShow.connect(self._fill_recent_menu)
        file_menu.addSeparator()
        file_menu.addAction(self.act_close)
        file_menu.addSeparator()
        file_menu.addAction(self.act_quit)

        view_menu = self.menuBar().addMenu("&Visualizza")
        for action in (self.act_zoom_in, self.act_zoom_out, self.act_actual):
            view_menu.addAction(action)
        view_menu.addSeparator()
        view_menu.addAction(self.act_fit_page)
        view_menu.addAction(self.act_fit_width)
        view_menu.addSeparator()
        for action in (self.act_prev, self.act_next, self.act_goto):
            view_menu.addAction(action)

        sign_menu = self.menuBar().addMenu("F&irma")
        sign_menu.addAction("Crea &firma…", lambda: self.create_signature(Kind.SIGNATURE))
        sign_menu.addAction("Crea &iniziali…", lambda: self.create_signature(Kind.INITIALS))
        sign_menu.addSeparator()
        sign_menu.addAction("&Gestisci firme…", self.manage_signatures)

    def _build_toolbar(self) -> None:
        bar = QToolBar("Strumenti")
        bar.setMovable(False)
        bar.setFloatable(False)
        # Finché non ci sono icone (#8), i simboli ◀ ▶ − + vanno ingranditi per essere leggibili.
        bar.setStyleSheet("QToolButton { font-size: 15px; padding: 2px 8px; }")
        self.addToolBar(bar)

        bar.addAction(self.act_open)
        bar.addSeparator()

        self.act_prev.setIconText("◀")
        self.act_next.setIconText("▶")
        bar.addAction(self.act_prev)
        self._page_box = QSpinBox()
        self._page_box.setKeyboardTracking(False)
        self._page_box.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._page_box.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self._page_box.setMinimumWidth(56)
        self._page_box.valueChanged.connect(lambda v: self.view.go_to_page(v - 1))
        bar.addWidget(self._page_box)
        self._page_total = QLabel(" / 0 ")
        bar.addWidget(self._page_total)
        bar.addAction(self.act_next)
        bar.addSeparator()

        self.act_zoom_out.setIconText("−")
        self.act_zoom_in.setIconText("+")
        bar.addAction(self.act_zoom_out)
        self._zoom_box = QComboBox()
        self._zoom_box.setEditable(True)
        self._zoom_box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._zoom_box.setMinimumWidth(140)
        self._zoom_box.addItems(list(FIT_LABELS.values()) + [f"{z}%" for z in ZOOM_PRESETS])
        self._zoom_box.activated.connect(lambda _: self._apply_zoom_text(self._zoom_box.currentText()))
        self._zoom_box.lineEdit().returnPressed.connect(
            lambda: self._apply_zoom_text(self._zoom_box.lineEdit().text())
        )
        bar.addWidget(self._zoom_box)
        bar.addAction(self.act_zoom_in)

        self._doc_widgets = [self._page_box, self._zoom_box]

    def _update_enabled(self) -> None:
        has_doc = self._path is not None
        for item in self._doc_actions + self._doc_widgets:
            item.setEnabled(has_doc)

    def _update_title(self) -> None:
        if self._path:
            self.setWindowTitle(f"{Path(self._path).name} – PieDF")
        else:
            self.setWindowTitle("PieDF")

    # --- Apertura file ----------------------------------------------------------------

    def _choose_file(self) -> None:
        start = str(Path(self._path).parent) if self._path else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Apri PDF", start, "Documenti PDF (*.pdf);;Tutti i file (*)"
        )
        if path:
            self.open_file(path)

    def open_file(self, path: str) -> bool:
        path = os.path.abspath(path)
        name = Path(path).name
        password = None
        while True:
            try:
                doc = open_document(path, password)
                break
            except PasswordRequired as e:
                prompt = "Password errata, riprova:" if e.wrong else f"«{name}» è protetto da password:"
                password, ok = QInputDialog.getText(
                    self, "Documento protetto", prompt, QLineEdit.EchoMode.Password
                )
                if not ok:
                    return False
            except OpenError as e:
                QMessageBox.critical(
                    self,
                    "Impossibile aprire il file",
                    f"PieDF non riesce ad aprire «{name}».\n\n"
                    "Il file potrebbe non essere un PDF oppure essere troppo danneggiato.\n\n"
                    f"Dettagli: {e}",
                )
                self._remove_recent(path)
                return False

        self._gen += 1
        self._path = path
        self._renderer.open(self._gen, path, doc.password)
        self.view.set_document(self._gen, doc.page_sizes)
        self._page_box.setRange(1, len(doc.page_sizes))
        self._page_total.setText(f" / {len(doc.page_sizes)} ")
        self._update_title()
        self._update_enabled()
        self._add_recent(path)
        self.view.setFocus()
        if doc.repaired:
            self.statusBar().showMessage("Il file era danneggiato ed è stato riparato per la lettura.", 10000)
        else:
            self.statusBar().clearMessage()
        return True

    def close_document(self) -> None:
        self._path = None
        self._gen += 1
        self._renderer.close(self._gen)
        self.view.clear()
        self._page_box.setRange(0, 0)
        self._page_total.setText(" / 0 ")
        self._update_title()
        self._update_enabled()

    def _on_render_failed(self, gen: int, message: str) -> None:
        if gen == self._gen:
            self.statusBar().showMessage(f"Errore di visualizzazione: {message}", 10000)

    # --- Drag & drop ------------------------------------------------------------------

    def _dropped_file(self, event) -> str | None:
        for url in event.mimeData().urls():
            if url.isLocalFile():
                return url.toLocalFile()
        return None

    def dragEnterEvent(self, event) -> None:
        if self._dropped_file(event):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        path = self._dropped_file(event)
        if path:
            event.acceptProposedAction()
            self.open_file(path)

    # --- Pagine e zoom ----------------------------------------------------------------

    def _on_page_changed(self, index: int) -> None:
        self._page_box.blockSignals(True)
        self._page_box.setValue(index + 1)
        self._page_box.blockSignals(False)

    def _focus_page_box(self) -> None:
        self._page_box.setFocus()
        self._page_box.selectAll()

    def _on_zoom_changed(self, zoom: float, fit: FitMode) -> None:
        text = FIT_LABELS.get(fit, f"{round(zoom * 100)}%")
        self._zoom_box.blockSignals(True)
        self._zoom_box.setEditText(text)
        self._zoom_box.blockSignals(False)

    def _apply_zoom_text(self, text: str) -> None:
        for mode, label in FIT_LABELS.items():
            if text == label:
                self.view.set_fit_mode(mode)
                return
        try:
            percent = float(text.strip().rstrip("%").replace(",", "."))
        except ValueError:
            self._on_zoom_changed(self.view.zoom, self.view.fit_mode)
            return
        self.view.set_zoom(percent / 100)
        self.view.setFocus()

    # --- Firme ------------------------------------------------------------------------

    def create_signature(self, kind: Kind) -> None:
        dialog = SignatureDialog(self.signatures, kind, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            saved = "Firma salvata." if dialog.saved.kind is Kind.SIGNATURE else "Iniziali salvate."
            self.statusBar().showMessage(saved, 5000)

    def manage_signatures(self) -> None:
        SignatureManager(self.signatures, self).exec()

    # --- File recenti -----------------------------------------------------------------

    def _recent(self) -> list[str]:
        return self._settings.value("recent_files", [], type=list)

    def _add_recent(self, path: str) -> None:
        key = os.path.normcase(path)
        files = [p for p in self._recent() if os.path.normcase(p) != key]
        self._settings.setValue("recent_files", [path] + files[: MAX_RECENT - 1])

    def _remove_recent(self, path: str) -> None:
        key = os.path.normcase(path)
        self._settings.setValue("recent_files", [p for p in self._recent() if os.path.normcase(p) != key])

    def _fill_recent_menu(self) -> None:
        menu = self._recent_menu
        menu.clear()
        files = self._recent()
        for i, path in enumerate(files, 1):
            action = menu.addAction(f"&{i % 10}  {Path(path).name}")
            action.setToolTip(path)
            action.setStatusTip(path)
            action.triggered.connect(lambda _=False, p=path: self.open_file(p))
        if not files:
            menu.addAction("Nessun file recente").setEnabled(False)
        else:
            menu.addSeparator()
            menu.addAction("Svuota elenco", lambda: self._settings.setValue("recent_files", []))

    # --- Chiusura ---------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self._renderer.shutdown()
        super().closeEvent(event)
