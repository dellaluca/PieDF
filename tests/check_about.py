"""Controllo end-to-end della finestra «Informazioni su PieDF» (#63).

    python tests/make_test_pdfs.py
    set QT_QPA_PLATFORM=offscreen
    python tests/check_about.py
"""
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out"


def main():
    import platform

    import pymupdf
    import PySide6
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QApplication, QMessageBox

    import piedf
    import piedf.paths as paths
    data = S / "data_about"
    shutil.rmtree(data, ignore_errors=True)
    data.mkdir(parents=True)
    paths.data_dir = lambda: data

    from piedf import about_dialog
    from piedf.main_window import MainWindow

    app = QApplication(["t"])
    results = []

    def check(name, ok, info=""):
        results.append(bool(ok))
        print(("OK  " if ok else "FAIL"), name, info, flush=True)

    answers, asked, opened = [], [], []

    def fake_exec(box):
        asked.append(box.text())
        return answers.pop(0)

    QMessageBox.exec = fake_exec
    QDesktopServices.openUrl = staticmethod(lambda url: opened.append(QUrl(url)) or True)

    win = MainWindow()
    win.show()
    app.processEvents()

    # --- Menu e voce ---
    menus = {a.text().replace("&", ""): a.menu() for a in win.menuBar().actions()}
    check("menu Aiuto per ultimo", list(menus)[-1] == "Aiuto", str(list(menus)))
    entry = [a for a in menus["Aiuto"].actions() if "Informazioni su PieDF" in a.text()]
    check("voce «Informazioni su PieDF…»", len(entry) == 1)

    shown = []
    real_exec = about_dialog.AboutDialog.exec
    about_dialog.AboutDialog.exec = lambda self: shown.append(self) or 0
    entry[0].trigger()
    about_dialog.AboutDialog.exec = real_exec
    check("la voce apre la finestra", len(shown) == 1)
    dlg = shown[0]

    # --- Contenuto ---
    text = dlg.text.text()
    full = text + " " + " ".join(l.text() for l in dlg.findChildren(type(dlg.text)))
    source = f"https://github.com/dellaluca/PieDF/tree/v{piedf.__version__}"
    check("versione", piedf.__version__ in full, piedf.__version__)
    check("link al sorgente della versione", source in text, source)
    check("licenza AGPL", "GNU Affero General Public License v3.0" in text)
    check("link alla licenza completa", "https://www.gnu.org/licenses/agpl-3.0.html" in text)
    check("PyMuPDF con versione", f"PyMuPDF {pymupdf.VersionBind}" in text)
    check("PySide6 con versione", f"(PySide6) {PySide6.__version__}" in text)
    check("Python con versione", f"Python {platform.python_version()}" in text)
    check("testo selezionabile", bool(dlg.text.textInteractionFlags() & dlg.text.textInteractionFlags().TextSelectableByMouse))
    check("logo", dlg.findChildren(type(dlg.text))[0].pixmap() is not None and not dlg.findChildren(type(dlg.text))[0].pixmap().isNull())
    check("«Mostra la licenza» attivo (LICENSE nel repo)", dlg.show_license.isEnabled())

    # --- Link: conferma rifiutata / accettata ---
    answers[:] = [QMessageBox.StandardButton.Cancel]
    dlg.text.linkActivated.emit(source)
    check("conferma rifiutata: niente aperto", not opened and len(asked) == 1 and source in asked[0], str(opened))
    answers[:] = [QMessageBox.StandardButton.Open]
    dlg.text.linkActivated.emit(source)
    check("conferma accettata: si apre il link giusto", [u.toString() for u in opened] == [source], str(opened))
    answers[:] = []
    n = len(asked)
    dlg.text.linkActivated.emit("file:///C:/Windows/notepad.exe")
    check("link non sicuro: niente conferma né apertura", len(asked) == n and len(opened) == 1)

    # --- «Mostra la licenza»: file locale, senza conferma ---
    dlg.show_license.click()
    check("licenza locale aperta senza conferma",
          len(opened) == 2 and opened[1].isLocalFile() and opened[1].toLocalFile().endswith("LICENSE") and len(asked) == n,
          str(opened[1:]))

    win.close()
    print(f"\n{sum(results)}/{len(results)} ok", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
