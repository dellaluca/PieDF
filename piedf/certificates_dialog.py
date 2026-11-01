"""Strumenti › Certificati… (#75): elenco dei certificati per il sigillo, import di un
.p12/.pfx, export del sigillo personale, predefinito, elimina. Il lavoro vero è in
`certificates` (senza Qt); il predefinito sta in `settings.ini` (`lock/certificate`)."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from piedf import certificates, sealing

DEFAULT_KEY = "lock/certificate"

EXPORT_TEXT = "Conserva il file e la password in un posto sicuro: chi li ha entrambi può firmare a nome tuo."
DELETE_PERSONAL = (
    "Eliminare il sigillo personale? I PDF già bloccati restano validi, ma non potrai più "
    "firmarne altri con questo sigillo."
)
DELETE_IMPORTED = "Eliminare il certificato «{name}»? I PDF già firmati restano validi."
REPLACE_PERSONAL = (
    "Questo è un sigillo personale esportato da PieDF. Diventerà il sigillo personale di questo "
    "PC al posto di quello che c'è: i PDF già bloccati restano validi, ma non potrai più "
    "firmarne altri con il sigillo attuale."
)
EMPTY_TEXT = (
    "Nessun certificato. Il sigillo personale si crea al primo «Salva e blocca»; con «Importa…» "
    "puoi usare un certificato .p12/.pfx o un sigillo esportato da un altro PC."
)


def _ask(parent, title: str, text: str, yes: str) -> bool:
    Button = QMessageBox.StandardButton
    box = QMessageBox(QMessageBox.Icon.Question, title, text, Button.Ok | Button.Cancel, parent)
    box.button(Button.Ok).setText(yes)
    box.button(Button.Cancel).setText("Annulla")
    box.setDefaultButton(Button.Cancel)
    return box.exec() == Button.Ok


def ask_foreign(parent) -> str:
    """«Il sigillo personale è stato creato su un altro PC…»: "new" (Crea nuovo), "import"
    (Importa il sigillo…) oppure "" (Annulla)."""
    Button = QMessageBox.StandardButton
    box = QMessageBox(QMessageBox.Icon.Question, "Sigillo personale", sealing.FOREIGN,
                      Button.Ok | Button.Cancel, parent)
    box.button(Button.Ok).setText("Crea nuovo")
    box.button(Button.Cancel).setText("Annulla")
    imported = box.addButton("Importa il sigillo…", QMessageBox.ButtonRole.ActionRole)
    box.setDefaultButton(Button.Cancel)
    result = box.exec()
    if box.clickedButton() is imported:
        return "import"
    return "new" if result == Button.Ok else ""


def import_certificate(parent, folder: Path, settings) -> str | None:
    """Sceglie un .p12/.pfx, chiede la password (di nuovo se sbagliata) e lo importa.
    Impronta del certificato importato, None se annullato o rifiutato (niente scritto)."""
    path, _ = QFileDialog.getOpenFileName(parent, "Importa certificato", "",
                                          "Certificati (*.p12 *.pfx);;Tutti i file (*)")
    if not path:
        return None
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        QMessageBox.critical(parent, "Importa certificato", f"Impossibile leggere il file.\n\nDettagli: {e}")
        return None
    prompt = f"Password di «{Path(path).name}»:"
    while True:
        password, ok = QInputDialog.getText(parent, "Importa certificato", prompt, QLineEdit.EchoMode.Password)
        if not ok:
            return None
        QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        try:
            pending = certificates.read_p12(folder, data, password)
        except certificates.CertificateError as e:
            QApplication.restoreOverrideCursor()
            if str(e) == certificates.BAD_PASSWORD:
                prompt = f"Password errata. Password di «{Path(path).name}»:"
                continue
            QMessageBox.warning(parent, "Importa certificato", str(e))
            return None
        QApplication.restoreOverrideCursor()
        break
    if pending.replaces and not _ask(parent, "Sigillo personale", REPLACE_PERSONAL, "Sostituisci"):
        return None
    try:
        certificates.install(folder, pending)
    except OSError as e:
        QMessageBox.critical(parent, "Importa certificato", f"Impossibile salvare il certificato.\n\nDettagli: {e}")
        return None
    # Chi importa un certificato vero vuole usarlo: diventa il predefinito se non se n'è scelto uno.
    if not pending.personal and not settings.value(DEFAULT_KEY, "", type=str):
        settings.setValue(DEFAULT_KEY, pending.fingerprint)
    return pending.fingerprint


class ExportPasswordDialog(QDialog):
    """Password del file esportato, scritta due volte, almeno `PASSWORD_MIN` caratteri."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Esporta il sigillo personale")
        text = QLabel(EXPORT_TEXT)
        text.setWordWrap(True)
        self.first = QLineEdit()
        self.second = QLineEdit()
        for edit in (self.first, self.second):
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.textChanged.connect(self._update_ok)
        self.hint = QLabel()
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Esporta…")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Annulla")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        form = QFormLayout()
        form.addRow("Password:", self.first)
        form.addRow("Ripeti la password:", self.second)
        layout = QVBoxLayout(self)
        layout.addWidget(text)
        layout.addLayout(form)
        layout.addWidget(self.hint)
        layout.addWidget(self.buttons)
        self.setMinimumWidth(420)
        self._update_ok()

    def _update_ok(self) -> None:
        first, second = self.first.text(), self.second.text()
        if len(first) < certificates.PASSWORD_MIN:
            hint = f"Almeno {certificates.PASSWORD_MIN} caratteri."
        elif first != second:
            hint = "Le due password non coincidono."
        else:
            hint = ""
        self.hint.setText(hint)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not hint)

    def password(self) -> str:
        return self.first.text()


def export_seal(parent, folder: Path) -> bool:
    """Password, file di destinazione (`<nome>_sigillo.p12`) e scrittura. True se esportato."""
    if sealing.identity_state(folder) != "ok":
        QMessageBox.warning(parent, "Esporta il sigillo personale",
                            "Il sigillo personale è stato creato su un altro PC: da qui non si può esportare.")
        return False
    dialog = ExportPasswordDialog(parent)
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    password = dialog.password()
    dialog.deleteLater()
    if not accepted:
        return False
    proposed = str(Path.home() / certificates.export_name(folder))
    dest, _ = QFileDialog.getSaveFileName(parent, "Esporta il sigillo personale", proposed, "Certificati (*.p12)")
    if not dest:
        return False
    if not Path(dest).suffix:
        dest += ".p12"
    try:
        data = certificates.export_personal(folder, password)
        sealing._write_atomic(Path(dest), data)
    except certificates.CertificateError as e:
        QMessageBox.warning(parent, "Esporta il sigillo personale", str(e))
        return False
    except OSError as e:
        QMessageBox.critical(parent, "Esporta il sigillo personale",
                             f"Impossibile scrivere «{Path(dest).name}».\n\nDettagli: {e}")
        return False
    return True


class CertificatesDialog(QDialog):
    """Elenco dei certificati con Importa / Esporta / Usa come predefinito / Elimina."""

    def __init__(self, folder: Path, settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Certificati")
        self._folder = Path(folder)
        self._settings = settings
        self._entries: list[certificates.CertInfo] = []
        text = QLabel("Certificati con cui «Salva e blocca» firma i PDF.")
        text.setWordWrap(True)
        self.tree = QTreeWidget()
        self.tree.setRootIsDecorated(False)
        self.tree.setHeaderLabels(["Nome", "Emittente", "Scadenza", "Predefinito"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.currentItemChanged.connect(self._update_buttons)
        self.empty = QLabel(EMPTY_TEXT)
        self.empty.setWordWrap(True)
        self.import_button = QPushButton("&Importa…")
        self.export_button = QPushButton("&Esporta…")
        self.default_button = QPushButton("Usa come &predefinito")
        self.delete_button = QPushButton("E&limina")
        self.import_button.clicked.connect(self.import_certificate)
        self.export_button.clicked.connect(self.export_selected)
        self.default_button.clicked.connect(self.make_default)
        self.delete_button.clicked.connect(self.delete_selected)
        row = QHBoxLayout()
        for button in (self.import_button, self.export_button, self.default_button, self.delete_button):
            row.addWidget(button)
        row.addStretch(1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("Chiudi")
        close.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(text)
        layout.addWidget(self.tree)
        layout.addWidget(self.empty)
        layout.addLayout(row)
        layout.addWidget(close)
        self.resize(640, 320)
        self.refresh()

    def default(self) -> str | None:
        return certificates.resolve(self._folder, self._settings.value(DEFAULT_KEY, "", type=str))

    def refresh(self, select: str | None = None) -> None:
        current = self.selected()
        select = select or (current.fingerprint if current else None)
        self._entries = certificates.entries(self._folder)
        default = self.default()
        self.tree.clear()
        for info in self._entries:
            name = f"{info.name} (sigillo personale)" if info.personal else info.name
            if not info.usable:
                name += " – non utilizzabile su questo PC"
            item = QTreeWidgetItem([name, info.issuer, info.expires.astimezone().strftime("%d/%m/%Y"),
                                    "✓" if info.fingerprint == default else ""])
            item.setData(0, Qt.ItemDataRole.UserRole, info.fingerprint)
            item.setTextAlignment(3, Qt.AlignmentFlag.AlignCenter)
            self.tree.addTopLevelItem(item)
            if info.fingerprint == select:
                self.tree.setCurrentItem(item)
        if self.tree.currentItem() is None and self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        for column in (1, 2, 3):
            self.tree.resizeColumnToContents(column)
        self.tree.setVisible(bool(self._entries))
        self.empty.setVisible(not self._entries)
        self._update_buttons()

    def selected(self) -> certificates.CertInfo | None:
        item = self.tree.currentItem()
        if item is None:
            return None
        fp = item.data(0, Qt.ItemDataRole.UserRole)
        return next((info for info in self._entries if info.fingerprint == fp), None)

    def _update_buttons(self, *args) -> None:
        info = self.selected()
        self.export_button.setEnabled(info is not None and info.personal and info.usable)
        self.default_button.setEnabled(info is not None and info.fingerprint != self.default())
        self.delete_button.setEnabled(info is not None)

    def import_certificate(self) -> None:
        fp = import_certificate(self, self._folder, self._settings)
        if fp is not None:
            self.refresh(fp)

    def export_selected(self) -> None:
        info = self.selected()
        if info is not None and info.personal:
            export_seal(self, self._folder)

    def make_default(self) -> None:
        info = self.selected()
        if info is not None:
            self._settings.setValue(DEFAULT_KEY, info.fingerprint)
            self.refresh()

    def delete_selected(self) -> None:
        info = self.selected()
        if info is None:
            return
        text = DELETE_PERSONAL if info.personal else DELETE_IMPORTED.format(name=info.name)
        if not _ask(self, "Elimina certificato", text, "Elimina"):
            return
        try:
            certificates.delete(self._folder, info.fingerprint)
        except OSError as e:
            QMessageBox.critical(self, "Elimina certificato", f"Impossibile eliminare il certificato.\n\nDettagli: {e}")
        if self._settings.value(DEFAULT_KEY, "", type=str) == info.fingerprint:
            self._settings.remove(DEFAULT_KEY)
        self.refresh()
