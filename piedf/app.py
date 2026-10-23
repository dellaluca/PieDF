from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from piedf import __version__
from piedf.main_window import MainWindow

ASSETS = Path(__file__).parent / "assets"


def main(argv: list[str]) -> int:
    app = QApplication(argv)
    app.setApplicationName("PieDF")
    app.setApplicationVersion(__version__)
    app.setWindowIcon(QIcon(str(ASSETS / "piedf.ico")))

    window = MainWindow()
    window.show()

    # Il primo argomento non-opzione è il file da aprire ("Apri con" di Windows).
    files = [a for a in app.arguments()[1:] if not a.startswith("-")]
    if files:
        window.open_file(files[0])

    return QApplication.exec()
