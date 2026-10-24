import multiprocessing
import threading

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtGui import QImage

from piedf import render_worker

# Se il processo di render muore (es. un PDF che manda in crash MuPDF) si riavvia,
# ma non all'infinito sullo stesso documento.
MAX_RESTARTS = 3
CLOSE_TIMEOUT = 5.0  # secondi di attesa perché il processo rilasci il file


class Renderer(QObject):
    """Interfaccia lato GUI verso il processo di render."""

    page_ready = Signal(int, int, float, QImage)  # gen, pagina, scala, immagine
    failed = Signal(int, str)  # gen, messaggio

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._ctx = multiprocessing.get_context("spawn")
        self._requests = self._ctx.Queue()
        self._results = self._ctx.Queue()
        self._process = None
        self._open_msg = None
        self._restarts = 0
        self._closed = threading.Condition()
        self._closed_gen: int | None = None
        self._start_process()

        self._reader = _ResultReader(self._results, self._on_closed)
        self._reader.page_ready.connect(self.page_ready)
        self._reader.failed.connect(self.failed)
        self._reader.start()

    def _start_process(self) -> None:
        self._process = self._ctx.Process(
            target=render_worker.run, args=(self._requests, self._results), daemon=True
        )
        self._process.start()

    def _ensure_alive(self) -> bool:
        if self._process.is_alive():
            return True
        if self._restarts >= MAX_RESTARTS:
            return False
        self._restarts += 1
        self._start_process()
        if self._open_msg is not None:
            self._requests.put(self._open_msg)
        return True

    def open(self, gen: int, path: str, password: str | None) -> None:
        self._open_msg = ("open", gen, path, password)
        self._restarts = 0
        if self._ensure_alive():
            self._requests.put(self._open_msg)

    def close(self, gen: int, wait: bool = False) -> bool:
        """Chiude il documento; con `wait` aspetta che il file sia davvero rilasciato.

        Serve prima di sovrascrivere il file aperto: su Windows è bloccato finché è aperto.
        Restituisce False se il processo non ha risposto in tempo.
        """
        self._open_msg = None
        if not self._process.is_alive():
            return True
        with self._closed:
            self._closed_gen = None
        self._requests.put(("open", gen, None, None))
        if not wait:
            return True
        with self._closed:
            return self._closed.wait_for(
                lambda: self._closed_gen == gen or not self._process.is_alive(), CLOSE_TIMEOUT
            )

    def _on_closed(self, gen: int) -> None:
        with self._closed:
            self._closed_gen = gen
            self._closed.notify_all()

    def request(self, gen: int, items: list[tuple[int, float]]) -> None:
        if self._ensure_alive():
            self._requests.put(("want", gen, items))
        else:
            self.failed.emit(gen, "Il motore di rendering si è interrotto su questo documento.")

    def shutdown(self) -> None:
        self._requests.put(("quit",))
        self._results.put(None)
        self._reader.wait(2000)
        self._process.join(2)
        if self._process.is_alive():
            self._process.terminate()


class _ResultReader(QThread):
    """Legge i risultati dal processo e li inoltra alla GUI come segnali."""

    page_ready = Signal(int, int, float, QImage)
    failed = Signal(int, str)

    def __init__(self, results, on_closed) -> None:
        super().__init__()
        self._results = results
        self._on_closed = on_closed  # chiamata da questo thread, non dalla GUI

    def run(self) -> None:
        while True:
            msg = self._results.get()
            if msg is None:
                return
            if msg[0] == "page":
                _, gen, page, scale, width, height, stride, samples = msg
                image = QImage(samples, width, height, stride, QImage.Format.Format_RGB888).copy()
                self.page_ready.emit(gen, page, scale, image)
            elif msg[0] == "error":
                self.failed.emit(msg[1], msg[2])
            elif msg[0] == "closed":
                self._on_closed(msg[1])
