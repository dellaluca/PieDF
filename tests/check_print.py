"""Controllo end-to-end della stampa (#25): si stampa su PDF con QPrinter e si controlla quel PDF.

    python tests/make_test_pdfs.py
    set QT_QPA_PLATFORM=offscreen
    python tests/check_print.py
"""
import hashlib
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out"
PDFS = S / "pdfs"
SHOTS = S / "shots"


def main():
    import pymupdf
    from PySide6.QtCore import QRectF, Qt, QTimer
    from PySide6.QtGui import QColor, QImage, QPageLayout, QPageRanges, QPageSize, QPainter, QPen
    from PySide6.QtPrintSupport import QPrinter
    from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog, QPushButton

    import piedf.paths as paths
    data = S / "data_stampa"
    work = S / "stampa"
    tmp = S / "tmp_stampa"  # i temporanei della stampa finiscono qui, per vedere se restano
    for d in (data, work, tmp):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    paths.data_dir = lambda: data
    tempfile.tempdir = str(tmp)
    SHOTS.mkdir(parents=True, exist_ok=True)

    from piedf.main_window import MainWindow
    from piedf.placements import Placement
    from piedf.printing import NO_PRINT_MESSAGE, PrintOptions
    from piedf.signatures import Kind

    try:
        import psutil
    except ImportError:
        psutil = None

    app = QApplication(["t"])
    results = []

    def check(name, ok, info=""):
        results.append(bool(ok))
        print(("OK  " if ok else "FAIL"), name, info, flush=True)

    def skip(name, why):
        print("SKIP", name, why, flush=True)

    def pump(sec=0.05):
        t = time.perf_counter()
        while time.perf_counter() - t < sec:
            app.processEvents(); time.sleep(0.01)

    warnings = []
    QMessageBox.warning = lambda parent, title, text, *a: warnings.append((title, text))
    errors = []
    QMessageBox.critical = lambda parent, title, text, *a: errors.append((title, text))

    w = MainWindow(); w.resize(1000, 800); w.show()
    asked = []

    def setup(name, fit=True, auto_rotate=True, ranges=None, dpi=150, current=False):
        """Al posto dei due dialog: stampa su PDF A4 verticale in `work/name`."""
        out = work / name

        def ask(printer):
            asked.append(name)
            printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
            printer.setOutputFileName(str(out))
            printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
            printer.setPageOrientation(QPageLayout.Orientation.Portrait)
            if dpi:
                printer.setResolution(dpi)
            if current:
                printer.setPrintRange(QPrinter.PrintRange.CurrentPage)
            elif ranges:
                printer.setPrintRange(QPrinter.PrintRange.PageRange)
                printer.setPageRanges(QPageRanges.fromString(ranges))
            return PrintOptions(fit=fit, auto_rotate=auto_rotate)
        w._ask_print = ask
        return out

    def wait_print(limit=300.0, on_pump=None):
        t = time.perf_counter()
        while w._print_job is not None and time.perf_counter() - t < limit:
            app.processEvents(); time.sleep(0.01)
            if on_pump:
                on_pump()
        pump(0.1)
        return w._print_job is None

    def images(path):
        """Per ogni pagina dell'uscita: (bbox, transform) della pagina stampata.

        La pagina arriva a strisce (vedi printing._draw): bbox è l'unione delle strisce,
        transform quella della prima (larghezza e rotazione sono le stesse per tutte).
        """
        doc = pymupdf.open(path)
        info = []
        for page in doc:
            bands = page.get_image_info()
            bbox = pymupdf.Rect(bands[0]["bbox"]) if bands else pymupdf.Rect()
            for band in bands[1:]:
                bbox |= pymupdf.Rect(band["bbox"])
            info.append((bbox, bands[0]["transform"] if bands else None))
        doc.close()
        return info

    def mapped(bbox, rect, page_w):
        """Rettangolo in punti della pagina originale → punti del foglio (pagina non ruotata)."""
        s = bbox.width / page_w
        return pymupdf.Rect(bbox.x0 + rect.x0 * s, bbox.y0 + rect.y0 * s, bbox.x0 + rect.x1 * s, bbox.y0 + rect.y1 * s)

    def count(path, page, rect, test, scale=2.0):
        doc = pymupdf.open(path)
        pix = doc[page].get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=rect, alpha=False)
        doc.close()
        n = 0
        for y in range(pix.height):
            for x in range(pix.width):
                if test(pix.pixel(x, y)):
                    n += 1
        return n

    def dark(c):
        return max(c) < 100

    def blue(c):
        return c[2] > 80 and c[2] > c[0] + 50

    def tmp_files():
        return sorted(p.name for p in tmp.iterdir())

    # --- 1. semplice.pdf ---
    w.open_file(str(PDFS / "semplice.pdf"))
    out = setup("semplice.pdf")
    w.print_document()
    check("1. stampa terminata", wait_print())
    info = images(out)
    check("1. una pagina", len(info) == 1 and info[0][1] is not None, str(info))
    bbox = info[0][0]
    title = mapped(bbox, pymupdf.Rect(72, 50, 330, 78), 595)
    n_dark = count(out, 0, title, dark)
    check("1. titolo visibile nel render", n_dark > 200, f"{n_dark} pixel scuri")
    blank = mapped(bbox, pymupdf.Rect(72, 300, 330, 330), 595)
    check("1. bianco fuori dal testo", count(out, 0, blank, dark) == 0)
    check("1. adatta: occupa l'area stampabile", abs(bbox.width - 575) < 2 or abs(bbox.height - 822) < 2, str(bbox))
    check("1. avanzamento chiuso", not [d for d in w.findChildren(QProgressDialog) if d.isVisible()])
    check("1. scelte salvate", w._settings.value("print/fit", type=bool) is True
          and w._settings.value("print/auto_rotate", type=bool) is True)
    doc = pymupdf.open(out); doc[0].get_pixmap().save(str(SHOTS / "stampa_semplice.png")); doc.close()

    # --- 2. Firma piazzata non salvata ---
    img = QImage(600, 200, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img); p.setPen(QPen(QColor("#1a237e"), 14)); p.drawLine(10, 180, 590, 20); p.end()
    sig = w.signatures.add(img, Kind.SIGNATURE)
    src = PDFS / "semplice.pdf"
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    sig_rect = QRectF(300, 400, 150, 50)
    w.placements.add(Placement(0, sig_rect, sig.path, Kind.SIGNATURE))
    out = setup("firma.pdf")
    temps_during = []
    w.print_document()
    temps_during.append(tmp_files())
    check("2. stampa terminata", wait_print())
    check("2. temporaneo creato durante la stampa", len(temps_during[0]) == 1, str(temps_during))
    bbox = images(out)[0][0]
    inside = mapped(bbox, pymupdf.Rect(300, 400, 450, 450), 595)
    outside = mapped(bbox, pymupdf.Rect(300, 470, 450, 520), 595)
    n_in, n_out = count(out, 0, inside, blue), count(out, 0, outside, blue)
    check("2. firma nella stampa, nel punto giusto", n_in > 200 and n_out == 0, f"{n_in} dentro, {n_out} sotto")
    check("2. originale non toccato", hashlib.sha256(src.read_bytes()).hexdigest() == before)
    check("2. temporaneo cancellato", tmp_files() == [], str(tmp_files()))
    check("2. firme ancora da salvare", w.placements.modified and len(w.placements.items) == 1)
    doc = pymupdf.open(out); doc[0].get_pixmap().save(str(SHOTS / "stampa_firma.png")); doc.close()
    w.placements.clear()

    # --- 3. Intervallo "2-4, 10" ---
    w.open_file(str(PDFS / "lungo_350.pdf"))
    out = setup("intervallo.pdf", ranges="2-4, 10")
    w.print_document()
    check("3. stampa terminata", wait_print())
    info = images(out)
    check("3. 4 pagine", len(info) == 4, str(len(info)))

    def band(doc, page, clip, scale):
        return doc[page].get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False)

    def diff(a, b):
        wdt, hgt = min(a.width, b.width), min(a.height, b.height)
        return sum(abs(a.pixel(x, y)[0] - b.pixel(x, y)[0]) for y in range(0, hgt, 2) for x in range(0, wdt, 2))

    orig = pymupdf.open(PDFS / "lungo_350.pdf")
    printed = pymupdf.open(out)
    head = pymupdf.Rect(0, 40, 595, 90)  # dove c'è "Pagina N di 350"
    refs = [band(orig, i, head, 0.5) for i in range(12)]
    order = []
    for i, (bbox, _) in enumerate(info):
        s = bbox.width / 595
        shot = band(printed, i, mapped(bbox, head, 595), 0.5 / s)
        order.append(min(range(12), key=lambda k: diff(shot, refs[k])) + 1)
    orig.close(); printed.close()
    check("3. pagine giuste nell'ordine giusto", order == [2, 3, 4, 10], str(order))

    # Pagina corrente
    w.view.go_to_page(6)
    out = setup("corrente.pdf", current=True)
    w.print_document()
    wait_print()
    check("pagina corrente: 1 pagina", len(images(out)) == 1)

    # --- 4. Orientamento automatico ---
    w.open_file(str(PDFS / "orientamenti.pdf"))
    for auto in (True, False):
        out = setup(f"orientamenti_{'auto' if auto else 'fisso'}.pdf", auto_rotate=auto)
        w.print_document()
        wait_print()
        info = images(out)
        for page, label in ((1, "orizzontale"), (2, "/Rotate 90")):
            bbox, (a, b, c, d, e, f) = info[page]
            rotated = abs(a) < 1e-3 * abs(b)
            fill = bbox.width * bbox.height / (575 * 822)
            if auto:
                check(f"4. {label} ruotata e grande", rotated and fill > 0.95, f"riempie {fill:.2f}")
            else:
                check(f"4. {label} senza rotazione: rimpicciolita", not rotated and fill < 0.6, f"riempie {fill:.2f}")
        bbox, (a, *_) = info[0]
        check(f"4. verticale mai ruotata ({auto})", a > 0)
        if auto:
            doc = pymupdf.open(out); doc[1].get_pixmap().save(str(SHOTS / "stampa_ruotata.png")); doc.close()
            # Testo dritto: "Orizzontale" in alto a sinistra della pagina, ruotata in senso orario → in alto a destra.
            n_right = count(out, 1, pymupdf.Rect(300, 10, 585, 400), dark)
            n_left = count(out, 1, pymupdf.Rect(10, 10, 295, 400), dark)
            check("4. ruotata in senso orario (titolo in alto a destra)", n_right > 100 and n_left == 0,
                  f"{n_right} destra, {n_left} sinistra")

    # --- 5. Dimensione reale ---
    w.open_file(str(PDFS / "semplice.pdf"))
    out = setup("reale.pdf", fit=False)
    w.print_document()
    wait_print()
    bbox, (a, b, *_) = images(out)[0]
    width, height = (a * a + b * b) ** 0.5, bbox.height
    check("5. A4 non scalata (1%)", abs(width - 595) < 5.95 and abs(height - 842) < 8.42, f"{width:.1f} x {height:.1f}")
    check("5. scelta salvata", w._settings.value("print/fit", type=bool) is False)

    # --- 6 e 8. 50 pagine a 600 dpi: GUI reattiva, memoria ---
    w.open_file(str(PDFS / "lungo_350.pdf"))
    out = setup("cinquanta.pdf", ranges="1-50", dpi=None)  # PdfFormat: 1200 dpi → 600
    proc = psutil.Process() if psutil else None
    base = proc.memory_info().rss if proc else 0
    peak = [base]
    ticks = []
    timer = QTimer(); timer.setInterval(10); timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
    labels = set()

    def sample():
        if proc:
            peak[0] = max(peak[0], proc.memory_info().rss)
        for dlg in w.findChildren(QProgressDialog):
            if dlg.isVisible():
                labels.add(dlg.labelText())

    def blame(gap_list):
        """Il passo della stampa (printing._PrintThread.steps) che copre di più la pausa più lunga."""
        if not gap_list:
            return "nessuna pausa"
        a, b = max(gap_list, key=lambda g: g[1] - g[0])
        cover = {}
        for name, s, e in steps:
            if min(b, e) > max(a, s) and name != "render":  # il render è in un altro processo
                cover[name] = cover.get(name, 0) + min(b, e) - max(a, s)
        name = max(cover, key=cover.get, default="nessun passo")
        return f"{(b - a) * 1000:.0f} ms, durante: {name} ({cover.get(name, 0) * 1000:.0f} ms)"

    t0 = time.perf_counter()
    timer.start()
    w.print_document()
    steps = w._print_job._thread.steps
    page_times = {}
    w._print_job.progress.connect(lambda n, total: page_times.setdefault(n, time.perf_counter()))
    done = wait_print(limit=400, on_pump=sample)
    timer.stop()
    secs = time.perf_counter() - t0
    # Fino alla penultima pagina: alla fine QPdfEngine (la "stampante" PDF del test) chiude il
    # file in painter.end() tenendo il GIL; con una stampante vera end() è EndDoc di Windows.
    until = page_times.get(49, time.perf_counter())
    spans = [(a, b) for a, b in zip(ticks, ticks[1:]) if b <= until]
    end_spans = [(a, b) for a, b in zip(ticks, ticks[1:]) if b > until]
    gaps = [b - a for a, b in spans]
    check("6. 50 pagine stampate", done and len(images(out)) == 50, f"{secs:.1f} s")
    check("6. pausa massima della GUI < 150 ms", gaps and max(gaps) < 0.150, blame(spans))
    print(f"INFO pausa massima dopo la pagina 49 (ultima pagina e chiusura del PDF): {blame(end_spans)}", flush=True)
    longest = {}
    for name, s, e in steps:
        if name != "ricezione":  # è soprattutto attesa del render, senza GIL
            longest[name] = max(longest.get(name, 0), e - s)
    print("INFO passo più lungo: " + ", ".join(f"{name} {sec * 1000:.0f} ms" for name, sec in
                                               sorted(longest.items(), key=lambda x: -x[1])), flush=True)
    check("6. avanzamento «Stampa pagina N di 50…»", "Stampa pagina 12 di 50…" in labels, f"{len(labels)} testi")
    if proc:
        grow = (peak[0] - base) / 2**20
        check("8. memoria della GUI < 400 MB in più", grow < 400, f"+{grow:.0f} MB")
    else:
        skip("8. memoria", "psutil non installato")
    out.unlink()  # circa 100 MB

    # --- 6. Annulla dopo 5 pagine ---
    w.placements.add(Placement(0, sig_rect, sig.path, Kind.SIGNATURE))  # così c'è un temporaneo
    out = setup("annullata.pdf", ranges="1-50")
    stopped = []

    def cancel_at_5():
        for dlg in w.findChildren(QProgressDialog):
            if dlg.isVisible() and dlg.value() >= 5 and not stopped:
                stopped.append(dlg.value())
                dlg.findChild(QPushButton).click()  # il pulsante Annulla

    w.print_document()
    job = w._print_job
    last = []
    job.progress.connect(lambda n, total: last.append(n))
    done = wait_print(limit=120, on_pump=cancel_at_5)
    check("6. annullata", done and stopped and "Stampa annullata" in w.statusBar().currentMessage(),
          w.statusBar().currentMessage())
    # +2: quando la GUI vede «5» la 6 può essere già finita (segnale in coda) e la 7 in corso.
    check("6. si ferma dopo la pagina in corso", last and last[-1] <= stopped[0] + 2 and last[-1] < 50,
          f"annullata a {stopped}, ultima {last[-1:]}")
    check("6. nessun temporaneo", tmp_files() == [], str(tmp_files()))
    check("6. processo di stampa chiuso", not job._process.is_alive())
    check("6. nessun errore", not errors, str(errors))
    w.placements.clear()

    # --- 7. Stampa vietata ---
    w.open_file(str(PDFS / "senza_stampa.pdf"))
    out = setup("vietata.pdf")
    n_asked = len(asked)
    w.print_document()
    pump(0.2)
    check("7. messaggio", warnings[-1:] == [("Stampa non consentita", NO_PRINT_MESSAGE)], str(warnings[-1:]))
    check("7. nessuna stampa", len(asked) == n_asked and not out.exists() and w._print_job is None)

    # Azione nel menu e nella toolbar, Ctrl+P
    check("azione Stampa…: Ctrl+P", w.act_print.shortcut().toString() == "Ctrl+P", w.act_print.shortcut().toString())
    w.close_document()
    check("Stampa disattivata senza documento", not w.act_print.isEnabled())
    from PySide6.QtWidgets import QToolBar
    check("Stampa nella toolbar", any(w.act_print in bar.actions() for bar in w.findChildren(QToolBar)))

    # Uscita durante la stampa: niente temporaneo né processo rimasti
    w.open_file(str(PDFS / "lungo_350.pdf"))
    w.placements.add(Placement(0, sig_rect, sig.path, Kind.SIGNATURE))
    setup("uscita.pdf", ranges="1-50")
    w.print_document()
    job = w._print_job
    t = time.perf_counter()
    while not tmp_files() and time.perf_counter() - t < 10:
        pump(0.05)
    had_temp = bool(tmp_files())
    pump(1.0)
    w._confirm_discard = lambda: True
    w.close(); app.processEvents()
    check("uscita durante la stampa: pulito", had_temp and tmp_files() == [] and not job._process.is_alive(),
          str(tmp_files()))

    print(f"\n{sum(results)}/{len(results)} ok", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
