"""Barra di ricerca (#14): Ctrl+F, risultati man mano, ◀ ▶, "3 di 27".

La ricerca nel testo delle pagine la fa il processo di render (vedi render_worker), le
caselle di testo di PieDF si cercano qui dal modello `Annotations` (il processo di render
le toglie dal suo documento e comunque `search_for` non legge le FreeText): costa poco e
trova anche quelle non salvate. Una casella trovata si evidenzia intera.
"""

from PySide6.QtCore import QEvent, QRectF, Qt, QTimer, Signal
from PySide6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QToolButton, QWidget

from piedf.annotations import Annotations, to_qrect
from piedf.document import TextBox
from piedf.renderer import Renderer

DELAY_MS = 300  # ritardo tra l'ultimo tasto e la ricerca
# I risultati che arrivano insieme si mettono in ordine una volta sola: con molti risultati
# riordinarli a ogni pagina bloccherebbe la GUI.
REBUILD_MS = 40
NO_RESULTS = "Nessun risultato"
NO_TEXT = "Questo documento non contiene testo ricercabile"
FIELD_STYLE = "QLineEdit { border: 1px solid #d93025; border-radius: 2px; padding: 1px; }"


class SearchBar(QFrame):
    """Sta sopra la vista (figlia di `PdfView`), come la barra delle caselle: non sposta le pagine."""

    # Risultato da portare in vista: pagina, rettangolo in punti, primo salto di una ricerca.
    show_hit = Signal(int, QRectF, bool)
    toggled = Signal()  # aperta o chiusa: la barra delle caselle di testo si sposta

    def __init__(self, view: QWidget, renderer: Renderer, annotations: Annotations) -> None:
        super().__init__(view)
        self._view = view
        self._annotations = annotations
        self.setObjectName("search_bar")
        self.setStyleSheet(
            "#search_bar { background: palette(window); border: 1px solid palette(mid); border-radius: 3px; }"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)
        self.field = QLineEdit()
        self.field.setPlaceholderText("Trova nel documento")
        self.field.setMinimumWidth(220)
        self.field.setClearButtonEnabled(True)
        self.field.installEventFilter(self)
        self.field.textChanged.connect(self._schedule)
        layout.addWidget(self.field)
        self.prev_button = self._button("◀", "Precedente (Maiusc+Invio, Maiusc+F3)", self.previous)
        layout.addWidget(self.prev_button)
        self.next_button = self._button("▶", "Successivo (Invio, F3)", self.next)
        layout.addWidget(self.next_button)
        self.count_label = QLabel()
        self.count_label.setMinimumWidth(90)
        layout.addWidget(self.count_label)
        self.case_box = QCheckBox("Maiuscole/minuscole")
        self.case_box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.case_box.toggled.connect(lambda _: self._start())
        layout.addWidget(self.case_box)
        layout.addWidget(self._button("✕", "Chiudi (Esc)", self.close_bar))

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DELAY_MS)
        self._timer.timeout.connect(self._start)
        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.setInterval(REBUILD_MS)
        self._rebuild_timer.timeout.connect(self._rebuild)
        renderer.search_hits.connect(self._on_hits)
        renderer.search_done.connect(self._on_done)
        annotations.changed.connect(self._on_annotations_changed)
        self._renderer = renderer

        self._gen = 0
        self._id = 0
        self._text = ""  # testo della ricerca in corso o finita ("" = nessuna)
        self._case = False
        self._start_page = 0
        self._searching = False
        self._has_text = True
        self._jumped = False  # il primo salto della ricerca è già fatto
        self._scanned: int | None = None  # ultima pagina con risultati arrivata
        self._pdf_hits: dict[int, list[QRectF]] = {}  # dal processo di render
        self._hits: list[tuple[int, QRectF]] = []  # tutti, caselle comprese, in ordine
        self._current: tuple[int, QRectF] | None = None
        self.hide()

    def _button(self, text: str, tip: str, slot) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setToolTip(tip)
        button.setAutoRaise(True)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(slot)
        return button

    # --- API --------------------------------------------------------------------------

    @property
    def hits(self) -> list[tuple[int, QRectF]]:
        return self._hits

    @property
    def current_index(self) -> int:
        """Posizione del risultato corrente (da 0), -1 se non c'è."""
        return self._hits.index(self._current) if self._current in self._hits else -1

    @property
    def searching(self) -> bool:
        return self._searching

    def open_bar(self) -> None:
        """Ctrl+F: mostra la barra (o ci torna) con il testo selezionato."""
        self.show()
        self.raise_()
        self.place()
        self.toggled.emit()
        self.field.setFocus()
        self.field.selectAll()
        if self.field.text().strip() and not self._text:
            self._start()

    def close_bar(self) -> None:
        """Esc o ✕: chiude la barra e toglie le evidenziazioni (il testo resta per la prossima volta)."""
        was_visible = self.isVisible()
        self._stop()
        self.hide()
        if was_visible:
            self._view.setFocus()
            self.toggled.emit()

    def reset(self, gen: int) -> None:
        """Documento aperto o chiuso: la ricerca si azzera."""
        self._gen = gen
        self.close_bar()

    def place(self) -> None:
        """In alto a destra sopra la vista."""
        viewport = self._view.viewport()
        self.adjustSize()
        x = viewport.x() + max(0, viewport.width() - self.width() - 8)
        self.move(x, viewport.y() + 8)

    def next(self) -> None:
        self._step(1)

    def previous(self) -> None:
        self._step(-1)

    # --- Ricerca ----------------------------------------------------------------------

    def _schedule(self) -> None:
        self._timer.start()

    def _start(self) -> None:
        self._timer.stop()
        text = self.field.text()
        if not text.strip() or not self._view.page_count:
            self._stop()
            return
        self._id += 1
        self._text, self._case = text, self.case_box.isChecked()
        self._start_page = max(0, self._view.current_page)
        self._searching = True
        self._has_text = True
        self._jumped = False
        self._scanned = None
        self._pdf_hits = {}
        self._current = None
        self._renderer.search(self._gen, self._id, text, self._case, self._start_page)
        self._rebuild()

    def _stop(self) -> None:
        self._timer.stop()
        self._rebuild_timer.stop()
        if self._searching:
            self._renderer.search(self._gen, self._id, "", False, 0)
        self._id += 1
        self._text = ""
        self._searching = False
        self._pdf_hits = {}
        self._hits = []
        self._current = None
        self._update()

    def _on_hits(self, gen: int, search_id: int, page: int, rects: list) -> None:
        if gen != self._gen or search_id != self._id:
            return
        self._pdf_hits[page] = [QRectF(x0, y0, x1 - x0, y1 - y0) for x0, y0, x1, y1 in rects]
        self._scanned = page
        if not self._rebuild_timer.isActive():
            self._rebuild_timer.start()

    def _on_done(self, gen: int, search_id: int, has_text: bool) -> None:
        if gen != self._gen or search_id != self._id:
            return
        self._searching = False
        self._has_text = has_text
        self._rebuild()

    def _on_annotations_changed(self) -> None:
        if self._text:
            self._rebuild()

    def _box_hits(self) -> list[tuple[int, QRectF]]:
        wanted = " ".join(self._text.split())
        if not self._case:
            wanted = wanted.lower()
        hits = []
        for item in self._annotations.items:
            if isinstance(item, TextBox):
                text = " ".join(item.text.split())
                if wanted in (text if self._case else text.lower()):
                    hits.append((item.page, to_qrect(item.rect)))
        return hits

    def _rebuild(self) -> None:
        """Rimette in ordine tutti i risultati (pagina, poi dall'alto in basso e da sinistra a destra)."""
        self._rebuild_timer.stop()
        hits = [(page, rect) for page, rects in self._pdf_hits.items() for rect in rects]
        if self._text:
            hits += self._box_hits()
        hits.sort(key=lambda h: (h[0], round(h[1].top(), 1), h[1].left()))
        self._hits = hits
        if self._current not in hits:
            self._current = None
        # Prima che arrivi una pagina con risultati non si sa ancora quale viene prima
        # (una casella di testo lontana non deve far saltare la vista).
        if not self._jumped and self._text and not (self._searching and self._scanned is None):
            target = self._first_after(self._scanned)
            if target is not None:
                self._jumped = True
                self._current = target
                self._update()
                self.show_hit.emit(target[0], target[1], True)
                return
        self._update()

    def _first_after(self, scanned: int | None) -> tuple[int, QRectF] | None:
        """Primo risultato dalla pagina di partenza in poi (poi da capo), tra le pagine già cercate:
        il processo le cerca in quest'ordine, `scanned` è l'ultima arrivata (None = tutte)."""
        n = self._view.page_count
        if not n or not self._hits:
            return None
        order = lambda page: (page - self._start_page) % n
        limit = n if scanned is None or not self._searching else order(scanned)
        candidates = [(order(h[0]), i) for i, h in enumerate(self._hits) if order(h[0]) <= limit]
        return self._hits[min(candidates)[1]] if candidates else None

    def _step(self, delta: int) -> None:
        if not self.isVisible():
            self.open_bar()
        if self._timer.isActive() or (self.field.text() != self._text and self.field.text().strip()):
            self._start()  # Invio prima del ritardo: si cerca subito
            return
        if not self._hits:
            return
        index = self.current_index
        if index < 0:
            target = self._first_after(None) if delta > 0 else self._hits[-1]
        else:
            target = self._hits[(index + delta) % len(self._hits)]
        self._jumped = True
        self._current = target
        self._update()
        self.show_hit.emit(target[0], target[1], False)

    def _update(self) -> None:
        by_page: dict[int, list[QRectF]] = {}
        for page, rect in self._hits:
            by_page.setdefault(page, []).append(rect)
        self._view.set_search_hits(by_page, self._current)
        empty = bool(self._text) and not self._searching and not self._hits
        if not self._text:
            label = ""
        elif empty:
            label = NO_RESULTS if self._has_text else NO_TEXT
        else:
            index = self.current_index
            dots = "…" if self._searching else ""
            label = f"{index + 1 if index >= 0 else 0} di {len(self._hits)}{dots}"
        self.count_label.setText(label)
        self.field.setStyleSheet(FIELD_STYLE if empty else "")
        self.prev_button.setEnabled(bool(self._hits))
        self.next_button.setEnabled(bool(self._hits))
        if self.isVisible():
            self.place()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.field and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._step(-1 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1)
                return True
            if key == Qt.Key.Key_Escape:
                self.close_bar()
                return True
        return super().eventFilter(obj, event)
