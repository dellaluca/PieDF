"""Controllo end-to-end del disegno: penna, evidenziatore e gomma, modificabili dopo il salvataggio (#22).

    python tests/make_test_pdfs.py
    set QT_QPA_PLATFORM=offscreen
    python tests/check_drawing.py

Il punto 1 su HiDPI gira in un processo figlio con QT_SCALE_FACTOR=2 (`--hidpi`).
"""
import math
import os
import queue
import shutil
import subprocess
import sys
import time
from multiprocessing import shared_memory
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HIDPI = "--hidpi" in sys.argv
ARGS = [a for a in sys.argv[1:] if a != "--hidpi"]
S = Path(ARGS[0]) if ARGS else Path(__file__).parent / "out"
PDFS = S / "pdfs"
SHOTS = S / "shots"

BLUE, RED = "#1a3fbf", "#c00000"


def main():
    import pymupdf
    from PySide6.QtCore import QPoint, QRectF, Qt
    from PySide6.QtGui import QColor
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox

    import piedf.paths as paths
    data = S / ("data_disegno_hidpi" if HIDPI else "data_disegno")
    work = S / ("disegno_hidpi" if HIDPI else "disegno")
    for d in (data, work):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    paths.data_dir = lambda: data
    SHOTS.mkdir(parents=True, exist_ok=True)

    from piedf import render_worker
    from piedf.annotations import ERASER, HIGHLIGHTER, HIGHLIGHTER_OPACITY, PEN
    from piedf.document import PIEDF_TITLE, Ink, open_document
    from piedf.ink import segment_distance, simplify, smooth_points, stroke_distance
    from piedf.main_window import DRAW_HINT, ERASER_HINT, MainWindow
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
    stack = w.placements.undo_stack
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

    def drag(start, end, steps=6):
        QTest.mouseMove(vp, start)
        QTest.mousePress(vp, L, NO, start)
        for i in range(1, steps + 1):
            QTest.mouseMove(vp, start + (end - start) * i / steps)
        QTest.mouseRelease(vp, L, NO, end)
        app.processEvents()

    def draw(page, points):
        """Trascina lungo i punti (in punti della pagina) e restituisce il tratto aggiunto (o None)."""
        count = len(model.items)
        vps = [at(page, x, y) for x, y in points]
        QTest.mouseMove(vp, vps[0])
        QTest.mousePress(vp, L, NO, vps[0])
        for p in vps[1:]:
            QTest.mouseMove(vp, p)
        QTest.mouseRelease(vp, L, NO, vps[-1])
        app.processEvents()
        return model.items[-1] if len(model.items) == count + 1 and isinstance(model.items[-1], Ink) else None

    def key(k, mod=NO):
        QTest.keyClick(app.focusWidget() or v, k, mod)
        app.processEvents()

    def open_doc(path):
        if w.placements.modified:
            box_answers.append(QMessageBox.StandardButton.Discard)
        return w.open_file(str(path))

    def save_as(dest):
        save_answers.append(str(dest))
        return w.save_as()

    def inks(path):
        """Ink del file: titolo, colore, spessore, opacità, punti come si vede."""
        doc = pymupdf.open(path)
        out = []
        for page in doc:
            for a in page.annots(types=[pymupdf.PDF_ANNOT_INK]):
                m = page.rotation_matrix
                out.append({
                    "page": page.number, "title": a.info["title"], "color": tuple(a.colors["stroke"]),
                    "width": a.border["width"], "opacity": a.opacity, "xref": a.xref,
                    "print": bool(a.flags & pymupdf.PDF_ANNOT_IS_PRINT),
                    "points": [[tuple(pymupdf.Point(p) * m) for p in s] for s in a.vertices],
                })
        doc.close()
        return out

    def near_polyline(points, line, tol):
        """Scarto massimo dei punti dalla spezzata `line`."""
        return max(min(segment_distance(p, a, b) for a, b in zip(line, line[1:])) for p in points)

    def file_pixmap(path, page, scale=2.0):
        doc = pymupdf.open(path)
        pix = doc[page].get_pixmap(matrix=pymupdf.Matrix(scale, scale))
        doc.close()
        return pix

    def colored(pix, x, y, test, scale=2.0, r=2):
        """C'è un pixel che passa `test` intorno a (x, y) in punti?"""
        cx, cy = round(x * scale), round(y * scale)
        return any(
            test(pix.pixel(px, py)) for px in range(max(0, cx - r), min(pix.width, cx + r + 1))
            for py in range(max(0, cy - r), min(pix.height, cy + r + 1))
        )

    def bluish(rgb):
        return rgb[2] > 140 and rgb[0] < 120 and rgb[1] < 140

    def reddish(rgb):
        return rgb[0] > 150 and rgb[1] < 110 and rgb[2] < 110

    def blue_in_view_cache(page, points, timeout=10):
        """Pixel blu sotto i punti nell'immagine del processo di render (senza la vista sopra)."""
        t = time.perf_counter()
        while time.perf_counter() - t < timeout:
            app.processEvents(); vp.repaint()
            cached = v._cache.get(page)
            if cached is not None and cached[0] == v._render_scale(page):
                scale, image = cached
                hits = 0
                for x, y in points:
                    c = QColor(image.pixel(round(x * scale), round(y * scale)))
                    hits += bluish((c.red(), c.green(), c.blue()))
                return hits
            time.sleep(0.02)
        return -1

    def blue_on_screen(page, points):
        vp.repaint()
        image = vp.grab().toImage()
        dpr = image.devicePixelRatio()
        hits = 0
        for x, y in points:
            p = v.page_rect_to_viewport(page, QRectF(x, y, 0, 0)).topLeft()
            c = QColor(image.pixel(round(p.x() * dpr), round(p.y() * dpr)))
            hits += bluish((c.red(), c.green(), c.blue()))
        return hits

    wave = [(100 + 10 * i, 300 + 20 * math.sin(i / 3)) for i in range(30)]

    def check_wave(label, ink):
        pts = [p for s in ink.strokes for p in s] if ink else []
        ok = bool(pts) and near_polyline(pts, wave, 1.0) < 1.0
        ends = bool(pts) and math.dist(pts[0], wave[0]) < 1.0 and math.dist(pts[-1], wave[-1]) < 1.0
        check(f"1. {label}: 1 tratto, punti nello spazio della pagina (1 pt)", ink is not None and ok and ends,
              f"scarto {near_polyline(pts, wave, 1.0):.2f} pt, {len(pts)} punti" if pts else "nessun tratto")

    # --- 1. Penna: 30 punti su semplice.pdf, anche a zoom 200% (HiDPI nel processo figlio) ---
    src = copy("semplice.pdf")
    check("1. apertura semplice.pdf", w.open_file(str(src)))
    if HIDPI:
        check("1. HiDPI: devicePixelRatio 2", v.devicePixelRatioF() == 2.0, str(v.devicePixelRatioF()))
        for zoom in (None, 2.0):
            if zoom:
                v.set_zoom(zoom)
            show(0, 300)
            w.set_draw_tool(PEN)
            check_wave(f"HiDPI, zoom {round(v.zoom * 100)}%", draw(0, wave))
            w.set_draw_tool(None)
        box_answers.append(QMessageBox.StandardButton.Discard)
        w.close(); app.processEvents()
        print(f"\n{sum(results)}/{len(results)} ok", flush=True)
        return 0 if all(results) else 1

    check("1. Disegna in toolbar e menu Strumenti, Ctrl+D",
          w.act_draw.isEnabled() and w.draw_button.isEnabled() and w.act_draw.shortcut().toString() == "Ctrl+D"
          and w.draw_button.defaultAction() is w.act_draw and w.draw_button.menu() is w.draw_menu
          and any(a is w.act_draw for m in w.menuBar().actions() if m.text() == "&Strumenti" for a in m.menu().actions()))
    labels = [a.text() for a in w.draw_menu.actions()]
    check("1. menu: Penna, Evidenziatore, Gomma, colori e spessori",
          labels[:3] == ["Penna", "Evidenziatore", "Gomma"]
          and {"Colore penna", "Spessore penna", "Colore evidenziatore"} <= set(labels), str(labels))
    w.set_draw_option("pen_color", BLUE, PEN)
    w.set_draw_option("pen_width", 4.0, PEN)
    check("1. Penna attiva con suggerimento e cursore a croce", v.draw_tool == PEN and w.act_draw.isChecked()
          and w.draw_actions[PEN].isChecked() and w.statusBar().currentMessage() == DRAW_HINT
          and vp.cursor().shape() == Qt.CursorShape.CrossCursor)
    show(0, 300)
    ink = draw(0, wave)
    check_wave("zoom adatta larghezza", ink)
    check("1. colore e spessore scelti, tratto nello stato modificato e nell'annulla",
          ink is not None and ink.color == BLUE and ink.width == 4.0 and ink.opacity == 1.0 and w.placements.modified
          and stack.count() == 1 and model.selected is None)
    check("1. la vista lo disegna dal modello", ink is not None and blue_on_screen(0, ink.strokes[0]) >= len(ink.strokes[0]) // 2)
    vp.grab().save(str(SHOTS / "disegno_1.png"))
    key(Qt.Key.Key_Z, CTRL)
    check("1. Ctrl+Z lo toglie", not model.items)
    v.set_zoom(2.0)
    show(0, 300)
    check_wave("zoom 200%", draw(0, wave))
    w.act_fit_width.trigger()
    show(0, 300)
    proc = subprocess.run([sys.executable, "-u", __file__, "--hidpi", *ARGS], capture_output=True, text=True,
                          env=dict(os.environ, QT_SCALE_FACTOR="2"), timeout=300)
    lines = [line for line in proc.stdout.splitlines() if line.startswith(("OK", "FAIL"))]
    check("1. HiDPI simulato (QT_SCALE_FACTOR=2): adatta larghezza e zoom 200%", proc.returncode == 0 and len(lines) >= 4,
          "; ".join(lines) + proc.stderr[-300:] * (proc.returncode != 0))

    # --- 2. Salvataggio: una Ink di PieDF con colore e spessore, pixel blu lungo il tratto ---
    ink = model.items[0] if model.items else None
    out2 = work / "semplice_disegno.pdf"
    check("2. salvataggio", save_as(out2) and not errors, str(errors))
    found = inks(out2)
    check("2. una Ink con titolo PieDF, blu, spessore 4, stampabile",
          len(found) == 1 and found[0]["title"] == PIEDF_TITLE and found[0]["width"] == 4.0 and found[0]["print"]
          and all(abs(a - b / 255) < 0.01 for a, b in zip(found[0]["color"], (0x1a, 0x3f, 0xbf))), str(found and found[0]["color"]))
    pix = file_pixmap(out2, 0)
    hits = sum(colored(pix, x, y, bluish) for x, y in ink.strokes[0]) if ink else 0
    check("2. render del file salvato: pixel blu lungo il tratto", ink is not None and hits == len(ink.strokes[0]),
          f"{hits}/{len(ink.strokes[0]) if ink else 0}")

    # --- 3. Riapertura: nel modello e non due volte; spostato di 50 pt ---
    ink = model.items[0] if model.items else None
    check("3. dopo il salvataggio il tratto è nel modello, modificabile",
          len(model.items) == 1 and isinstance(ink, Ink) and ink.xref > 0 and ink.color == BLUE and ink.width == 4.0
          and not w.placements.modified)
    show(0, 300)
    check("3. niente doppioni: il processo di render non lo disegna", ink is not None and blue_in_view_cache(0, ink.strokes[0]) == 0)
    check("3. ma la vista sì", ink is not None and blue_on_screen(0, ink.strokes[0]) >= len(ink.strokes[0]) // 2)
    w.set_draw_tool(None)
    before = [p for s in ink.strokes for p in s]
    middle = ink.strokes[0][len(ink.strokes[0]) // 2]
    QTest.mouseMove(vp, at(0, *middle)); app.processEvents()
    check("3. senza strumento: mano aperta sopra il tratto", vp.cursor().shape() == Qt.CursorShape.OpenHandCursor)
    click(at(0, *middle))
    check("3. clic: selezionato", model.selected is ink)
    drag(at(0, *middle), at(0, middle[0] + 50, middle[1]))
    after = [p for s in ink.strokes for p in s]
    moved = [(b[0] - a[0], b[1] - a[1]) for a, b in zip(before, after)]
    check("3. trascinato di 50 pt", moved and all(abs(dx - 50) < 1 and abs(dy) < 1 for dx, dy in moved),
          str(moved[:1]))
    vp.grab().save(str(SHOTS / "disegno_selezionato.png"))
    key(Qt.Key.Key_Z, CTRL)
    check("3. Ctrl+Z: spostamento annullato", [p for s in ink.strokes for p in s] == before)
    key(Qt.Key.Key_Y, CTRL)
    check("3. salvataggio sullo stesso file", w.save() and not errors, str(errors))
    found = [f for f in inks(out2) if f["title"] == PIEDF_TITLE]
    check("3. una sola Ink, spostata", len(found) == 1 and near_polyline(found[0]["points"][0], after, 0.5) < 0.5
          and abs(found[0]["points"][0][0][0] - before[0][0] - 50) < 1, str(len(found)))

    # --- 4. Gomma: cancella tutto il tratto; un'Ink di un altro programma resta ---
    ink = model.items[0] if model.items else None
    w.set_draw_tool(ERASER)
    check("4. Gomma attiva con il suo cursore", v.draw_tool == ERASER and w.statusBar().currentMessage() == ERASER_HINT
          and vp.cursor().shape() == Qt.CursorShape.BitmapCursor)
    click(at(0, *ink.strokes[0][2]))
    check("4. clic sul tratto: cancellato tutto", not model.items and w.placements.modified)
    key(Qt.Key.Key_Z, CTRL)
    check("4. Ctrl+Z: torna", model.items == [ink])
    # Trascinata veloce attraverso il tratto: lo prende anche se nessun evento cade proprio sopra.
    x0, y0, x1, y1 = ink.rect
    drag(at(0, (x0 + x1) / 2, y0 - 60), at(0, (x0 + x1) / 2, y1 + 60), steps=2)
    check("4. gomma trascinata veloce attraverso il tratto: cancellato", not model.items)
    check("4. salvataggio", w.save() and not errors, str(errors))
    check("4. nessuna Ink PieDF nel file", not [f for f in inks(out2) if f["title"] == PIEDF_TITLE])
    w.set_draw_tool(None)

    other = copy("disegno_altro.pdf")
    check("4. apertura disegno_altro.pdf", open_doc(other))
    check("4. l'Ink di un altro programma non è nel modello", not model.items)
    show(0, 500)
    click(at(0, 400, 530))
    check("4. clic sopra: non si seleziona", model.selected is None)
    w.set_draw_tool(ERASER)
    click(at(0, 400, 530))
    check("4. la gomma non la cancella", not stack.count())
    w.set_draw_tool(PEN)
    draw(0, [(100, 600), (200, 620), (300, 600)])
    w.set_draw_tool(None)
    check("4. salvataggio con un tratto PieDF", save_as(work / "altro_salvato.pdf") and not errors, str(errors))
    found = inks(work / "altro_salvato.pdf")
    foreign = [f for f in found if f["title"] == "Mario Bianchi"]
    check("4. quella dell'altro programma intatta",
          len(found) == 2 and len(foreign) == 1
          and foreign[0]["points"] == [[(300.0, 500.0), (400.0, 530.0), (500.0, 500.0)]], str(foreign))

    # --- 5. Evidenziatore su una riga di testo ---
    src = copy("semplice.pdf", "evidenziato.pdf")
    open_doc(src)
    w.set_draw_option("highlighter_color", "#ffe600", HIGHLIGHTER)
    check("5. Evidenziatore attivo con il suo cursore", v.draw_tool == HIGHLIGHTER and w.draw_actions[HIGHLIGHTER].isChecked()
          and vp.cursor().shape() == Qt.CursorShape.BitmapCursor)
    show(0, 110)
    # "Testo di prova per il viewer." a (72, 110), corpo 12: la riga va da y 101 a 113 circa.
    hl = draw(0, [(68 + 6 * i, 106) for i in range(32)])
    check("5. tratto largo 12 pt, opacità 0,35, giallo", hl is not None and hl.width == 12.0
          and hl.opacity == HIGHLIGHTER_OPACITY and hl.color == "#ffe600")
    out5 = work / "evidenziato_salvato.pdf"
    save_as(out5)
    found = inks(out5)
    check("5. opacità 0,35 nel file", len(found) == 1 and abs(found[0]["opacity"] - 0.35) < 0.01, str(found and found[0]["opacity"]))
    pix = file_pixmap(out5, 0)
    words = [wd for wd in pymupdf.open(out5)[0].get_text("words") if wd[1] > 95 and wd[3] < 116]
    dark = yellow = 0
    for wd in words:
        for py in range(round(wd[1] * 2), round(wd[3] * 2)):
            for px in range(round(wd[0] * 2), round(wd[2] * 2)):
                r, g, b = pix.pixel(px, py)
                dark += r + g + b < 200
                yellow += r > 200 and g > 200 and b < 200
    check("5. il testo si legge ancora: pixel scuri del testo e giallo intorno", words and dark > 150 and yellow > 500,
          f"{len(words)} parole, {dark} pixel scuri, {yellow} gialli")
    pix.save(str(SHOTS / "disegno_evidenziato.png"))

    # --- 6. ruotate.pdf: un tratto orizzontale per pagina, orizzontale e nel punto visto ---
    src = copy("ruotate.pdf")
    open_doc(src)
    w.set_draw_option("pen_color", RED, PEN)
    for page in range(4):
        show(page, 300)
        draw(page, [(100 + 10 * i, 300) for i in range(21)])
    check("6. un tratto per pagina", [i.page for i in model.items] == [0, 1, 2, 3])
    out6 = work / "ruotate_disegno.pdf"
    save_as(out6)
    doc = pymupdf.open(out6)
    for page in range(4):
        pix = doc[page].get_pixmap(matrix=pymupdf.Matrix(2, 2))
        on_line = all(colored(pix, x, 300, reddish) for x in (102, 150, 200, 250, 298))
        off_line = not any(colored(pix, x, y, reddish) for x, y in ((200, 310), (200, 290), (90, 300), (310, 300)))
        check(f"6. pagina /Rotate {doc[page].rotation}: tratto orizzontale nel punto visto", on_line and off_line,
              f"sulla riga={on_line} fuori={off_line}")
    doc.close()

    # --- 7. Lo strumento resta attivo: 3 tratti di seguito; Esc; Ctrl+Z uno alla volta ---
    src = copy("semplice.pdf", "tre_tratti.pdf")
    open_doc(src)
    w.act_draw.trigger()
    check("7. Ctrl+D / Disegna accende l'ultimo strumento (Penna)", v.draw_tool == PEN)
    show(0, 400)
    for i in range(3):
        draw(0, [(100 + 20 * j, 350 + 40 * i) for j in range(10)])
    check("7. 3 tratti senza riattivare lo strumento", len(model.items) == 3 and v.draw_tool == PEN and stack.count() == 3)
    key(Qt.Key.Key_Escape)
    check("7. Esc lo disattiva", v.draw_tool is None and not w.act_draw.isChecked() and w.statusBar().currentMessage() != DRAW_HINT)
    counts = []
    for _ in range(3):
        key(Qt.Key.Key_Z, CTRL)
        counts.append(len(model.items))
    check("7. Ctrl+Z toglie un tratto alla volta", counts == [2, 1, 0], str(counts))
    w.act_draw.trigger()
    w.draw_button.click()
    check("7. un altro clic sul pulsante spegne lo strumento", v.draw_tool is None and not w.act_draw.isChecked())

    # --- 8. Semplificazione: 2000 punti → meno di 300 nel file, scarto sotto 0,5 pt ---
    w.set_draw_tool(PEN)
    v.set_zoom(1.0)
    show(0, 400)
    raw = []
    for i in range(2000):  # spirale: 2000 punti a 1-2 px l'uno dall'altro
        a = i / 2000 * 4 * 2 * math.pi
        r = 60 + 50 * i / 2000
        raw.append((300 + r * math.cos(a), 400 + r * math.sin(a)))
    vps = [at(0, x, y) for x, y in raw]
    QTest.mouseMove(vp, vps[0])
    QTest.mousePress(vp, L, NO, vps[0])
    for p in vps[1:]:
        QTest.mouseMove(vp, p)
    # Il tratto originale è quello levigato che si vede mentre si disegna: i punti tenuti
    # (a 2 px l'uno dall'altro) più quello del rilascio.
    live = list(v._stroke[1]) if v.drawing else []
    QTest.mouseRelease(vp, L, NO, vps[-1])
    app.processEvents()
    seen = [(v.to_page_point(0, p).x(), v.to_page_point(0, p).y()) for p in vps]
    if live and live[-1] != seen[-1]:
        live.append(seen[-1])
    smooth = smooth_points(live) if live else seen
    out8 = work / "spirale.pdf"
    save_as(out8)
    found = inks(out8)
    saved = found[0]["points"][0] if found else []
    err = max(stroke_distance(p, tuple(saved)) for p in smooth) if saved else 99
    err_raw = max(stroke_distance(p, tuple(saved)) for p in seen) if saved else 99
    check("8. 2000 punti mossi → meno di 300 nel file", found and len(saved) < 300, f"{len(saved)} punti")
    check("8. scarto massimo dal tratto levigato < 0,5 pt", live and err < 0.5,
          f"{err:.3f} pt (dai pixel del mouse, prima della levigatura: {err_raw:.3f} pt)")
    big = smooth_points(raw)
    small = simplify(big)
    err = max(stroke_distance(p, tuple(small)) for p in big)
    check("8. simplify su 2000 punti (senza GUI)", len(small) < 300 and err < 0.5, f"{len(big)} → {len(small)}, {err:.3f} pt")

    # --- 9. GUI reattiva mentre si disegna su pesante.pdf ---
    w.set_draw_tool(None)
    open_doc(copy("pesante.pdf"))
    v.set_zoom(4.0)
    w.set_draw_tool(PEN)
    start = QPoint(100, 200)
    QTest.mouseMove(vp, start)
    QTest.mousePress(vp, L, NO, start)
    gaps, i = [], 0
    last = t0 = time.perf_counter()
    while time.perf_counter() - t0 < 3:
        i += 1
        QTest.mouseMove(vp, start + QPoint(round(300 + 250 * math.sin(i / 40)) - 300, (i // 3) % 400))
        app.processEvents(); vp.repaint()
        now = time.perf_counter(); gaps.append(now - last); last = now
        time.sleep(0.005)
    QTest.mouseRelease(vp, L, NO, start + QPoint(0, 10))
    app.processEvents()
    check("9. GUI reattiva durante il disegno su pesante.pdf", max(gaps) < 0.15 and len(model.items) == 1,
          f"pausa max {max(gaps) * 1000:.0f} ms su {len(gaps)} movimenti")
    w.set_draw_tool(None)

    # --- Default in settings.ini ---
    s = w._settings
    check("default: colore, spessore e strumento in settings.ini",
          (s.value("draw/pen_color"), float(s.value("draw/pen_width")), s.value("draw/highlighter_color"), s.value("draw/tool"))
          == (RED, 4.0, "#ffe600", PEN))
    w2 = MainWindow()
    check("default: restano in una nuova finestra", w2.view.draw_style == {
        "tool": PEN, "pen_color": RED, "pen_width": 4.0, "highlighter_color": "#ffe600"}, str(w2.view.draw_style))
    w2._renderer.shutdown(); w2.deleteLater()

    # --- Precedenza: con la penna il clic su un link disegna; senza strumento il link funziona ---
    src = copy("link.pdf")
    open_doc(src)
    followed = []
    v.link_goto.connect(lambda *a: followed.append(a))
    show(0, 100)
    blue_in_view_cache(0, [(0, 0)])  # aspetta il render e i link
    pump(0.3)
    w.set_draw_tool(PEN)
    click(at(0, 150, 112))
    check("link: con la penna il clic su un link non lo segue", not followed and len(model.items) == 1)
    QTest.mouseMove(vp, at(0, 150, 172)); app.processEvents()
    check("link: con la penna niente manina sui link", vp.cursor().shape() == Qt.CursorShape.CrossCursor)
    key(Qt.Key.Key_Escape)
    click(at(0, 250, 172))
    check("link: senza strumenti il link funziona ancora", followed, str(followed))
    w.go_back()

    # --- Canc elimina il tratto selezionato; la manina vince sulla penna ---
    ink = model.items[0]
    click(at(0, *ink.strokes[0][0]))
    key(Qt.Key.Key_Delete)
    check("Canc elimina il tratto selezionato", not model.items)
    w.set_draw_tool(PEN)
    QTest.keyPress(v, Qt.Key.Key_Space); app.processEvents()
    drag(at(0, 300, 600), at(0, 300, 500))
    QTest.keyRelease(v, Qt.Key.Key_Space); app.processEvents()
    check("manina: Spazio + trascinamento non disegna", not model.items and v.draw_tool == PEN)
    w.set_draw_tool(None)

    # --- Stampa: i tratti non salvati si stampano ---
    w.set_draw_tool(PEN)
    show(0, 650)
    ink = draw(0, [(100 + 10 * i, 650) for i in range(20)])
    w.set_draw_tool(None)
    path, temp = print_source(str(src), None, [], {}, [], model.pending())
    check("stampa: con tratti non salvati si stampa un temporaneo", temp is not None
          and any(f["title"] == PIEDF_TITLE for f in inks(path)))
    results_q, acks = queue.Queue(), queue.Queue()
    try:
        # Le pagine arrivano nella memoria condivisa (#34), non nel messaggio.
        shm = shared_memory.SharedMemory(create=True, size=595 * 842 * 3 * 2)
        render_worker.print_pages(path, None, [(0, 1.0)], [shm.name], acks, results_q)
        msg = results_q.get(timeout=10)
        ok = False
        if msg[0] == "page":
            _, _, width, height, stride, _ = msg
            pix = pymupdf.Pixmap(pymupdf.csRGB, width, height, bytes(shm.buf[:stride * height]), False)
            ok = all(colored(pix, x, 650, reddish, scale=1.0, r=1) for x in (110, 150, 190))
        check("stampa: il processo di stampa disegna il tratto", ok, str(msg[:2]))
        shm.close(); shm.unlink()
    finally:
        remove_quietly(temp)

    # --- Organizza / Ruota (#26): prima si salvano i tratti, che poi restano modificabili ---
    def wait_job(limit=60.0):
        t = time.perf_counter()
        while w._page_job is not None and time.perf_counter() - t < limit:
            app.processEvents(); time.sleep(0.005)
        pump(0.1)
        return w._page_job is None

    box_answers.append(QMessageBox.StandardButton.Cancel)
    w.rotate_current(90)
    check("pagine: Ruota con un tratto non salvato chiede di salvare (Annulla)",
          not box_answers and w._page_job is None and model.pending() is not None)
    box_answers.append(QMessageBox.StandardButton.Save)
    save_answers.append(str(work / "link_disegno.pdf"))
    w.rotate_current(90)
    check("pagine: Salva e poi ruota", wait_job() and w._original is not None and w.modified, str(errors))
    ink = model.items[0] if len(model.items) == 1 else None
    check("pagine: il tratto salvato torna nel modello nel temporaneo", isinstance(ink, Ink) and model.pending() is None)

    # --- open_document legge i tratti ---
    doc = open_document(str(out6))
    check("open_document: PdfDocument.annotations con le Ink", len(doc.annotations) == 4
          and all(isinstance(a, Ink) and a.color == RED for a in doc.annotations))

    box_answers.append(QMessageBox.StandardButton.Discard)
    w.close(); app.processEvents()
    print(f"\n{sum(results)}/{len(results)} ok", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
