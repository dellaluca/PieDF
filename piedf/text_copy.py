"""Copia del testo (#49): parole delle pagine che la vista non ha ancora, lette in un processo a parte.

Il processo di render della vista non si usa: con Seleziona tutto su un documento lungo
terrebbe ferme per secondi le pagine da disegnare. Il processo (`render_worker.copy_words`)
apre il file per conto suo, legge le pagine chieste e finisce.

Il thread che legge la pipe non può contare sull'EOF (#54): su Windows il lato in scrittura
passa al figlio come copia nel processo della GUI (`DupHandle`), che il figlio si prende solo
quando parte. Se il processo viene chiuso prima, la copia resta aperta e `recv()` non
finirebbe mai: il thread controlla la pipe a intervalli brevi e si ferma con `stop()`.
"""

import multiprocessing

from PySide6.QtCore import QCoreApplication, QObject, QThread, Signal

from piedf import render_worker

CLOSE_TIMEOUT = 2.0  # secondi di attesa perché il processo annullato rilasci il file
POLL_S = 0.05  # ogni quanto il thread che legge controlla se deve fermarsi


class CopyJob(QObject):
    """Parole di `pages` (pagine del documento `path`), una pagina alla volta.

    `words_ready` per ogni pagina; `failed` se il processo non arriva in fondo (errore,
    processo morto a metà). Dopo `close()` non emette più niente; va chiamato sempre, anche
    quando la copia è finita: aspetta che il processo e il thread che legge siano fermi
    (un `QThread` distrutto mentre gira chiude PieDF con `qFatal`). Si chiude da solo
    all'uscita dall'applicazione.
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
        self._closed = False
        self._reader = _PipeReader(reader)
        self._reader.message.connect(self._on_message)
        self._reader.start()
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.close)

    @property
    def process(self):
        return self._process

    @property
    def reader(self) -> QThread:
        return self._reader

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
        """Ferma il processo (se lavora ancora), aspetta che rilasci il file e che il thread finisca.

        Si può chiamare più volte e da ogni stato (in corso, finita, fallita).
        """
        self._active = False
        if self._closed:
            return
        self._closed = True
        self._reader.stop()
        if self._process.is_alive():
            self._process.terminate()
        self._process.join(CLOSE_TIMEOUT)
        # Senza limite: il thread esce entro POLL_S, o finito il messaggio che sta leggendo
        # (il processo è fermo: o lo ha scritto intero, o la pipe è chiusa e arriva l'EOF).
        self._reader.wait()


class _PipeReader(QThread):
    """Legge i messaggi del processo; ("eof",) quando la pipe si chiude. Si ferma con `stop()`."""

    message = Signal(object)

    def __init__(self, conn) -> None:
        super().__init__()
        self._conn = conn
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        try:
            while not self._stop:
                if self._conn.poll(POLL_S):  # True anche quando la pipe si chiude: recv() dà EOF
                    self.message.emit(self._conn.recv())
        except (EOFError, OSError):
            self.message.emit(("eof",))
        finally:
            self._conn.close()
