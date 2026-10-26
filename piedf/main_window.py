import os
import tempfile
from pathlib import Path

from PySide6.QtCore import QDate, QEvent, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QDesktopServices, QFont, QFontDatabase, QIcon, QKeySequence, QPixmap
from PySide6.QtPrintSupport import QAbstractPrintDialog, QPrintDialog, QPrinter, QPrinterInfo
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QSpinBox,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from piedf import fonts, paths
from piedf.annotations import (
    COLORS,
    DEFAULT_DRAW,
    DEFAULT_STYLE,
    ERASER,
    FONT_LABELS,
    HIGHLIGHTER,
    HIGHLIGHTER_COLORS,
    MAX_SIZE,
    MIN_SIZE,
    PEN,
    PEN_COLORS,
    PEN_WIDTHS,
    TOOL_LABELS,
    Annotations,
)
from piedf.document import OpenError, PasswordRequired, PdfDocument, open_document
from piedf.forms import FormValues
from piedf.page_ops import MAIN, PageRef
from piedf.page_organizer import PageJob, PageOrganizer, open_with_password, run_with_progress
from piedf.placements import Placements
from piedf.positions import Positions
from piedf.printing import NO_PRINT_MESSAGE, PrintJob, PrintOptions, PrintOptionsDialog, can_print, print_source, selected_pages
from piedf.renderer import Renderer
from piedf.search_bar import SearchBar
from piedf.sidebar import PAGES_TAB, TOC_TAB, Sidebar
from piedf.signature_dialog import SignatureDialog
from piedf.signature_manager import SignatureManager, thumbnail_pixmap
from piedf.signatures import Kind, Signature, SignatureStore
from piedf.viewer import BOX_FONTS, FitMode, PdfView
from piedf.writer import SaveError, has_digital_signatures, save_with_signatures

MAX_RECENT = 10
MAX_RECENT_FONTS = 5
MISSING_FONT_ROLE = Qt.ItemDataRole.UserRole + 1  # voce del font di una casella che qui non è installato
FONTS_RETRY_MS = 200  # l'elenco dei font si legge in un thread: finché non è pronto ci sono solo i tre standard
POSITION_SAVE_MS = 30_000
FIT_NAMES = {FitMode.WIDTH: "width", FitMode.PAGE: "page", FitMode.NONE: None}
FIT_LABELS = {FitMode.WIDTH: "Adatta larghezza", FitMode.PAGE: "Adatta pagina"}
ZOOM_PRESETS = [50, 75, 100, 125, 150, 200, 300, 400]
MENU_THUMB = QSize(160, 52)
PLACING_HINT = "Clic per posizionare la firma · Esc per annullare"
TEXT_HINT = "Clic sulla pagina per scrivere · Esc per annullare"
DATE_HINT = "Clic sulla pagina per inserire la data · Esc per annullare"
DRAW_HINT = "Trascina sulla pagina per disegnare · Esc per finire"
ERASER_HINT = "Clic o trascina su un tratto per cancellarlo · Esc per finire"
COPY_HINT = "Copia del testo…"
COPY_DIALOG_MS = 500  # una copia che dura di più mostra l'avanzamento, con Annulla
ALIGN_LABELS = ["Sinistra", "Centro", "Destra"]
FORM_MESSAGE = "Questo documento contiene campi compilabili."
NOT_INSTALLED = "{} (non installato)"
FONT_FALLBACK = "Il font «{}» non è installato e non ha tutti i caratteri del testo: la casella usa Helvetica."
XFA_MESSAGE = "Questo modulo usa una tecnologia (XFA) che PieDF non supporta: i campi non si possono compilare."
MAX_HISTORY = 50
SIDEBAR_WIDTH = 220  # px logici, larghezza del pannello laterale la prima volta
SAVE_FIRST = "Salva o scarta le modifiche prima di organizzare le pagine."
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
        # Stesso stack delle firme: annulla/ripeti e "modificato" valgono per tutto.
        self.forms = FormValues(self.placements.undo_stack, self)
        self.annotations = Annotations(self.placements.undo_stack, self)
        self._path: str | None = None
        self._password: str | None = None
        self._save_path: str | None = None  # dove scrive "Salva"; None finché non si è salvato
        # Dopo "Organizza pagine" il documento aperto è un temporaneo: `_original` è il file
        # da cui viene (nome nel titolo e proposto al salvataggio). Non None = pagine da salvare.
        self._original: str | None = None
        self._temps: list[str] = []  # temporanei delle pagine, da cancellare quando non servono più
        self._page_job: PageJob | None = None
        self._gen = 0
        # Cronologia dei link interni: posizioni (pagina, y in punti) come scroll_position.
        self._back: list[tuple[int, float]] = []
        self._forward: list[tuple[int, float]] = []
        self._print_job: PrintJob | None = None
        self._copy_done = (0, 0)  # pagine lette e da leggere dalla copia in corso
        # Copia del testo in corso (#49): avanzamento, mostrato dopo COPY_DIALOG_MS.
        self._copy_dialog: QProgressDialog | None = None
        self._copy_timer = QTimer(self)
        self._copy_timer.setSingleShot(True)
        self._copy_timer.setInterval(COPY_DIALOG_MS)
        self._copy_timer.timeout.connect(self._show_copy_dialog)

        self._renderer = Renderer(self)
        self._renderer.failed.connect(self._on_render_failed)
        self.view = PdfView(self._renderer)
        self.view.page_changed.connect(self._on_page_changed)
        self.view.zoom_changed.connect(self._on_zoom_changed)
        self.view.placing_changed.connect(self._on_placing_changed)
        self.view.link_goto.connect(self._follow_link)
        self.view.link_uri.connect(self._open_uri)
        self.view.set_placements(self.placements)
        self.view.set_forms(self.forms)
        self.view.set_annotations(self.annotations)
        self.view.text_tool_changed.connect(self._on_text_tool_changed)
        self.view.text_box_changed.connect(self._update_text_bar)
        self.view.text_style = self._text_style()
        self.view.draw_style = self._draw_style()
        self.view.draw_tool_changed.connect(self._on_draw_tool_changed)
        self.view.copy_busy.connect(self._on_copy_busy)
        self.view.copy_progress.connect(self._on_copy_progress)
        self.view.copy_failed.connect(self._on_copy_failed)
        self.view.font_fallback.connect(lambda family: self.statusBar().showMessage(FONT_FALLBACK.format(family), 10_000))
        self.view.set_highlight(self._settings.value("highlight_fields", True, type=bool))
        self.search_bar = SearchBar(self.view, self._renderer, self.annotations)
        self.search_bar.show_hit.connect(self._show_search_hit)
        self.search_bar.toggled.connect(self._place_text_bar)
        self._build_form_bar()
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.form_bar)
        layout.addWidget(self.view)
        self.setCentralWidget(central)

        self.sidebar = Sidebar(self._renderer, self)
        self.sidebar.page_clicked.connect(self._go_to_thumbnail)
        self.sidebar.toc_clicked.connect(lambda page, y: self._follow_link(page, self.view.view_y(page, y)))
        self.view.rotation_changed.connect(self._on_rotation_changed)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.sidebar)

        # Se il programma si blocca, la posizione di lettura si perde al massimo per questo intervallo.
        self._position_timer = QTimer(self)
        self._position_timer.timeout.connect(lambda: self._save_position(force=False))
        self._position_timer.start(POSITION_SAVE_MS)

        self._build_actions()
        self._build_menu()
        self._build_toolbar()
        self._build_text_bar()
        self._restore_sidebar()
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
        self.act_print = self._action("S&tampa…", self.print_document, [QKeySequence.StandardKey.Print])
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
        self.act_text = self._action("Aggiungi &testo", self.view.start_text_tool, ["Ctrl+T"])
        self.act_date = self._action(
            "Aggiungi &data", lambda: self.view.start_text_tool(QDate.currentDate().toString("dd/MM/yyyy"))
        )
        self.act_forward = self._action("A&vanti", self.go_forward, ["Alt+Right"])
        self.act_find = self._action("&Trova…", self.search_bar.open_bar, [QKeySequence.StandardKey.Find])
        self.act_find_next = self._action("Trova &successivo", self.search_bar.next, ["F3"])
        self.act_find_prev = self._action("Trova &precedente", self.search_bar.previous, ["Shift+F3"])
        # Disegna: accende l'ultimo strumento usato, o lo spegne (come Esc).
        self.act_draw = self._action("&Disegna", self._toggle_draw, ["Ctrl+D"])
        self.act_draw.setCheckable(True)
        self.draw_actions = {}
        self._draw_group = QActionGroup(self)
        self._draw_group.setExclusionPolicy(QActionGroup.ExclusionPolicy.ExclusiveOptional)
        for tool, label in TOOL_LABELS.items():
            action = QAction(label, self)
            action.setCheckable(True)
            action.triggered.connect(lambda checked, t=tool: self.set_draw_tool(t if checked else None))
            self._draw_group.addAction(action)
            self.draw_actions[tool] = action

        self.act_organize = self._action("&Organizza pagine…", lambda: self.organize_pages(), ["Ctrl+Shift+O"])
        self.act_merge = self._action("&Unisci PDF…", self.merge_files)
        self.act_rotate_left = self._action("Ruota pagina corrente a &sinistra", lambda: self.rotate_current(-90))
        self.act_rotate_right = self._action("Ruota pagina corrente a &destra", lambda: self.rotate_current(90))
        # Solo a schermo: il file non cambia (al contrario di "Ruota pagina corrente").
        self.act_view_right = self._action("&Ruota vista a destra", lambda: self.view.rotate_view(90), ["Ctrl+Shift++"])
        self.act_view_left = self._action("Ruota vista a s&inistra", lambda: self.view.rotate_view(-90), ["Ctrl+Shift+-"])
        self.act_sidebar = self._action("Pannello &laterale", self._set_sidebar, ["F4"])
        self.act_sidebar.setCheckable(True)

        # Il testo segue l'ultimo comando: "Annulla spostamento firma".
        stack = self.placements.undo_stack
        self.act_undo = stack.createUndoAction(self, "Annulla")
        self.act_undo.setShortcuts([QKeySequence("Ctrl+Z")])
        self.act_redo = stack.createRedoAction(self, "Ripeti")
        self.act_redo.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])
        self.addAction(self.act_undo)
        self.addAction(self.act_redo)
        self.act_copy = self._action("&Copia", self.copy_text, [QKeySequence.StandardKey.Copy])
        self.act_select_all = self._action("Seleziona &tutto", self.select_all_text, [QKeySequence.StandardKey.SelectAll])

        self._doc_actions = [
            self.act_save_as, self.act_print, self.act_close, self.act_zoom_in, self.act_zoom_out, self.act_actual,
            self.act_fit_page, self.act_fit_width, self.act_prev, self.act_next, self.act_goto,
            self.act_text, self.act_date, self.act_organize, self.act_rotate_left, self.act_rotate_right,
            self.act_find, self.act_find_next, self.act_find_prev,
            self.act_draw, *self.draw_actions.values(), self.act_copy, self.act_select_all,
            self.act_view_right, self.act_view_left,
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
        file_menu.addAction(self.act_print)
        file_menu.addSeparator()
        file_menu.addAction(self.act_close)
        file_menu.addSeparator()
        file_menu.addAction(self.act_quit)

        edit_menu = self.menuBar().addMenu("&Modifica")
        edit_menu.addAction(self.act_undo)
        edit_menu.addAction(self.act_redo)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_copy)
        edit_menu.addAction(self.act_select_all)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_find)
        edit_menu.addAction(self.act_find_next)
        edit_menu.addAction(self.act_find_prev)

        view_menu = self.menuBar().addMenu("&Visualizza")
        view_menu.addAction(self.act_sidebar)
        view_menu.addSeparator()
        for action in (self.act_zoom_in, self.act_zoom_out, self.act_actual):
            view_menu.addAction(action)
        view_menu.addSeparator()
        view_menu.addAction(self.act_fit_page)
        view_menu.addAction(self.act_fit_width)
        view_menu.addSeparator()
        view_menu.addAction(self.act_view_right)
        view_menu.addAction(self.act_view_left)
        view_menu.addSeparator()
        for action in (self.act_prev, self.act_next, self.act_goto):
            view_menu.addAction(action)
        view_menu.addSeparator()
        view_menu.addAction(self.act_back)
        view_menu.addAction(self.act_forward)

        pages_menu = self.menuBar().addMenu("&Pagine")
        pages_menu.addAction(self.act_organize)
        pages_menu.addAction(self.act_merge)
        pages_menu.addSeparator()
        pages_menu.addAction(self.act_rotate_left)
        pages_menu.addAction(self.act_rotate_right)

        sign_menu = self.menuBar().addMenu("F&irma")
        sign_menu.addAction("Crea &firma…", lambda: self.create_signature(Kind.SIGNATURE))
        sign_menu.addAction("Crea &iniziali…", lambda: self.create_signature(Kind.INITIALS))
        sign_menu.addSeparator()
        sign_menu.addAction("&Gestisci firme…", self.manage_signatures)
        sign_menu.addSeparator()
        sign_menu.addAction(self.act_text)
        sign_menu.addAction(self.act_date)
        sign_menu.addSeparator()
        self._build_draw_menu()
        sign_menu.addAction(self.act_draw)
        sign_menu.addMenu(self.draw_menu)

    def _build_toolbar(self) -> None:
        bar = QToolBar("Strumenti")
        bar.setMovable(False)
        bar.setFloatable(False)
        # Finché non ci sono icone (#8), i simboli ◀ ▶ − + vanno ingranditi per essere leggibili.
        bar.setStyleSheet("QToolButton { font-size: 15px; padding: 2px 8px; }")
        self.addToolBar(bar)

        bar.addAction(self.act_open)
        bar.addAction(self.act_save)
        bar.addAction(self.act_print)
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
        self.act_text.setIconText("T")
        self.act_text.setToolTip("Aggiungi testo (Ctrl+T)")
        self.act_date.setIconText("Data")
        self.act_date.setToolTip("Aggiungi la data di oggi")
        bar.addAction(self.act_text)
        bar.addAction(self.act_date)
        # Clic sul pulsante: accende o spegne il disegno; la freccia apre strumenti e colori.
        self.draw_button = QToolButton()
        self.draw_button.setDefaultAction(self.act_draw)
        self.draw_button.setToolTip("Disegna a mano libera: penna, evidenziatore, gomma (Ctrl+D)")
        self.draw_button.setMenu(self.draw_menu)
        self.draw_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        bar.addWidget(self.draw_button)

        self._doc_widgets = [self._page_box, self._zoom_box, self.sign_button, self.draw_button]

    def _build_draw_menu(self) -> None:
        """Strumenti di disegno, colori e spessori: scegliere un colore accende il suo strumento."""
        menu = QMenu("Strumenti di &disegno", self)
        for action in self.draw_actions.values():
            menu.addAction(action)
        menu.addSeparator()
        self.draw_option_actions = {}
        for title, key, options, tool in (
            ("Colore penna", "pen_color", PEN_COLORS, PEN),
            ("Spessore penna", "pen_width", PEN_WIDTHS, PEN),
            ("Colore evidenziatore", "highlighter_color", HIGHLIGHTER_COLORS, HIGHLIGHTER),
        ):
            submenu = menu.addMenu(title)
            group = QActionGroup(self)
            for value, label in options.items():
                action = QAction(label, self)
                if key == "pen_width":
                    action.setText(f"{label} ({value:g} pt)")
                else:
                    swatch = QPixmap(12, 12)
                    swatch.fill(QColor(value))
                    action.setIcon(QIcon(swatch))
                action.setCheckable(True)
                action.setChecked(self.view.draw_style[key] == value)
                action.triggered.connect(lambda _=False, k=key, v=value, t=tool: self.set_draw_option(k, v, t))
                group.addAction(action)
                submenu.addAction(action)
                self.draw_option_actions[(key, value)] = action
        self.draw_menu = menu

    def _draw_style(self) -> dict:
        """Strumento, colori e spessore del disegno, ricordati in settings.ini."""
        s = self._settings
        style = {
            "tool": s.value("draw/tool", DEFAULT_DRAW["tool"], type=str),
            "pen_color": s.value("draw/pen_color", DEFAULT_DRAW["pen_color"], type=str),
            "pen_width": s.value("draw/pen_width", DEFAULT_DRAW["pen_width"], type=float),
            "highlighter_color": s.value("draw/highlighter_color", DEFAULT_DRAW["highlighter_color"], type=str),
        }
        for key, options in (("tool", TOOL_LABELS), ("pen_color", PEN_COLORS), ("pen_width", PEN_WIDTHS),
                             ("highlighter_color", HIGHLIGHTER_COLORS)):
            if style[key] not in options:
                style[key] = DEFAULT_DRAW[key]
        return style

    def set_draw_tool(self, tool: str | None) -> None:
        if tool is None:
            self.view.cancel_draw_tool()
        else:
            self._settings.setValue("draw/tool", tool)
            self.view.draw_style["tool"] = tool
            self.view.start_draw_tool(tool)
        self._on_draw_tool_changed(self.view.draw_tool)  # anche se non è cambiato (es. nessun documento)

    def set_draw_option(self, key: str, value, tool: str) -> None:
        """Colore o spessore: diventa il default e accende lo strumento che lo usa."""
        self._settings.setValue(f"draw/{key}", value)
        self.view.draw_style[key] = value
        self.set_draw_tool(tool)

    def _toggle_draw(self) -> None:
        if self.view.draw_tool is not None:
            self.set_draw_tool(None)
        else:
            self.set_draw_tool(self.view.draw_style["tool"])

    def _on_draw_tool_changed(self, tool) -> None:
        self._update_enabled()
        self.act_draw.setChecked(tool is not None)
        for name, action in self.draw_actions.items():
            action.setChecked(name == tool)
        if tool is not None:
            self.statusBar().showMessage(ERASER_HINT if tool == ERASER else DRAW_HINT)
        elif self.statusBar().currentMessage() in (DRAW_HINT, ERASER_HINT):
            self.statusBar().clearMessage()

    def _build_text_bar(self) -> None:
        """Proprietà della casella di testo: visibile solo con una casella selezionata o in modifica.

        Sta sopra la vista, in alto a destra: comparendo non sposta le pagine (né la casella
        che si sta trascinando).
        """
        bar = QToolBar("Testo", self.view)
        bar.setObjectName("text_bar")
        bar.setStyleSheet("#text_bar { background: palette(window); border: 1px solid palette(mid); border-radius: 3px; }")
        self.text_bar = bar
        self.view.installEventFilter(self)
        # Senza focus: l'editor della casella resta attivo mentre si cambia il formato.
        self.font_box = QComboBox()
        self.font_box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Larghezza fissa: senza, la combo misurerebbe ogni voce nel suo font.
        self.font_box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.font_box.setMinimumContentsLength(16)
        self.font_box.setMaxVisibleItems(20)
        self.font_box.activated.connect(lambda _: self._choose_font(self.font_box.currentData()))
        fonts.preload()
        self._fill_font_box()
        bar.addWidget(self.font_box)
        self.size_box = QSpinBox()
        self.size_box.setRange(MIN_SIZE, MAX_SIZE)
        self.size_box.setSuffix(" pt")
        self.size_box.setKeyboardTracking(False)
        self.size_box.valueChanged.connect(lambda v: self._set_text_style(size=float(v)))
        bar.addWidget(self.size_box)
        self.color_box = QComboBox()
        self.color_box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for color, label in COLORS.items():
            swatch = QPixmap(12, 12)
            swatch.fill(QColor(color))
            self.color_box.addItem(QIcon(swatch), label, color)
        self.color_box.activated.connect(lambda _: self._set_text_style(color=self.color_box.currentData()))
        bar.addWidget(self.color_box)
        group = QActionGroup(self)
        self.align_actions = []
        for align, label in enumerate(ALIGN_LABELS):
            action = QAction(label, self)
            action.setCheckable(True)
            action.triggered.connect(lambda _=False, a=align: self._set_text_style(align=a))
            group.addAction(action)
            bar.addAction(action)
            self.align_actions.append(action)
        bar.hide()

    def _text_style(self) -> dict:
        """Formato delle prossime caselle, ricordato in settings.ini."""
        s = self._settings
        style = {
            "font": s.value("text/font", DEFAULT_STYLE["font"], type=str),
            "size": s.value("text/size", DEFAULT_STYLE["size"], type=float),
            "color": s.value("text/color", DEFAULT_STYLE["color"], type=str),
            "align": s.value("text/align", DEFAULT_STYLE["align"], type=int),
        }
        if not style["font"]:  # un font non più installato lo sostituisce la vista alla prossima casella
            style["font"] = DEFAULT_STYLE["font"]
        if style["color"] not in COLORS:
            style["color"] = DEFAULT_STYLE["color"]
        style["size"] = float(max(MIN_SIZE, min(MAX_SIZE, style["size"])))
        style["align"] = style["align"] if 0 <= style["align"] < 3 else 0
        return style

    def _fill_font_box(self) -> None:
        """Ultimi usati, poi Helvetica/Times/Courier, poi i font installati in ordine alfabetico,
        ognuno scritto nel suo font. Finché l'elenco non è pronto (thread di `fonts.preload`)
        ci sono solo i tre standard e si riprova poco dopo."""
        combo = self.font_box
        ready = fonts.ready()
        families = []
        if ready:
            known = set(QFontDatabase.families())  # solo quelli che anche l'editor sa mostrare
            families = sorted((f for f in fonts.installed() if f in known and f not in FONT_LABELS), key=str.casefold)
        recent = [f for f in self._recent_fonts() if f in FONT_LABELS or f in families]
        combo.blockSignals(True)
        combo.clear()
        size = combo.font().pointSizeF()
        for group in (recent, list(FONT_LABELS), families):
            if group and combo.count():
                combo.insertSeparator(combo.count())
            for family in group:
                face, hint = BOX_FONTS.get(family, (family, QFont.StyleHint.AnyStyle))
                font = QFont(face)
                font.setStyleHint(hint)
                font.setPointSizeF(size)
                combo.addItem(FONT_LABELS.get(family, family), family)
                combo.setItemData(combo.count() - 1, font, Qt.ItemDataRole.FontRole)
        combo.blockSignals(False)
        if not ready:
            QTimer.singleShot(FONTS_RETRY_MS, self._fill_font_box)
        elif self.text_bar.isVisible():
            self._update_text_bar(self.view.active_text_box)

    def _recent_fonts(self) -> list[str]:
        return [f for f in self._settings.value("text/recent_fonts", [], type=list) if f][:MAX_RECENT_FONTS]

    def _choose_font(self, family: str | None) -> None:
        if not family:
            return
        recent = [family] + [f for f in self._recent_fonts() if f != family]
        self._settings.setValue("text/recent_fonts", recent[:MAX_RECENT_FONTS])
        self._fill_font_box()
        self._set_text_style(font=family)

    def _set_text_style(self, **changes) -> None:
        for key, value in changes.items():
            self._settings.setValue(f"text/{key}", value)
        self.view.set_text_style(**changes)

    def _update_text_bar(self, box) -> None:
        if box is None:
            self.text_bar.hide()
            return
        for widget in (self.font_box, self.size_box, self.color_box):
            widget.blockSignals(True)
        combo = self.font_box
        for i in reversed(range(combo.count())):
            if combo.itemData(i, MISSING_FONT_ROLE):
                combo.removeItem(i)
        if combo.findData(box.font) < 0 and box.font not in FONT_LABELS:
            # Font della casella non installato qui: l'aspetto è quello incorporato nel file.
            combo.insertItem(0, NOT_INSTALLED.format(box.font) if fonts.ready() else box.font, box.font)
            combo.setItemData(0, True, MISSING_FONT_ROLE)
        combo.setCurrentIndex(max(0, combo.findData(box.font)))
        self.size_box.setValue(round(box.size))
        index = self.color_box.findData(box.color)
        self.color_box.setCurrentIndex(index)  # -1 = un colore non della lista (da un altro programma)
        for widget in (self.font_box, self.size_box, self.color_box):
            widget.blockSignals(False)
        self.align_actions[box.align if 0 <= box.align < 3 else 0].setChecked(True)
        self.text_bar.show()
        self.text_bar.raise_()
        self._place_text_bar()

    def _place_text_bar(self) -> None:
        """In alto a destra; con la barra di ricerca aperta, sotto di lei."""
        bar, viewport = self.text_bar, self.view.viewport()
        bar.adjustSize()
        x = viewport.x() + max(0, viewport.width() - bar.width() - 8)
        y = viewport.y() + 8
        if self.search_bar.isVisible():
            y = self.search_bar.geometry().bottom() + 5
        bar.move(x, y)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.view and event.type() == QEvent.Type.Resize:
            if self.search_bar.isVisible():
                self.search_bar.place()
            if self.text_bar.isVisible():
                self._place_text_bar()
        return super().eventFilter(obj, event)

    # --- Ricerca ----------------------------------------------------------------------

    def _show_search_hit(self, page: int, rect, first: bool) -> None:
        """Porta in vista un risultato; il primo salto di una ricerca si può annullare con Indietro."""
        if first:
            self._back = (self._back + [self.view.scroll_position()])[-MAX_HISTORY:]
            self._forward = []
            self._update_enabled()
        self.view.scroll_to_rect(page, rect)

    def keyPressEvent(self, event) -> None:
        # Esc arriva qui se la vista non lo usa (nessuno strumento, niente selezionato).
        if event.key() == Qt.Key.Key_Escape and self.search_bar.isVisible():
            self.search_bar.close_bar()
        else:
            super().keyPressEvent(event)

    def _build_form_bar(self) -> None:
        """Barra sottile sopra la vista, solo per i documenti con campi compilabili."""
        self.form_bar = QFrame()
        self.form_bar.setObjectName("form_bar")
        self.form_bar.setStyleSheet("#form_bar { background: #e8f0fe; border-bottom: 1px solid #c6d4f0; }")
        layout = QHBoxLayout(self.form_bar)
        layout.setContentsMargins(8, 3, 8, 3)
        self.form_label = QLabel(FORM_MESSAGE)
        layout.addWidget(self.form_label)
        layout.addStretch()
        self.highlight_button = QPushButton("Evidenzia campi")
        self.highlight_button.setCheckable(True)
        self.highlight_button.setChecked(self._settings.value("highlight_fields", True, type=bool))
        self.highlight_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.highlight_button.toggled.connect(self._set_highlight)
        layout.addWidget(self.highlight_button)
        self.form_bar.hide()

    def _set_highlight(self, on: bool) -> None:
        self._settings.setValue("highlight_fields", on)
        self.view.set_highlight(on)

    def _update_form_bar(self, doc: PdfDocument | None) -> None:
        if doc is not None and doc.xfa:
            self.form_label.setText(XFA_MESSAGE)
            self.highlight_button.hide()
            self.form_bar.show()
        elif doc is not None and any(f.fillable for f in doc.fields):
            self.form_label.setText(FORM_MESSAGE)
            self.highlight_button.show()
            self.form_bar.show()
        else:
            self.form_bar.hide()

    def _update_enabled(self) -> None:
        has_doc = self._path is not None
        for item in self._doc_actions + self._doc_widgets:
            item.setEnabled(has_doc)
        self.act_save.setEnabled(has_doc and self.modified)
        self.act_back.setEnabled(has_doc and bool(self._back))
        self.act_forward.setEnabled(has_doc and bool(self._forward))
        # La vista si ruota solo senza firme, campi, caselle o tratti da salvare e senza strumenti
        # attivi: con la vista ruotata il documento si legge soltanto. Raddrizzarla si può sempre.
        tool = self.view.placing or self.view.text_tool or self.view.draw_tool is not None
        can_rotate = has_doc and (self.view.rotation != 0 or not (self.placements.modified or tool))
        self.act_view_right.setEnabled(can_rotate)
        self.act_view_left.setEnabled(can_rotate)

    @property
    def modified(self) -> bool:
        """Firme, campi o pagine da salvare."""
        return self.placements.modified or self._original is not None

    def _shown_path(self) -> str:
        """Il file che l'utente conosce: l'originale se il documento aperto è un temporaneo."""
        return self._original or self._path

    def _update_title(self) -> None:
        if self._path:
            star = "* " if self.modified else ""
            self.setWindowTitle(f"{star}{Path(self._shown_path()).name} – PieDF")
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
        self._set_document(path, doc)
        self._add_recent(path)
        self._restore_position(path, len(doc.page_sizes))
        self.view.setFocus()
        if doc.repaired:
            self.statusBar().showMessage("Il file era danneggiato ed è stato riparato per la lettura.", 10000)
        else:
            self.statusBar().clearMessage()
        return True

    def _set_document(
        self, path: str, doc: PdfDocument, original: str | None = None, save_path: str | None = None,
    ) -> None:
        """Mostra `doc` (già aperto e con la password giusta) senza chiedere nulla."""
        self.placements.clear()
        self._clear_history()
        self._gen += 1
        self._path = path
        self._password = doc.password
        self._save_path = save_path
        self._original = original
        self._renderer.open(self._gen, path, doc.password)
        self.view.set_document(self._gen, doc.page_sizes, path, doc.password)
        self.sidebar.set_document(self._gen, doc.page_sizes, doc.toc)
        self.sidebar.set_current(self.view.current_page)
        self.forms.set_fields(doc.fields)
        self.annotations.set_items(doc.annotations)
        self._update_form_bar(doc)
        self.search_bar.reset(self._gen)
        self._page_box.setRange(1, len(doc.page_sizes))
        self._page_total.setText(f" / {len(doc.page_sizes)} ")
        self._update_title()
        self._update_enabled()
        self._remove_temps()

    def close_document(self) -> bool:
        if not self._confirm_discard():
            return False
        self._save_position()
        self.placements.clear()
        self._clear_history()
        self._path = None
        self._password = None
        self._save_path = None
        self._original = None
        self._gen += 1
        self._renderer.close(self._gen)
        self._remove_temps()
        self.view.clear()
        self.sidebar.clear()
        self.forms.set_fields([])
        self.annotations.set_items([])
        self._update_form_bar(None)
        self.search_bar.reset(self._gen)
        self._page_box.setRange(0, 0)
        self._page_total.setText(" / 0 ")
        self._update_title()
        self._update_enabled()
        return True

    def _confirm_discard(self) -> bool:
        """Chiede se salvare o scartare le modifiche non salvate; False se l'utente annulla."""
        self.view.commit_editor()
        if self._path is None or not self.modified:
            return True
        name = Path(self._shown_path()).name
        Button = QMessageBox.StandardButton
        if self.forms.changed() or self.annotations.pending() is not None or self._original is not None:
            title, text = "Modifiche non salvate", f"Le modifiche a «{name}» non sono state salvate."
        else:
            title, text = "Firme non salvate", f"Le firme aggiunte a «{name}» non sono state salvate."
        box = QMessageBox(QMessageBox.Icon.Warning, title, text, Button.Save | Button.Discard | Button.Cancel, self)
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
        if self._path is None or self._path in self._temps or not self.view.page_count:
            return  # di un temporaneo non si ricorda la posizione
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

    # --- Copia del testo ---------------------------------------------------------------

    def _text_editor(self) -> QLineEdit | QPlainTextEdit | None:
        """Campo di testo con il focus (editor sul posto, numero di pagina, zoom): Copia e Seleziona
        tutto valgono per lui. Con le scorciatoie ci pensa già lui; serve per il menu."""
        focus = QApplication.focusWidget()
        if isinstance(focus, (QAbstractSpinBox, QComboBox)):
            focus = focus.findChild(QLineEdit)  # il testo sta nel suo QLineEdit (se è modificabile)
        return focus if isinstance(focus, (QLineEdit, QPlainTextEdit)) else None

    def copy_text(self) -> None:
        editor = self._text_editor()
        if editor is not None:
            editor.copy()
        elif self.view.editor is None:  # un altro editor (elenco, menu a tendina) non ha testo da copiare
            self.view.copy_selection()

    def select_all_text(self) -> None:
        editor = self._text_editor()
        if editor is not None:
            editor.selectAll()
        elif self.view.editor is None:
            self.view.select_all_text()

    def _on_copy_busy(self, busy: bool) -> None:
        if busy:
            QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
            self.statusBar().showMessage(COPY_HINT)
            self._copy_timer.start()
            return
        QApplication.restoreOverrideCursor()
        self._copy_timer.stop()
        if self.statusBar().currentMessage() == COPY_HINT:
            self.statusBar().clearMessage()
        if self._copy_dialog is not None:
            dialog, self._copy_dialog = self._copy_dialog, None
            dialog.canceled.disconnect(self.view.cancel_copy)
            dialog.close()
            dialog.deleteLater()

    def _show_copy_dialog(self) -> None:
        if not self.view.copying or self._copy_dialog is not None:
            return
        dialog = QProgressDialog(COPY_HINT, "Annulla", 0, 1, self)
        dialog.setWindowTitle("Copia")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setMinimumDuration(0)
        dialog.canceled.connect(self.view.cancel_copy)
        self._copy_dialog = dialog
        dialog.show()
        self._on_copy_progress(*self._copy_done)

    def _on_copy_progress(self, done: int, total: int) -> None:
        self._copy_done = (done, total)
        dialog = self._copy_dialog
        if dialog is not None:
            dialog.setLabelText(f"Copia del testo: {done} pagine lette su {total}…")
            dialog.setMaximum(total)
            dialog.setValue(done)  # modale: elabora gli eventi, la copia può finire qui dentro

    def _on_copy_failed(self) -> None:
        QMessageBox.warning(self, "Copia non riuscita", "Impossibile copiare il testo.")

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
            src = Path(self._shown_path())
            suffix = "_modificato" if self._original is not None else "_firmato"
            proposed = str(src.with_name(f"{src.stem}{suffix}.pdf"))
        title = "Salva PDF modificato" if self._original is not None else "Salva PDF firmato"
        # Il dialog chiede già la conferma se il file esiste.
        dest, _ = QFileDialog.getSaveFileName(self, title, proposed, "Documenti PDF (*.pdf)")
        if not dest:
            return False
        if not Path(dest).suffix:
            dest += ".pdf"
        return self._save_to(os.path.abspath(dest))

    def _save_to(self, dest: str) -> bool:
        self.view.commit_editor()
        self.view.cancel_copy()  # il processo della copia tiene aperto il file
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
            save_with_signatures(
                src, password, self.placements.items, dest, self.placements.missing_pngs(), self.forms.changed(),
                self.annotations.pending(),
            )
        except SaveError as e:
            if overwrite:
                self._renderer.open(self._gen, src, password)
                self.view.reload()
            QMessageBox.critical(self, "Salvataggio non riuscito", str(e))
            return False

        # Le firme ora fanno parte del PDF: non sono più modificabili né annullabili (i campi sì);
        # le caselle di testo tornano nel modello dal file riaperto, modificabili.
        page, zoom, fit = self.view.current_page, self.view.zoom, self.view.fit_mode
        scroll_x, scroll_y = self.view.horizontalScrollBar().value(), self.view.verticalScrollBar().value()
        self.placements.clear()
        self._original = None  # le pagine ora sono nel file salvato
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

    # --- Stampa -----------------------------------------------------------------------

    def print_document(self) -> None:
        if self._path is None or self._print_job is not None:
            return
        try:
            allowed = can_print(self._path, self._password)
        except Exception as e:
            QMessageBox.critical(self, "Stampa non riuscita", f"Impossibile rileggere il documento.\n\nDettagli: {e}")
            return
        if not allowed:
            QMessageBox.warning(self, "Stampa non consentita", NO_PRINT_MESSAGE)
            return
        self.view.cancel_placing()
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        name = self._settings.value("print/printer", "", type=str)
        if name and name in QPrinterInfo.availablePrinterNames():
            printer.setPrinterName(name)
        options = self._ask_print(printer)
        if options is None:
            return
        self._settings.setValue("print/fit", options.fit)
        self._settings.setValue("print/auto_rotate", options.auto_rotate)
        if printer.outputFormat() == QPrinter.OutputFormat.NativeFormat and printer.printerName():
            self._settings.setValue("print/printer", printer.printerName())
        self.start_print(printer, options)

    def _ask_print(self, printer: QPrinter) -> PrintOptions | None:
        """Opzioni di PieDF e poi il dialog di stampa di sistema; None se si annulla."""
        saved = PrintOptions(
            fit=self._settings.value("print/fit", True, type=bool),
            auto_rotate=self._settings.value("print/auto_rotate", True, type=bool),
        )
        options_dialog = PrintOptionsDialog(saved, self)
        if options_dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        count = self.view.page_count
        printer.setFromTo(1, count)
        dialog = QPrintDialog(printer, self)
        dialog.setWindowTitle("Stampa")
        dialog.setMinMax(1, count)
        Option = QAbstractPrintDialog.PrintDialogOption
        dialog.setOption(Option.PrintPageRange, True)
        dialog.setOption(Option.PrintCurrentPage, True)
        dialog.setOption(Option.PrintSelection, False)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return options_dialog.options()

    def start_print(self, printer: QPrinter, options: PrintOptions) -> None:
        pages = selected_pages(printer, self.view.page_count, self.view.current_page)
        if not pages:
            QMessageBox.warning(self, "Stampa", "Nessuna pagina da stampare nell'intervallo scelto.")
            return
        self.view.commit_editor()
        try:
            path, temp = print_source(
                self._path, self._password, self.placements.items, self.placements.missing_pngs(),
                self.forms.changed(), self.annotations.pending(),
            )
        except SaveError as e:
            QMessageBox.critical(self, "Stampa non riuscita", str(e))
            return
        job = PrintJob(printer, path, self._password, pages, self.view.page_sizes, options, temp, self)
        self._print_job = job
        progress = QProgressDialog(f"Stampa pagina 1 di {job.total}…", "Annulla", 0, job.total, self)
        progress.setWindowTitle("Stampa")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.canceled.connect(job.cancel)

        def on_progress(done: int, total: int) -> None:
            progress.setValue(done)
            if done < total:
                progress.setLabelText(f"Stampa pagina {done + 1} di {total}…")

        def on_finished(completed: bool, error: str) -> None:
            progress.canceled.disconnect(job.cancel)
            progress.close()
            progress.deleteLater()
            self._print_job = None
            job.deleteLater()
            if error:
                QMessageBox.critical(self, "Stampa non riuscita", error)
            elif completed:
                self.statusBar().showMessage(f"Stampa inviata: {job.total} pagine", 10000)
            else:
                self.statusBar().showMessage("Stampa annullata", 10000)

        job.progress.connect(on_progress)
        job.finished.connect(on_finished)
        job.start()

    # --- Pagine -----------------------------------------------------------------------

    def _ensure_saved(self) -> bool:
        """Le pagine si organizzano solo senza firme, campi o caselle di testo da salvare: chiede Salva/Scarta/Annulla."""
        self.view.commit_editor()
        if self._path is None:
            return False
        if not self.placements.modified:
            return True
        Button = QMessageBox.StandardButton
        box = QMessageBox(
            QMessageBox.Icon.Warning, "Modifiche non salvate", SAVE_FIRST,
            Button.Save | Button.Discard | Button.Cancel, self,
        )
        box.button(Button.Save).setText("Salva")
        box.button(Button.Discard).setText("Scarta")
        box.button(Button.Cancel).setText("Annulla")
        box.setDefaultButton(Button.Save)
        answer = box.exec()
        if answer == Button.Save:
            return self.save() and not self.placements.modified
        if answer != Button.Discard:
            return False
        # Scarta: si riparte dal file (anche da un temporaneo di pagine, che resta da salvare).
        self.view.cancel_placing()
        try:
            doc = open_document(self._path, self._password)
        except (OpenError, PasswordRequired) as e:
            QMessageBox.critical(self, "Impossibile riaprire il documento", str(e))
            return False
        position = self.view.scroll_position()
        self._set_document(self._path, doc, self._original, self._save_path)
        self.view.set_scroll_position(*position)
        return True

    def organize_pages(self, files: list[tuple[str, PdfDocument]] = ()) -> None:
        """Apre "Organizza pagine"; i `files` (Unisci PDF) si aggiungono in fondo."""
        if self._page_job is not None or not self._ensure_saved():
            return
        self.view.cancel_placing()
        dialog = PageOrganizer(self._path, self._password, self.view.page_sizes, self._shown_path(), self)
        for path, doc in files:
            dialog.add_document(path, doc, at=len(dialog.refs))
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        sources, refs, changed = dialog.sources, dialog.refs, dialog.changed
        dialog.deleteLater()
        if accepted and changed:
            self.apply_pages(sources, refs)

    def merge_files(self) -> None:
        start = str(Path(self._shown_path()).parent) if self._path else ""
        paths, _ = QFileDialog.getOpenFileNames(self, "Unisci PDF", start, "Documenti PDF (*.pdf)")
        if not paths:
            return
        if self._path is None:  # senza documento aperto il primo file diventa il documento
            if not self.open_file(paths[0]):
                return
            paths = paths[1:]
        elif not self._ensure_saved():
            return
        files = []
        for path in paths:
            doc = open_with_password(self, path)
            if doc is not None:
                files.append((os.path.abspath(path), doc))
        if files or not paths:
            self.organize_pages(files)

    def rotate_current(self, delta: int) -> None:
        if self._page_job is not None or not self._ensure_saved():
            return
        current = self.view.current_page
        refs = [PageRef(MAIN, i, size, delta % 360 if i == current else 0) for i, size in enumerate(self.view.page_sizes)]
        self.apply_pages([(self._path, self._password)], refs)

    def apply_pages(self, sources: list[tuple[str, str | None]], refs: list[PageRef]) -> None:
        """Costruisce il documento riorganizzato in un temporaneo (fuori dalla GUI) e lo mostra
        come documento modificato, alla stessa pagina e zoom."""
        fd, tmp = tempfile.mkstemp(prefix="piedf-pagine-", suffix=".pdf")
        os.close(fd)
        self._temps.append(tmp)
        job = PageJob(sources, refs, tmp, self._password, self)
        self._page_job = job
        original = self._shown_path()
        gen = self._gen

        def done(doc, error: str) -> None:
            self._page_job = None
            if doc is None or gen != self._gen:
                self._remove_temps()
                if error:
                    QMessageBox.critical(self, "Modifica delle pagine non riuscita", error)
                return
            page, zoom, fit = self.view.current_page, self.view.zoom, self.view.fit_mode
            self._set_document(tmp, doc, original)
            if fit == FitMode.NONE:
                self.view.set_zoom(zoom)
            else:
                self.view.set_fit_mode(fit)
            self.view.go_to_page(min(page, len(doc.page_sizes) - 1))
            self.view.setFocus()
            self.statusBar().showMessage("Pagine modificate: salva per conservare le modifiche.", 10000)

        run_with_progress(self, job, "Modifica delle pagine…", done)

    def _remove_temps(self) -> None:
        """Cancella i temporanei delle pagine non più aperti (su Windows un file ancora aperto
        dal processo di render non si cancella: si riprova la volta dopo, al più tardi all'uscita)."""
        keep = []
        for path in self._temps:
            if path == self._path or (self._page_job is not None and path == self._page_job.dest):
                keep.append(path)
                continue
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError:
                keep.append(path)
        self._temps = keep

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
        self.sidebar.set_current(index)

    def _go_to_thumbnail(self, index: int) -> None:
        self.view.go_to_page(index)
        self.view.setFocus()

    def _on_rotation_changed(self, rotation: int) -> None:
        self.sidebar.set_rotation(rotation)
        self._update_enabled()

    # --- Pannello laterale ------------------------------------------------------------

    def _restore_sidebar(self) -> None:
        """Visibile, larghezza e scheda come l'ultima volta (settings.ini, `sidebar/…`)."""
        s = self._settings
        visible = s.value("sidebar/visible", False, type=bool)
        tab = s.value("sidebar/tab", PAGES_TAB, type=int)
        self.sidebar.tabs.setCurrentIndex(tab if tab in (PAGES_TAB, TOC_TAB) else PAGES_TAB)
        self.sidebar.tabs.currentChanged.connect(lambda i: self._settings.setValue("sidebar/tab", i))
        self.act_sidebar.setChecked(visible)
        self.sidebar.setVisible(visible)
        self._apply_sidebar_width()

    def _apply_sidebar_width(self) -> None:
        width = self._settings.value("sidebar/width", SIDEBAR_WIDTH, type=int)
        width = max(self.sidebar.minimumSizeHint().width(), min(width, max(self.width() // 2, 1)))
        self.resizeDocks([self.sidebar], [width], Qt.Orientation.Horizontal)

    def _set_sidebar(self, visible: bool) -> None:
        if not visible:
            self._save_sidebar_width()
        self.act_sidebar.setChecked(visible)
        self.sidebar.setVisible(visible)
        if visible:
            self._apply_sidebar_width()
        self._settings.setValue("sidebar/visible", visible)

    def _save_sidebar_width(self) -> None:
        if self.sidebar.isVisible():
            self._settings.setValue("sidebar/width", self.sidebar.width())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not event.spontaneous() and self.sidebar.isVisible():
            self._apply_sidebar_width()  # prima di mostrarsi la finestra non ha ancora la sua misura

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
        self._update_enabled()
        if placing:
            self.statusBar().showMessage(PLACING_HINT)
        elif self.statusBar().currentMessage() == PLACING_HINT:
            self.statusBar().clearMessage()

    def _on_text_tool_changed(self, active: bool) -> None:
        self._update_enabled()
        if active:
            self.statusBar().showMessage(DATE_HINT if self.view.date_tool else TEXT_HINT)
        elif self.statusBar().currentMessage() in (TEXT_HINT, DATE_HINT):
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
        self._save_sidebar_width()
        if self._print_job is not None:
            self._print_job.cancel()
            self._print_job.wait()
        if self._page_job is not None:
            self._page_job.cancel()
        self.view.cancel_copy()
        self._settings.sync()  # all'uscita Python può non distruggere QSettings
        self._renderer.shutdown()
        self._path = None
        self._remove_temps()
        super().closeEvent(event)
