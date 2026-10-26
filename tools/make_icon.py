"""Icona di PieDF: `python tools/make_icon.py` → piedf/assets/piedf.ico da piedf/assets/piedf.svg.

L'ICO contiene tutte le dimensioni che Windows usa (barra delle applicazioni, Esplora file,
titolo della finestra, schermi ad alta densità), ognuna come PNG: Windows le accetta da Vista.
Sotto i 32 px la firma bianca diventa rumore, quindi si disegna senza (`SMALL_WITHOUT`).
QtSvg serve solo qui: l'exe usa l'ICO e lo esclude (piedf.spec).
"""

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "piedf" / "assets" / "piedf.svg"
ICO = ROOT / "piedf" / "assets" / "piedf.ico"
SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]
SMALL = 32  # sotto questa dimensione niente firma
SMALL_WITHOUT = 'stroke="#FFFFFF"'  # il tratto della firma nell'SVG


def render(svg: bytes, size: int) -> bytes:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(svg)).render(painter)
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def without_signature(svg: str) -> str:
    lines = [line for line in svg.splitlines() if SMALL_WITHOUT not in line]
    return "\n".join(lines)


def main() -> int:
    QGuiApplication.instance() or QGuiApplication(sys.argv)
    full = SVG.read_text(encoding="utf-8")
    small = without_signature(full)
    pngs = [render((small if size < SMALL else full).encode(), size) for size in SIZES]

    header = struct.pack("<HHH", 0, 1, len(SIZES))
    offset = 6 + 16 * len(SIZES)
    entries, blobs = b"", b""
    for size, png in zip(SIZES, pngs):
        side = 0 if size >= 256 else size  # 0 = 256 nel formato ICO
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(png), offset)
        blobs += png
        offset += len(png)
    ICO.write_bytes(header + entries + blobs)
    print(f"scritto {ICO} ({ICO.stat().st_size // 1024} KB, {len(SIZES)} dimensioni)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
