"""Dati di una versione precedente di PieDF: ricerca, copia e importazione delle firme (#64).

Senza Qt, così si prova con cartelle temporanee. Gli originali non si toccano mai.
"""

import hashlib
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

MAX_ENTRIES = 200  # voci di cartella lette dalla ricerca
MAX_SECONDS = 0.2
EXE_NAME = "PieDF.exe"
SETTINGS = "settings.ini"
POSITIONS = "posizioni.json"
SKIP_KEY = "data_import/skip"


@dataclass(frozen=True)
class DataSummary:
    signatures: int
    settings: bool
    positions: bool

    def describe(self) -> str:
        parts = []
        if self.signatures:
            parts.append(f"{self.signatures} firm{'a' if self.signatures == 1 else 'e'}")
        if self.settings:
            parts.append("impostazioni")
        if self.positions:
            parts.append("posizioni di lettura")
        if len(parts) > 1:
            return ", ".join(parts[:-1]) + " e " + parts[-1]
        return parts[0] if parts else "nessun dato"


def _png_files(data: Path) -> list[Path]:
    try:
        return [f for f in (data / "firme").iterdir() if f.suffix.lower() == ".png" and f.is_file()]
    except OSError:
        return []


def is_valid(data: Path) -> bool:
    """Una cartella `data` vale se ha almeno una firma PNG oppure `settings.ini`."""
    try:
        return (data / SETTINGS).is_file() or bool(_png_files(data))
    except OSError:
        return False


def summarize(data: Path) -> DataSummary:
    try:
        return DataSummary(len(_png_files(data)), (data / SETTINGS).is_file(), (data / POSITIONS).is_file())
    except OSError:
        return DataSummary(len(_png_files(data)), False, False)


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.normcase(a.resolve()) == os.path.normcase(b.resolve())
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def last_modified(data: Path) -> float:
    """Data di modifica più recente tra i file dentro `data`."""
    newest = 0.0
    for root, _dirs, files in os.walk(data):
        for name in files:
            try:
                newest = max(newest, os.stat(os.path.join(root, name)).st_mtime)
            except OSError:
                pass
    return newest


def find_previous(exe_dir: Path, own_data: Path, appdata: Path | None = None) -> Path | None:
    """Cartella `data` della versione precedente più recente, o None.

    Cerca nelle cartelle sorelle di quella dell'exe e delle due sopra (`X/*/PieDF/data`,
    `X/*/data`), poi in `appdata`. Al massimo `MAX_ENTRIES` voci e `MAX_SECONDS`: gli
    errori si ignorano.
    """
    deadline = time.monotonic() + MAX_SECONDS
    budget = MAX_ENTRIES
    found: list[Path] = []

    def consider(data: Path) -> None:
        if not _same(data, own_data) and is_valid(data):
            found.append(data)

    parents = [exe_dir.parent]
    if exe_dir.parent.parent != exe_dir.parent:
        parents.append(exe_dir.parent.parent)
    for parent in parents:
        try:
            entries = os.scandir(parent)
        except OSError:
            continue
        with entries:
            while budget > 0 and time.monotonic() < deadline:
                try:
                    entry = next(entries)
                except (StopIteration, OSError):
                    break
                budget -= 1
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                base = Path(entry.path)
                consider(base / "data")
                consider(base / "PieDF" / "data")
    if appdata is not None:
        consider(appdata)

    unique = list(dict.fromkeys(found))
    return max(unique, key=last_modified) if unique else None


def resolve_source(path: Path) -> Path | None:
    """Dalla cartella scelta a mano alla sua `data`: vale la cartella con `PieDF.exe`,
    la sua `data\\` o `data\\firme\\`."""
    candidates = [path / "data", path / "PieDF" / "data", path]
    if path.name.lower() == "firme":
        candidates.insert(0, path.parent)
    for candidate in candidates:
        if is_valid(candidate):
            return candidate
    return None


def exe_version(data: Path) -> str | None:
    """Versione del `PieDF.exe` accanto a `data` (solo Windows), altrimenti None."""
    exe = data.parent / EXE_NAME
    if sys.platform != "win32" or not exe.is_file():
        return None
    try:
        import ctypes
        from ctypes import wintypes

        version = ctypes.windll.version
        version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        version.GetFileVersionInfoW.restype = wintypes.BOOL
        version.VerQueryValueW.argtypes = [
            ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
        version.VerQueryValueW.restype = wintypes.BOOL

        size = version.GetFileVersionInfoSizeW(str(exe), None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(exe), 0, size, buffer):
            return None
        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)) or length.value < 16:
            return None
        # VS_FIXEDFILEINFO: dwFileVersionMS e dwFileVersionLS sono i DWORD in posizione 2 e 3.
        info = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
        ms, ls = info[2], info[3]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}"
    except Exception:
        return None


def copy_data(src: Path, dst: Path) -> int:
    """Copia firme, `settings.ini` e `posizioni.json` da `src` a `dst`. Restituisce le firme copiate."""
    dst.mkdir(parents=True, exist_ok=True)
    copied = 0
    pngs = _png_files(src)
    if pngs:
        (dst / "firme").mkdir(exist_ok=True)
    for png in pngs:
        target = dst / "firme" / png.name
        if not target.exists():
            shutil.copy2(png, target)
            copied += 1
    for name in (SETTINGS, POSITIONS):
        if (src / name).is_file():
            shutil.copy2(src / name, dst / name)
    return copied


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def import_signatures(src: Path, dst: Path) -> tuple[int, int]:
    """Aggiunge a `dst/firme` le firme di `src/firme` che non ci sono già (stesso contenuto).
    Restituisce (importate, già presenti)."""
    known = set()
    for png in _png_files(dst):
        try:
            known.add(_digest(png))
        except OSError:
            pass
    imported = present = 0
    for png in sorted(_png_files(src)):
        try:
            digest = _digest(png)
        except OSError:
            continue
        if digest in known:
            present += 1
            continue
        (dst / "firme").mkdir(parents=True, exist_ok=True)
        target = dst / "firme" / png.name
        n = 1
        while target.exists():  # stesso nome ma contenuto diverso
            target = dst / "firme" / f"{png.stem}_{n}{png.suffix}"
            n += 1
        shutil.copy2(png, target)
        known.add(digest)
        imported += 1
    return imported, present
