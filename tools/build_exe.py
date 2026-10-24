"""Costruisce l'exe portatile di PieDF con PyInstaller (#8).

    pip install -r requirements.txt -r requirements-build.txt
    python tools/build_exe.py              -> dist/PieDF/PieDF.exe + dist/PieDF-<versione>-win64.zip
    python tools/build_exe.py --onefile    -> dist/PieDF.exe (solo per confrontare dimensione e avvio)
"""
import argparse
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"


def version() -> str:
    return re.search(r'__version__ = "([^"]+)"', (ROOT / "piedf" / "__init__.py").read_text()).group(1)


def size_mb(path: Path) -> float:
    files = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file() and not p.is_symlink()]
    return sum(p.stat().st_size for p in files) / 1e6


def make_zip(folder: Path, dest: Path) -> None:
    """Zip con dentro la cartella `PieDF/`, senza dati utente rimasti da una prova."""
    dest.unlink(missing_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(folder.rglob("*")):
            rel = p.relative_to(folder.parent)
            if p.is_file() and rel.parts[1] != "data":
                z.write(p, rel.as_posix())


def main() -> int:
    parser = argparse.ArgumentParser(description="Costruisce l'exe portatile di PieDF.")
    parser.add_argument("--onefile", action="store_true", help="un solo exe invece della cartella")
    parser.add_argument("--no-zip", action="store_true", help="non creare lo zip della cartella")
    args = parser.parse_args()

    mode = "onefile" if args.onefile else "onedir"
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--distpath", str(DIST), "--workpath", str(ROOT / "build" / mode), str(ROOT / "piedf.spec")]
    if args.onefile:
        cmd += ["--", "--onefile"]
    print(" ".join(cmd), flush=True)
    if subprocess.call(cmd, cwd=ROOT) != 0:
        print("Build non riuscita.", file=sys.stderr)
        return 1

    exe_name = "PieDF.exe" if sys.platform == "win32" else "PieDF"
    if args.onefile:
        exe = DIST / exe_name
        print(f"\n{exe}: {size_mb(exe):.1f} MB")
        return 0

    folder = DIST / "PieDF"
    print(f"\n{folder / exe_name}: cartella {size_mb(folder):.1f} MB")
    if not args.no_zip:
        tag = "win64" if sys.platform == "win32" else sys.platform
        zip_path = DIST / f"PieDF-{version()}-{tag}.zip"
        make_zip(folder, zip_path)
        print(f"{zip_path}: {size_mb(zip_path):.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
