"""Controllo end-to-end dell'exe portatile (#8). Solo Windows, dopo la build.

    python tools/build_exe.py
    python tests/make_test_pdfs.py
    python tests/check_exe.py

Se non è Windows o se manca dist/PieDF/PieDF.exe stampa SKIP ed esce 0.
"""
import csv
import io
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out"
PDFS = S / "pdfs"
DIST = ROOT / "dist"
EXE = DIST / "PieDF" / "PieDF.exe"
ONEFILE = DIST / "PieDF.exe"
WINDOW_TIMEOUT = 15.0
EXIT_TIMEOUT = 10.0
SETTINGS_TIMEOUT = 5.0
MAX_ZIP_MB = 70


def pids(image="PieDF.exe") -> list[int]:
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True).stdout
    return [int(row[1]) for row in csv.reader(io.StringIO(out)) if len(row) > 1 and row[0].lower() == image.lower()]


def windows(pid_list) -> list[tuple[int, str]]:
    """Finestre visibili dei processi indicati: (hwnd, titolo)."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pid_list and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            if buf.value:
                found.append((hwnd, buf.value))
        return True

    user32.EnumWindows(callback, 0)
    return found


def close_window(hwnd) -> None:
    import ctypes
    WM_CLOSE = 0x0010
    ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def size_mb(path: Path) -> float:
    files = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
    return sum(p.stat().st_size for p in files) / 1e6


def main():
    if sys.platform != "win32":
        print("SKIP exe: solo su Windows", flush=True)
        return 0
    if not EXE.exists():
        print(f"SKIP exe: manca {EXE.relative_to(ROOT)} (prima python tools/build_exe.py)", flush=True)
        return 0

    results = []

    def check(name, ok, info=""):
        results.append(bool(ok))
        print(("OK  " if ok else "FAIL"), name, info, flush=True)

    def wait_for(cond, timeout):
        t = time.perf_counter()
        while time.perf_counter() - t < timeout:
            value = cond()
            if value:
                return value
            time.sleep(0.1)
        return cond()

    def run(exe: Path, pdf: Path, env=None):
        """Avvia l'exe e aspetta la finestra: (processo, finestra, secondi) oppure finestra None."""
        t = time.perf_counter()
        proc = subprocess.Popen([str(exe), str(pdf)], env=env)
        win = wait_for(lambda: [w for w in windows(pids()) if "PieDF" in w[1]], WINDOW_TIMEOUT)
        return proc, (win[0] if win else None), time.perf_counter() - t

    def stop(proc, win) -> bool:
        """Chiude la finestra come l'utente; True se non resta nessun processo PieDF."""
        if win:
            close_window(win[0])
        gone = wait_for(lambda: not pids(), EXIT_TIMEOUT)
        if not gone:
            subprocess.run(["taskkill", "/F", "/T", "/IM", "PieDF.exe"], capture_output=True)
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        return bool(gone)

    pdf = PDFS / "semplice.pdf"
    check("PDF di prova presente", pdf.exists(), "(python tests/make_test_pdfs.py)")
    check("nessun PieDF già in esecuzione", not pids(), str(pids()))

    # --- 1. Dimensioni ---
    folder_mb = size_mb(EXE.parent)
    zips = sorted(DIST.glob("PieDF-*-win64.zip"))
    print(f"INFO onedir: cartella {folder_mb:.1f} MB", flush=True)
    if zips:
        zip_mb = size_mb(zips[-1])
        check(f"zip sotto {MAX_ZIP_MB} MB", zip_mb < MAX_ZIP_MB, f"{zips[-1].name}: {zip_mb:.1f} MB")
    check("niente tests/ nell'exe", not list(EXE.parent.rglob("check_*.py")))
    check("icona inclusa", (EXE.parent / "_internal" / "piedf" / "assets" / "piedf.ico").exists())

    check("LICENSE inclusa", (EXE.parent / "_internal" / "LICENSE").exists())

    # --- 2. Avvio con un file: finestra, GUI + render, recenti in data/ ---
    data = EXE.parent / "data"
    shutil.rmtree(data, ignore_errors=True)
    proc, win, secs = run(EXE, pdf)
    check("la finestra si apre", win is not None, f"{secs:.1f} s" + (f" «{win[1]}»" if win else ""))
    if win:
        print(f"INFO onedir: avvio fino alla finestra {secs:.2f} s", flush=True)
        check("titolo con il nome del file", "semplice.pdf" in win[1], win[1])
        time.sleep(2)  # il processo di render parte subito dopo la finestra
        running = pids()
        check("esattamente 2 processi PieDF (GUI + render)", len(running) == 2, str(running))
        check("una sola finestra", len(windows(running)) == 1, str(windows(running)))
    ini = data / "settings.ini"
    text = ini.read_text(encoding="utf-8", errors="replace") if ini.exists() else ""
    check("data/settings.ini accanto all'exe", ini.exists(), str(ini))
    check("il file è tra i recenti", "semplice.pdf" in text, text.strip()[:200])
    check("alla chiusura non resta nessun processo", stop(proc, win), str(pids()))

    # --- 3. Cartella non scrivibile: dati in %APPDATA%\PieDF ---
    ro = S / "exe_sola_lettura"
    appdata = S / "appdata_exe"
    user = os.environ.get("USERNAME", "")
    subprocess.run(["icacls", str(ro), "/remove:d", user, "/T", "/C", "/Q"], capture_output=True)
    for d in (ro, appdata):
        shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(EXE.parent, ro / "PieDF", ignore=shutil.ignore_patterns("data"))
    appdata.mkdir(parents=True)
    # Solo creazione di file e cartelle (WD, AD): la "W" generica toglie anche SYNCHRONIZE
    # e Windows non riesce più nemmeno ad avviare l'exe (Access is denied).
    denied = subprocess.run(["icacls", str(ro / "PieDF"), "/deny", f"{user}:(OI)(CI)(WD,AD)"],
                            capture_output=True, text=True)
    try:
        if denied.returncode != 0 or _can_write(ro / "PieDF"):
            print("SKIP cartella non scrivibile: icacls non ha tolto la scrittura", denied.stdout, flush=True)
        else:
            env = dict(os.environ, APPDATA=str(appdata))
            proc, win, secs = run(ro / "PieDF" / "PieDF.exe", pdf, env)
            check("non scrivibile: la finestra si apre", win is not None, f"{secs:.1f} s")
            check("non scrivibile: niente data/ accanto all'exe", not (ro / "PieDF" / "data").exists())
            # La finestra compare prima che il file sia aperto: settings.ini arriva subito dopo.
            ini = appdata / "PieDF" / "settings.ini"
            check("non scrivibile: settings.ini in %APPDATA%\\PieDF", wait_for(ini.exists, SETTINGS_TIMEOUT), str(ini))
            check("non scrivibile: alla chiusura non resta nessun processo", stop(proc, win), str(pids()))
    finally:
        subprocess.run(["icacls", str(ro), "/remove:d", user, "/T", "/C", "/Q"], capture_output=True)
        shutil.rmtree(ro, ignore_errors=True)

    # --- 4. Sigillo (#70): l'exe sigilla senza finestra, pyHanko valida il risultato ---
    internal = EXE.parent / "_internal"
    seal_parts = ["cryptography", "pyhanko", "pyhanko_certvalidator", "asn1crypto", "tzdata", "certifi",
                  "uritools", "tzlocal", "_cffi_backend"]
    seal_mb = sum(size_mb(p) for name in seal_parts for p in internal.glob(f"{name}*"))
    # I moduli Python puri (pyHanko, asn1crypto…) stanno nell'archivio PYZ: qui solo file nativi e dati.
    print(f"INFO sigillo: file nativi e dati nell'exe {seal_mb:.1f} MB (cryptography, cffi, certifi, tzdata)", flush=True)
    check("sigillo: aiohttp e lxml esclusi dall'exe",
          not list(internal.glob("aiohttp*")) and not list(internal.glob("lxml*")))
    seal_dir = S / "sigillo_exe"
    shutil.rmtree(seal_dir, ignore_errors=True)
    seal_dir.mkdir(parents=True)
    sealed = seal_dir / "sigillato.pdf"
    t = time.perf_counter()
    try:
        code = subprocess.run([str(EXE), "--piedf-sigilla", str(PDFS / "modulo.pdf"), str(sealed),
                               str(seal_dir / "sigillo"), "Prova Exe"], timeout=120).returncode
    except subprocess.TimeoutExpired:
        code = "timeout"
    error = Path(str(sealed) + ".errore.txt")
    check("sigillo: l'exe sigilla un PDF", code == 0 and sealed.exists(),
          f"codice {code}, {time.perf_counter() - t:.1f} s " + (error.read_text(encoding="utf-8") if error.exists() else ""))
    check("sigillo: nessun processo PieDF rimasto", wait_for(lambda: not pids(), EXIT_TIMEOUT), str(pids()))
    if sealed.exists():
        try:
            from asn1crypto import pem, x509
            from pyhanko.pdf_utils.reader import PdfFileReader
            from pyhanko.sign.fields import MDPPerm
            from pyhanko.sign.validation import validate_pdf_signature
            from pyhanko.sign.validation.status import SignatureCoverageLevel
            from pyhanko_certvalidator import ValidationContext
            cert = x509.Certificate.load(pem.unarmor((seal_dir / "sigillo" / "certificato.pem").read_bytes())[2])
            with open(sealed, "rb") as f:
                sig = PdfFileReader(io.BytesIO(f.read())).embedded_signatures[0]
            status = validate_pdf_signature(sig, ValidationContext(trust_roots=[cert]))
            check("sigillo: pyHanko lo valida (integro, certificazione, nessuna modifica, file intero)",
                  status.intact and status.valid and status.trusted and sig.docmdp_level == MDPPerm.NO_CHANGES
                  and status.coverage == SignatureCoverageLevel.ENTIRE_FILE, status.summary())
        except Exception as e:
            check("sigillo: pyHanko lo valida", False, repr(e))
        blob = (seal_dir / "sigillo" / "chiave.dpapi").read_bytes()
        check("sigillo: chiave cifrata con DPAPI (nessuna chiave in chiaro)", b"PRIVATE KEY" not in blob and len(blob) > 100)
    shutil.rmtree(seal_dir, ignore_errors=True)

    # --- 5. Onefile, solo per i numeri della PR (se è stato costruito) ---
    if ONEFILE.exists():
        print(f"INFO onefile: {size_mb(ONEFILE):.1f} MB", flush=True)
        proc, win, secs = run(ONEFILE, pdf)
        if win:
            time.sleep(2)
            print(f"INFO onefile: avvio fino alla finestra {secs:.2f} s, processi PieDF {len(pids())}", flush=True)
        stop(proc, win)
        shutil.rmtree(DIST / "data", ignore_errors=True)

    shutil.rmtree(data, ignore_errors=True)  # lo zip non deve contenere dati di prova
    print(f"\n{sum(results)}/{len(results)} ok", flush=True)
    return 0 if all(results) else 1


def _can_write(directory: Path) -> bool:
    try:
        probe = directory / ".write-test"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


if __name__ == "__main__":
    sys.exit(main())
