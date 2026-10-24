"""Controllo end-to-end della compilazione dei moduli PDF (#21).

    python tests/make_test_pdfs.py
    set QT_QPA_PLATFORM=offscreen
    python tests/check_forms.py
"""
import shutil
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
    from PySide6.QtWidgets import (
        QApplication, QComboBox, QFileDialog, QInputDialog, QLineEdit, QListWidget, QMessageBox, QPlainTextEdit,
    )

    import piedf.paths as paths
    data = S / "data_moduli"
    work = S / "moduli"
    for d in (data, work):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    paths.data_dir = lambda: data
    SHOTS.mkdir(parents=True, exist_ok=True)

    from piedf.document import CHECKBOX, COMBO, LIST, OFF, RADIO, SIGNATURE, TEXT, open_document
    from piedf.main_window import FORM_MESSAGE, XFA_MESSAGE, MainWindow
    from piedf.placements import Placement
    from piedf.signatures import Kind

    app = QApplication(["t"])
    results = []

    def check(name, ok, info=""):
        results.append(bool(ok))
        print(("OK  " if ok else "FAIL"), name, info, flush=True)

    def pump(sec=0.05):
        t = time.perf_counter()
        while time.perf_counter() - t < sec:
            app.processEvents(); time.sleep(0.01)

    # Dialog finti.
    save_answers = []
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: ((save_answers.pop(0) if save_answers else ""), ""))
    box_answers = []
    boxes = []
    def fake_exec(box):
        boxes.append(box.text())
        return box_answers.pop(0) if box_answers else QMessageBox.StandardButton.Ok
    QMessageBox.exec = fake_exec
    errors = []
    QMessageBox.critical = staticmethod(lambda parent, title, text, *a: errors.append(text))
    QInputDialog.getText = staticmethod(lambda *a, **k: ("pie", True))

    w = MainWindow(); w.resize(1000, 800); w.show()
    v, forms, model = w.view, w.forms, w.placements
    vp = v.viewport()
    L = Qt.MouseButton.LeftButton
    NO = Qt.KeyboardModifier.NoModifier
    CTRL = Qt.KeyboardModifier.ControlModifier

    def field(name, index=0):
        return [f for f in forms.fields if f.name == name][index]

    def field_pos(f):
        x0, y0, x1, y1 = f.rect
        return v.page_rect_to_viewport(f.page, QRectF(x0, y0, x1 - x0, y1 - y0)).center().toPoint()

    def click_field(f):
        v._ensure_visible(f)
        app.processEvents()
        pos = field_pos(f)
        QTest.mouseMove(vp, pos)
        QTest.mouseClick(vp, L, NO, pos)
        app.processEvents()

    def key(k, mod=NO):
        QTest.keyClick(app.focusWidget() or v, k, mod)
        app.processEvents()

    def dark_in_render(f, timeout=10):
        """Pixel scuri nel rettangolo del campo nell'immagine della pagina mostrata dal viewer."""
        t = time.perf_counter()
        count = 0
        while time.perf_counter() - t < timeout:
            app.processEvents()
            vp.repaint()
            cached = v._cache.get(f.page)
            if cached is not None and cached[0] == v._render_scale(f.page):
                scale, image = cached
                x0, y0, x1, y1 = (round(c * scale) for c in f.rect)
                count = 0
                for y in range(y0 + 2, y1 - 2):
                    for x in range(x0 + 2, x1 - 2, 2):
                        if QColor(image.pixel(x, y)).lightness() < 100:
                            count += 1
                if count > 20:
                    return count
            time.sleep(0.02)
        return count

    def widgets(path, password=None):
        doc = pymupdf.open(path)
        if password:
            doc.authenticate(password)
        values = {}
        for page in doc:
            for wd in page.widgets():
                key_ = wd.field_name + (f":{wd.on_state()}" if wd.field_type == RADIO else "")
                values[key_] = wd.field_value
        doc.close()
        return values

    # --- 1. Barra informativa e campi letti ---
    src = work / "modulo.pdf"
    shutil.copy(PDFS / "modulo.pdf", src)
    original = src.read_bytes()
    check("1. apertura modulo.pdf", w.open_file(str(src)))
    app.processEvents()
    check("1. barra informativa con Evidenzia campi", w.form_bar.isVisible() and w.form_label.text() == FORM_MESSAGE
          and w.highlight_button.isVisible() and w.highlight_button.isChecked(), w.form_label.text())
    types = {f.name: f.type for f in forms.fields}
    expected = {"Nome": TEXT, "Note": TEXT, "CAP": TEXT, "Codice": TEXT, "Privacy": CHECKBOX, "Scelta": RADIO,
                "Provincia": COMBO, "Colore": LIST, "Firma": SIGNATURE, "Citta": TEXT}
    check("1. tutti i campi con il tipo giusto", types == expected and len(forms.fields) == 12, str(types))
    rects = {f.name: tuple(round(c) for c in f.rect) for f in forms.fields}
    check("1. rettangoli di pagina 1", rects["Nome"] == (150, 100, 400, 120) and rects["Colore"] == (150, 420, 300, 480)
          and [tuple(round(c) for c in f.rect) for f in forms.fields if f.name == "Scelta"]
          == [(150, 340, 166, 356), (200, 340, 216, 356), (250, 340, 266, 356)], str(rects))
    citta = field("Citta")
    x0, y0, x1, y1 = citta.rect
    check("1. pagina 2 ruotata: rettangolo come si vede, dentro 842×595",
          citta.page == 1 and v._sizes[1] == (842, 595) and 0 <= x0 < x1 <= 842 and 0 <= y0 < y1 <= 595
          and (round(x0), round(y0), round(x1), round(y1)) == (100, 100, 400, 120), str(citta.rect))
    check("1. proprietà: max_len, sola lettura, multiriga, voci",
          field("CAP").max_len == 5 and field("Codice").readonly and field("Codice").value == "ABC-123"
          and field("Note").multiline and [t for _, t in field("Provincia").choices] == ["Milano", "Roma", "Napoli", "Torino"]
          and [t for _, t in field("Colore").choices] == ["Rosso", "Verde", "Blu"]
          and all(f.value == OFF for f in forms.fields if f.type == RADIO))
    pump(0.5)
    vp.repaint()
    v.grab().save(str(SHOTS / "moduli_evidenziati.png"))

    # Evidenziazione: azzurra sopra il campo, ricordata nelle impostazioni.
    image = v.grab().toImage()
    pos = field_pos(field("Nome"))
    tint = QColor(image.pixel(pos.x() + 60, pos.y()))
    check("1. evidenziazione azzurra", tint.blue() > tint.red() + 20, tint.name())
    w.highlight_button.click()
    image = v.grab().toImage()
    plain = QColor(image.pixel(pos.x() + 60, pos.y()))
    check("evidenzia campi spento: niente azzurro, salvato in settings.ini",
          plain.name() == "#ffffff" and w._settings.value("highlight_fields", type=bool) is False, plain.name())
    w.highlight_button.click()

    # --- 2. Testo semplice: scrivo e confermo con Invio ---
    nome = field("Nome")
    click_field(nome)
    editor = v.editor
    check("2. clic sul testo: editor sul posto", isinstance(editor, QLineEdit) and v.editing_field is nome
          and editor.hasFocus(), type(editor).__name__)
    geo = editor.geometry() if editor else None
    expected_geo = v.page_rect_to_viewport(0, QRectF(150, 100, 250, 20)).toAlignedRect()
    check("2. editor sopra il campo", geo == expected_geo, f"{geo} vs {expected_geo}")
    QTest.keyClicks(editor, "Mario Rossi")
    key(Qt.Key.Key_Return)
    check("2. Invio: valore confermato, editor chiuso, titolo con *", nome.value == "Mario Rossi" and v.editor is None
          and w.windowTitle().startswith("* ") and w.act_save.isEnabled(), w.windowTitle())
    dark = dark_in_render(nome)
    check("2. il render della pagina mostra il testo prima del salvataggio", dark > 20 and src.read_bytes() == original,
          f"{dark} px scuri")
    vp.repaint()
    v.grab().save(str(SHOTS / "moduli_compilato.png"))

    # Il processo di render riapre il file (salvataggio fallito, riavvio): i valori cambiati si rimandano.
    # (Uccidere il processo mentre aspetta sulla coda ne lascerebbe il lock preso: non è il caso reale.)
    w._renderer.close(w._gen, wait=True)
    w._renderer.open(w._gen, str(src), None)
    v._clear_cache(); v.reload()
    dark = dark_in_render(nome, 20)
    check("2. riaprendo il file nel processo di render il valore c'è ancora", dark > 20, f"{dark} px scuri")

    # Esc annulla la modifica in corso.
    click_field(nome)
    v.editor.selectAll()
    QTest.keyClicks(v.editor, "xyz")
    key(Qt.Key.Key_Escape)
    check("Esc annulla la modifica", nome.value == "Mario Rossi" and v.editor is None and model.undo_stack.count() == 1)

    # Zoom e scroll: l'editor segue il campo, il font scala.
    click_field(nome)
    size = v.editor.font().pixelSize()
    v.set_zoom(v.zoom * 2)
    app.processEvents()
    x0, y0, x1, y1 = nome.rect
    expected_geo = v.page_rect_to_viewport(0, QRectF(x0, y0, x1 - x0, y1 - y0)).toAlignedRect()
    check("editor riposizionato con lo zoom, font scalato",
          v.editor.geometry() == expected_geo and abs(v.editor.font().pixelSize() - 2 * size) <= 1,
          f"{v.editor.geometry()} vs {expected_geo}, font {size} → {v.editor.font().pixelSize()}")
    v.verticalScrollBar().setValue(v.verticalScrollBar().value() + 50)
    app.processEvents()
    expected_geo = v.page_rect_to_viewport(0, QRectF(x0, y0, x1 - x0, y1 - y0)).toAlignedRect()
    check("editor riposizionato con lo scroll", v.editor.geometry() == expected_geo)
    key(Qt.Key.Key_Escape)
    w.act_fit_width.trigger()
    app.processEvents()

    # Multiriga: Invio va a capo, Ctrl+Invio conferma.
    note = field("Note")
    click_field(note)
    check("multiriga: editor a più righe", isinstance(v.editor, QPlainTextEdit))
    QTest.keyClicks(v.editor, "riga uno")
    key(Qt.Key.Key_Return)
    QTest.keyClicks(v.editor, "riga due")
    key(Qt.Key.Key_Return, CTRL)
    check("multiriga: Invio a capo, Ctrl+Invio conferma", note.value == "riga uno\nriga due" and v.editor is None,
          repr(note.value))

    # --- 3. max_len e sola lettura ---
    cap = field("CAP")
    click_field(cap)
    QTest.keyClicks(v.editor, "abcdefgh")
    key(Qt.Key.Key_Return)
    check("3. max_len 5: restano 5 caratteri", cap.value == "abcde", repr(cap.value))
    click_field(field("Codice"))
    check("3. il campo in sola lettura non si apre", v.editor is None and field("Codice").value == "ABC-123")

    # --- 4. Casella, annulla e ripeti ---
    privacy = field("Privacy")
    click_field(privacy)
    check("4. clic: casella spuntata", privacy.checked and privacy.value == "Yes")
    QTest.keyClick(v, Qt.Key.Key_Z, CTRL); app.processEvents()
    check("4. Ctrl+Z: non spuntata", not privacy.checked and privacy.value == OFF)
    QTest.keyClick(v, Qt.Key.Key_Y, CTRL); app.processEvents()
    check("4. Ctrl+Y: di nuovo spuntata", privacy.checked)
    check("4. testo del menu Annulla", w.act_undo.text() == "Annulla casella «Privacy»", w.act_undo.text())

    # --- 5. Radio, combo, list ---
    radios = [f for f in forms.fields if f.name == "Scelta"]
    click_field(radios[1])
    check("5. radio: solo il 2° acceso", [r.checked for r in radios] == [False, True, False])
    click_field(radios[0])
    check("5. radio: clic sul 1° spegne il 2°", [r.checked for r in radios] == [True, False, False])
    QTest.keyClick(v, Qt.Key.Key_Z, CTRL); app.processEvents()
    check("5. radio: Ctrl+Z torna al 2°", [r.checked for r in radios] == [False, True, False])

    combo = field("Provincia")
    click_field(combo)
    editor = v.editor
    check("5. combo: si apre la scelta", isinstance(editor, QComboBox) and editor.count() == 4)
    editor.hidePopup()
    app.processEvents()
    QTest.keyClick(editor, Qt.Key.Key_Down); QTest.keyClick(editor, Qt.Key.Key_Down)
    app.processEvents()
    check("5. combo: 3ª voce scelta", combo.value == "Napoli", combo.value)
    key(Qt.Key.Key_Return)

    lst = field("Colore")
    click_field(lst)
    editor = v.editor
    check("5. list: elenco delle voci", isinstance(editor, QListWidget) and editor.count() == 3)
    item_pos = editor.visualItemRect(editor.item(1)).center()
    QTest.mouseClick(editor.viewport(), L, NO, item_pos)
    app.processEvents()
    check("5. list: 2ª voce scelta", lst.value == "Verde", lst.value)
    key(Qt.Key.Key_Escape)

    # --- 6. Tab e Maiusc+Tab ---
    v.go_to_page(0); app.processEvents()
    click_field(nome)
    names = []
    for _ in range(9):
        key(Qt.Key.Key_Tab)
        names.append(v.focus_field.name if v.focus_field else None)
    check("6. Tab percorre i campi nell'ordine della pagina e passa alla pagina 2",
          names == ["Note", "CAP", "Privacy", "Scelta", "Scelta", "Scelta", "Provincia", "Colore", "Citta"]
          and v.current_page == 1 and isinstance(v.editor, QLineEdit), str(names))
    rect = v.editor.geometry() if v.editor else None
    check("6. campo della pagina ruotata in vista, editor orizzontale",
          rect is not None and vp.rect().contains(rect) and rect.width() > rect.height(), str(rect))
    QTest.keyClicks(v.editor, "Torino")
    key(Qt.Key.Key_Backtab)
    check("6. Maiusc+Tab torna indietro e conferma", v.focus_field is lst and citta.value == "Torino"
          and v.current_page == 0, v.focus_field.name if v.focus_field else "")
    key(Qt.Key.Key_Backtab)
    key(Qt.Key.Key_Backtab)
    check("6. con Tab sui radio: Spazio accende", v.focus_field is radios[2] and v.editor is None)
    key(Qt.Key.Key_Space)
    check("6. Spazio sul 3° radio", [r.checked for r in radios] == [False, False, True])
    key(Qt.Key.Key_Escape)
    check("Esc toglie il campo attivo", v.focus_field is None)

    # --- 9a. Campo firma: niente editor ---
    click_field(field("Firma"))
    check("9. clic sul campo firma: niente editor", v.editor is None and v.focus_field is None)

    # --- 7. Salvataggio: i campi restano campi ---
    expected_values = {"Nome": "Mario Rossi", "Note": "riga uno\nriga due", "CAP": "abcde", "Privacy": "Yes",
                       "Scelta:Uno": "Off", "Scelta:Due": "Off", "Scelta:Tre": "Tre", "Provincia": "Napoli",
                       "Colore": "Verde", "Citta": "Torino", "Codice": "ABC-123"}
    dest = work / "modulo_firmato.pdf"
    save_answers.append(str(dest))
    box_answers.append(QMessageBox.StandardButton.Ok)  # avviso "firme digitali" (c'è un campo firma)
    w.act_save.trigger(); app.processEvents()
    saved = widgets(dest)
    check("7. salvato senza errori", dest.exists() and not errors and w._path == str(dest)
          and not w.windowTitle().startswith("*"), str(errors))
    check("7. i valori sono quelli scritti", all(saved.get(k) == v_ for k, v_ in expected_values.items()),
          str({k: saved.get(k) for k in expected_values}))
    doc = pymupdf.open(dest)
    count = sum(1 for page in doc for _ in page.widgets())
    images = sum(len(page.get_images()) for page in doc)
    doc.close()
    check("7. i campi sono ancora widget, nessuna immagine", count == 12 and images == 0, f"{count} widget")
    check("7. l'originale non è cambiato", src.read_bytes() == original)
    check("7. riaperto in PieDF con i valori salvati", field("Nome").value == "Mario Rossi" and field("Privacy").checked
          and field("Scelta", 2).checked and model.undo_stack.count() == 0)
    click_field(field("Privacy"))
    click_field(field("Nome"))
    v.editor.selectAll()
    QTest.keyClicks(v.editor, "Luigi Bianchi")
    key(Qt.Key.Key_Return)
    check("7. dopo la riapertura i campi si cambiano di nuovo", not field("Privacy").checked
          and field("Nome").value == "Luigi Bianchi" and w.windowTitle().startswith("* "))
    w.act_save.trigger(); app.processEvents()  # stesso file: incrementale
    saved = widgets(dest)
    check("7. secondo salvataggio sullo stesso file", saved.get("Nome") == "Luigi Bianchi" and saved.get("Privacy") == "Off"
          and saved.get("CAP") == "abcde" and not errors, str(errors))

    # --- 8. Campo + firma in un solo salvataggio; modulo cifrato ---
    sig_img = QImage(600, 200, QImage.Format.Format_ARGB32)
    sig_img.fill(Qt.GlobalColor.transparent)
    p = QPainter(sig_img); p.setPen(QPen(QColor("#1a237e"), 14)); p.drawLine(10, 180, 590, 20); p.end()
    sig = w.signatures.add(sig_img, Kind.SIGNATURE)
    cif = work / "modulo_cifrato.pdf"
    shutil.copy(PDFS / "modulo_cifrato.pdf", cif)
    check("8. modulo cifrato aperto con la password", w.open_file(str(cif)) and len(forms.fields) == 12)
    model.add(Placement(0, QRectF(150, 520, 150, 50), sig.path, Kind.SIGNATURE))
    click_field(field("Nome"))
    QTest.keyClicks(v.editor, "Anna Verdi")
    key(Qt.Key.Key_Return)
    click_field(field("Privacy"))
    check("8. firma e campi nello stesso annulla/ripeti", model.undo_stack.count() == 3)
    dest = work / "modulo_cifrato_firmato.pdf"
    save_answers.append(str(dest))
    box_answers.append(QMessageBox.StandardButton.Ok)
    w.act_save.trigger(); app.processEvents()
    doc = pymupdf.open(dest)
    needs = doc.needs_pass
    auth = doc.authenticate("pie")
    nome_v = [wd.field_value for wd in doc[0].widgets() if wd.field_name == "Nome"]
    privacy_v = [wd.field_value for wd in doc[0].widgets() if wd.field_name == "Privacy"]
    images = len(doc[0].get_images())
    doc.close()
    check("8. un solo salvataggio con firma e campi", nome_v == ["Anna Verdi"] and privacy_v == ["Yes"] and images == 1,
          f"{nome_v} {privacy_v} {images} immagini")
    check("8. resta cifrato con la stessa password", needs and auth)

    # Conferma alla chiusura con un campo cambiato.
    click_field(field("CAP"))
    QTest.keyClicks(v.editor, "12345")
    n_box = len(boxes)
    box_answers.append(QMessageBox.StandardButton.Cancel)
    w.act_close.trigger(); app.processEvents()
    check("conferma alla chiusura con l'editor ancora aperto", boxes[n_box:] ==
          ["Le modifiche a «modulo_cifrato_firmato.pdf» non sono state salvate."] and w._path == str(dest)
          and field("CAP").value == "12345", str(boxes[n_box:]))
    model.undo_stack.undo()

    # --- 9b. PDF senza campi e XFA ---
    w.open_file(str(PDFS / "semplice.pdf"))
    app.processEvents()
    QTest.mouseClick(vp, L, NO, vp.rect().center()); app.processEvents()
    check("9. PDF senza campi: nessuna barra, nessun editor", not w.form_bar.isVisible() and v.editor is None
          and not forms.fields)
    w.open_file(str(PDFS / "modulo_xfa.pdf"))
    app.processEvents()
    check("9. XFA: messaggio e nessun campo modificabile", w.form_bar.isVisible() and w.form_label.text() == XFA_MESSAGE
          and not w.highlight_button.isVisible() and not forms.fields, w.form_label.text())
    QTest.mouseClick(vp, L, NO, field_pos(open_document(str(PDFS / "modulo.pdf")).fields[0])); app.processEvents()
    check("9. XFA: clic su un campo non apre niente", v.editor is None)

    # --- 10. Modulo lungo: 50 pagine, 1000 campi ---
    t0 = time.perf_counter()
    ok = w.open_file(str(PDFS / "modulo_lungo.pdf"))
    topen = time.perf_counter() - t0
    check("10. modulo lungo: apertura sotto 1 s", ok and len(forms.fields) == 1000 and topen < 1, f"{topen:.2f}s")

    def wait_rendered(timeout=15):
        t = time.perf_counter()
        while time.perf_counter() - t < timeout:
            app.processEvents()
            vp.repaint()
            vis = list(v._visible_range())
            if vis and all(p in v._cache and v._cache[p][0] == v._render_scale(p) for p in vis):
                return time.perf_counter() - t
            time.sleep(0.01)
        return None

    wait_rendered()
    gaps, last = [], time.perf_counter()
    for _ in range(120):
        v.verticalScrollBar().setValue(v.verticalScrollBar().value() + 300)
        app.processEvents(); vp.repaint()
        now = time.perf_counter(); gaps.append(now - last); last = now
    dt = wait_rendered()
    check("10. scroll veloce, poi render delle pagine visibili", dt is not None and dt < 3,
          f"{dt and round(dt, 2)}s, ridisegno max {max(gaps) * 1000:.0f} ms")
    check("10. ridisegno con 1000 campi evidenziati fluido", max(gaps) < 0.15, f"{max(gaps) * 1000:.0f} ms")
    v.go_to_page(48); app.processEvents()
    click_field(forms.tab_order[-21])  # ultimo campo della pagina 49
    key(Qt.Key.Key_Tab)
    check("10. Tab dall'ultimo campo di una pagina al primo della successiva",
          v.focus_field is forms.tab_order[-20] and v.editor is not None and vp.rect().contains(v.editor.geometry()),
          v.focus_field.name if v.focus_field else "")
    key(Qt.Key.Key_Escape)

    box_answers.append(QMessageBox.StandardButton.Discard)
    w.close(); app.processEvents()
    # --- 11. Gli altri controlli si lanciano a parte (vedi PR) ---
    print(f"\n{sum(results)}/{len(results)} ok", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
