import os
import sys
from pathlib import Path

from PySide6.QtCore import QSettings


def data_dir() -> Path:
    """Cartella dei dati utente: `data/` accanto all'exe (modalità portatile), altrimenti %APPDATA%\\PieDF."""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent.parent
    portable = base / "data"
    if _writable(portable):
        return portable

    fallback = Path(os.environ.get("APPDATA", Path.home())) / "PieDF"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-test"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def settings() -> QSettings:
    return QSettings(str(data_dir() / "settings.ini"), QSettings.Format.IniFormat)
