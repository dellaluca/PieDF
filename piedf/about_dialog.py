"""Finestra «Informazioni su PieDF»: versione, licenza AGPL e codice sorgente (#63)."""
import platform
import sys
from pathlib import Path

import pymupdf
import PySide6
from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout
from pyhanko.version import __version__ as pyhanko_version  # solo la versione: pyHanko non si carica

from piedf import REPO_URL, __version__
from piedf.external import open_external

LICENSE_URL = "https://www.gnu.org/licenses/agpl-3.0.html"
ASSETS = Path(__file__).parent / "assets"


def source_url() -> str:
    """Il codice sorgente della versione in uso: il tag `v<versione>`, non `main`."""
    return f"{REPO_URL}/tree/v{__version__}"


def license_file() -> Path | None:
    """`LICENSE` nell'exe (`_internal`) o alla radice del repo; `None` se manca."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    path = base / "LICENSE"
    return path if path.is_file() else None


def components() -> list[tuple[str, str]]:
    """Componenti usati con la loro licenza (le versioni si leggono a runtime)."""
    return [
        (f"PyMuPDF {pymupdf.VersionBind} / MuPDF {pymupdf.VersionFitz}", "AGPL-3.0, Artifex"),
        (f"Qt for Python (PySide6) {PySide6.__version__}", "LGPL-3.0"),
        (f"pyHanko {pyhanko_version}", "MIT"),
        (f"Python {platform.python_version()}", "PSF License"),
    ]


class AboutDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Informazioni su PieDF")
        self.setModal(True)
        self.setMinimumWidth(460)
        icon = QIcon(str(ASSETS / "piedf.ico"))

        head = QHBoxLayout()
        logo = QLabel()
        sizes = icon.availableSizes()
        biggest = max(sizes, key=lambda s: s.width()) if sizes else QSize(64, 64)
        logo.setPixmap(icon.pixmap(biggest).scaled(
            64, 64, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        head.addWidget(logo, 0, Qt.AlignmentFlag.AlignTop)
        title = QLabel(f"<h2>PieDF {__version__}</h2>Lettore PDF portatile per Windows, con firma visiva.")
        title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        head.addWidget(title, 1)

        comps = "".join(f"<li>{name}: {lic}</li>" for name, lic in components())
        src = source_url()
        self.text = QLabel(
            "<p>PieDF è software libero, rilasciato con la licenza "
            "GNU Affero General Public License v3.0. Puoi usarlo, studiarlo, modificarlo e "
            "ridistribuirlo alle condizioni della licenza. PieDF è fornito senza alcuna garanzia.</p>"
            f'<p>Codice sorgente di questa versione:<br><a href="{src}">{src}</a></p>'
            f'<p>Licenza completa:<br><a href="{LICENSE_URL}">{LICENSE_URL}</a></p>'
            f"<p>Componenti usati:</p><ul>{comps}</ul>"
        )
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.TextFormat.RichText)
        self.text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.text.setOpenExternalLinks(False)
        self.text.linkActivated.connect(lambda uri: open_external(uri, self))

        self.show_license = QPushButton("Mostra la licenza")
        path = license_file()
        self.show_license.setEnabled(path is not None)
        if path is not None:
            self.show_license.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))))
        close = QPushButton("Chiudi")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(self.show_license)
        buttons.addStretch(1)
        buttons.addWidget(close)

        layout = QVBoxLayout(self)
        layout.addLayout(head)
        layout.addWidget(self.text)
        layout.addLayout(buttons)
