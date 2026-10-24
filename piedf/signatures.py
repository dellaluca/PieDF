"""Firme salvate e trattamento delle immagini di firma.

Ogni firma è un PNG con trasparenza in `<dati>/firme/`. Il tipo (firma o iniziali)
è nel prefisso del nome file, così non serve un indice separato.
"""

import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QImage, QPainter

from piedf import paths

# Le firme si salvano al massimo a questa larghezza: abbastanza per la stampa
# di una firma (circa 6 cm a 300 dpi) senza file inutilmente grandi.
MAX_WIDTH = 1200
MAX_HEIGHT = 600
PADDING = 4  # px trasparenti lasciati attorno al contenuto


class Kind(Enum):
    SIGNATURE = "firma"
    INITIALS = "iniziali"

    @property
    def label(self) -> str:
        return "Firma" if self is Kind.SIGNATURE else "Iniziali"


@dataclass(frozen=True)
class Signature:
    path: Path
    kind: Kind

    def image(self) -> QImage:
        return QImage(str(self.path))


class SignatureStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or paths.data_dir() / "firme"

    def list(self, kind: Kind | None = None) -> list[Signature]:
        """Firme salvate, dalla più recente."""
        if not self.directory.is_dir():
            return []
        result = []
        for file in self.directory.glob("*.png"):
            prefix = file.name.split("_", 1)[0]
            try:
                file_kind = Kind(prefix)
            except ValueError:
                continue
            if kind is None or file_kind is kind:
                result.append(Signature(file, file_kind))
        result.sort(key=lambda s: s.path.stat().st_mtime, reverse=True)
        return result

    def add(self, image: QImage, kind: Kind) -> Signature:
        self.directory.mkdir(parents=True, exist_ok=True)
        name = f"{kind.value}_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}.png"
        path = self.directory / name
        if not image.save(str(path), "PNG"):
            raise OSError(f"Impossibile salvare la firma in {path}")
        return Signature(path, kind)

    def delete(self, signature: Signature) -> None:
        try:
            os.remove(signature.path)
        except FileNotFoundError:
            pass


# --- Trattamento immagini -------------------------------------------------------------


def _alpha_lut(white_from: int, ink_until: int) -> bytes:
    """Tabella grigio → alfa: chiaro = trasparente, scuro = opaco, sfumato in mezzo
    per bordi morbidi."""
    table = bytearray(256)
    for g in range(256):
        if g >= white_from:
            table[g] = 0
        elif g <= ink_until:
            table[g] = 255
        else:
            table[g] = round(255 * (white_from - g) / (white_from - ink_until))
    return bytes(table)


def paper_level(gray: QImage) -> int:
    """Grigio più frequente tra i toni chiari: il colore della carta (foto un po' scure incluse)."""
    small = gray.scaled(200, 200, Qt.AspectRatioMode.KeepAspectRatio)
    data = bytes(small.constBits())
    counts = [data.count(bytes([g])) for g in range(128, 256)]
    return 128 + counts.index(max(counts))


def remove_background(image: QImage) -> QImage:
    """Rende trasparente lo sfondo chiaro di una foto o scansione di una firma."""
    gray = image.convertToFormat(QImage.Format.Format_Grayscale8)
    # Soglie relative alla carta: tutto ciò che è appena più scuro (rumore, grana) sparisce.
    white_from = max(60, paper_level(gray) - 30)
    lut = _alpha_lut(white_from, max(0, white_from - 80))
    # Le righe possono avere byte di riempimento: tradurli non fa danni.
    alpha_bytes = bytes(gray.constBits()).translate(lut)
    alpha = QImage(alpha_bytes, gray.width(), gray.height(), gray.bytesPerLine(),
                   QImage.Format.Format_Grayscale8).copy()
    mask = QImage(image.size(), QImage.Format.Format_ARGB32)
    mask.fill(Qt.GlobalColor.white)
    mask.setAlphaChannel(alpha)

    # DestinationIn moltiplica l'alfa: tiene anche la trasparenza che l'immagine aveva già.
    result = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(result)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
    painter.drawImage(0, 0, mask)
    painter.end()
    return result.convertToFormat(QImage.Format.Format_ARGB32)


def content_rect(image: QImage, min_alpha: int = 64) -> QRect:
    """Rettangolo dei pixel ben visibili (alfa >= min_alpha); vuoto se l'immagine è trasparente.
    I pixel quasi trasparenti (rumore della carta) non allargano il ritaglio; i bordi sfumati
    dei tratti restano dentro grazie a PADDING."""
    alpha = image.convertToFormat(QImage.Format.Format_Alpha8)
    width, stride = alpha.width(), alpha.bytesPerLine()
    # Azzera il rumore quasi trasparente, poi cerca righe e colonne non vuote.
    noise = bytes(0 if a < min_alpha else a for a in range(256))
    data = bytes(alpha.constBits()).translate(noise)
    top = bottom = None
    left, right = width, -1
    for y in range(alpha.height()):
        row = data[y * stride : y * stride + width]
        stripped = row.lstrip(b"\0")
        if not stripped:
            continue
        if top is None:
            top = y
        bottom = y
        left = min(left, width - len(stripped))
        right = max(right, len(row.rstrip(b"\0")) - 1)
    if top is None:
        return QRect()
    return QRect(left, top, right - left + 1, bottom - top + 1)


def finalize(image: QImage) -> QImage | None:
    """Ritaglia sul contenuto, lascia un piccolo margine e limita le dimensioni.
    Restituisce None se l'immagine è vuota."""
    rect = content_rect(image)
    if rect.isEmpty():
        return None
    rect = rect.adjusted(-PADDING, -PADDING, PADDING, PADDING).intersected(image.rect())
    cropped = image.copy(rect).convertToFormat(QImage.Format.Format_ARGB32)
    if cropped.width() > MAX_WIDTH or cropped.height() > MAX_HEIGHT:
        cropped = cropped.scaled(MAX_WIDTH, MAX_HEIGHT, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
    return cropped
