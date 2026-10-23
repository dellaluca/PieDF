from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QFileDialog, QLabel, QMainWindow


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PieDF")
        self.resize(1000, 800)

        # Segnaposto finché il viewer (#2) non prende il suo posto.
        self._placeholder = QLabel("Apri un PDF con File › Apri (Ctrl+O)")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCentralWidget(self._placeholder)

        self._build_menu()

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        open_action = QAction("&Apri…", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self._choose_file)
        file_menu.addAction(open_action)

        file_menu.addSeparator()

        quit_action = QAction("&Esci", self)
        # StandardKey.Quit non ha una scorciatoia su Windows.
        quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Apri PDF", "", "Documenti PDF (*.pdf);;Tutti i file (*)"
        )
        if path:
            self.open_file(path)

    def open_file(self, path: str) -> None:
        # Il caricamento vero arriva con il viewer (#2).
        self.setWindowTitle(f"{Path(path).name} – PieDF")
        self._placeholder.setText(f"Selezionato: {path}")
