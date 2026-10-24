import os
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSpinBox,
    QToolBar,
    QToolButton,
    QWidgetAction,
)

from piedf import paths
from piedf.document import OpenError, PasswordRequired, open_document
from piedf.placements import Placements
from piedf.positions import Positions
from piedf.renderer import Renderer
from piedf.signature_dialog import SignatureDialog
from piedf.signature_manager import SignatureManager, thumbnail_pixmap
from piedf.signatures import Kind, Signature, SignatureStore
from piedf.viewer import FitMode, PdfView
from piedf.writer import SaveError, has_digital_signatures, save_with_signatures

MAX_RECENT = 10
POSITION_SAVE_MS = 30_000
FIT_NAMES = {FitMode.WIDTH: "width", FitMode.PAGE: "page", FitMode.NONE: None}
FIT_LABELS = {FitMode.WIDTH: "Adatta larghezza", FitMode.PAGE: "Adatta pagina"}
ZOOM_PRESETS = [50, 75, 100, 125, 150, 200, 300, 400]
MENU_THUMB = QSize(160, 52)
PLACING_HINT = "Clic per posizionare la firma · Esc per annullare"
MAX_HISTORY = 50
# Gli unici link esterni che si seguono: niente file:, launch, javascript: o altri PDF.
SAFE_SCHEMES = ("http", "https", "mailto")


class ThumbnailItem(QLabel):
    """Voce del menu Firma: solo la miniatura (un'icona di QMenu sarebbe troppo piccola)."""

    clicked = Signal()

    def __init__(self, signature: Signature) -> None:
        super().__init__()
        self.setPixmap(thumbnail_pixmap(signature.image(), MENU_THUMB))
        self.setToolTip(signature.kind.label)
        self.setMargin(3)
        self.setStyleSheet("QLabel:hover { background: palette(highlight); }")

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.resize(1000, 800)
        self.setAcceptDrops(True)

        self._settings = paths.settings()
        self._positions = Positions(paths.data_dir() / "posizioni.json")
        self.signatures = SignatureStore()
        self.placements = Placements(self)
        self.placements.changed.connect(self._update_title)
        self.placements.undo_stack.cleanChanged.connect(self._update_enabled)
        self._path: str | None = None
        self._password: str | None = None
        self._save_path: str | None = None  # dove scrive "Salva"; None finché non si è salvato
        self._gen = 0
        # Cronologia dei link interni: posizioni (pagina, y in punti) come scroll_position.
        self._back: list[tuple[int, float]] = []
        self._forward: list[tuple[int, float]] = []

        self._renderer = Renderer(self)
        self._renderer.failed.connect(self._on_render_failed)
        self.view = PdfView(self._renderer)
        self.view.page_changed.connect(self._on_page_changed)
        self.view.zoom_changed.connect(self._on_zoom_changed)
        self.view.placing_changed.connect(self._on_placing_changed)
        self.view.link_goto.connect(self._follow_link)
        self.view.link_uri.connect(self._open_uri)
        self.view.set_placements(self.placements)
        self.setCentralWidget(self.view)

        # Se il programma si blocca, la posizione di lettura si perde al massimo per questo intervallo.
        self._position_timer = QTimer(self)
        self._position_timer.timeout.connect(lambda: self._save_position(force=False))
        self._position_timer.start(POSITION_SAVE_MS)

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
        self.act_save = self._action("&Salva", self.save, [QKeySequence.StandardKey.Save])
        self.act_save_as = self._action("Salva &con nome…", self.save_as, ["Ctrl+Shift+S"])
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
        self.act_back = self._action("I&ndietro", self.go_back, ["Alt+Left"])
        self.act_forward = self._action("A&vanti", self.go_forward, ["Alt+Right"])

        # Il testo segue l'ultimo comando: "Annulla spostamento firma".
        stack = self.placements.undo_stack
        self.act_undo = stack.createUndoAction(self, "Annulla")
        self.act_undo.setShortcuts([QKeySequence("Ctrl+Z")])
        self.act_redo = stack.createRedoAction(self, "Ripeti")
        self.act_redo.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])
        self.addAction(self.act_undo)
        self.addAction(self.act_redo)

        self._doc_actions = [
            self.act_save_as, self.act_close, self.act_zoom_in, self.act_zoom_out, self.act_actual,
            self.act_fit_page, self.act_fit_width, self.act_prev, self.act_next, self.act_goto,
        ]

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(self.act_open)
        self._recent_menu = file_menu.addMenu("File &recenti")
        self._recent_menu.aboutToShow.connect(self._fill_recent_menu)
        file_menu.addSeparator()
        file_menu.addAction(self.act_save)
        file_menu.addAction(self.act_save_as)
        file_menu.addSeparator()
        file_menu.addAction(self.act_close)
        file_menu.addSeparator()
        file_menu.addAction(self.act_quit)

        edit_menu = self.menuBar().addMenu("&Modifica")
        edit_menu.addAction(self.act_undo)
        edit_menu.addAction(self.act_redo)

        view_menu = self.menuBar().addMenu("&Visualizza")
        for action in (self.act_zoom_in, self.act_zoom_out, self.act_actual):
            view_menu.addAction(action)
        view_menu.addSeparator()
        view_menu.addAction(self.act_fit_page)
        view_menu.addAction(self.act_fit_width)
        view_menu.addSeparator()
        for action in (self.act_prev, self.act_next, self.act_goto):
            view_menu.addAction(action)
        view_menu.addSeparator()
        view_menu.addAction(self.act_back)
        view_menu.addAction(self.act_forward)

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
        bar.addAction(self.act_save)
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
        bar.addSeparator()

        self._sign_menu = QMenu(self)
        self._sign_menu.aboutToShow.connect(self._fill_sign_menu)
        self.sign_button = QToolButton()
        self.sign_button.setText("Firma ▾")
        self.sign_button.setToolTip("Metti una firma sul documento")
        self.sign_button.setMenu(self._sign_menu)
        self.sign_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        # La freccia è già nel testo.
        self.sign_button.setStyleSheet("QToolButton::menu-indicator { image: none; }")
        bar.addWidget(self.sign_button)

        self._doc_widgets = [self._page_box, self._zoom_box, self.sign_button]

    def _update_enabled(self) -> None:
        has_doc = self._path is not None
        for item in self._doc_actions + self._doc_widgets:
            item.setEnabled(has_doc)
        self.act_save.setEnabled(has_doc and self.placements.modified)
        self.act_back.setEnabled(has_doc and bool(self._back))
        self.act_forward.setEnabled(has_doc and bool(self._forward))

    def _update_title(self) -> None:
        if self._path:
            star = "* " if self.placements.modified else ""
            self.setWindowTitle(f"{star}{Path(self._path).name} – PieDF")
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

    def open_file(self, path: str, password: str | None = None) -> bool:
        path = os.path.abspath(path)
        name = Path(path).name
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

        if not self._confirm_discard():
            return False
        self._save_position()
        self.placements.clear()
        self._clear_history()
        self._gen += 1
        self._path = path
        self._password = doc.password
        self._save_path = None
        self._renderer.open(self._gen, path, doc.password)
        self.view.set_document(self._gen, doc.page_sizes)
        self._page_box.setRange(1, len(doc.page_sizes))
        self._page_total.setText(f" / {len(doc.page_sizes)} ")
        self._update_title()
        self._update_enabled()
        self._add_recent(path)
        self._restore_position(path, len(doc.page_sizes))
        self.view.setFocus()
        if doc.repaired:
            self.statusBar().showMessage("Il file era danneggiato ed è stato riparato per la lettura.", 10000)
        else:
            self.statusBar().clearMessage()
        return True

    def close_document(self) -> bool:
        if not self._confirm_discard():
            return False
        self._save_position()
        self.placements.clear()
        self._clear_history()
        self._path = None
        self._password = None
        self._save_path = None
        self._gen += 1
        self._renderer.close(self._gen)
        self.view.clear()
        self._page_box.setRange(0, 0)
        self._page_total.setText(" / 0 ")
        self._update_title()
        self._update_enabled()
        return True

    def _confirm_discard(self) -> bool:
        """Chiede se salvare o scartare le firme non salvate; False se l'utente annulla."""
        if self._path is None or not self.placements.modified:
            return True
        Button = QMessageBox.StandardButton
        box = QMessageBox(
            QMessageBox.Icon.Warning, "Firme non salvate",
            f"Le firme aggiunte a «{Path(self._path).name}» non sono state salvate.",
            Button.Save | Button.Discard | Button.Cancel, self,
        )
        box.button(Button.Save).setText("Salva")
        box.button(Button.Discard).setText("Scarta")
        box.button(Button.Cancel).setText("Annulla")
        box.setDefaultButton(Button.Save)
        answer = box.exec()
        if answer == Button.Save:
            return self.save()
        return answer == Button.Discard

    # --- Posizione di lettura ---------------------------------------------------------

    def _save_position(self, force: bool = True) -> None:
        if self._path is None or not self.view.page_count:
            return
        page, y = self.view.scroll_position()
        fit, zoom = FIT_NAMES[self.view.fit_mode], self.view.zoom
        if force or not self._positions.same(self._path, page, y, fit, zoom):
            self._positions.set(self._path, page, y, fit, zoom)

    def _restore_position(self, path: str, page_count: int) -> None:
        entry = self._positions.get(path)
        if entry is None:
            return
        page, y = entry["pagina"], entry["y"]
        if page >= page_count:
            page, y = page_count - 1, 0.0
        self.view.go_to_page(page)  # Adatta pagina misura la pagina corrente
        if entry["fit"] == "width":
            self.view.set_fit_mode(FitMode.WIDTH)
        elif entry["fit"] == "page":
            self.view.set_fit_mode(FitMode.PAGE)
        else:
            self.view.set_zoom(entry["zoom"])
        self.view.set_scroll_position(page, y)

    # --- Link e cronologia -----------------------------------------------------------

    def _follow_link(self, page: int, y: float) -> None:
        self._back = (self._back + [self.view.scroll_position()])[-MAX_HISTORY:]
        self._forward = []
        self.view.set_scroll_position(page, y)
        self._update_enabled()

    def go_back(self) -> None:
        self._step_history(self._back, self._forward)

    def go_forward(self) -> None:
        self._step_history(self._forward, self._back)

    def _step_history(self, source: list, dest: list) -> None:
        if not source or self._path is None:
            return
        dest.append(self.view.scroll_position())
        del dest[:-MAX_HISTORY]
        self.view.set_scroll_position(*source.pop())
        self._update_enabled()

    def _clear_history(self) -> None:
        self._back.clear()
        self._forward.clear()

    def _open_uri(self, uri: str) -> None:
        url = QUrl(uri, QUrl.ParsingMode.StrictMode)
        scheme = url.scheme().lower()
        ok = url.isValid() and scheme in SAFE_SCHEMES and bool(url.path() if scheme == "mailto" else url.host())
        if not ok:
            self.statusBar().showMessage("Link non supportato per sicurezza", 10000)
            return
        # L'indirizzo per intero e codificato (niente caratteri simili Unicode): è quello che si apre
        # davvero, che può essere diverso dal testo visibile nel PDF.
        address = bytes(url.toEncoded()).decode("ascii")
        Button = QMessageBox.StandardButton
        box = QMessageBox(
            QMessageBox.Icon.Question, "Link esterno",
            f"Aprire questo indirizzo nel browser?\n\n{address}",
            Button.Open | Button.Cancel, self,
        )
        box.button(Button.Open).setText("Apri")
        box.button(Button.Cancel).setText("Annulla")
        box.setDefaultButton(Button.Cancel)
        if box.exec() == Button.Open:
            QDesktopServices.openUrl(url)

    def mousePressEvent(self, event) -> None:
        # Tasti laterali del mouse, anche sopra la vista (che non li usa e li lascia passare).
        if event.button() == Qt.MouseButton.BackButton:
            self.go_back()
        elif event.button() == Qt.MouseButton.ForwardButton:
            self.go_forward()
        else:
            super().mousePressEvent(event)

    # --- Salvataggio ------------------------------------------------------------------

    def save(self) -> bool:
        """Il primo Salva di un documento chiede il nome, per non sovrascrivere l'originale per sbaglio."""
        if self._path is None:
            return False
        if self._save_path is None:
            return self.save_as()
        return self._save_to(self._save_path)

    def save_as(self) -> bool:
        if self._path is None:
            return False
        if self._save_path is not None:
            proposed = self._save_path
        else:
            src = Path(self._path)
            proposed = str(src.with_name(f"{src.stem}_firmato.pdf"))
        # Il dialog chiede già la conferma se il file esiste.
        dest, _ = QFileDialog.getSaveFileName(
            self, "Salva PDF firmato", proposed, "Documenti PDF (*.pdf)"
        )
        if not dest:
            return False
        if not Path(dest).suffix:
            dest += ".pdf"
        return self._save_to(os.path.abspath(dest))

    def _save_to(self, dest: str) -> bool:
        if has_digital_signatures(self._path, self._password):
            Button = QMessageBox.StandardButton
            box = QMessageBox(
                QMessageBox.Icon.Warning, "Firme digitali",
                "Questo documento contiene firme digitali. Aggiungere la firma le renderà "
                "non più valide in Acrobat. Continuare?",
                Button.Ok | Button.Cancel, self,
            )
            box.button(Button.Ok).setText("Continua")
            box.button(Button.Cancel).setText("Annulla")
            box.setDefaultButton(Button.Cancel)
            if box.exec() != Button.Ok:
                return False

        self.view.cancel_placing()
        src, password = self._path, self._password
        overwrite = os.path.normcase(dest) == os.path.normcase(src)
        # Il processo di render tiene aperto il file: va rilasciato prima di sovrascriverlo.
        if overwrite and not self._renderer.close(self._gen, wait=True):
            self._renderer.open(self._gen, src, password)
            QMessageBox.critical(self, "Salvataggio non riuscito", "Il documento è ancora in uso, riprova.")
            return False
        try:
            save_with_signatures(src, password, self.placements.items, dest, self.placements.missing_pngs())
        except SaveError as e:
            if overwrite:
                self._renderer.open(self._gen, src, password)
                self.view.reload()
            QMessageBox.critical(self, "Salvataggio non riuscito", str(e))
            return False

        # Le firme ora fanno parte del PDF: non sono più modificabili né annullabili.
        page, zoom, fit = self.view.current_page, self.view.zoom, self.view.fit_mode
        scroll_x, scroll_y = self.view.horizontalScrollBar().value(), self.view.verticalScrollBar().value()
        self.placements.clear()
        if not self.open_file(dest, password):
            return False
        if fit == FitMode.NONE:
            self.view.set_zoom(zoom)
        else:
            self.view.set_fit_mode(fit)
        self.view.go_to_page(page)
        self.view.horizontalScrollBar().setValue(scroll_x)
        self.view.verticalScrollBar().setValue(scroll_y)
        self._save_path = dest
        self.statusBar().showMessage(f"Salvato in {Path(dest).name}", 10000)
        return True

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

    def create_signature(self, kind: Kind) -> Signature | None:
        dialog = SignatureDialog(self.signatures, kind, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            saved = "Firma salvata." if dialog.saved.kind is Kind.SIGNATURE else "Iniziali salvate."
            self.statusBar().showMessage(saved, 5000)
            return dialog.saved
        return None

    def manage_signatures(self) -> None:
        SignatureManager(self.signatures, self).exec()

    def place_signature(self, signature: Signature) -> None:
        self._sign_menu.close()
        self.view.start_placing(signature)

    def _create_and_place(self, kind: Kind) -> None:
        signature = self.create_signature(kind)
        if signature is not None and self._path is not None:
            self.place_signature(signature)

    def _fill_sign_menu(self) -> None:
        menu = self._sign_menu
        menu.clear()
        signatures = self.signatures.list(Kind.SIGNATURE)
        initials = self.signatures.list(Kind.INITIALS)
        for group in (signatures, initials):
            if group and not menu.isEmpty():
                menu.addSeparator()
            for signature in group:
                action = QWidgetAction(menu)
                item = ThumbnailItem(signature)
                item.clicked.connect(action.trigger)
                action.setDefaultWidget(item)
                action.setToolTip(signature.kind.label)
                action.triggered.connect(lambda _=False, s=signature: self.place_signature(s))
                menu.addAction(action)
        if not menu.isEmpty():
            menu.addSeparator()
        menu.addAction("Nuova firma…", lambda: self._create_and_place(Kind.SIGNATURE))
        menu.addAction("Nuove iniziali…", lambda: self._create_and_place(Kind.INITIALS))
        if signatures or initials:
            menu.addAction("Gestisci firme…", self.manage_signatures)

    def _on_placing_changed(self, placing: bool) -> None:
        if placing:
            self.statusBar().showMessage(PLACING_HINT)
        elif self.statusBar().currentMessage() == PLACING_HINT:
            self.statusBar().clearMessage()

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
        menu.addAction("Dimentica le posizioni", self._positions.clear)

    # --- Chiusura ---------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        if not self._confirm_discard():
            event.ignore()
            return
        self._save_position()
        self._renderer.shutdown()
        super().closeEvent(event)
