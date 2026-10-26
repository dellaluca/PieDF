"""Copia del testo (#49): parole delle pagine che la vista non ha ancora, lette in un processo a parte.

Il processo di render della vista non si usa: con Seleziona tutto su un documento lungo
terrebbe ferme per secondi le pagine da disegnare. Il processo (`render_worker.copy_words`)
apre il file per conto suo, legge le pagine chieste e finisce.
"""

import multiprocessing

from PySide6.QtCore import QObject, QThread, Signal

from piedf import render_worker

CLOSE_TIMEOUT = 2.0  # secondi di attesa perché il processo annullato rilasci il file


class CopyJob(QObject):
    """Parole di `pages` (pagine del documento `path`), una pagina alla volta.

    `words_ready` per ogni pagina; `failed` se il processo non arriva in fondo (errore,
    processo morto a metà). Dopo `close()` non emette più niente.
    """

    words_ready = Signal(int, list)  # pagina, parole (vedi render_worker)
    failed = Signal(str)

    def __init__(self, path: str, password: str | None, pages: list[int], parent: QObject | None = None) -> None:
        super().__init__(parent)
        ctx = multiprocessing.get_context("spawn")
        reader, writer = ctx.Pipe(duplex=False)
        self._process = ctx.Process(
            target=render_worker.copy_words, args=(path, password, list(pages), writer), daemon=True
        )
        self._process.start()
        writer.close()  # scrive solo il processo: se muore, chi legge riceve EOF
        self._active = True
        self._reader = _PipeReader(reader)
        self._reader.message.connect(self._on_message)
        self._reader.start()

    @property
    def process(self):
        return self._process

    def _on_message(self, msg: tuple) -> None:
        if not self._active:  # messaggi già in coda quando la copia è stata chiusa
            return
        if msg[0] == "words":
            self.words_ready.emit(msg[1], msg[2])
        elif msg[0] == "done":
            self._active = False
        else:
            self._active = False
            self.failed.emit(msg[1] if msg[0] == "error" else "il processo di lettura si è interrotto")

    def close(self) -> None:
        """Ferma il processo (se lavora ancora) e aspetta che rilasci il file."""
        self._active = False
        if self._process.is_alive():
            self._process.terminate()
        self._process.join(CLOSE_TIMEOUT)
        self._reader.wait(int(CLOSE_TIMEOUT * 1000))


class _PipeReader(QThread):
    """Legge i messaggi del processo; ("eof",) quando la pipe si chiude."""

    message = Signal(object)

    def __init__(self, conn) -> None:
        super().__init__()
        self._conn = conn

    def run(self) -> None:
        try:
            while True:
                self.message.emit(self._conn.recv())
        except (EOFError, OSError):
            self.message.emit(("eof",))
        finally:
            self._conn.close()
