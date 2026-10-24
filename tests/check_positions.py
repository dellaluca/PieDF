"""Controllo end-to-end della posizione di lettura ricordata per ogni file (#23).

    python tests/make_test_pdfs.py
    set QT_QPA_PLATFORM=offscreen
    python tests/check_positions.py
"""
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out"
PDFS = S / "pdfs"


def main():
    import pymupdf
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

    import piedf.paths as paths
    data = S / "data_posizioni"
    work = S / "posizioni"
    for d in (data, work):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    paths.data_dir = lambda: data
    posfile = data / "posizioni.json"

    from piedf.main_window import MainWindow
    from piedf.placements import Placement
    from piedf.positions import Positions, file_key
    from piedf.signatures import Kind
    from piedf.viewer import FitMode

    app = QApplication(["t"])
    results = []

    def check(name, ok, info=""):
        results.append(bool(ok))
        print(("OK  " if ok else "FAIL"), name, info, flush=True)

    def pump(sec=0.05):
        t = time.perf_counter()
        while time.perf_counter() - t < sec:
            app.processEvents(); time.sleep(0.01)

    def copy(name, new_name=None):
        dest = work / (new_name or name)
        shutil.copy(PDFS / name, dest)
        return str(dest)

    def window(width=1000, height=800):
        w = MainWindow(); w.resize(width, height); w.show(); pump(0.2)
        return w

    def stored():
        return json.loads(posfile.read_text(encoding="utf-8"))

    lungo = copy("lungo_350.pdf", "lungo.pdf")
    semplice = copy("semplice.pdf")
    w = window()
    v = w.view

    # --- 1. pagina 120, zoom 150%, mezza pagina ---
    w.open_file(lungo)
    check("4. file mai visto: pagina 1, Adatta larghezza", v.current_page == 0 and v.fit_mode == FitMode.WIDTH)
    v.set_zoom(1.5)
    v.set_scroll_position(119, 421.0)
    pump(0.1)
    before = v.scroll_position()
    w.open_file(semplice)
    check("1. aprendo un altro file la posizione è salvata", file_key(lungo) in stored(), str(list(stored())))
    check("1. nuovo file: pagina 1", v.current_page == 0 and v.fit_mode == FitMode.WIDTH)
    w.open_file(lungo); pump(0.1)
    after = v.scroll_position()
    check("1. riaperto: pagina 120, zoom 150%, stessa posizione",
          after[0] == 119 == before[0] and abs(after[1] - before[1]) < 2 and abs(v.zoom - 1.5) < 1e-9
          and v.fit_mode == FitMode.NONE, f"{before} → {after}, zoom {v.zoom}")
    check("1. la pagina mostrata è la 120", v.current_page == 119 and w._page_box.value() == 120)

    # --- 2. finestra ridimensionata ---
    w.close_document()
    w.resize(700, 500); pump(0.2)
    w.open_file(lungo); pump(0.1)
    after = v.scroll_position()
    check("2. finestra ridimensionata: stessa pagina e posizione in punti",
          after[0] == 119 and abs(after[1] - before[1]) < 2, f"{before} → {after}")
    w.resize(1000, 800); pump(0.2)

    # --- 3. Adatta pagina e Adatta larghezza ---
    v.set_fit_mode(FitMode.PAGE); pump(0.1)
    w.open_file(semplice)
    w.open_file(lungo); pump(0.1)
    check("3. Adatta pagina ricordato", v.fit_mode == FitMode.PAGE and stored()[file_key(lungo)]["fit"] == "page",
          str(v.fit_mode))
    v.set_fit_mode(FitMode.WIDTH)
    w.open_file(semplice)
    w.open_file(lungo); pump(0.1)
    check("3. Adatta larghezza ricordato (non la percentuale)", v.fit_mode == FitMode.WIDTH)

    # --- 5. il file è cambiato e ha meno pagine ---
    w.close_document()
    Positions(posfile).set(lungo, 299, 100.0, None, 1.0)
    check("5. posizione salvata a pagina 300", stored()[file_key(lungo)]["pagina"] == 299)
    short = pymupdf.open()
    for _ in range(10):
        short.new_page()
    short.save(lungo); short.close()
    w.close(); w = window(); v = w.view
    ok = w.open_file(lungo); pump(0.1)
    check("5. file di 10 pagine: si apre all'ultima, senza errori", ok and v.current_page == 9 and v.page_count == 10,
          f"pagina {v.current_page + 1}")
    w.close_document()
    shutil.copy(PDFS / "lungo_350.pdf", lungo)

    # --- 6. Ctrl+W, uscita, riavvio ---
    w.open_file(lungo)
    v.set_zoom(2.0); v.set_scroll_position(49, 300.0); pump(0.1)
    expected = v.scroll_position()
    QTest.keyClick(v, Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier); app.processEvents()
    check("6. Ctrl+W salva la posizione", w._path is None and stored()[file_key(lungo)]["pagina"] == 49,
          str(stored()[file_key(lungo)]))
    w.close(); app.processEvents()
    w = window(); v = w.view
    w.open_file(lungo); pump(0.1)
    check("6. dopo il riavvio si riapre lì", v.scroll_position()[0] == 49 and abs(v.scroll_position()[1] - expected[1]) < 2
          and abs(v.zoom - 2.0) < 1e-9, f"{v.scroll_position()}")
    v.set_scroll_position(77, 50.0); pump(0.1)
    expected = v.scroll_position()
    w.close(); app.processEvents()
    w = window(); v = w.view
    w.open_file(lungo); pump(0.1)
    check("6. uscita dalla finestra salva la posizione", v.scroll_position()[0] == 77
          and abs(v.scroll_position()[1] - expected[1]) < 2, f"{v.scroll_position()}")

    # --- Timer: salva senza chiudere, solo se è cambiato ---
    v.set_scroll_position(10, 20.0); pump(0.1)
    check("timer: ogni 30 s", w._position_timer.interval() == 30_000 and w._position_timer.isActive())
    w._save_position(force=False)
    check("timer: salva la posizione corrente", stored()[file_key(lungo)]["pagina"] == 10)
    mtime = posfile.stat().st_mtime_ns
    w._save_position(force=False)
    check("timer: non riscrive se nulla è cambiato", posfile.stat().st_mtime_ns == mtime)

    # --- 7. 600 file ---
    w.close_document()
    pdfs = [work / f"f{i:03d}.pdf" for i in range(600)]
    base = pymupdf.open(); base.new_page(); base.save(pdfs[0]); base.close()
    for p in pdfs[1:]:
        shutil.copy(pdfs[0], p)
    for p in pdfs:
        w.open_file(str(p))
    w.close_document()
    keys = list(stored())
    check("7. 600 file aperti: ne restano 500", len(keys) == 500, str(len(keys)))
    check("7. restano i più recenti", file_key(str(pdfs[-1])) in keys and file_key(str(pdfs[100])) in keys
          and file_key(str(pdfs[99])) not in keys and file_key(str(pdfs[0])) not in keys)

    # --- 8. JSON corrotto ---
    w.close(); app.processEvents()
    posfile.write_text("{non è json", encoding="utf-8")
    w = window(); v = w.view
    ok = w.open_file(lungo); pump(0.1)
    check("8. JSON corrotto: nessun errore, pagina 1", ok and v.current_page == 0 and v.fit_mode == FitMode.WIDTH)
    w.close_document()
    check("8. il file viene riscritto valido", isinstance(stored(), dict) and file_key(lungo) in stored())
    posfile.write_text("[1, 2]", encoding="utf-8")
    w2 = window()
    check("8. JSON di tipo sbagliato ignorato", stored() == {})
    w2.close()
    posfile.write_text(json.dumps({"x": {"pagina": "a"}, "y": 3}), encoding="utf-8")
    w2 = window()
    check("8. voci non valide scartate", w2._positions.get("x") is None)
    w2.close()
    check("8. niente .tmp lasciato", not list(data.glob("*.tmp")))

    w.close(); app.processEvents()

    # --- 9. Dimentica le posizioni ---
    w = window(); v = w.view
    w.open_file(lungo); w.open_file(semplice)
    recent_before = w._recent()
    w._fill_recent_menu()
    menu_texts = [a.text() for a in w._recent_menu.actions()]
    check("9. voce di menu presente", "Dimentica le posizioni" in menu_texts, str(menu_texts))
    next(a for a in w._recent_menu.actions() if a.text() == "Dimentica le posizioni").trigger()
    check("9. il file è vuoto, i recenti restano",
          stored() == {} and w._recent() == recent_before and len(recent_before) >= 2)
    w.close_document()

    # --- 10. dopo un salvataggio con firma la posizione resta ---
    saved = []
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (saved.pop(0), ""))
    QMessageBox.critical = staticmethod(lambda *a, **k: print("ERRORE", a))
    img = QImage(600, 200, QImage.Format.Format_ARGB32)
    img.fill(QColor("#1a237e"))
    sig = w.signatures.add(img, Kind.SIGNATURE)
    w.open_file(lungo)
    v.set_zoom(1.5); v.set_scroll_position(4, 150.0); pump(0.1)
    expected = v.scroll_position()
    w.placements.add(Placement(4, QRectF(300, 400, 150, 50), sig.path, Kind.SIGNATURE))
    dest = work / "lungo_firmato.pdf"
    saved.append(str(dest))
    w.save(); pump(0.1)
    now = v.scroll_position()
    check("10. salvato", w._path == str(dest) and dest.exists(), str(w._path))
    check("10. la posizione corrente resta", now[0] == expected[0] and abs(now[1] - expected[1]) < 2
          and abs(v.zoom - 1.5) < 1e-9 and v.fit_mode == FitMode.NONE, f"{expected} → {now}")
    w.close_document()
    w.close(); app.processEvents()

    print(f"\n{sum(results)}/{len(results)} ok", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
