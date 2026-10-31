"""Controllo end-to-end della lettura dei font installati (#59): mai all'avvio, in un processo
a parte, con la cache su disco.

    python tests/make_test_pdfs.py
    set QT_QPA_PLATFORM=offscreen
    python tests/check_fonts.py
"""
import json
import os
import shutil
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out"
PDFS = S / "pdfs"
FAKE_FONTS = 300
NAME_RECORDS = 2000  # record della tabella name di ogni font finto: la lettura costa come un font vero
STARTUP_RUNS = 3


def fake_font(path: Path, family: str) -> None:
    """Un .ttf finto ma leggibile da `fonts.font_info`: OS/2, head, name (molte lingue), glyf vuota."""
    os2 = bytearray(64)
    os2[4:6] = struct.pack(">H", 400)  # usWeightClass
    os2[62:64] = struct.pack(">H", 0x40)  # fsSelection: REGULAR (fsType 0: si può incorporare)
    head = bytes(54)
    fam, style = family.encode("utf-16-be"), "Regular".encode("utf-16-be")
    records = b""
    for i in range(NAME_RECORDS // 2):
        records += struct.pack(">6H", 3, 1, 0x400 + i, 1, len(fam), 0)
        records += struct.pack(">6H", 3, 1, 0x400 + i, 2, len(style), len(fam))
    count = NAME_RECORDS // 2 * 2
    name = struct.pack(">3H", 0, count, 6 + 12 * count) + records + fam + style
    tables = [(b"OS/2", bytes(os2)), (b"glyf", bytes(4)), (b"head", head), (b"name", name)]
    offset = 12 + 16 * len(tables)
    directory, body = b"", b""
    for tag, data in tables:
        directory += struct.pack(">4sIII", tag, 0, offset + len(body), len(data))
        body += data + bytes(-len(data) % 4)
    path.write_bytes(struct.pack(">IHHHH", 0x00010000, len(tables), 0, 0, 0) + directory + body)


def main():
    import pymupdf
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    import piedf.paths as paths
    root = S / "font_installati"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    data = [root / "dati"]
    data[0].mkdir()
    paths.data_dir = lambda: data[0]

    from piedf import fonts
    from piedf.document import TextBox, add_text_box, embed_fonts
    from piedf.main_window import FONTS_LOADING, MainWindow

    app = QApplication(["t"])
    results = []

    def check(name, ok, info=""):
        results.append(bool(ok))
        print(("OK  " if ok else "FAIL"), name, info, flush=True)

    def pump(sec=0.05):
        t = time.perf_counter()
        while time.perf_counter() - t < sec:
            app.processEvents(); time.sleep(0.005)

    def wait_fonts(limit=60):
        t = time.perf_counter()
        while not fonts.ready() and time.perf_counter() - t < limit:
            app.processEvents(); time.sleep(0.005)
        return fonts.ready()

    def reset(dirs, fresh_data=True):
        """Come a un avvio nuovo: niente elenco in memoria, `font_dirs` sostituita."""
        fonts.shutdown()
        fonts._installed = None
        fonts._faces.clear()
        fonts.font_dirs = lambda: list(dirs)
        if fresh_data:
            data[0] = root / f"dati_{time.perf_counter_ns()}"
            data[0].mkdir()

    real_dirs = fonts.font_dirs()

    # --- Un PDF con una casella in un font installato (aprendolo si chiede l'elenco) ---
    real = fonts.scan(real_dirs)
    choice = next((f for f in ("Georgia", "Segoe UI", "Lato", "DejaVu Serif", "Liberation Serif", "DejaVu Sans")
                   if f in real), None)
    check("un font installato da provare", choice is not None, str(sorted(real)[:20]))
    if choice is None:
        print(f"\n{sum(results)}/{len(results)} ok", flush=True)
        return 1
    with_box = root / "casella_font.pdf"
    fonts._installed = {choice: real[choice]}
    doc = pymupdf.open(PDFS / "semplice.pdf")
    box = TextBox(0, (100, 300, 300, 320), text="Prova font", font=choice, size=12)
    add_text_box(doc[0], box, embed_fonts(doc, [box]).get(id(box), 0))
    doc.save(with_box)
    doc.close()

    # --- 300 font finti ---
    folder = root / "font"
    folder.mkdir()
    for i in range(FAKE_FONTS):
        fake_font(folder / f"finto{i:03}.ttf", f"Finto {i:03}")
    empty = root / "vuota"
    empty.mkdir()
    t = time.perf_counter()
    scanned = fonts.scan([folder])
    scan_s = time.perf_counter() - t
    check("300 font finti letti da scan", len(scanned) == FAKE_FONTS, f"{len(scanned)} famiglie")
    print(f"INFO lettura di 300 font finti nel processo della GUI: {scan_s * 1000:.0f} ms", flush=True)

    # --- 1. Avvio senza documento: nessuna lettura ---
    reset([folder])
    w = MainWindow(); w.show(); app.processEvents()
    check("1. avvio: i font non si leggono", fonts._installed is None and not fonts.loading()
          and not (data[0] / fonts.CACHE_FILE).exists())
    w.open_file(str(PDFS / "semplice.pdf"))
    pump(0.2)
    check("1. documento senza caselle: i font non si leggono", fonts._installed is None and not fonts.loading())

    # --- 2. Modalità Testo: si chiede l'elenco, la tendina dice «Caricamento dei font…» ---
    w.act_text.trigger()
    items = [w.font_box.itemText(i) for i in range(w.font_box.count())]
    check("2. modalità Testo: lettura avviata in un processo", fonts.loading() and fonts._job[0].is_alive())
    check("2. intanto la tendina ha i tre standard e «Caricamento dei font…»",
          [w.font_box.itemData(i) for i in range(w.font_box.count()) if w.font_box.itemData(i)][-3:] == ["Helv", "TiRo", "Cour"]
          and items[-1] == FONTS_LOADING, str(items))
    check("2. l'elenco arriva", wait_fonts() and len(fonts.installed()) == FAKE_FONTS, str(len(fonts._installed or {})))
    pump(0.5)  # il timer della tendina
    items = [w.font_box.itemText(i) for i in range(w.font_box.count())]
    check("2. la tendina non dice più «Caricamento dei font…»", FONTS_LOADING not in items, str(items[-3:]))
    cache_file = data[0] / fonts.CACHE_FILE
    cached = fonts.load_cache(cache_file)
    check("2. cache scritta dal processo in fonts.json", len(cached) == FAKE_FONTS
          and all(info is not None and info.families[0][0].startswith("Finto") for _, _, info in cached.values()))
    w.view.cancel_text_tool()
    w._renderer.shutdown(); w.close(); w.deleteLater(); app.processEvents()

    # --- 3. Avvio fino a open_file: 300 font non lo rallentano ---
    def startup(dirs):
        reset(dirs)
        t = time.perf_counter()
        win = MainWindow(); win.show()
        ok = win.open_file(str(with_box))
        app.processEvents()
        elapsed = time.perf_counter() - t
        loading = fonts.loading()
        return win, ok, elapsed, loading

    w, *_ = startup([empty])  # riscaldamento: la prima finestra paga import e cache di Qt
    wait_fonts()
    w._renderer.shutdown(); w.close(); w.deleteLater(); app.processEvents()
    times = {"senza": [], "con": []}
    for run in range(STARTUP_RUNS):
        for label, dirs in (("senza", [empty]), ("con", [folder])):
            w, ok, elapsed, loading = startup(dirs)
            times[label].append(elapsed)
            if label == "con" and run == 0:
                check("3. documento aperto, titolo col nome del file", ok and "casella_font.pdf" in w.windowTitle(),
                      w.windowTitle())
                check("3. casella con un font non standard: lettura avviata all'apertura", loading)
                # --- 4. Il ciclo degli eventi non si ferma mentre il processo legge i font ---
                ticks = []
                timer = QTimer(); timer.setInterval(10); timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
                timer.start()
                t0 = time.perf_counter()
                done = wait_fonts()
                pump(0.3)  # anche il riempimento della tendina
                timer.stop()
                gaps = [b - a for a, b in zip(ticks, ticks[1:])]
                check("4. font letti mentre la GUI gira", done and len(fonts.installed()) == FAKE_FONTS,
                      f"{time.perf_counter() - t0:.2f} s")
                check("4. pausa massima della GUI < 150 ms", gaps and max(gaps) < 0.150,
                      f"{max(gaps, default=0) * 1000:.0f} ms su {len(gaps)} intervalli")
            else:
                wait_fonts()
            w._renderer.shutdown(); w.close(); w.deleteLater(); app.processEvents()
    without, with_fonts = min(times["senza"]), min(times["con"])
    check("3. avvio fino a open_file: con 300 font al massimo 100 ms in più", with_fonts - without < 0.100,
          f"senza {without * 1000:.0f} ms, con {with_fonts * 1000:.0f} ms")

    # --- 5. Cache su disco: si rileggono solo i file nuovi o cambiati ---
    calls = []
    original_info = fonts.font_info
    fonts.font_info = lambda path: calls.append(path) or original_info(path)
    try:
        cache_file = root / "cache_prova.json"
        first = fonts.scan_cached([folder], cache_file)
        check("5. primo giro: letti tutti", len(calls) == FAKE_FONTS and len(first) == FAKE_FONTS, str(len(calls)))
        stamp = cache_file.stat().st_mtime_ns
        calls.clear()
        time.sleep(0.05)
        second = fonts.scan_cached([folder], cache_file)
        check("5. secondo giro: nessun file riletto, cache non riscritta", not calls and second == first
              and cache_file.stat().st_mtime_ns == stamp, str(len(calls)))
        changed = folder / "finto000.ttf"
        fake_font(changed, "Cambiato 000")
        os.utime(changed, ns=(time.time_ns(), time.time_ns() + 10_000_000))
        (folder / "finto001.ttf").unlink()
        calls.clear()
        third = fonts.scan_cached([folder], cache_file)
        cached = fonts.load_cache(cache_file)
        check("5. file cambiato: riletto solo quello", calls == [str(changed)] and "Cambiato 000" in third
              and "Finto 000" not in third, str(calls))
        check("5. file sparito: esce dalla cache e dall'elenco", "Finto 001" not in third
              and str(folder / "finto001.ttf") not in cached and len(cached) == FAKE_FONTS - 1)
        cache_file.write_text("{ non è JSON", encoding="utf-8")
        calls.clear()
        fourth = fonts.scan_cached([folder], cache_file)
        check("5. JSON illeggibile = cache vuota: letti tutti di nuovo", len(calls) == FAKE_FONTS - 1 and fourth == third)
        check("5. e riscritto valido", json.loads(cache_file.read_text(encoding="utf-8")).get("versione") == fonts.CACHE_VERSION)
        cache_file.write_text(json.dumps({"versione": fonts.CACHE_VERSION, "font": {"x": [1, 2, {"families": 3}]}}),
                              encoding="utf-8")
        check("5. contenuto non valido = cache vuota", fonts.load_cache(cache_file) == {})
        check("5. nessun temporaneo rimasto", not list(root.glob("cache_prova.json.*")))
    finally:
        fonts.font_info = original_info

    # --- 6. Il processo che legge i font si chiude all'uscita ---
    reset([folder])
    fonts.request()
    process = fonts._job[0]
    fonts.shutdown()
    check("6. shutdown chiude il processo", not process.is_alive() and fonts._job is None and fonts._installed is None)
    fonts.request()
    check("6. dopo shutdown si può chiedere di nuovo", fonts.loading() and wait_fonts())
    reset([empty])
    fonts.request()
    process = fonts._job[0]
    app.aboutToQuit.emit()
    check("6. aboutToQuit chiude il processo", not process.is_alive() and fonts._job is None)

    fonts.font_dirs = lambda: real_dirs
    print(f"\n{sum(results)}/{len(results)} ok", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
