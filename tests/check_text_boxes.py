"""Controllo end-to-end delle caselle di testo e della data, modificabili dopo il salvataggio (#7).

    python tests/make_test_pdfs.py
    set QT_QPA_PLATFORM=offscreen
    python tests/check_text_boxes.py
"""
import queue
import shutil
import sys
import time
from multiprocessing import shared_memory
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out"
PDFS = S / "pdfs"
SHOTS = S / "shots"


def main():
    import pymupdf
    from PySide6.QtCore import QDate, QPoint, QPointF, QRectF, Qt
    from PySide6.QtGui import QColor
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox, QPlainTextEdit

    import piedf.paths as paths
    data = S / "data_testo"
    work = S / "testo"
    for d in (data, work):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    paths.data_dir = lambda: data
    SHOTS.mkdir(parents=True, exist_ok=True)

    from piedf import render_worker
    from piedf.document import PIEDF_TITLE, TextBox, open_document
    from piedf.main_window import DATE_HINT, FONT_FALLBACK, NOT_INSTALLED, TEXT_HINT, MainWindow
    from piedf.printing import print_source, remove_quietly

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
    QMessageBox.exec = lambda box: box_answers.pop(0) if box_answers else QMessageBox.StandardButton.Ok
    errors = []
    QMessageBox.critical = staticmethod(lambda parent, title, text, *a: errors.append(text))
    QInputDialog.getText = staticmethod(lambda *a, **k: ("", False))

    w = MainWindow(); w.resize(1000, 800); w.show()
    v, model = w.view, w.annotations
    vp = v.viewport()
    L = Qt.MouseButton.LeftButton
    NO = Qt.KeyboardModifier.NoModifier
    CTRL = Qt.KeyboardModifier.ControlModifier

    def copy(name, new_name=None):
        dest = work / (new_name or name)
        shutil.copy(PDFS / name, dest)
        return dest

    def at(page, x, y):
        """Punto della vista sopra (x, y) in punti della pagina come si vede."""
        return v.page_rect_to_viewport(page, QRectF(x, y, 0, 0)).topLeft().toPoint()

    def show(page, y):
        v.set_scroll_position(page, max(0.0, y - 150))
        app.processEvents()

    def click(pos):
        QTest.mouseMove(vp, pos)
        QTest.mouseClick(vp, L, NO, pos)
        app.processEvents()

    def double_click(pos):
        QTest.mouseMove(vp, pos)
        QTest.mouseDClick(vp, L, NO, pos)
        app.processEvents()

    def drag(start, end, steps=6):
        QTest.mouseMove(vp, start)
        QTest.mousePress(vp, L, NO, start)
        for i in range(1, steps + 1):
            QTest.mouseMove(vp, start + (end - start) * i / steps)
        QTest.mouseRelease(vp, L, NO, end)
        app.processEvents()

    def key(k, mod=NO):
        QTest.keyClick(app.focusWidget() or v, k, mod)
        app.processEvents()

    def write(text):
        for i, line in enumerate(text.split("\n")):
            if i:
                QTest.keyClick(v.editor, Qt.Key.Key_Return)
            if line.isascii():
                QTest.keyClicks(v.editor, line)
            else:  # QTest non sa simulare i tasti dei caratteri non ASCII
                v.editor.insertPlainText(line)
        app.processEvents()

    def add_text(page, x, y, text):
        """Testo → clic → scrivi → Esc. Restituisce la casella creata (o None)."""
        show(page, y)
        w.act_text.trigger()
        click(at(page, x, y))
        if not isinstance(v.editor, QPlainTextEdit):
            return None
        write(text)
        key(Qt.Key.Key_Escape)
        return model.items[-1] if model.items and model.items[-1].text == text else None

    def open_doc(path):
        if w.placements.modified:
            box_answers.append(QMessageBox.StandardButton.Discard)
        return w.open_file(str(path))

    def center(box):
        x0, y0, x1, y1 = box.rect
        return at(box.page, (x0 + x1) / 2, (y0 + y1) / 2)

    def save_as(dest):
        save_answers.append(str(dest))
        return w.save_as()

    def freetexts(path):
        doc = pymupdf.open(path)
        out = []
        for page in doc:
            for a in page.annots(types=[pymupdf.PDF_ANNOT_FREE_TEXT]):
                r = a.rect * page.rotation_matrix
                out.append({"page": page.number, "rect": (r.x0, r.y0, r.x1, r.y1), "text": a.info["content"],
                            "title": a.info["title"], "print": bool(a.flags & pymupdf.PDF_ANNOT_IS_PRINT)})
        doc.close()
        return out

    def page_text(path, page=0):
        doc = pymupdf.open(path)
        text = doc[page].get_text()
        doc.close()
        return text

    def inked(pix, rect, scale=1.0):
        """Pixel non bianchi (testo nero o colorato) nel rettangolo, in una pixmap della pagina come si vede."""
        x0, y0, x1, y1 = (round(c * scale) for c in rect)
        count = 0
        for y in range(max(0, y0), min(pix.height, y1)):
            for x in range(max(0, x0), min(pix.width, x1)):
                if sum(pix.pixel(x, y)[:3]) < 450:
                    count += 1
        return count

    def inked_in_file(path, page, rect):
        doc = pymupdf.open(path)
        pix = doc[page].get_pixmap(matrix=pymupdf.Matrix(2, 2))
        doc.close()
        return inked(pix, rect, 2.0)

    def inked_in_view_cache(page, rect, timeout=10):
        """Pixel scuri nel rettangolo dell'immagine del processo di render (senza la vista sopra)."""
        t = time.perf_counter()
        while time.perf_counter() - t < timeout:
            app.processEvents(); vp.repaint()
            cached = v._cache.get(page)
            if cached is not None and cached[0] == v._render_scale(page):
                scale, image = cached
                x0, y0, x1, y1 = (round(c * scale) for c in rect)
                return sum(
                    1 for y in range(y0, y1) for x in range(x0, x1, 2) if QColor(image.pixel(x, y)).lightness() < 128
                )
            time.sleep(0.02)
        return -1

    def inked_on_screen(box):
        vp.repaint()
        image = vp.grab().toImage()
        x0, y0, x1, y1 = box.rect
        r = v.page_rect_to_viewport(box.page, QRectF(x0, y0, x1 - x0, y1 - y0)).toAlignedRect()
        dpr = image.devicePixelRatio()
        return sum(
            1 for y in range(r.top() + 2, r.bottom() - 2) for x in range(r.left() + 2, r.right() - 2, 2)
            if QColor(image.pixel(round(x * dpr), round(y * dpr))).lightness() < 128
        )

    # --- 1. Testo su semplice.pdf, salvato come FreeText di PieDF ---
    src = copy("semplice.pdf")
    check("1. apertura semplice.pdf", w.open_file(str(src)))
    check("1. Testo e Data in toolbar e menu Firma, Ctrl+T",
          w.act_text.isEnabled() and w.act_date.isEnabled() and w.act_text.shortcut().toString() == "Ctrl+T"
          and w.act_text.iconText() == "T" and w.act_date.iconText() == "Data")
    show(0, 300)
    w.act_text.trigger()
    check("1. modalità Testo con suggerimento", v.text_tool and w.statusBar().currentMessage() == TEXT_HINT)
    click(at(0, 100, 300))
    check("1. clic: casella larga 180 pt con il cursore", isinstance(v.editor, QPlainTextEdit) and v.editor.hasFocus()
          and v.editing_box is not None and abs((v.editing_box.rect[2] - v.editing_box.rect[0]) - 180) < 0.01
          and not v.text_tool, str(v.editing_box and v.editing_box.rect))
    check("1. barra delle proprietà visibile in modifica", w.text_bar.isVisible())
    write("Mario Rossi")
    click(at(0, 450, 450))  # clic fuori: conferma
    box = model.items[0] if model.items else None
    check("1. clic fuori conferma, si esce dalla modalità", box is not None and box.text == "Mario Rossi"
          and v.editor is None and not v.text_tool and w.placements.modified, str(box))
    check("1. la casella si vede nella vista (disegnata dal modello)", box is not None and inked_on_screen(box) > 20)
    QTest.qWait(50)
    vp.grab().save(str(SHOTS / "testo_1.png"))
    out1 = work / "semplice_testo.pdf"
    check("1. salvataggio", save_as(out1) and not errors, str(errors))
    ft = freetexts(out1)
    check("1. get_text contiene «Mario Rossi»", "Mario Rossi" in page_text(out1))
    check("1. una FreeText con titolo PieDF, stampabile", len(ft) == 1 and ft[0]["title"] == PIEDF_TITLE and ft[0]["print"], str(ft))
    check("1. render del file salvato: pixel scuri nel rettangolo", ft and inked_in_file(out1, 0, ft[0]["rect"]) > 30)

    # --- 2. Riapertura: nel modello, non nel render; spostata e modificata ---
    box = model.items[0] if model.items else None
    check("2. dopo il salvataggio la casella è nel modello, modificabile",
          len(model.items) == 1 and box.xref > 0 and box.text == "Mario Rossi" and not w.placements.modified)
    check("2. niente doppioni: il processo di render non la disegna",
          box is not None and inked_in_view_cache(0, box.rect) == 0)
    check("2. ma la vista sì", box is not None and inked_on_screen(box) > 20)
    old_rect = box.rect
    drag(center(box), center(box) + QPoint(round(60 * v._scale()), round(100 * v._scale())))
    moved = (box.rect[0] - old_rect[0], box.rect[1] - old_rect[1])
    check("2. trascinata di (60, 100) pt", abs(moved[0] - 60) < 2 and abs(moved[1] - 100) < 2, str(moved))
    double_click(center(box))
    check("2. doppio clic: di nuovo in modifica", v.editing_box is box and isinstance(v.editor, QPlainTextEdit))
    v.editor.selectAll()
    write("Maria Rossi")
    key(Qt.Key.Key_Escape)
    check("2. testo cambiato", box.text == "Maria Rossi" and v.editor is None)
    new_rect = box.rect
    check("2. salvataggio sullo stesso file", w.save() and not errors, str(errors))
    ft = freetexts(out1)
    check("2. una sola FreeText con «Maria Rossi» nella nuova posizione",
          len(ft) == 1 and ft[0]["text"] == "Maria Rossi" and all(abs(a - b) < 0.5 for a, b in zip(ft[0]["rect"], new_rect)),
          str(ft))
    check("2. «Mario Rossi» non c'è più", "Mario Rossi" not in page_text(out1) and "Maria Rossi" in page_text(out1))

    # --- 3. Eliminata dopo la riapertura ---
    box = model.items[0]
    show(0, box.rect[1])
    click(center(box))
    check("3. clic: selezionata, barra delle proprietà visibile", model.selected is box and w.text_bar.isVisible())
    key(Qt.Key.Key_Delete)
    check("3. Canc la elimina", not model.items and not w.text_bar.isVisible())
    check("3. salvataggio", w.save() and not errors, str(errors))
    check("3. nessuna FreeText PieDF nel file", not [f for f in freetexts(out1) if f["title"] == PIEDF_TITLE])

    # --- 4. FreeText di un altro programma ---
    other = copy("testo_altro.pdf")
    check("4. apertura testo_altro.pdf", w.open_file(str(other)))
    check("4. la FreeText di un altro programma non è nel modello", not model.items)
    check("4. ma si vede nel render", inked_in_view_cache(0, (300, 500, 500, 530)) > 10)
    show(0, 500)
    click(at(0, 400, 515))
    check("4. clic sopra: non si seleziona", model.selected is None and v.editor is None)
    add_text(0, 100, 600, "Mia nota")
    check("4. salvataggio con una casella PieDF", save_as(work / "altro_salvato.pdf") and not errors, str(errors))
    ft = freetexts(work / "altro_salvato.pdf")
    foreign = [f for f in ft if f["title"] == "Mario Bianchi"]
    check("4. quella dell'altro programma intatta",
          len(ft) == 2 and len(foreign) == 1 and foreign[0]["text"] == "Nota di un altro"
          and all(abs(a - b) < 0.01 for a, b in zip(foreign[0]["rect"], (300, 500, 500, 530))), str(ft))

    # --- 5. Data ---
    src = copy("semplice.pdf", "data.pdf")
    open_doc(src)
    today = QDate.currentDate().toString("dd/MM/yyyy")
    show(0, 400)
    w.act_date.trigger()
    check("5. modalità Data con suggerimento", v.text_tool and w.statusBar().currentMessage() == DATE_HINT)
    click(at(0, 120, 400))
    box = model.items[0] if model.items else None
    check("5. casella già confermata con la data di oggi", box is not None and box.text == today and v.editor is None
          and not v.text_tool and model.selected is box, str(box))
    check("5. la data non va a capo", box is not None and abs((box.rect[3] - box.rect[1]) - box.size * 1.2) < 0.01)
    w.size_box.setValue(30)
    check("5. ingrandita: la casella si allarga, la data resta su una riga (MuPDF non spezza le parole)",
          box is not None and box.size == 30 and abs((box.rect[3] - box.rect[1]) - 36) < 0.01
          and box.rect[2] - box.rect[0] > 120, str(box and box.rect))
    w.size_box.setValue(11)
    save_as(work / "data_salvata.pdf")
    check("5. il file contiene la data gg/mm/aaaa", today in page_text(work / "data_salvata.pdf"), today)

    # --- 6. Caratteri accentati ed euro, con i tre font ---
    src = copy("semplice.pdf", "accenti.pdf")
    open_doc(src)
    sample = "àèéìòù €"
    for i, font in enumerate(("Helv", "TiRo", "Cour")):
        w.view.text_style["font"] = font
        add_text(0, 100, 300 + 60 * i, sample)
    check("6. tre caselle", len(model.items) == 3 and [b.font for b in model.items] == ["Helv", "TiRo", "Cour"])
    save_as(work / "accenti_salvato.pdf")
    lines = [line for line in page_text(work / "accenti_salvato.pdf").splitlines() if line.startswith("à")]
    check("6. «àèéìòù €» uguale in get_text con Helvetica, Times e Courier", lines == [sample] * 3, str(lines))
    w.view.text_style["font"] = "Helv"

    # --- 7. Pagine ruotate: testo nel rettangolo visto e dritto ---
    src = copy("ruotate.pdf")
    open_doc(src)
    for page in range(4):
        add_text(page, 100, 300, "Ruota")
    check("7. una casella per pagina", [b.page for b in model.items] == [0, 1, 2, 3])
    expected = {b.page: b.rect for b in model.items}
    out7 = work / "ruotate_testo.pdf"
    save_as(out7)
    ft = freetexts(out7)
    doc = pymupdf.open(out7)
    for page in range(4):
        pg = doc[page]
        f = [x for x in ft if x["page"] == page]
        same_rect = len(f) == 1 and all(abs(a - b) < 0.5 for a, b in zip(f[0]["rect"], expected[page]))
        lines = [
            line for block in pg.get_text("dict")["blocks"] for line in block.get("lines", [])
            if "".join(s["text"] for s in line["spans"]) == "Ruota"
        ]
        upright, inside = False, False
        if lines:
            m = pg.rotation_matrix
            d = pymupdf.Point(lines[0]["dir"]) * m - pymupdf.Point(0, 0) * m  # direzione come si vede
            upright = abs(d.x - 1) < 0.01 and abs(d.y) < 0.01
            bbox = pymupdf.Rect(lines[0]["bbox"]) * m
            x0, y0, x1, y1 = expected[page]
            # Il bbox del testo comprende tutto l'occhio del font: sporge un poco sopra la riga.
            inside = bbox.x0 >= x0 - 1 and bbox.x1 <= x1 + 1 and bbox.y0 >= y0 - 4 and bbox.y1 <= y1 + 1
        pix = pg.get_pixmap(matrix=pymupdf.Matrix(2, 2))
        check(f"7. pagina /Rotate {pg.rotation}: rettangolo, testo dritto e dentro, pixel scuri",
              same_rect and upright and inside and inked(pix, expected[page], 2.0) > 20,
              f"rect={same_rect} dritto={upright} dentro={inside} {bbox if lines else ''}")
    doc.close()

    # --- 8. Maniglia laterale: larghezza, a capo, font invariato ---
    src = copy("semplice.pdf", "larghezza.pdf")
    open_doc(src)
    box = add_text(0, 100, 300, "uno due tre quattro cinque sei sette otto")
    x0, y0, x1, y1 = box.rect
    size, height = box.size, y1 - y0
    click(center(box))
    handle = at(0, x1, (y0 + y1) / 2)
    QTest.mouseMove(vp, handle); app.processEvents()
    check("8. cursore orizzontale sulla maniglia laterale", vp.cursor().shape() == Qt.CursorShape.SizeHorCursor)
    drag(handle, handle - QPoint(round(100 * v._scale()), 0))
    width = box.rect[2] - box.rect[0]
    check("8. la larghezza cambia, il lato sinistro resta", abs(width - 80) < 2 and abs(box.rect[0] - x0) < 0.01, f"{width:.1f}")
    check("8. il font no, le righe aumentano", box.size == size and box.rect[3] - box.rect[1] > height + 1,
          f"{height:.1f} → {box.rect[3] - box.rect[1]:.1f}")
    corner = at(0, box.rect[2], box.rect[3])
    QTest.mouseMove(vp, corner); app.processEvents()
    check("8. niente maniglie d'angolo", vp.cursor().shape() != Qt.CursorShape.SizeFDiagCursor)

    # --- 9. Formato: salvato e riletto uguale, default delle prossime ---
    click(center(box))
    w.font_box.setCurrentIndex(w.font_box.findData("TiRo")); w.font_box.activated.emit(w.font_box.currentIndex())
    w.size_box.setValue(18)
    w.color_box.setCurrentIndex(w.color_box.findData("#1a3fbf")); w.color_box.activated.emit(w.color_box.currentIndex())
    w.align_actions[2].trigger()
    app.processEvents()
    check("9. formato applicato alla casella", (box.font, box.size, box.color, box.align) == ("TiRo", 18.0, "#1a3fbf", 2),
          str((box.font, box.size, box.color, box.align)))
    s = w._settings
    check("9. ricordato in settings.ini", (s.value("text/font"), float(s.value("text/size")), s.value("text/color"),
                                           int(s.value("text/align"))) == ("TiRo", 18.0, "#1a3fbf", 2))
    expected = (box.text, box.font, box.size, box.color, box.align, box.rect)
    save_as(work / "formato.pdf")
    box = model.items[0] if model.items else None
    got = box and (box.text, box.font, box.size, box.color, box.align, box.rect)
    check("9. riletto uguale alla riapertura", got is not None and got[:5] == expected[:5]
          and all(abs(a - b) < 0.01 for a, b in zip(got[5], expected[5])), f"{got} != {expected}")
    nuova = add_text(0, 100, 500, "Default")
    check("9. le prossime caselle usano il formato scelto",
          nuova is not None and (nuova.font, nuova.size, nuova.color, nuova.align) == ("TiRo", 18.0, "#1a3fbf", 2))
    w2 = MainWindow()
    check("9. il formato resta anche in una nuova finestra",
          w2.view.text_style == {"font": "TiRo", "size": 18.0, "color": "#1a3fbf", "align": 2}, str(w2.view.text_style))
    w2._renderer.shutdown(); w2.deleteLater()
    w._set_text_style(font="Helv", size=11.0, color="#000000", align=0)

    # --- 10. Casella vuota; annulla/ripeti ---
    src = copy("semplice.pdf", "annulla.pdf")
    open_doc(src)
    stack = w.placements.undo_stack
    show(0, 300)
    w.act_text.trigger()
    click(at(0, 100, 300))
    write("   ")
    key(Qt.Key.Key_Escape)
    check("10. casella vuota alla conferma: nessun elemento", not model.items and stack.count() == 0 and v.editor is None)
    box = add_text(0, 100, 300, "Annullami")
    check("10. creata", box is not None and stack.count() == 1)
    key(Qt.Key.Key_Z, CTRL)
    check("10. Ctrl+Z: creazione annullata", not model.items)
    key(Qt.Key.Key_Y, CTRL)
    check("10. Ctrl+Y: di nuovo", model.items == [box])
    start = box.rect
    drag(center(box), center(box) + QPoint(round(50 * v._scale()), 0))
    moved = box.rect
    key(Qt.Key.Key_Z, CTRL)
    check("10. Ctrl+Z: spostamento annullato", box.rect == start)
    key(Qt.Key.Key_Y, CTRL)
    check("10. Ctrl+Y: spostata di nuovo", box.rect == moved)
    double_click(center(box))
    v.editor.selectAll()
    write("Cambiato")
    key(Qt.Key.Key_Escape)
    key(Qt.Key.Key_Z, CTRL)
    check("10. Ctrl+Z: testo annullato", box.text == "Annullami", box.text)
    key(Qt.Key.Key_Y, CTRL)
    check("10. Ctrl+Y: testo di nuovo", box.text == "Cambiato", box.text)
    click(center(box))
    key(Qt.Key.Key_Delete)
    check("10. eliminata", not model.items)
    key(Qt.Key.Key_Z, CTRL)
    check("10. Ctrl+Z: eliminazione annullata", model.items == [box])
    key(Qt.Key.Key_Y, CTRL)
    check("10. Ctrl+Y: eliminata di nuovo", not model.items)
    double_click(at(0, 300, 300))  # niente sotto: non succede niente
    box = add_text(0, 100, 300, "Svuotami")
    double_click(center(box))
    v.editor.selectAll()
    key(Qt.Key.Key_Delete)
    key(Qt.Key.Key_Escape)
    check("10. svuotata in modifica: sparisce", not model.items)
    key(Qt.Key.Key_Z, CTRL)
    check("10. Ctrl+Z: torna", model.items == [box] and box.text == "Svuotami")

    # --- 11. Font installati sul PC, incorporati nel PDF (#59) ---
    from pymupdf import mupdf

    from piedf import fonts
    from piedf.annotations import fitted_rect
    from piedf.document import longest_word, text_box_height, text_box_lines

    def box_font(path):
        """(BaseFont, file del font incorporato) del font nell'aspetto della prima FreeText di PieDF."""
        doc = pymupdf.open(path)
        try:
            for page in doc:
                for a in page.annots(types=[pymupdf.PDF_ANNOT_FREE_TEXT]):
                    ap = int(doc.xref_get_key(a.xref, "AP/N")[1].split()[0])
                    kind, ref = doc.xref_get_key(ap, f"Resources/Font/{fonts.RESOURCE}")
                    if kind != "xref":
                        continue
                    fx = int(ref.split()[0])
                    cid = int(doc.xref_get_key(fx, "DescendantFonts")[1].strip("[] ").split()[0])
                    desc = int(doc.xref_get_key(cid, "FontDescriptor")[1].split()[0])
                    files = [k for k in ("FontFile2", "FontFile3") if doc.xref_get_key(desc, k)[0] == "xref"]
                    return doc.xref_get_key(fx, "BaseFont")[1], bool(files)
        finally:
            doc.close()
        return None

    def choose_font(family):
        index = w.font_box.findData(family)
        w.font_box.setCurrentIndex(index)
        w.font_box.activated.emit(index)
        app.processEvents()

    installed = fonts.installed()
    # Georgia e Segoe UI ci sono in ogni Windows; altrove (il worker su Linux) un font libero.
    choice = next((f for f in ("Georgia", "Segoe UI", "DejaVu Serif", "Liberation Serif", "DejaVu Sans")
                   if f in installed), None)
    check("11. un font installato da provare", choice is not None, str(sorted(installed)))
    t = time.perf_counter()
    w._fill_font_box()
    elapsed = time.perf_counter() - t
    check("11. elenco dei font costruito in meno di 100 ms", elapsed < 0.1, f"{elapsed * 1000:.0f} ms, {w.font_box.count()} voci")
    items = [w.font_box.itemData(i) for i in range(w.font_box.count()) if w.font_box.itemData(i)]
    recent = w._recent_fonts()
    rest = items[len(recent) + 3:]
    check("11. ordine: ultimi usati, Helvetica/Times/Courier, poi gli altri in ordine alfabetico",
          items[:len(recent)] == recent and items[len(recent):len(recent) + 3] == ["Helv", "TiRo", "Cour"]
          and rest == sorted(rest, key=str.casefold) and choice in rest, str(items[:12]))
    index = w.font_box.findData(choice)
    item_font = w.font_box.itemData(index, Qt.ItemDataRole.FontRole)
    check("11. ogni font scritto nel suo font", item_font is not None and item_font.family() == choice)

    src = copy("semplice.pdf", "font.pdf")
    open_doc(src)
    sample = "Città € 10"  # 10 caratteri
    box = add_text(0, 100, 300, sample)
    click(center(box))
    choose_font(choice)
    w.size_box.setValue(14)
    w.color_box.setCurrentIndex(w.color_box.findData("#c00000")); w.color_box.activated.emit(w.color_box.currentIndex())
    w.align_actions[1].trigger()
    app.processEvents()
    check("11. font scelto applicato alla casella", box.font == choice and box.size == 14, str(box))
    check("11. tra gli ultimi usati, in cima all'elenco", w._recent_fonts()[0] == choice and w.font_box.itemData(0) == choice
          and w._settings.value("text/recent_fonts", [], type=list)[0] == choice)
    check("11. la vista disegna la casella col font scelto", inked_on_screen(box) > 20)
    expected = (box.text, box.font, box.size, box.color, box.align)
    out11 = work / "font_salvato.pdf"
    check("11. salvataggio", save_as(out11) and not errors, str(errors))
    font = box_font(out11)
    check("11. 1: nel PDF il font è incorporato, in subset", font is not None and font[1]
          and font[0].lstrip("/")[6:7] == "+" and choice.replace(" ", "")[:5] in font[0].replace("#20", ""), str(font))
    check("11. 1: get_text estrae il testo uguale", sample in page_text(out11), repr(page_text(out11)))
    added = out11.stat().st_size - (PDFS / "semplice.pdf").stat().st_size
    check("11. 6: 10 caratteri nel font incorporato aggiungono meno di 200 KB", added < 200 * 1024, f"{added / 1024:.0f} KB")
    check("11. il render del file salvato ha il testo", inked_in_file(out11, 0, box.rect) > 30)
    box = model.items[0] if model.items else None
    got = box and (box.text, box.font, box.size, box.color, box.align)
    check("11. 2: riaperto: stesso font, dimensione, colore e allineamento", got == expected and box.embedded is not None,
          f"{got} != {expected}")
    click(center(box))
    check("11. 2: il menu mostra il font", w.font_box.currentData() == choice and w.font_box.currentText() == choice)
    double_click(center(box))
    v.editor.selectAll()
    write("Città € 11 nuovo")
    key(Qt.Key.Key_Escape)
    check("11. 2: modificabile, il font resta", box.text == "Città € 11 nuovo" and box.font == choice)
    check("11. 2: salvato di nuovo", w.save() and not errors and "Città € 11 nuovo" in page_text(out11), str(errors))

    # 3. La misura segue il font scelto.
    word = "Precipitevolissimevolmente"
    cour, other = longest_word(word, "Cour", 14), longest_word(word, choice, 14)
    narrow = TextBox(0, (0, 0, 20, 0), text=word, font=choice, size=14)
    wide = TextBox(0, (0, 0, 20, 0), text=word, font="Cour", size=14)
    check("11. 3: larghezza minima: Courier e il font scelto danno larghezze diverse, la casella le rispetta",
          abs(cour - other) > 5 and abs(fitted_rect(narrow, QRectF(0, 0, 20, 0)).width() - other) < 0.01
          and abs(fitted_rect(wide, QRectF(0, 0, 20, 0)).width() - cour) < 0.01, f"{cour:.1f} / {other:.1f}")
    text = "uno due tre quattro cinque sei sette otto nove dieci"
    lines_cour, lines_other = text_box_lines(text, "Cour", 12, 120), text_box_lines(text, choice, 12, 120)
    check("11. 3: a capo e altezza con le misure del font scelto",
          lines_cour != lines_other and text_box_height(text, choice, 12, 120) == lines_other * 1.2 * 12,
          f"Courier {lines_cour} righe, {choice} {lines_other}")
    helvetica = fonts.Face(mupdf.fz_new_base14_font("Helvetica"))
    samples = ["a b c", "Prova di testo che va a capo più volte", "parolalunghissimaeoltre e poi altro",
               " spazi   in   mezzo  ", "riga\n\nvuota in mezzo", "€ città  x  y ", "lungo di "]
    same = all(len(fonts.break_lines(s, helvetica, 11, wdt)) == text_box_lines(s, "Helv", 11, wdt)
               for s in samples for wdt in (30, 60, 100))
    check("11. 3: a capo come MuPDF (stesse righe con Helvetica)", same)

    # 4. Font che non si possono incorporare, e senza contorni.
    def patched(name, fs_type=None, glyf=False):
        data = bytearray(Path(installed[choice]).read_bytes())
        count = int.from_bytes(data[4:6], "big")
        for i in range(count):
            rec = 12 + 16 * i
            tag, offset = bytes(data[rec:rec + 4]), int.from_bytes(data[rec + 8:rec + 12], "big")
            if tag == b"OS/2" and fs_type is not None:
                data[offset + 8:offset + 10] = fs_type.to_bytes(2, "big")
            if glyf and tag in (b"glyf", b"CFF "):
                data[rec:rec + 4] = b"xxxx"
        folder = work / name
        folder.mkdir()
        path = folder / ("prova" + Path(installed[choice]).suffix)
        path.write_bytes(data)
        return folder, path

    allowed, allowed_file = patched("font_permesso", fs_type=0)
    restricted, restricted_file = patched("font_ristretto", fs_type=0x0002)
    bitmap, bitmap_file = patched("font_bitmap", glyf=True)
    info = fonts.font_info(restricted_file)
    check("11. 4: fsType 0x0002 letto: incorporamento vietato", info is not None and not info.embeddable
          and fonts.font_info(allowed_file).embeddable)
    check("11. 4: il font con fsType restrittivo non è nell'elenco, lo stesso con fsType 0 sì",
          choice not in fonts.scan([restricted]) and fonts.scan([allowed]).get(choice) == str(allowed_file))
    check("11. 4: nemmeno un font senza contorni", not fonts.font_info(bitmap_file).outlines and not fonts.scan([bitmap]))

    # 5. Font non installato alla riapertura (simulato).
    original_list = fonts._installed
    fonts._installed = {k: p for k, p in original_list.items() if k != choice}
    fonts._faces.clear()
    w._fill_font_box()
    try:
        open_doc(out11)
        box = model.items[0] if model.items else None
        check("11. 5: riaperto senza il font: la casella ha ancora il suo font", box is not None and box.font == choice
              and box.embedded is not None)
        click(center(box))
        check("11. 5: il menu lo segna «non installato»",
              w.font_box.currentData() == choice and w.font_box.currentText() == NOT_INSTALLED.format(choice),
              w.font_box.currentText())
        check("11. 5: l'aspetto resta quello salvato (font incorporato)", inked_on_screen(box) > 20)
        check("11. 5: finché non si tocca il testo non cambia niente", model.pending() is None)
        font_before = box_font(out11)
        drag(center(box), center(box) + QPoint(0, round(40 * v._scale())))
        check("11. 5: spostata e salvata: stesso font incorporato, riusato", save_as(out11) and not errors
              and box_font(out11) == font_before and "Città € 11 nuovo" in page_text(out11),
              f"{errors} {box_font(out11)} {font_before} {page_text(out11)!r}")
        box = model.items[0]
        double_click(center(box))
        v.editor.selectAll()
        write("nuovo 11 Città")
        key(Qt.Key.Key_Escape)
        check("11. 5: testo nuovo con i caratteri incorporati: il font resta", box.font == choice and not errors)
        check("11. 5: salvato col font incorporato", save_as(out11) and box_font(out11) == font_before
              and "nuovo 11 Città" in page_text(out11))
        box = model.items[0]
        w.statusBar().clearMessage()
        double_click(center(box))
        v.editor.selectAll()
        write("Zebra")
        key(Qt.Key.Key_Escape)
        check("11. 5: caratteri che mancano: passa a Helvetica con un messaggio",
              box.font == "Helv" and w.statusBar().currentMessage() == FONT_FALLBACK.format(choice),
              f"{box.font} {w.statusBar().currentMessage()!r}")
        check("11. 5: salvato in Helvetica, nessun errore", save_as(out11) and not errors and "Zebra" in page_text(out11)
              and box_font(out11) is None, str(errors))
    finally:
        fonts._installed = original_list
        fonts._faces.clear()
        w._fill_font_box()

    # Stampa e Organizza pagine trattano queste caselle come le altre.
    src = copy("semplice.pdf", "font_pagine.pdf")
    open_doc(src)
    box = add_text(0, 100, 300, "Stampa font")
    click(center(box))
    choose_font(choice)
    path, temp = print_source(str(src), None, [], {}, [], model.pending())
    check("11. stampa: il temporaneo ha la casella col font incorporato", temp is not None
          and "Stampa font" in page_text(path) and (box_font(path) or (0, False))[1])
    remove_quietly(temp)
    saved = work / "font_pagine_salvato.pdf"
    save_as(saved)
    w.rotate_current(90)
    t = time.perf_counter()
    while w._page_job is not None and time.perf_counter() - t < 60:
        app.processEvents(); time.sleep(0.005)
    pump(0.1)
    box = model.items[0] if len(model.items) == 1 else None
    check("11. pagine: ruotata, la casella resta col suo font e il font incorporato",
          box is not None and box.font == choice and box.embedded is not None and (box_font(w._path) or (0, False))[1],
          str(box))
    w._set_text_style(font="Helv", size=11.0, color="#000000", align=0)
    w.activateWindow()  # dopo il dialog dell'avanzamento
    app.processEvents()
    if w.modified:
        box_answers.append(QMessageBox.StandardButton.Discard)

    # --- Link: in modalità Testo e con una casella selezionata non scattano ---
    src = copy("link.pdf")
    open_doc(src)
    followed = []
    v.link_goto.connect(lambda *a: followed.append(a))
    v.link_uri.connect(lambda *a: followed.append(a))
    show(0, 100)
    inked_in_view_cache(0, (0, 0, 1, 1))  # aspetta il render e i link
    pump(0.3)
    link_pos = at(0, 150, 112)  # link interno verso pagina 3
    w.act_text.trigger()
    click(link_pos)
    check("link: in modalità Testo il clic su un link apre la casella", not followed and isinstance(v.editor, QPlainTextEdit))
    write("Sopra il link")
    key(Qt.Key.Key_Escape)
    box = model.items[0] if model.items else None
    click(at(0, 450, 700))
    check("link: casella sopra il link selezionabile", box is not None and (click(center(box)) or model.selected is box)
          and not followed)
    click(at(0, 500, 470))  # deseleziona
    check("link: senza strumenti il link funziona ancora", (click(at(0, 250, 172)) or followed) and followed, str(followed))

    # --- Stampa: le caselle non salvate si stampano, quelle salvate anche ---
    path, temp = print_source(str(src), None, [], {}, [], model.pending())
    check("stampa: con caselle non salvate si stampa un temporaneo", temp is not None
          and any(f["text"] == "Sopra il link" for f in freetexts(path)))
    results_q, acks = queue.Queue(), queue.Queue()
    # Le pagine arrivano nella memoria condivisa (#34), non nel messaggio.
    shm = shared_memory.SharedMemory(create=True, size=595 * 842 * 3 * 2)
    render_worker.print_pages(path, None, [(0, 1.0)], [shm.name], acks, results_q)
    msg = results_q.get(timeout=10)
    pix = None
    if msg[0] == "page":
        _, _, width, height, stride, _ = msg
        pix = pymupdf.Pixmap(pymupdf.csRGB, width, height, bytes(shm.buf[:stride * height]), False)
    shm.close()
    shm.unlink()
    check("stampa: il processo di stampa disegna la casella", pix is not None and inked(pix, box.rect) > 20)
    remove_quietly(temp)
    path, temp = print_source(str(out1), None, [], {}, [], None)
    check("stampa: senza modifiche si stampa il file così com'è", path == str(out1) and temp is None)

    # --- Organizza / Ruota pagina (#26): prima si salvano le caselle, che poi restano modificabili ---
    def wait_job(limit=60.0):
        t = time.perf_counter()
        while w._page_job is not None and time.perf_counter() - t < limit:
            app.processEvents(); time.sleep(0.005)
        pump(0.1)
        return w._page_job is None

    src = copy("semplice.pdf", "pagine.pdf")
    open_doc(src)
    box = add_text(0, 100, 300, "Prima di ruotare")
    box_answers.append(QMessageBox.StandardButton.Cancel)
    w.organize_pages()
    check("pagine: Organizza con una casella non salvata chiede di salvare (Annulla)",
          not box_answers and w._page_job is None and model.pending() is not None and w._original is None)
    box_answers.append(QMessageBox.StandardButton.Cancel)
    w.rotate_current(90)
    check("pagine: anche Ruota pagina corrente", not box_answers and w._page_job is None and model.pending() is not None)
    saved = work / "pagine_testo.pdf"
    box_answers.append(QMessageBox.StandardButton.Save)
    save_answers.append(str(saved))
    w.rotate_current(90)
    check("pagine: Salva e poi ruota", wait_job() and w._original == str(saved) and w.modified, str(errors))
    box = model.items[0] if len(model.items) == 1 else None
    check("pagine: la casella salvata è nel temporaneo e torna nel modello", box is not None
          and box.text == "Prima di ruotare" and model.pending() is None
          and [f["text"] for f in freetexts(w._path)] == ["Prima di ruotare"])
    if box is not None:
        show(box.page, box.rect[1])
    check("pagine: il processo di render la toglie (nessun doppione)", box is not None and inked_in_view_cache(0, box.rect) == 0)
    check("pagine: la vista disegna la casella nel temporaneo", box is not None and inked_on_screen(box) > 20)
    click(center(box))
    key(Qt.Key.Key_Delete)
    rotated = work / "pagine_ruotata.pdf"
    check("pagine: nel temporaneo la casella si elimina", not model.items and save_as(rotated))
    doc = pymupdf.open(rotated)
    check("pagine: salvato ruotato e senza la casella", doc[0].rotation == 90 and not freetexts(rotated))
    doc.close()

    # --- Il documento aperto dalla GUI resta chiuso; open_document legge le caselle ---
    doc = open_document(str(work / "formato.pdf"))
    check("open_document: PdfDocument.annotations", len(doc.annotations) == 1 and isinstance(doc.annotations[0], TextBox))

    box_answers.append(QMessageBox.StandardButton.Discard)
    w.close(); app.processEvents()
    print(f"\n{sum(results)}/{len(results)} ok", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
