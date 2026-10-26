import os
import sys
from pathlib import Path

from PySide6.QtCore import QSettings


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def base_dir() -> Path:
    """Cartella dell'exe (da sorgente, la radice del repo). I controlli la sostituiscono."""
    if is_frozen():
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def appdata_dir() -> Path:
    return Path(os.environ.get("APPDATA", Path.home())) / "PieDF"


def data_dir() -> Path:
    """Cartella dei dati utente: `data/` accanto all'exe (modalità portatile), altrimenti %APPDATA%\\PieDF."""
    portable = base_dir() / "data"
    if _writable(portable):
        return portable

    fallback = appdata_dir()
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _writable(directory: Path) -> bool:
    created = not directory.exists()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-test"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        # Non lasciare una cartella data/ vuota se poi si usa %APPDATA%.
        if created:
            try:
                directory.rmdir()
            except OSError:
                pass
        return False


def settings() -> QSettings:
    return QSettings(str(data_dir() / "settings.ini"), QSettings.Format.IniFormat)
