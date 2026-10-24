import os
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from piedf.signatures import Kind, Signature, SignatureStore, finalize, remove_background

INK_COLORS = {"Nero": QColor("#111111"), "Blu": QColor("#1a3d8f")}
STROKE_WIDTHS = {"Sottile": 2.0, "Medio": 3.2, "Spesso": 5.0}
# Il disegno si esporta a risoluzione tripla rispetto allo schermo, per una firma nitida in stampa.
EXPORT_SCALE = 3
# Font calligrafici presenti in ogni Windows 10/11: nessun font da includere nell'exe.
SCRIPT_FONTS = [
    ("Segoe Script", "segoesc.ttf"),
    ("Segoe Print", "segoepr.ttf"),
    ("Ink Free", "Inkfree.ttf"),
    ("Gabriola", "Gabriola.ttf"),
]
TYPED_PIXEL_SIZE = 110
MAX_SOURCE_SIDE = 2400  # le foto si riducono prima di elaborarle


def script_fonts() -> list[str]:
    """Famiglie calligrafiche disponibili; se Qt non le vede le registra dalla cartella Fonts."""
    available = set(QFontDatabase.families())
    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    result = []
    for family, file in SCRIPT_FONTS:
        if family not in available and (fonts_dir / file).exists():
            QFontDatabase.addApplicationFont(str(fonts_dir / file))
        if family in QFontDatabase.families():
            result.append(family)
    return result


def checkerboard() -> QBrush:
    """Sfondo a scacchi per mostrare cosa è trasparente."""
    tile = QPixmap(16, 16)
    tile.fill(QColor("#ffffff"))
    painter = QPainter(tile)
    painter.fillRect(0, 0, 8, 8, QColor("#e6e6e6"))
    painter.fillRect(8, 8, 8, 8, QColor("#e6e6e6"))
    painter.end()
    return QBrush(tile)


class PreviewLabel(QLabel):
    """Mostra un'immagine con trasparenza su scacchiera, adattata allo spazio."""

    def __init__(self, empty_text: str) -> None:
        super().__init__()
        self._image: QImage | None = None
        self._empty_text = empty_text
        self.setMinimumSize(520, 200)

    def set_image(self, image: QImage | None) -> None:
        self._image = image
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), checkerboard())
        painter.setPen(QColor("#c8c8c8"))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        if self._image is None or self._image.isNull():
            painter.setPen(QColor("#808080"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._empty_text)
            return
        area = QRectF(self.rect().adjusted(16, 16, -16, -16))
        size = self._image.size().scaled(area.size().toSize(), Qt.AspectRatioMode.KeepAspectRatio)
        # Non ingrandire oltre la dimensione reale: un'immagine piccola resterebbe sgranata.
        if size.width() > self._image.width():
            size = self._image.size()
        target = QRectF(0, 0, size.width(), size.height())
        target.moveCenter(area.center())
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(target, self._image)


# --- Disegna --------------------------------------------------------------------------


class SignaturePad(QWidget):
    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(520, 200)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, False)  # il tocco arriva come mouse
        self._strokes: list[list[QPointF]] = []
        self._drawing = False
        self.color = INK_COLORS["Nero"]
        self.stroke_width = STROKE_WIDTHS["Medio"]

    def is_empty(self) -> bool:
        return not self._strokes

    def clear(self) -> None:
        self._strokes.clear()
        self.update()
        self.changed.emit()

    def undo(self) -> None:
        if self._strokes:
            self._strokes.pop()
            self.update()
            self.changed.emit()

    def set_style(self, color: QColor, width: float) -> None:
        self.color, self.stroke_width = color, width
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drawing = True
            self._strokes.append([event.position()])
            self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._drawing:
            stroke = self._strokes[-1]
            p = event.position()
            last = stroke[-1]
            # Punti troppo vicini aggiungono solo rumore al tratto.
            if (p.x() - last.x()) ** 2 + (p.y() - last.y()) ** 2 >= 1.0:
                stroke.append(p)
                self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._drawing and event.button() == Qt.MouseButton.LeftButton:
            self._drawing = False
            self.changed.emit()

    def _pen(self) -> QPen:
        return QPen(self.color, self.stroke_width, Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)

    def _draw_strokes(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(self._pen())
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for stroke in self._strokes:
            if len(stroke) == 1:
                # Un clic singolo è un punto (es. il puntino di una "i").
                painter.save()
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(self.color)
                r = self.stroke_width / 2
                painter.drawEllipse(stroke[0], r, r)
                painter.restore()
            else:
                painter.drawPath(smooth_path(stroke))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.white)
        painter.setPen(QColor("#c8c8c8"))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        # Linea guida come su un modulo cartaceo.
        y = round(self.height() * 0.72)
        painter.setPen(QPen(QColor("#b0b0b0"), 1, Qt.PenStyle.DashLine))
        painter.drawLine(24, y, self.width() - 24, y)
        painter.setPen(QColor("#a0a0a0"))
        painter.drawText(24, y - 6, "×")
        if self.is_empty():
            painter.drawText(self.rect().adjusted(0, 0, 0, -self.height() // 2),
                             Qt.AlignmentFlag.AlignCenter, "Firma qui con mouse, penna o dito")
        self._draw_strokes(painter)

    def to_image(self) -> QImage | None:
        if self.is_empty():
            return None
        image = QImage(self.size() * EXPORT_SCALE, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.scale(EXPORT_SCALE, EXPORT_SCALE)
        self._draw_strokes(painter)
        painter.end()
        return finalize(image)


def smooth_path(points: list[QPointF]) -> QPainterPath:
    """Curva morbida che passa per i punti medi: toglie gli spigoli del mouse."""
    path = QPainterPath(points[0])
    for prev, cur in zip(points, points[1:]):
        mid = (prev + cur) / 2
        path.quadTo(prev, mid)
    path.lineTo(points[-1])
    return path


class DrawTab(QWidget):
    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.pad = SignaturePad()
        self.pad.changed.connect(self.changed)

        self.color_box = QComboBox()
        self.color_box.addItems(INK_COLORS)
        self.width_box = QComboBox()
        self.width_box.addItems(STROKE_WIDTHS)
        self.width_box.setCurrentText("Medio")
        for box in (self.color_box, self.width_box):
            box.currentTextChanged.connect(self._update_style)
        undo = QPushButton("Annulla tratto")
        undo.clicked.connect(self.pad.undo)
        clear = QPushButton("Cancella")
        clear.clicked.connect(self.pad.clear)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Colore:"))
        controls.addWidget(self.color_box)
        controls.addWidget(QLabel("Tratto:"))
        controls.addWidget(self.width_box)
        controls.addStretch()
        controls.addWidget(undo)
        controls.addWidget(clear)

        layout = QVBoxLayout(self)
        layout.addWidget(self.pad, 1)
        layout.addLayout(controls)

    def _update_style(self) -> None:
        self.pad.set_style(INK_COLORS[self.color_box.currentText()],
                           STROKE_WIDTHS[self.width_box.currentText()])

    def has_content(self) -> bool:
        return not self.pad.is_empty()

    def to_image(self) -> QImage | None:
        return self.pad.to_image()


# --- Scrivi ---------------------------------------------------------------------------


def render_text(text: str, family: str, color: QColor) -> QImage | None:
    text = text.strip()
    if not text:
        return None
    font = QFont(family)
    font.setPixelSize(TYPED_PIXEL_SIZE)
    path = QPainterPath()
    path.addText(0, QFontMetricsF(font).ascent(), font, text)
    bounds = path.boundingRect().adjusted(-8, -8, 8, 8)
    image = QImage(bounds.size().toSize() + QSize(1, 1), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(-bounds.topLeft())
    painter.fillPath(path, color)
    painter.end()
    return finalize(image)


class TypeTab(QWidget):
    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.name = QLineEdit()
        self.name.setPlaceholderText("Scrivi il tuo nome")
        self.name.textChanged.connect(self._refresh)

        self.fonts = QListWidget()
        for family in script_fonts() or [QFont().family()]:
            item = QListWidgetItem(family)
            item.setData(Qt.ItemDataRole.UserRole, family)
            font = QFont(family)
            font.setPointSize(16)
            item.setFont(font)
            self.fonts.addItem(item)
        # Altezza esatta per vedere tutti i font senza scorrere.
        rows = sum(self.fonts.sizeHintForRow(i) for i in range(self.fonts.count()))
        self.fonts.setFixedHeight(rows + 2 * self.fonts.frameWidth() + 2)
        self.fonts.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.fonts.setCurrentRow(0)
        self.fonts.currentRowChanged.connect(self._refresh)

        self.color_box = QComboBox()
        self.color_box.addItems(INK_COLORS)
        self.color_box.currentTextChanged.connect(self._refresh)

        self.preview = PreviewLabel("L'anteprima comparirà qui")

        top = QHBoxLayout()
        top.addWidget(self.name, 1)
        top.addWidget(QLabel("Colore:"))
        top.addWidget(self.color_box)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.fonts)
        layout.addWidget(self.preview, 1)

    def _family(self) -> str:
        item = self.fonts.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else QFont().family()

    def _refresh(self) -> None:
        text = self.name.text()
        # Ogni voce dell'elenco mostra il nome scritto in quel font.
        for i in range(self.fonts.count()):
            item = self.fonts.item(i)
            family = item.data(Qt.ItemDataRole.UserRole)
            item.setText(text.strip() or family)
        self.preview.set_image(self.to_image())
        self.changed.emit()

    def has_content(self) -> bool:
        return bool(self.name.text().strip())

    def to_image(self) -> QImage | None:
        return render_text(self.name.text(), self._family(), INK_COLORS[self.color_box.currentText()])


# --- Immagine -------------------------------------------------------------------------


class ImageTab(QWidget):
    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._source: QImage | None = None

        choose = QPushButton("Scegli immagine…")
        choose.clicked.connect(self._choose)
        self.file_label = QLabel("Una foto o scansione della firma su sfondo chiaro (PNG, JPG)")
        self.file_label.setStyleSheet("color: gray")
        self.transparent = QCheckBox("Rendi trasparente lo sfondo chiaro")
        self.transparent.setChecked(True)
        self.transparent.toggled.connect(self._refresh)
        self.preview = PreviewLabel("Nessuna immagine")

        top = QHBoxLayout()
        top.addWidget(choose)
        top.addWidget(self.file_label, 1)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.transparent)
        layout.addWidget(self.preview, 1)

    def _choose(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Scegli l'immagine della firma", "", "Immagini (*.png *.jpg *.jpeg *.bmp)"
        )
        if path:
            self.load(path)

    def load(self, path: str) -> bool:
        image = QImage(path)
        if image.isNull():
            QMessageBox.warning(self, "Immagine non valida", f"Impossibile leggere «{Path(path).name}».")
            return False
        if max(image.width(), image.height()) > MAX_SOURCE_SIDE:
            image = image.scaled(MAX_SOURCE_SIDE, MAX_SOURCE_SIDE, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        self._source = image
        self.file_label.setText(Path(path).name)
        self._refresh()
        return True

    def _refresh(self) -> None:
        self.preview.set_image(self.to_image())
        self.changed.emit()

    def has_content(self) -> bool:
        return self._source is not None

    def to_image(self) -> QImage | None:
        if self._source is None:
            return None
        if self.transparent.isChecked():
            return finalize(remove_background(self._source))
        return finalize(self._source.convertToFormat(QImage.Format.Format_ARGB32))


# --- Dialog ---------------------------------------------------------------------------


class SignatureDialog(QDialog):
    """Crea una nuova firma (o delle iniziali) e la salva nell'archivio."""

    def __init__(self, store: SignatureStore, kind: Kind = Kind.SIGNATURE, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Crea firma")
        self._store = store
        self.saved: Signature | None = None

        self.kind_group = QButtonGroup(self)
        kind_row = QHBoxLayout()
        kind_row.addWidget(QLabel("Tipo:"))
        for k in Kind:
            radio = QRadioButton(k.label)
            radio.setChecked(k is kind)
            self.kind_group.addButton(radio)
            radio.setProperty("kind", k.value)
            kind_row.addWidget(radio)
        kind_row.addStretch()

        self.draw_tab = DrawTab()
        self.type_tab = TypeTab()
        self.image_tab = ImageTab()
        self.tabs = QTabWidget()
        self.tabs.addTab(self.draw_tab, "Disegna")
        self.tabs.addTab(self.type_tab, "Scrivi")
        self.tabs.addTab(self.image_tab, "Immagine")
        for tab in (self.draw_tab, self.type_tab, self.image_tab):
            tab.changed.connect(self._update_buttons)
        self.tabs.currentChanged.connect(self._update_buttons)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                        | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("Salva")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Annulla")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(kind_row)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.buttons)
        self.resize(640, 560)
        self._update_buttons()

    @property
    def kind(self) -> Kind:
        return Kind(self.kind_group.checkedButton().property("kind"))

    def set_kind(self, kind: Kind) -> None:
        for button in self.kind_group.buttons():
            button.setChecked(button.property("kind") == kind.value)

    def _update_buttons(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(
            self.tabs.currentWidget().has_content()
        )

    def accept(self) -> None:
        image = self.tabs.currentWidget().to_image()
        if image is None:
            QMessageBox.warning(self, "Firma vuota", "Non c'è niente da salvare: la firma è vuota.")
            return
        try:
            self.saved = self._store.add(image, self.kind)
        except OSError as e:
            QMessageBox.critical(self, "Impossibile salvare", str(e))
            return
        super().accept()
