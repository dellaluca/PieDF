"""Controllo end-to-end della copia dei dati da una versione precedente (#64).

    set QT_QPA_PLATFORM=offscreen
    python tests/check_data_import.py
"""
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out"


def main():
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

    import piedf.paths as paths
    from piedf import data_import

    root = OUT / "data_import"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)

    app = QApplication(["t"])
    results = []

    def check(name, ok, info=""):
        results.append(bool(ok))
        print(("OK  " if ok else "FAIL"), name, info, flush=True)

    def png(path: Path, shade: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        image = QImage(40, 20, QImage.Format.Format_ARGB32)
        image.fill(QColor(shade, 0, 0))
        image.save(str(path), "PNG")

    def old_version(base: Path, name: str, shades=(10, 20, 30), mtime=None) -> Path:
        data = base / name / "PieDF" / "data"
        for i, shade in enumerate(shades):
            png(data / "firme" / f"firma_{i}.png", shade)
        (data / "settings.ini").write_text("[print]\nfit=false\n", encoding="utf-8")
        (data / "posizioni.json").write_text("{}", encoding="utf-8")
        if mtime is not None:
            for f in data.rglob("*"):
                if f.is_file():
                    import os
                    os.utime(f, (mtime, mtime))
        return data

    def snapshot(data: Path) -> dict:
        return {str(f.relative_to(data)): (f.read_bytes(), f.stat().st_mtime) for f in data.rglob("*") if f.is_file()}

    # --- 1. ricerca e copia ---
    base = root / "uno"
    old = old_version(base, "PieDF-0.2.0-win64")
    new_exe = base / "PieDF-0.3.0-win64" / "PieDF"
    new_data = new_exe / "data"
    new_data.mkdir(parents=True)
    check("la ricerca trova la versione vecchia", data_import.find_previous(new_exe, new_data) == old)
    check("la propria cartella è esclusa", data_import.find_previous(new_exe, old) is None)
    summary = data_import.summarize(old)
    check("riepilogo", summary.signatures == 3 and summary.settings and summary.positions, summary.describe())
    check("testo del riepilogo", summary.describe() == "3 firme, impostazioni e posizioni di lettura")

    paths.base_dir = lambda: new_exe
    paths.is_frozen = lambda: True
    paths.appdata_dir = lambda: root / "nessun_appdata"

    from piedf.main_window import MainWindow

    asked = []
    answers = ["copy"]
    MainWindow._ask_copy_data = lambda self, source: (asked.append(source), answers.pop(0))[1]

    before = snapshot(old)
    window = MainWindow()
    check("offerta fatta", window.offer_previous_data() is True and asked == [old])
    check("firme copiate", len(list((new_data / "firme").glob("*.png"))) == 3)
    check("settings.ini copiato", (new_data / "settings.ini").read_text(encoding="utf-8") == "[print]\nfit=false\n")
    check("posizioni.json copiato", (new_data / "posizioni.json").is_file())
    check("firme nel menu", len(window.signatures.list()) == 3)
    check("preferenze rilette", window._settings.value("print/fit", True, type=bool) is False)
    check("originali invariati", snapshot(old) == before)
    window.close()

    asked.clear()
    window = MainWindow()
    check("dopo la copia non chiede più", window.offer_previous_data() is False and not asked)
    window.close()

    # --- 2. No: non lo chiede più ---
    shutil.rmtree(new_data)
    new_data.mkdir()
    answers[:] = ["no"]
    asked.clear()
    window = MainWindow()
    check("No: niente copia", window.offer_previous_data() is False and len(asked) == 1)
    check("No: nessuna firma", not window.signatures.list())
    window.close()
    asked.clear()
    window = MainWindow()
    check("No: al riavvio non chiede più", window.offer_previous_data() is False and not asked)
    window.close()

    # --- 3. nessuna versione precedente ---
    alone = root / "solo" / "PieDF-0.3.0-win64" / "PieDF"
    paths.base_dir = lambda: alone
    (alone / "data").mkdir(parents=True)
    asked.clear()
    window = MainWindow()
    check("nessuna versione precedente: nessun dialog", window.offer_previous_data() is False and not asked)
    window.close()

    # --- 4. due versioni: la più recente ---
    base = root / "due"
    now = time.time()
    old_version(base, "PieDF-0.1.0-win64", mtime=now - 5000)
    newer = old_version(base, "PieDF-0.2.0-win64", mtime=now - 100)
    mine = base / "PieDF-0.3.0-win64" / "PieDF"
    (mine / "data").mkdir(parents=True)
    check("due versioni: la più recente", data_import.find_previous(mine, mine / "data") == newer)
    # %APPDATA% (cartella data diretta) e versione scompattata senza la cartella PieDF
    appdata = root / "appdata_due"
    png(appdata / "firme" / "firma_a.png", 1)
    check("%APPDATA% come candidata", data_import.find_previous(alone, alone / "data", appdata) == appdata)
    flat = root / "piatto"
    png(flat / "vecchia" / "data" / "firme" / "iniziali_x.png", 5)
    (flat / "nuova").mkdir()
    check("X/*/data", data_import.find_previous(flat / "nuova", flat / "nuova" / "data") == flat / "vecchia" / "data")

    # --- risoluzione della cartella scelta a mano ---
    pieDF = old.parent
    check("resolve: cartella dell'exe", data_import.resolve_source(pieDF) == old)
    check("resolve: data", data_import.resolve_source(old) == old)
    check("resolve: data/firme", data_import.resolve_source(old / "firme") == old)
    check("resolve: cartella vuota", data_import.resolve_source(root / "solo") is None)

    # --- 5. Importa firme ---
    from piedf.signature_manager import SignatureManager
    from piedf.signatures import SignatureStore

    mydata = root / "mie"
    (mydata / "firme").mkdir(parents=True)
    shutil.copy2(old / "firme" / "firma_0.png", mydata / "firme" / "firma_zzz.png")
    store = SignatureStore(mydata / "firme")
    manager = SignatureManager(store)
    QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: str(pieDF))
    messages = []
    QMessageBox.information = staticmethod(lambda parent, title, text, *a, **k: messages.append(text))
    manager.import_signatures()
    check("importa: messaggio", messages == ["Importate 2 firme, 1 era già presente."], str(messages))
    check("importa: 3 firme in tutto", len(store.list()) == 3)
    messages.clear()
    manager.import_signatures()
    check("importa di nuovo: niente di nuovo", messages == ["Importate 0 firme, 3 erano già presenti."], str(messages))
    check("importa: lista aggiornata", sum(w.count() for w in manager.lists.values()) == 3 + 1)  # 3 firme + segnaposto iniziali
    check("importa: originali invariati", snapshot(old) == before)
    manager.close()

    # --- 6. limiti della ricerca ---
    big = root / "grande"
    mine = big / "z" / "PieDF"
    (mine / "data").mkdir(parents=True)
    for i in range(5000):
        (big / f"c{i:05d}").mkdir()
    calls = []
    real_valid = data_import.is_valid
    data_import.is_valid = lambda d: (calls.append(d), real_valid(d))[1]
    start = time.perf_counter()
    data_import.find_previous(mine, mine / "data")
    elapsed = time.perf_counter() - start
    data_import.is_valid = real_valid
    check("5000 sottocartelle: entro 200 ms", elapsed < 0.4, f"{elapsed * 1000:.0f} ms")
    check("5000 sottocartelle: non legge oltre il limite", len(calls) <= 2 * data_import.MAX_ENTRIES, f"{len(calls)} cartelle")

    print(f"{sum(results)}/{len(results)} ok")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
