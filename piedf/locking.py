"""«Salva e blocca» (#70), lato GUI: conferma, nome del sigillo e `SealJob`, il processo che
appiattisce e sigilla il PDF con `sealing.run` (pyHanko tiene il GIL: fuori dalla GUI)."""

import multiprocessing
import os
import queue

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QLabel, QLineEdit, QVBoxLayout

from piedf import sealing

POLL_MS = 30

CONFIRM_TEXT = (
    "Il PDF verrà salvato e sigillato. Dopo non si potrà più modificare: firme, caselle di testo, "
    "disegni e campi diventano parte delle pagine. Se qualcuno lo modifica con un altro programma, "
    "il sigillo lo segnalerà."
)
TIMESTAMP_TEXT = "Aggiungi data certa (marca temporale: si collega a internet)"
NAME_TEXT = (
    "Questo nome comparirà nel sigillo dei PDF che blocchi. Non è verificato da nessuno: "
    "chi apre il file vedrà che è un sigillo personale."
)


class LockDialog(QDialog):
    """Conferma di «Salva e blocca» con la scelta della marca temporale."""

    def __init__(self, timestamp: bool, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Salva e blocca")
        text = QLabel(CONFIRM_TEXT)
        text.setWordWrap(True)
        self.timestamp_box = QCheckBox(TIMESTAMP_TEXT)
        self.timestamp_box.setChecked(timestamp)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Salva e blocca")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Annulla")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(text)
        layout.addSpacing(6)
        layout.addWidget(self.timestamp_box)
        layout.addWidget(buttons)
        self.setMinimumWidth(440)

    def timestamp(self) -> bool:
        return self.timestamp_box.isChecked()


class SealNameDialog(QDialog):
    """Nome da mostrare nel sigillo, al primo uso: obbligatorio, massimo `NAME_MAX` caratteri."""

    def __init__(self, name: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Sigillo personale")
        text = QLabel(NAME_TEXT)
        text.setWordWrap(True)
        self.edit = QLineEdit(name[:sealing.NAME_MAX])
        self.edit.setMaxLength(sealing.NAME_MAX)
        self.edit.setPlaceholderText("Nome e cognome")
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Annulla")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.edit.textChanged.connect(self._update_ok)
        layout = QVBoxLayout(self)
        layout.addWidget(text)
        layout.addWidget(QLabel("Nome nel sigillo:"))
        layout.addWidget(self.edit)
        layout.addWidget(self.buttons)
        self.setMinimumWidth(420)
        self._update_ok()

    def _update_ok(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(self.name()))

    def name(self) -> str:
        return sealing.clean_name(self.edit.text())


def ask_seal_name(parent, default: str) -> str | None:
    """Chiede il nome del sigillo; None se l'utente annulla."""
    dialog = SealNameDialog(default, parent)
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    name = dialog.name()
    dialog.deleteLater()
    return name if accepted and name else None


class SealJob(QObject):
    """Sigillo di un PDF con `sealing.run` in un processo "spawn"."""

    # "done", "signed", "foreign", "timestamp", "error" oppure "" se annullato; messaggio
    finished = Signal(str, str)

    def __init__(self, src: str, password: str | None, dest: str, folder, tsa_url: str | None,
                 name: str | None, parent=None) -> None:
        super().__init__(parent)
        self.dest = dest
        ctx = multiprocessing.get_context("spawn")
        self._results = ctx.Queue()
        self._process = ctx.Process(
            target=sealing.run, args=(src, password, dest, str(folder), tsa_url, name, self._results), daemon=True,
        )
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._done = False

    def start(self) -> None:
        self._process.start()
        self._timer.start()

    def cancel(self) -> None:
        """Ferma il processo; `dest` non viene toccato (si scrive accanto e poi si sostituisce)."""
        if self._done:
            return
        self._process.terminate()
        self._process.join(5)
        try:
            os.remove(self.dest + sealing.TMP_SUFFIX)
        except OSError:
            pass
        self._finish("", "")

    def _poll(self) -> None:
        if self._done:
            return
        try:
            msg = self._results.get_nowait()
        except queue.Empty:
            if self._process.is_alive():
                return
            try:  # il risultato può arrivare insieme alla fine del processo
                msg = self._results.get(timeout=0.5)
            except queue.Empty:
                self._finish("error", "Il processo si è interrotto durante il blocco del documento.")
                return
        if msg[0] == "done":
            self._finish("done", "")
        else:
            self._finish(msg[1], msg[2])

    def _finish(self, result: str, message: str) -> None:
        self._done = True
        self._timer.stop()
        self.finished.emit(result, message)
