"""Controllo end-to-end del salvataggio del PDF firmato (#5).

    python tests/make_test_pdfs.py
    set QT_QPA_PLATFORM=offscreen
    python tests/check_save.py
"""
import os
import shutil
import stat
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out"
PDFS = S / "pdfs"
SHOTS = S / "shots"


def main():
    import pymupdf
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter, QPen
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox

    import piedf.paths as paths
    data = S / "data_salvataggio"
    work = S / "salvataggio"
    for d in (data, work):
        if d.exists():
            for root, dirs, _ in os.walk(d):  # la cartella in sola lettura di un giro precedente
                for name in dirs:
                    os.chmod(Path(root) / name, stat.S_IRWXU)
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    paths.data_dir = lambda: data
    SHOTS.mkdir(parents=True, exist_ok=True)

    from piedf.main_window import MainWindow
    from piedf.placements import Placement
    from piedf.signatures import Kind
    from piedf.writer import TMP_SUFFIX, SaveError, save_with_signatures

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

    def copy(name, new_name=None):
        dest = work / (new_name or name)
        shutil.copy(PDFS / name, dest)
        return dest

    w = MainWindow(); w.resize(1000, 800); w.show()
    v, model = w.view, w.placements

    # Firma 3:1: tratto blu spesso su sfondo trasparente.
    img = QImage(600, 200, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img); p.setPen(QPen(QColor("#1a237e"), 14)); p.drawLine(10, 180, 590, 20); p.end()
    sig = w.signatures.add(img, Kind.SIGNATURE)
    # Firma con la tacca: tutta blu, con un quadrato rosso in alto a sinistra.
    img = QImage(600, 200, QImage.Format.Format_ARGB32)
    img.fill(QColor("#1a237e"))
    p = QPainter(img); p.fillRect(0, 0, 80, 80, QColor("#ff0000")); p.end()
    notch = w.signatures.add(img, Kind.SIGNATURE)

    def pl(page, x, y, signature=sig, width=150.0):
        return Placement(page, QRectF(x, y, width, width / 3), signature.path, Kind.SIGNATURE)

    def render(path, page, password=None, scale=2.0):
        doc = pymupdf.open(path)
        if password:
            doc.authenticate(password)
        pix = doc[page].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        doc.close()
        return pix

    def pixels(pix, rect_pt, scale=2.0):
        """Pixel (x, y, rgb) dentro un rettangolo in punti della pagina come si vede."""
        x0, y0 = max(0, int(rect_pt.left() * scale)), max(0, int(rect_pt.top() * scale))
        x1, y1 = min(pix.width, int(rect_pt.right() * scale)), min(pix.height, int(rect_pt.bottom() * scale))
        for y in range(y0, y1):
            for x in range(x0, x1):
                yield x, y, pix.pixel(x, y)

    def white(rgb):
        return min(rgb) >= 250

    # --- 1. semplice.pdf + una firma ---
    src = PDFS / "semplice.pdf"
    out1 = work / "semplice_firmato.pdf"
    p1 = pl(0, 300, 400)
    save_with_signatures(str(src), None, [p1], str(out1))
    doc = pymupdf.open(out1)
    imgs, text = doc[0].get_images(), doc[0].get_text()
    doc.close()
    limit = src.stat().st_size + sig.path.stat().st_size + 20 * 1024
    check("1. immagine nella pagina", len(imgs) == 1, str(imgs))
    check("1. testo ancora testo", "PieDF - documento semplice" in text)
    check("1. dimensione < originale + PNG + 20 KB", out1.stat().st_size < limit, f"{out1.stat().st_size} < {limit}")
    check("1. niente .piedf-tmp", not list(work.glob("*" + TMP_SUFFIX)))

    # --- 2. Render: inchiostro dentro, bianco a 20 pt ---
    pix = render(out1, 0)
    pix.save(str(SHOTS / "salvataggio_semplice.png"))
    ink = sum(1 for *_, c in pixels(pix, p1.rect) if not white(c))
    ring = p1.rect.adjusted(-20, -20, 20, 20)
    outside = [c for x, y, c in pixels(pix, ring) if not p1.rect.adjusted(-19, -19, 19, 19).contains(x / 2, y / 2)]
    check("2. inchiostro dentro il rettangolo", ink > 500, f"{ink} px")
    check("2. bianco a 20 pt fuori", outside and all(white(c) for c in outside), f"{len(outside)} px")

    # --- 3. ruotate.pdf: tacca in alto a sinistra su 0/90/180/270 ---
    src = PDFS / "ruotate.pdf"
    out3 = work / "ruotate_firmato.pdf"
    doc = pymupdf.open(src)
    sizes = [(pg.rect.width, pg.rect.height) for pg in doc]
    rotations = [pg.rotation for pg in doc]
    doc.close()
    rects = [pl(i, sizes[i][0] - 250, sizes[i][1] - 150, notch) for i in range(4)]
    save_with_signatures(str(src), None, rects, str(out3))
    for i, r in enumerate(rects):
        pix = render(out3, i)
        pix.save(str(SHOTS / f"salvataggio_ruotata_{rotations[i]}.png"))
        red = [(x / 2, y / 2) for x, y, c in pixels(pix, r.rect.adjusted(-5, -5, 5, 5)) if c[0] > 200 and c[1] < 80 and c[2] < 80]
        blue = sum(1 for *_, c in pixels(pix, r.rect) if c[2] > 80 and c[0] < 80)
        stray = sum(1 for x, y, c in pixels(pix, r.rect.adjusted(-20, -20, 20, 20))
                    if not r.rect.adjusted(-1, -1, 1, 1).contains(x / 2, y / 2) and not white(c))
        cx = sum(x for x, _ in red) / len(red) if red else -1
        cy = sum(y for _, y in red) / len(red) if red else -1
        mid = r.rect.center()
        check(f"3. rotate {rotations[i]}: tacca in alto a sinistra, firma al suo posto",
              red and cx < mid.x() and cy < mid.y() and r.rect.left() <= cx and r.rect.top() <= cy
              and blue > 0.6 * r.rect.width() * r.rect.height() * 4 and stray == 0,
              f"tacca ({cx:.0f}, {cy:.0f}) in rect ({r.rect.left():.0f}, {r.rect.top():.0f}, {r.rect.width():.0f}×{r.rect.height():.0f}), fuori {stray}")

    # --- 4. Stesso PNG piazzato tre volte (due pagine) → un solo xref ---
    out4 = work / "lungo_stesso_png.pdf"
    save_with_signatures(str(PDFS / "lungo_350.pdf"), None, [pl(0, 100, 300), pl(0, 300, 500), pl(1, 100, 300)], str(out4))
    doc = pymupdf.open(out4)
    # Riusando l'xref la pagina ha due nomi di risorsa (fzImg0, fzImg1) per lo stesso oggetto.
    xrefs = {im[0] for im in doc[0].get_images() + doc[1].get_images()}
    objects = sum(1 for x in range(1, doc.xref_length()) if doc.xref_get_key(x, "Subtype")[1] == "/Image")
    rects = len(doc[0].get_image_rects(min(xrefs))) if xrefs else 0
    check("4. una sola immagine nel file (+ la sua maschera alfa)", len(xrefs) == 1 and objects == 2 and rects == 2,
          f"xref {xrefs}, {objects} oggetti immagine, {rects} posizioni a pagina 1")
    doc.close()

    # --- 5. cifrato.pdf: resta cifrato con la stessa password ---
    out5 = work / "cifrato_firmato.pdf"
    save_with_signatures(str(PDFS / "cifrato.pdf"), "pie", [pl(0, 300, 400)], str(out5))
    doc = pymupdf.open(out5)
    needs = doc.needs_pass
    wrong = doc.authenticate("sbagliata")
    auth = doc.authenticate("pie")
    check("5. cifrato: chiede ancora la password, con «pie» mostra la firma",
          needs and not wrong and auth and len(doc[0].get_images()) == 1, doc.metadata.get("encryption"))
    doc.close()
    doc = pymupdf.open(out5)
    check("5. cifrato: owner password conservata", doc.authenticate("owner") == 4)
    doc.close()

    # --- 6. lungo_350.pdf, firma a pagina 200 ---
    out6 = work / "lungo_firmato.pdf"
    save_with_signatures(str(PDFS / "lungo_350.pdf"), None, [pl(199, 300, 400)], str(out6))
    doc = pymupdf.open(out6)
    with_images = [i for i in range(doc.page_count) if doc[i].get_images()]
    check("6. 350 pagine, firma solo a pagina 200", doc.page_count == 350 and with_images == [199], str(with_images))
    doc.close()

    # Firma con il PNG eliminato: l'immagine arriva dalla memoria.
    gone = w.signatures.add(QImage(str(sig.path)), Kind.SIGNATURE)
    data_png = gone.path.read_bytes()
    w.signatures.delete(gone)
    out_gone = work / "png_eliminato.pdf"
    save_with_signatures(str(PDFS / "semplice.pdf"), None, [pl(0, 300, 400, gone)], str(out_gone), {gone.path: data_png})
    doc = pymupdf.open(out_gone)
    check("PNG eliminato: firma scritta dalla memoria", len(doc[0].get_images()) == 1)
    doc.close()
    try:
        save_with_signatures(str(PDFS / "semplice.pdf"), None, [pl(0, 300, 400, gone)], str(work / "x.pdf"))
        check("PNG mancante senza memoria → SaveError", False)
    except SaveError as e:
        check("PNG mancante senza memoria → SaveError", not (work / "x.pdf").exists(), str(e).splitlines()[0])

    # --- 8a. Cartella non scrivibile → SaveError, niente temporaneo ---
    if sys.platform != "win32" and os.geteuid() != 0:
        ro = work / "sola_lettura"
        ro.mkdir()
        os.chmod(ro, stat.S_IRUSR | stat.S_IXUSR)
        try:
            save_with_signatures(str(PDFS / "semplice.pdf"), None, [pl(0, 300, 400)], str(ro / "out.pdf"))
            check("8. cartella non scrivibile → SaveError", False)
        except SaveError as e:
            check("8. cartella non scrivibile → SaveError", "Impossibile scrivere" in str(e), str(e).splitlines()[0])
        check("8. nessun .piedf-tmp rimasto", not list(ro.iterdir()))
        os.chmod(ro, stat.S_IRWXU)
    else:
        skip("8. cartella non scrivibile", "(chmod non vale su Windows o da root)")

    # --- 8b. File bloccato (solo Windows: un file aperto non si può sostituire) ---
    if sys.platform == "win32":
        locked = copy("semplice.pdf", "bloccato.pdf")
        handle = open(locked, "rb")
        try:
            save_with_signatures(str(PDFS / "semplice.pdf"), None, [pl(0, 300, 400)], str(locked))
            check("8. file bloccato → SaveError", False)
        except SaveError as e:
            check("8. file bloccato → SaveError", True, str(e).splitlines()[0])
        handle.close()
        check("8. file bloccato: nessun .piedf-tmp, originale intatto",
              not list(work.glob("*" + TMP_SUFFIX)) and locked.read_bytes() == (PDFS / "semplice.pdf").read_bytes())
    else:
        skip("8. file bloccato", "(solo Windows)")

    # --- GUI: dialog finti ---
    save_dialogs = []  # (cartella/nome proposto); risposta in save_answers
    save_answers = []
    def fake_save(parent, caption, proposed, filter_=""):
        save_dialogs.append(proposed)
        return (save_answers.pop(0) if save_answers else ""), ""
    QFileDialog.getSaveFileName = staticmethod(fake_save)
    errors = []
    QMessageBox.critical = staticmethod(lambda parent, title, text, *a: errors.append(text))
    boxes = []
    box_answers = []
    def fake_exec(box):
        boxes.append((box.text(), sorted(b.text() for b in box.buttons())))
        return box_answers.pop(0)
    QMessageBox.exec = fake_exec
    passwords = []
    def fake_get_text(*a, **k):
        passwords.append(a[2] if len(a) > 2 else "")
        return "pie", True
    QInputDialog.getText = staticmethod(fake_get_text)

    def wait_rendered(timeout=15):
        t = time.perf_counter()
        while time.perf_counter() - t < timeout:
            app.processEvents()
            v.viewport().repaint()
            vis = list(v._visible_range())
            if vis and all(p in v._cache and v._cache[p][0] == v._render_scale(p) for p in vis):
                return True
            time.sleep(0.01)
        return False

    # Azioni e menu.
    check("Salva e Salva con nome disabilitati senza documento", not w.act_save.isEnabled() and not w.act_save_as.isEnabled())
    file_menu = w.menuBar().actions()[0].menu()
    labels = [a.text() for a in file_menu.actions()]
    check("menu File con Salva / Salva con nome", "&Salva" in labels and "Salva &con nome…" in labels, str(labels))
    check("scorciatoie Ctrl+S / Ctrl+Shift+S",
          w.act_save.shortcut().toString() == "Ctrl+S" and w.act_save_as.shortcut().toString() == "Ctrl+Shift+S")

    # --- 7. Primo Salva = Salva con nome, poi sovrascrittura dello stesso file aperto ---
    lungo = copy("lungo_350.pdf", "contratto.pdf")
    w.open_file(str(lungo))
    v.set_zoom(1.5); v.go_to_page(4); pump(0.2)
    check("Salva disabilitato senza modifiche, Salva con nome abilitato", not w.act_save.isEnabled() and w.act_save_as.isEnabled())
    model.add(pl(4, 300, 400))
    check("Salva abilitato con modifiche", w.act_save.isEnabled())
    dest = work / "contratto_firmato.pdf"
    save_answers.append(str(dest))
    QTest.keyClick(v, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier); app.processEvents()
    check("primo Salva propone <nome>_firmato.pdf nella stessa cartella",
          save_dialogs == [str(work / "contratto_firmato.pdf")], str(save_dialogs))
    check("salvato: il viewer mostra il file salvato, stessa pagina e zoom",
          w._path == str(dest) and v.current_page == 4 and abs(v.zoom - 1.5) < 1e-9,
          f"{w._path}, pagina {v.current_page + 1}, zoom {v.zoom}")
    check("salvato: titolo senza *, barra di stato", w.windowTitle() == "contratto_firmato.pdf – PieDF"
          and w.statusBar().currentMessage() == "Salvato in contratto_firmato.pdf",
          f"{w.windowTitle()} / {w.statusBar().currentMessage()}")
    check("originale intatto", lungo.read_bytes() == (PDFS / "lungo_350.pdf").read_bytes())
    check("10. dopo il salvataggio: stack vuoto, Salva disabilitato",
          model.undo_stack.count() == 0 and model.items == [] and not w.act_save.isEnabled())
    QTest.keyClick(v, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier); app.processEvents()
    check("10. Ctrl+Z non fa nulla", model.items == [] and w.windowTitle() == "contratto_firmato.pdf – PieDF")
    check("firma visibile nel viewer dopo la riapertura", wait_rendered())
    v.grab().save(str(SHOTS / "salvataggio_riaperto.png"))

    model.add(pl(4, 100, 600))
    n = len(save_dialogs)
    size_before = dest.stat().st_size
    w.act_save.trigger(); app.processEvents()

    def signature_count(path, page):
        doc = pymupdf.open(path)
        count = len(doc[page].get_image_info())  # get_image_rects confronta il contenuto, non l'xref
        doc.close()
        return count

    count = signature_count(dest, 4)
    check("7. secondo Salva: stesso file senza chiedere", len(save_dialogs) == n and not errors, str(errors))
    check("7. sovrascrittura del file aperto: riaperto, stessa pagina, senza *",
          w._path == str(dest) and v.current_page == 4 and w.windowTitle() == "contratto_firmato.pdf – PieDF"
          and abs(v.zoom - 1.5) < 1e-9, f"{w.windowTitle()}, pagina {v.current_page + 1}")
    grown = dest.stat().st_size - size_before
    check("7. il file ha tutte e due le firme e nessun temporaneo",
          count == 2 and not list(work.glob("*" + TMP_SUFFIX)), f"{count} firme")
    check("7. salvataggio incrementale compresso (< PNG + 20 KB in più)",
          0 < grown < sig.path.stat().st_size + 20 * 1024, f"+{grown} byte")
    check("7. il render riparte sul file sovrascritto", wait_rendered())

    # --- 8c. Errore dalla GUI: messaggio, documento ancora modificato ---
    if sys.platform != "win32" and os.geteuid() != 0:
        ro = work / "sola_lettura"
        os.chmod(ro, stat.S_IRUSR | stat.S_IXUSR)
        model.add(pl(4, 300, 200))
        save_answers.append(str(ro / "x.pdf"))
        w.act_save_as.trigger(); app.processEvents()
        check("8. GUI: messaggio d'errore, documento ancora modificato",
              len(errors) == 1 and "Impossibile scrivere" in errors[0] and model.modified and len(model.items) == 1
              and w.windowTitle().startswith("* ") and w._path == str(dest) and not list(ro.iterdir()), w.windowTitle())
        os.chmod(ro, stat.S_IRWXU)
        model.undo_stack.undo()
    else:
        skip("8. GUI: cartella non scrivibile", "(chmod non vale su Windows o da root)")

    # Errore sovrascrivendo il file aperto: il viewer continua a funzionare.
    model.add(pl(4, 300, 200))
    os.chmod(dest, stat.S_IRUSR)
    if sys.platform == "win32":
        handle = open(dest, "rb")
    n_err = len(errors)
    w.act_save.trigger(); app.processEvents()
    if sys.platform == "win32":
        handle.close()
    os.chmod(dest, stat.S_IRUSR | stat.S_IWUSR)
    if sys.platform == "win32" or os.geteuid() != 0:
        check("errore sul file aperto: messaggio, firme ancora modificabili", len(errors) == n_err + 1
              and model.modified and w._path == str(dest), errors[-1].splitlines()[0] if errors else "")
        v._clear_cache(); v.reload()
        check("errore sul file aperto: il render riparte", wait_rendered())
    model.undo_stack.undo()

    # Cifrato dalla GUI: dopo il salvataggio non richiede la password.
    cif = copy("cifrato.pdf")
    w.open_file(str(cif))
    n_pw = len(passwords)
    model.add(pl(0, 300, 400))
    save_answers.append(str(work / "cifrato_gui.pdf"))
    w.act_save.trigger(); app.processEvents()
    check("cifrato: riaperto dopo il salvataggio senza chiedere la password",
          w._path == str(work / "cifrato_gui.pdf") and len(passwords) == n_pw == 1 and not model.modified, str(passwords))
    doc = pymupdf.open(work / "cifrato_gui.pdf")
    check("cifrato: il file salvato chiede la password", doc.needs_pass and doc.authenticate("pie"))
    doc.close()

    # --- 9. firmato_digitale.pdf: avviso, con Annulla non si salva ---
    fd = copy("firmato_digitale.pdf")
    w.open_file(str(fd))
    model.add(pl(0, 100, 400))
    save_answers.append(str(work / "firmato_digitale_firmato.pdf"))
    box_answers.append(QMessageBox.StandardButton.Cancel)
    n_box = len(boxes)
    w.act_save.trigger(); app.processEvents()
    warned = boxes[n_box:]
    check("9. avviso firme digitali", len(warned) == 1 and warned[0][0] ==
          "Questo documento contiene firme digitali. Aggiungere la firma le renderà non più valide in Acrobat. Continuare?"
          and warned[0][1] == ["Annulla", "Continua"], str(warned))
    check("9. con Annulla non si salva niente", not (work / "firmato_digitale_firmato.pdf").exists()
          and model.modified and w._path == str(fd))
    save_answers.append(str(work / "firmato_digitale_firmato.pdf"))
    box_answers.append(QMessageBox.StandardButton.Ok)
    w.act_save.trigger(); app.processEvents()
    check("9. con Continua si salva", (work / "firmato_digitale_firmato.pdf").exists() and not model.modified)

    # Documento senza firme digitali: nessun avviso (già visto sopra con contratto.pdf).
    check("nessun avviso senza firme digitali", all("firme digitali" not in t for t, _ in boxes[:n_box]))

    # --- Conferma di #4 con il pulsante Salva ---
    model.add(pl(0, 300, 200))
    box_answers.append(QMessageBox.StandardButton.Save)
    box_answers.append(QMessageBox.StandardButton.Ok)  # avviso firme digitali
    n_box = len(boxes)
    ok = w.open_file(str(PDFS / "semplice.pdf"))
    check("conferma: Salva, Scarta, Annulla", boxes[n_box][1] == ["Annulla", "Salva", "Scarta"], str(boxes[n_box:]))
    n_imgs = signature_count(work / "firmato_digitale_firmato.pdf", 0)
    check("conferma con Salva: salvato (stesso file) e aperto il nuovo",
          ok and w._path == str(PDFS / "semplice.pdf") and n_imgs == 2 and not model.modified, f"{n_imgs} firme")
    model.add(pl(0, 300, 200))
    box_answers.append(QMessageBox.StandardButton.Save)
    n_dlg = len(save_dialogs)
    ok = w.open_file(str(PDFS / "orientamenti.pdf"))  # Salva → Salva con nome → Annulla
    check("conferma con Salva e poi Annulla nel dialog: resta aperto", ok is False and len(save_dialogs) == n_dlg + 1
          and w._path == str(PDFS / "semplice.pdf") and model.modified)
    box_answers.append(QMessageBox.StandardButton.Discard)
    w.close(); app.processEvents()

    # --- 11. Gli altri controlli si lanciano a parte (vedi PR) ---
    print(f"\n{sum(results)}/{len(results)} ok", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
