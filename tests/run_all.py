"""Genera i PDF di prova ed esegue tutti i tests/check_*.py, uno dopo l'altro, con un timeout.

    python tests/run_all.py                  # tutti (check_exe solo se c'è la build in dist/)
    python tests/run_all.py pages forms      # solo check_pages.py e check_forms.py

Stampa per ogni controllo il riepilogo e le righe FAIL/Traceback, poi un riepilogo finale.
Uscita 1 se qualcosa fallisce. È il comando unico per i worker: niente catene di comandi.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

TESTS = Path(__file__).parent
ROOT = TESTS.parent
TIMEOUT = 600


def run(cmd: list[str]) -> tuple[int, str]:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        p = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=TIMEOUT)
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return 124, out + f"\nTIMEOUT dopo {TIMEOUT} s"


def main() -> int:
    wanted = sys.argv[1:]
    code, out = run([sys.executable, "tests/make_test_pdfs.py"])
    if code:
        print(out)
        print("FAIL make_test_pdfs.py")
        return 1

    checks = sorted(TESTS.glob("check_*.py"))
    if wanted:
        checks = [c for c in checks if c.stem.removeprefix("check_") in wanted]
    elif not (ROOT / "dist" / "PieDF" / "PieDF.exe").exists():
        checks = [c for c in checks if c.name != "check_exe.py"]  # serve la build

    failed = []
    for c in checks:
        start = time.monotonic()
        code, out = run([sys.executable, "-u", str(c.relative_to(ROOT))])
        lines = out.strip().splitlines()
        summary = lines[-1] if lines else ""
        print(f"== {c.name}: {'ok' if code == 0 else 'FALLITO'} ({time.monotonic() - start:.0f} s) {summary}", flush=True)
        if code:
            failed.append(c.name)
            for line in lines:
                if line.startswith("FAIL") or "Traceback" in line or "Error" in line or "TIMEOUT" in line:
                    print("   ", line, flush=True)

    print(f"\n{len(checks) - len(failed)}/{len(checks)} controlli superati"
          + (f"; falliti: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
