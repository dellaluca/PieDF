"""Controllo end-to-end della scheda Firme del pannello laterale (#73): elenco e verifica delle
firme digitali, salto al campo, versione firmata, dettagli del certificato.

    python tests/make_test_pdfs.py
    set QT_QPA_PLATFORM=offscreen
    python tests/check_signatures_panel.py
"""
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out"
PDFS = S / "pdfs"
SHOTS = S / "shots"


def main():
    import pymupdf
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

    SHOTS.mkdir(parents=True, exist_ok=True)
    data = S / "data_firme"
    shutil.rmtree(data, ignore_errors=True)
    data.mkdir(parents=True)
    work = S / "firme_lavoro"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    import piedf.paths as paths
    paths.data_dir = lambda: data
    import piedf.locking as locking
    import piedf.main_window as main_window
    import piedf.signatures_panel as signatures_panel
    from piedf import sealing
    from piedf.main_window import MainWindow
    from piedf.sidebar import PAGES_TAB, SIGNATURES_TAB

    app = QApplication(["t"])
    results = []

    def check(name, ok, info=""):
        results.append(bool(ok))
        print(("OK  " if ok else "FAIL"), name, info, flush=True)

    def skip(name, why):
        print("SKIP", name, why, flush=True)

    def pump(sec=0.05):
        t = time.perf_counter()
        while time.perf_counter() - t < sec:
            app.processEvents(); time.sleep(0.01)

    def wait_check(timeout=60):
        t = time.perf_counter()
        while w._seal_check is not None and time.perf_counter() - t < timeout:
            app.processEvents(); time.sleep(0.01)
        pump(0.05)

    jobs = []
    real_job = main_window.SealCheckJob

    class SpyJob(real_job):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            jobs.append(a[0])
    main_window.SealCheckJob = SpyJob

    errors = []
    QMessageBox.critical = lambda parent, title, text, *a, **k: errors.append(text)
    QMessageBox.warning = lambda parent, title, text, *a, **k: errors.append(text)
    save_asked, save_answers = [], []

    def fake_save(parent, title, proposed, *a, **k):
        save_asked.append(proposed)
        return (save_answers.pop(0) if save_answers else "", "")
    QFileDialog.getSaveFileName = fake_save

    w = MainWindow(); w.resize(1100, 800); w.show(); app.processEvents()
    v, sb = w.view, w.sidebar
    panel = sb.signatures

    def tab_visible():
        return sb.tabs.isTabVisible(SIGNATURES_TAB)

    def rows():
        return panel.rows()

    def open_signed(path):
        jobs.clear()
        w.open_file(str(path))
        checking = tab_visible() and panel.message.text() == sealing.SIGNATURES_CHECKING and not panel.rows()
        wait_check()
        return checking

    # --- 8. PDF senza firme: scheda nascosta, nessun processo ---------------------------------
    jobs.clear()
    w.open_file(str(PDFS / "semplice.pdf")); pump(0.1)
    check("8. PDF senza firme: scheda Firme nascosta", not tab_visible() and sb.tabs.count() == 3)
    check("8. PDF senza firme: nessun processo di verifica", not jobs and w._seal_check is None, str(jobs))
    check("8. PDF senza firme: niente nella barra di stato", w.signatures_note.isHidden())
    jobs.clear()
    w.open_file(str(PDFS / "firmato_digitale.pdf")); pump(0.1)
    check("8. campo firma vuoto: niente scheda né processo", not tab_visible() and not jobs)
    from piedf.document import open_document
    t = time.perf_counter()
    open_document(str(PDFS / "modulo_lungo.pdf"))
    opening = time.perf_counter() - t
    doc = pymupdf.open(PDFS / "modulo_lungo.pdf")
    t = time.perf_counter()
    n = sealing.signature_count(doc)
    quick = time.perf_counter() - t
    doc.close()
    check("8. controllo rapido delle firme su 1000 campi: meno del 15% dell'apertura", n == 0 and quick < 0.15 * opening,
          f"{quick * 1000:.1f} ms, apertura {opening * 1000:.0f} ms")

    # --- 1. Una firma di approvazione integra ---------------------------------------------------
    check("il pannello all'inizio è nascosto", not sb.isVisible())
    checking = open_signed(PDFS / "firme_una.pdf")
    check("1. scheda Firme visibile con «Controllo delle firme…» finché non arriva il risultato", checking)
    check("1. il pannello non si apre da solo", not sb.isVisible())
    check("1. barra di stato: «Questo documento ha 1 firma digitale» con «Mostra»",
          w.signatures_note.isVisible() and "Questo documento ha 1 firma digitale" in w.signatures_note.text()
          and ">Mostra</a>" in w.signatures_note.text(), w.signatures_note.text())
    check("1. un solo processo di verifica", len(jobs) == 1, str(jobs))
    r = rows()
    check("1. una riga", len(r) == 1, str(r))
    first = r[0].split("\n") if r else []
    check("1. nome, tipo, stato, identità",
          first[:1] == ["Mario Rossi"] and "Firma di approvazione" in first and "Integra" in first
          and "Identità non verificata da PieDF" in first, str(first))
    check("1. data dichiarata dal firmatario", len(first) > 1 and first[1].endswith(", data dichiarata dal firmatario"),
          str(first[1:2]))
    check("1. nota sulla revoca in fondo", panel.note.text() == sealing.REVOCATION_NOTE and not panel.note.isHidden())
    check("1. firma di approvazione: il documento resta modificabile", not w._locked and w.seal_bar.isHidden()
          and w.act_text.isEnabled())
    w.signatures_note.linkActivated.emit("mostra"); pump(0.1)
    check("1. «Mostra» apre il pannello sulla scheda Firme", sb.isVisible() and sb.tabs.currentIndex() == SIGNATURES_TAB)
    sb.grab().save(str(SHOTS / "scheda_firme.png"))

    # --- 8. Clic su una firma visibile ----------------------------------------------------------
    v.go_to_page(0); pump(0.1)
    QTest.mouseClick(panel.cards[0], Qt.MouseButton.LeftButton)
    pump(0.1)
    info = w._signatures[0] if w._signatures else None
    check("8. clic sulla firma visibile → pagina 3", v.current_page == 2, f"pagina {v.current_page + 1}")
    if info is not None:
        from PySide6.QtCore import QRectF
        x0, y0, x1, y1 = info.rect
        rect = v.page_rect_to_viewport(info.page, QRectF(x0, y0, x1 - x0, y1 - y0))
        check("8. il campo della firma è in vista", v.viewport().rect().contains(rect.toRect()), str(rect))
    check("8. il salto entra nella cronologia (Indietro)", w.act_back.isEnabled())

    # --- 2. Due firme in revisioni successive ---------------------------------------------------
    open_signed(PDFS / "firme_due.pdf")
    r = rows()
    check("2. due righe in ordine di revisione", len(r) == 2 and r[0].startswith("Mario Rossi") and r[1].startswith("Anna Verdi"),
          str(r))
    check("2. la prima: «Integra, con modifiche ammesse dopo»; la seconda: «Integra»",
          len(r) == 2 and "Integra, con modifiche ammesse dopo" in r[0].split("\n") and "Integra" in r[1].split("\n"), str(r))
    check("2. barra di stato: «2 firme digitali»", "Questo documento ha 2 firme digitali" in w.signatures_note.text())
    check("2. la scheda resta quella aperta", sb.tabs.currentIndex() == SIGNATURES_TAB)
    v.go_to_page(0); pump(0.05)
    QTest.mouseClick(panel.cards[1], Qt.MouseButton.LeftButton)
    pump(0.1)
    check("8. firma invisibile: niente salto", v.current_page == 0, f"pagina {v.current_page + 1}")

    # --- 7. Salva la versione firmata ------------------------------------------------------------
    menu = panel.context_menu(0)
    texts = [a.text() for a in menu.actions()]
    check("7. menu contestuale: versione firmata e dettagli", texts == [signatures_panel.SAVE_TEXT, signatures_panel.DETAILS_TEXT],
          str(texts))
    version = work / "versione_1.pdf"
    save_answers[:] = [str(version)]
    menu.actions()[0].trigger(); pump(0.1)
    check("7. propone <nome>_versione_firmata_1.pdf",
          save_asked and Path(save_asked[-1]).name == "firme_due_versione_firmata_1.pdf", str(save_asked))
    status, sigs = sealing.verify(str(version), None, None, roots=[]) if version.exists() else (None, None)
    check("7. la versione firmata della prima firma ha una sola firma, integra",
          sigs is not None and len(sigs) == 1 and sigs[0].state == sealing.INTACT and sigs[0].signer == "Mario Rossi",
          str(sigs and [(s.signer, s.state) for s in sigs]))
    original = (PDFS / "firme_due.pdf").read_bytes()
    check("7. sono i byte iniziali del file, nessun temporaneo",
          version.exists() and original.startswith(version.read_bytes()) and len(version.read_bytes()) < len(original)
          and not list(work.glob("*.piedf-tmp")))
    check("7. barra di stato «Versione firmata salvata in …»",
          w.statusBar().currentMessage() == "Versione firmata salvata in versione_1.pdf", w.statusBar().currentMessage())
    errors.clear()
    save_answers[:] = [str(PDFS / "firme_due.pdf")]
    w.save_signed_version(0)
    check("7. sul documento aperto no: messaggio e file invariato",
          errors and (PDFS / "firme_due.pdf").read_bytes() == original, str(errors))
    errors.clear()

    shown = []

    def fake_exec(dialog):
        shown.append(dict(dialog.values))
        return 0
    signatures_panel.CertificateDetailsDialog.exec = fake_exec
    panel.context_menu(0).actions()[1].trigger(); pump(0.05)
    details = shown[0] if shown else {}
    check("7. Dettagli del certificato: soggetto, emittente, validità, serie, impronta",
          list(details) == ["Soggetto", "Emittente", "Valido dal", "Valido fino al", "Numero di serie", "Impronta SHA-256"]
          and "Mario Rossi" in details["Soggetto"] and "CA di prova PieDF" in details["Emittente"]
          and len(details["Impronta SHA-256"].replace(" ", "")) == 64, str(details))

    # --- 3. Modificato dopo la firma / 4. Non valida ----------------------------------------------
    open_signed(PDFS / "firme_modificato.pdf")
    r = rows()
    check("3. salvataggio incrementale che cambia una pagina: «Modificato dopo la firma»",
          len(r) == 1 and "Modificato dopo la firma" in r[0].split("\n"), str(r))
    check("3. pallino arancione", panel.cards
          and panel.cards[0].icon.pixmap().toImage().pixelColor(7, 7).name() == signatures_panel.STATE_COLOR[sealing.MODIFIED])
    open_signed(PDFS / "firme_alterato.pdf")
    r = rows()
    check("4. byte firmati alterati: «Non valida»", len(r) == 1 and "Non valida" in r[0].split("\n"), str(r))

    # --- 5. Sigillo personale di PieDF -----------------------------------------------------------
    folder = sealing.seal_dir(data)
    mine = work / "bloccato_da_me.pdf"
    sealing.seal(str(PDFS / "semplice.pdf"), None, str(mine), folder, None, "Luca Bianchi")
    open_signed(mine)
    r = rows()
    lines = r[0].split("\n") if r else []
    check("5. sigillo personale: «da te, sigillo personale», tipo certificazione",
          lines[:1] == ["Luca Bianchi (da te, sigillo personale)"]
          and "Certificazione (nessuna modifica consentita)" in lines and "Integra" in lines, str(lines))
    bar = w.seal_label.text() if w.seal_bar.isVisible() else ""
    check("5. la barra del sigillo dice «da te» come la scheda, con un solo processo",
          "da te (Luca Bianchi)" in bar and len(jobs) == 1 and w._locked, f"{bar} {jobs}")
    theirs = work / "bloccato_da_altri.pdf"
    sealing.seal(str(PDFS / "semplice.pdf"), None, str(theirs), work / "sigillo_altri", None, "Anna Neri")
    open_signed(theirs)
    r = rows()
    check("5. sigillo personale di un altro: «(sigillo personale)», non «da te»",
          r and r[0].split("\n")[0] == "Anna Neri (sigillo personale)", str(r))

    # --- 6. Catena verificata ---------------------------------------------------------------------
    locking.TRUST_ROOTS = [(PDFS / "firme_ca.der").read_bytes()]
    open_signed(PDFS / "firme_una.pdf")
    r = rows()
    check("6. radice di prova come fiducia: «verificata: certificato emesso da CA di prova PieDF»",
          r and "Identità verificata: certificato emesso da CA di prova PieDF, riconosciuto da Windows" in r[0].split("\n"), str(r))
    locking.TRUST_ROOTS = None
    # Come nell'exe: senza i moduli che piedf.spec esclude (oscrypto, requests, aiohttp…) la
    # catena si verifica lo stesso (#71: senza `trust_roots` esplicito pyHanko usava oscrypto).
    import ast, subprocess
    spec = ast.parse((ROOT / "piedf.spec").read_text(encoding="utf-8"))
    excluded = next(ast.literal_eval(n.value) for n in spec.body
                    if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "EXCLUDES")
    blocked = sorted({m for m in excluded if not m.startswith("PySide6")})
    probe = (
        "import sys, importlib.abc\n"
        f"BLOCKED = {blocked!r}\n"
        "class Block(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path, target=None):\n"
        "        if name.split('.')[0] in BLOCKED: raise ModuleNotFoundError(name)\n"
        "sys.meta_path.insert(0, Block())\n"
        "from pathlib import Path\n"
        "from piedf import sealing\n"
        "_, sigs = sealing.verify(sys.argv[1], None, None, roots=[Path(sys.argv[2]).read_bytes()])\n"
        "print([(s.state, s.trusted) for s in sigs])\n"
        "_, sigs = sealing.verify(sys.argv[1], None, None)\n"
        "print([(s.state, s.trusted) for s in sigs])\n"
    )
    out = subprocess.run([sys.executable, "-c", probe, str(PDFS / "firme_una.pdf"), str(PDFS / "firme_ca.der")],
                         capture_output=True, text=True, cwd=ROOT).stdout.strip().splitlines()
    check("6. senza i moduli esclusi dall'exe: catena verificata con la radice di prova, senza no",
          out[-2:] == [f"[('{sealing.INTACT}', True)]", f"[('{sealing.INTACT}', False)]"], f"{out[-2:]} bloccati: {blocked}")
    if sys.platform != "win32":
        check("6. fuori da Windows nessuna radice di sistema", sealing.system_roots() == ([], []))
    else:
        roots, _ = sealing.system_roots()
        check("6. Windows: radici dall'archivio dei certificati", len(roots) > 0, str(len(roots)))
        open_signed(PDFS / "firme_una.pdf")
        check("6. Windows: la CA di prova non è nell'archivio → «identità non verificata»",
              rows() and "Identità non verificata da PieDF" in rows()[0].split("\n"), str(rows()))

    # --- Risultato vecchio scartato ----------------------------------------------------------------
    locking.CHECK_DELAY = 2.0
    jobs.clear()
    w.open_file(str(PDFS / "firme_una.pdf")); pump(0.2)
    w.open_file(str(PDFS / "semplice.pdf"))
    pump(2.5)
    check("aperto un altro file prima della fine: scheda nascosta, nessun risultato vecchio",
          not tab_visible() and not panel.rows() and w._seal_check is None and w.signatures_note.isHidden()
          and sb.tabs.currentIndex() == PAGES_TAB)
    locking.CHECK_DELAY = 0.0

    open_signed(PDFS / "firme_una.pdf")
    w.act_close.trigger(); pump(0.1)
    check("chiudendo il documento: scheda nascosta e niente nella barra di stato",
          not tab_visible() and w.signatures_note.isHidden() and w._seal_check is None)
    check("nessun errore inatteso", not errors, str(errors))

    w.close()
    print(f"\n{sum(results)}/{len(results)} ok", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
