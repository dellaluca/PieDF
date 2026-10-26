"""Stampa (#25): impaginazione sul foglio, opzioni e lavoro di stampa.

Le pagine le disegna un processo di render dedicato (`render_worker.print_pages`), alla
risoluzione della stampante (massimo 600 dpi). Un thread le riceve e le disegna con
`QPainter` sul `QPrinter`: Qt permette di dipingere su un QPrinter fuori dal thread della
GUI, così la compressione delle immagini grandi non blocca l'interfaccia. I pixel passano
per la memoria condivisa (`PRINT_AHEAD` slot creati qui), non per la coda (#34).
"""

import math
import multiprocessing
import os
import queue
import tempfile
import time
from dataclasses import dataclass
from multiprocessing import shared_memory

import pymupdf
from PySide6.QtCore import QCoreApplication, QObject, QPointF, QRect, QRectF, QThread, Signal
from PySide6.QtGui import QImage, QPageLayout, QPainter
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QRadioButton,
    QVBoxLayout,
)

from piedf import render_worker
from piedf.writer import save_with_signatures

MAX_DPI = 600
NO_PRINT_MESSAGE = "L'autore di questo documento non permette la stampa."
# Strisce di pagina sul QPrinter (vedi _draw): drawImage tiene il GIL per tutta la striscia.
BAND_BYTES = 2**19  # la striscia più grande (~4 ms su un PC veloce)
BAND_S = 0.008  # durata cercata di una striscia: sulle macchine lente le righe diminuiscono
BAND_MIN_ROWS = 16
YIELD_S = 0.001  # pausa dopo ogni striscia, vedi _draw
PROCESS_EXIT_S = 5.0  # attesa massima perché il processo di stampa esca
POLL_S = 0.2  # ogni quanto il thread di stampa controlla Annulla mentre aspetta una pagina


@dataclass
class PrintOptions:
    fit: bool = True  # Adatta alla pagina; False = Dimensione reale
    auto_rotate: bool = True


def can_print(path: str, password: str | None) -> bool:
    """False se i permessi del documento vietano la stampa."""
    doc = pymupdf.open(path)
    try:
        if doc.needs_pass:
            doc.authenticate(password or "")
        return bool(doc.permissions & pymupdf.PDF_PERM_PRINT)
    finally:
        doc.close()


def print_source(
    src: str, password: str | None, placements: list, streams: dict, fields: list = (),
    annotations: tuple | None = None,
) -> tuple[str, str | None]:
    """File da stampare e temporaneo da cancellare dopo (None se si stampa `src` così com'è).

    Con firme, campi modulo o caselle di testo non salvati si scrivono in un temporaneo con
    lo stesso writer del salvataggio, così si stampa quello che si vede. Solleva `SaveError`.
    """
    if not placements and not fields and annotations is None:
        return src, None
    fd, tmp = tempfile.mkstemp(prefix="piedf-stampa-", suffix=".pdf")
    os.close(fd)
    try:
        save_with_signatures(src, password, placements, tmp, streams, fields, annotations)
    except Exception:
        remove_quietly(tmp)
        raise
    return tmp, tmp


def remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def layout(page: tuple[float, float], area: tuple[float, float], options: PrintOptions) -> tuple[bool, float]:
    """(ruotata di 90°, scala) di una pagina in punti dentro l'area stampabile in punti."""
    w, h = page
    aw, ah = area
    # Orizzontale su un foglio verticale (o il contrario): si ruota invece di rimpicciolire.
    rotated = options.auto_rotate and w != h and aw != ah and (w > h) != (aw > ah)
    if rotated:
        w, h = h, w
    scale = min(aw / w, ah / h) if options.fit else 1.0
    return rotated, scale


def selected_pages(printer: QPrinter, page_count: int, current: int) -> list[int]:
    """Pagine (da 0) scelte nel dialog di stampa, nell'ordine in cui si stampano."""
    Range = QPrinter.PrintRange
    if printer.printRange() == Range.CurrentPage:
        return [current]
    if printer.printRange() != Range.PageRange:
        return list(range(page_count))
    ranges = [(r.from_, r.to) for r in printer.pageRanges().toRangeList()]
    if not ranges and printer.fromPage() > 0:
        ranges = [(printer.fromPage(), printer.toPage())]
    pages = []
    for first, last in ranges:
        pages += range(max(first, 1) - 1, min(last, page_count))
    return pages


class PrintOptionsDialog(QDialog):
    """Le opzioni di PieDF, prima del dialog di stampa di Windows (che non si può estendere)."""

    def __init__(self, options: PrintOptions, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Stampa")
        box = QGroupBox("Dimensione")
        self._fit = QRadioButton("&Adatta alla pagina")
        self._actual = QRadioButton("&Dimensione reale")
        (self._fit if options.fit else self._actual).setChecked(True)
        size_layout = QVBoxLayout(box)
        size_layout.addWidget(self._fit)
        size_layout.addWidget(self._actual)
        self._rotate = QCheckBox("&Orientamento automatico")
        self._rotate.setToolTip("Ruota le pagine orizzontali sul foglio verticale invece di rimpicciolirle")
        self._rotate.setChecked(options.auto_rotate)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Continua…")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Annulla")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout_ = QVBoxLayout(self)
        layout_.addWidget(box)
        layout_.addWidget(self._rotate)
        layout_.addWidget(buttons)

    def options(self) -> PrintOptions:
        return PrintOptions(fit=self._fit.isChecked(), auto_rotate=self._rotate.isChecked())


class PrintJob(QObject):
    """Una stampa in corso: processo di render dedicato + thread che disegna sul QPrinter."""

    progress = Signal(int, int)  # pagine stampate, totale
    finished = Signal(bool, str)  # completata; messaggio d'errore ("" se completata o annullata)

    def __init__(
        self, printer: QPrinter, path: str, password: str | None, pages: list[int],
        page_sizes: list[tuple[float, float]], options: PrintOptions, temp: str | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._temp = temp
        dpi = min(printer.resolution(), MAX_DPI)
        area = printer.pageLayout().paintRect(QPageLayout.Unit.Point).size()
        placed = [(p, *layout(page_sizes[p], (area.width(), area.height()), options)) for p in pages]
        if not printer.supportsMultipleCopies() and printer.copyCount() > 1:
            placed *= printer.copyCount()  # copie fascicolate fatte qui (es. stampa su PDF)
        items = [(p, scale * dpi / 72) for p, _, scale in placed]
        # RGB della pagina più grande, con un pixel di margine per lato per gli arrotondamenti.
        size = max((math.ceil(page_sizes[p][0] * s) + 1) * 3 * (math.ceil(page_sizes[p][1] * s) + 1) for p, s in items)
        self._slots = [shared_memory.SharedMemory(create=True, size=size) for _ in range(render_worker.PRINT_AHEAD)]

        ctx = multiprocessing.get_context("spawn")
        self._acks = ctx.Queue()
        self._results = ctx.Queue()
        self._process = ctx.Process(
            target=render_worker.print_pages,
            args=(path, password, items, [shm.name for shm in self._slots], self._acks, self._results), daemon=True,
        )
        self._thread = _PrintThread(printer, placed, page_sizes, (area.width(), area.height()), self)
        self._thread.progress.connect(self.progress)
        self._thread.finished.connect(self._on_finished)
        app = QCoreApplication.instance()
        if app is not None:  # se l'applicazione esce senza closeEvent: mai un QThread vivo all'uscita
            app.aboutToQuit.connect(self._on_quit)

    @property
    def total(self) -> int:
        return len(self._thread.placed)

    def start(self) -> None:
        self._process.start()
        self._thread.start()

    def cancel(self) -> None:
        """Si ferma alla prossima striscia della pagina in corso; la stampa iniziata viene interrotta (`abort`)."""
        self._thread.cancelled = True

    def wait(self) -> None:
        """Aspetta la fine e pulisce subito (all'uscita il segnale `finished` non arriverebbe più)."""
        self._thread.wait()
        self._cleanup()

    def _on_quit(self) -> None:
        if self._thread.isRunning():
            self.cancel()
            self.wait()

    def _on_finished(self) -> None:
        self._cleanup()
        self.finished.emit(self._thread.completed, self._thread.error)

    def _cleanup(self) -> None:
        if self._process.is_alive():  # annullata o errore: il processo sta ancora disegnando
            self._process.terminate()
            self._process.join(PROCESS_EXIT_S)
        for shm in self._slots:
            try:
                shm.close()
            except BufferError:  # un'immagine ancora viva dopo un errore: la libera il GC
                pass
            try:
                shm.unlink()  # su Windows non fa niente: la memoria va via con l'ultimo handle
            except OSError:
                pass
        self._slots = []
        if self._temp is not None:
            remove_quietly(self._temp)
            self._temp = None


class _PrintThread(QThread):
    progress = Signal(int, int)

    def __init__(self, printer, placed, page_sizes, area, job: PrintJob) -> None:
        super().__init__()
        self.printer = printer
        self.placed = placed  # [(pagina, ruotata, scala)]
        self.page_sizes = page_sizes
        self.area = area
        self.job = job
        self.cancelled = False
        self.completed = False
        self.error = ""
        self.band_bytes = BAND_BYTES  # dimensione delle strisce, adattata in _draw
        # [(passo, inizio, fine)] in secondi di perf_counter: per capire dai controlli quale
        # passo tiene il GIL e blocca la GUI ("render" è nel processo di stampa, senza GIL).
        self.steps: list[tuple[str, float, float]] = []

    def _step(self, name: str, start: float) -> None:
        self.steps.append((name, start, time.perf_counter()))

    def run(self) -> None:
        printer = self.printer
        painter = QPainter()
        if not painter.begin(printer):
            self.error = "Impossibile avviare la stampa sulla stampante scelta."
            return
        painter.scale(printer.resolution() / 72, printer.resolution() / 72)  # da qui in punti
        total = len(self.placed)
        try:
            for n, (page, rotated, scale) in enumerate(self.placed):
                t = time.perf_counter()
                msg = self._next_page()
                self._step("ricezione", t)
                if msg is None:
                    break
                if msg[0] == "error":
                    self.error = msg[1]
                    break
                _, _, width, height, stride, render_s = msg
                self.steps.append(("render", t - render_s, t))  # circa: prima della richiesta
                t = time.perf_counter()
                pixels = self.job._slots[n % render_worker.PRINT_AHEAD].buf[:stride * height]
                image = QImage(pixels, width, height, stride, QImage.Format.Format_RGB888)
                self._step("immagine", t)
                if n:
                    t = time.perf_counter()
                    printer.newPage()
                    self._step("newPage", t)
                self._draw(painter, image, self.page_sizes[page], rotated, scale)
                del image  # prima di liberare lo slot: il processo ci scrive la pagina dopo
                pixels.release()
                if self.cancelled:  # pagina interrotta a metà: tanto la stampa si annulla
                    break
                self.job._acks.put(n)
                self.progress.emit(n + 1, total)
            else:
                self.completed = True
        finally:
            if not self.completed:
                printer.abort()
            t = time.perf_counter()
            painter.end()
            self._step("end", t)
        if self.completed:
            # Il processo ha finito e sta uscendo: si aspetta qui, non nel thread della GUI.
            self.job._process.join(PROCESS_EXIT_S)

    def _next_page(self):
        """Il prossimo messaggio del processo; None se annullata o se il processo è morto."""
        while not self.cancelled:
            try:
                return self.job._results.get(timeout=POLL_S)
            except queue.Empty:
                if not self.job._process.is_alive() and self.job._results.empty():
                    return ("error", "Il motore di rendering si è interrotto durante la stampa.")
        return None

    def _draw(self, painter: QPainter, image: QImage, size, rotated: bool, scale: float) -> None:
        """La pagina centrata nell'area stampabile, ruotata di 90° in senso orario se serve."""
        w, h = size[0] * scale, size[1] * scale
        painter.save()
        painter.translate(QPointF(self.area[0] / 2, self.area[1] / 2))
        if rotated:
            painter.rotate(90)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        # A strisce: drawImage tiene il GIL (~0,5 s per un A4 a 600 dpi) e la GUI si bloccherebbe.
        # Ogni striscia ha una riga in più, coperta dalla successiva: niente fessure bianche.
        # Le righe seguono la durata dell'ultima striscia (BAND_S), anche da una pagina all'altra.
        bpl = image.bytesPerLine()
        max_rows = max(BAND_MIN_ROWS, BAND_BYTES // bpl)
        rows = min(max_rows, max(BAND_MIN_ROWS, self.band_bytes // bpl))
        k = h / image.height()
        top = 0
        while top < image.height() and not self.cancelled:
            n =min(rows + 1, image.height() - top)
            t = time.perf_counter()
            painter.drawImage(QRectF(-w / 2, -h / 2 + top * k, w, n * k), image, QRect(0, top, image.width(), n))
            self._step("disegno", t)
            top += rows
            spent = self.steps[-1][2] - t
            rows = min(max_rows, max(BAND_MIN_ROWS, rows // 2, min(rows * 2, int(rows * BAND_S / max(spent, 1e-4)))))
            # Una pausa vera e non sleep(0): rilasciando il GIL anche solo per un attimo la GUI che lo
            # aspetta si sveglia subito, invece di aspettare il suo intervallo (5 ms, ~16 su Windows)
            # prima di chiederlo, e intanto la striscia dopo è già partita.
            time.sleep(YIELD_S)
        self.band_bytes = rows * bpl
        painter.restore()
