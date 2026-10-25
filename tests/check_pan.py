"""Controllo end-to-end della manina: Spazio + trascinamento e tasto centrale (#37).

    python tests/make_test_pdfs.py
    set QT_QPA_PLATFORM=offscreen
    python tests/check_pan.py
"""
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out"
PDFS = S / "pdfs"


def main():
    from PySide6.QtCore import QEvent, QPoint, QRectF, Qt
    from PySide6.QtGui import QColor, QFocusEvent, QImage, QKeyEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox, QPlainTextEdit

    import piedf.paths as paths
    data = S / "data_manina"
    shutil.rmtree(data, ignore_errors=True)
    data.mkdir(parents=True)
    paths.data_dir = lambda: data

    from piedf.main_window import MainWindow
    from piedf.signatures import Kind
    from piedf.viewer import FitMode

    app = QApplication(["t"])
    results = []

    def check(name, ok, info=""):
        results.append(bool(ok))
        print(("OK  " if ok else "FAIL"), name, info, flush=True)

    def pump(sec=0.05):
        t = time.perf_counter()
        while time.perf_counter() - t < sec:
            app.processEvents(); time.sleep(0.01)

    def wait(cond, sec=10):
        t = time.perf_counter()
        while not cond() and time.perf_counter() - t < sec:
            app.processEvents(); time.sleep(0.01)
        return cond()

    # Cambiando documento le modifiche fatte qui si scartano.
    QMessageBox.exec = lambda box: QMessageBox.StandardButton.Discard

    w = MainWindow(); w.resize(1000, 800); w.show(); pump(0.2)
    v = w.view
    vp = v.viewport()
    hbar, vbar = v.horizontalScrollBar(), v.verticalScrollBar()
    L, M = Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton
    NO = Qt.KeyboardModifier.NoModifier
    OPEN, CLOSED = Qt.CursorShape.OpenHandCursor, Qt.CursorShape.ClosedHandCursor
    SPACE = Qt.Key.Key_Space

    def at(page, x, y):
        return v.page_rect_to_viewport(page, QRectF(x, y, 0, 0)).topLeft().toPoint()

    def shape():
        return vp.cursor().shape()

    def bars():
        return hbar.value(), vbar.value()

    def space(press, repeat=False):
        kind = QEvent.Type.KeyPress if press else QEvent.Type.KeyRelease
        QApplication.sendEvent(v, QKeyEvent(kind, SPACE, NO, " ", repeat))
        app.processEvents()

    def drag(start, delta, button=L, steps=10, during=None):
        """Trascinamento a passi (niente accelerazione): `during` si chiama a metà, col tasto premuto."""
        QTest.mouseMove(vp, start)
        QTest.mousePress(vp, button, NO, start)
        for i in range(1, steps + 1):
            QTest.mouseMove(vp, start + QPoint(delta.x() * i // steps, delta.y() * i // steps))
            if i == steps // 2 and during is not None:
                during()
        QTest.mouseRelease(vp, button, NO, start + delta)
        app.processEvents()

    def near(a, b, tol=1):
        return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol

    img = QImage(600, 200, QImage.Format.Format_ARGB32)
    img.fill(QColor("#1a237e"))
    sig = w.signatures.add(img, Kind.SIGNATURE)
    center = QPoint(500, 400)

    # --- 1. Spazio + trascinamento a zoom 300% ---
    check("apertura lungo_350.pdf", w.open_file(str(PDFS / "lungo_350.pdf")))
    v.set_zoom(3.0); pump(0.1)
    v.set_scroll_position(20, 0.0)
    hbar.setValue(hbar.maximum() // 2); app.processEvents()
    check("1. a 300% c'è spazio per spostarsi in tutte le direzioni",
          200 < hbar.value() < hbar.maximum() - 200 and vbar.value() > 300, f"{bars()} max {hbar.maximum()}")
    QTest.mouseMove(vp, center); app.processEvents()
    normal = shape()
    space(True)
    check("1. Spazio: mano aperta", v.panning and shape() == OPEN, str(shape()))
    before = bars()
    seen = []
    drag(center, QPoint(-200, -300), during=lambda: seen.append(shape()))
    check("1. durante il trascinamento: mano chiusa", seen == [CLOSED], str(seen))
    check("1. le barre aumentano di (200, 300) ± 1 px", near(bars(), (before[0] + 200, before[1] + 300)),
          f"{before} → {bars()}")
    check("1. rilascio il mouse: di nuovo mano aperta", shape() == OPEN, str(shape()))
    space(False)
    check("1. rilascio lo Spazio: cursore normale", not v.panning and shape() == normal and shape() not in (OPEN, CLOSED),
          str(shape()))
    check("1. le pagine si caricano come nello scorrimento", wait(lambda: all(p in v._cache for p in v._visible_range())))

    # --- 2. Tasto centrale senza Spazio ---
    before = bars()
    seen = []
    drag(center, QPoint(-200, -300), button=M, during=lambda: seen.append(shape()))
    check("2. tasto centrale: stesso spostamento", near(bars(), (before[0] + 200, before[1] + 300)),
          f"{before} → {bars()}")
    check("2. mano chiusa durante, cursore normale dopo", seen == [CLOSED] and not v.panning and shape() == normal,
          f"{seen} {shape()}")
    before = bars()
    drag(center, QPoint(150, 100), button=M)
    check("2. anche nell'altra direzione", near(bars(), (before[0] - 150, before[1] - 100)), f"{before} → {bars()}")

    # --- 3. Auto-repeat ---
    space(True)
    states = []
    for _ in range(5):
        space(False, repeat=True)
        space(True, repeat=True)
        states.append((v.panning, shape()))
    check("3. auto-repeat: la manina resta attiva", all(s == (True, OPEN) for s in states), str(states))
    before = bars()
    drag(center, QPoint(0, -100))
    check("3. e trascina", near(bars(), (before[0], before[1] + 100)), f"{before} → {bars()}")
    space(False)
    check("3. si esce solo con il rilascio vero", not v.panning and shape() == normal)

    # Rilascio lo Spazio durante il trascinamento: continua fino al rilascio del mouse.
    space(True)
    before = bars()
    QTest.mouseMove(vp, center)
    QTest.mousePress(vp, L, NO, center)
    QTest.mouseMove(vp, center + QPoint(0, -50))
    space(False)
    check("Spazio rilasciato durante il trascinamento: mano chiusa", v.panning and shape() == CLOSED, str(shape()))
    QTest.mouseMove(vp, center + QPoint(0, -100))
    QTest.mouseRelease(vp, L, NO, center + QPoint(0, -100)); app.processEvents()
    check("il trascinamento è continuato fino al rilascio del mouse", near(bars(), (before[0], before[1] + 100))
          and not v.panning and shape() == normal, f"{before} → {bars()} {shape()}")

    # --- 4. Firma in posizionamento ---
    w.place_signature(sig)
    QTest.mouseMove(vp, center); app.processEvents()
    check("4. posizionamento: croce", v.placing and shape() == Qt.CursorShape.CrossCursor)
    space(True)
    check("4. Spazio: mano aperta, anteprima nascosta", shape() == OPEN and v._placing_rect() is None)
    before = bars()
    drag(center, QPoint(-100, -150))
    check("4. Spazio + trascinamento: nessuna firma piazzata, la vista si sposta",
          not w.placements.items and v.placing and near(bars(), (before[0] + 100, before[1] + 150)),
          f"{len(w.placements.items)} {before} → {bars()}")
    QTest.mouseClick(vp, Qt.MouseButton.RightButton, NO, center); app.processEvents()
    check("4. il clic destro con lo Spazio non annulla il posizionamento", v.placing)
    space(False)
    check("4. rilasciato lo Spazio: di nuovo la croce", v.placing and shape() == Qt.CursorShape.CrossCursor
          and v._placing_rect() is not None, str(shape()))
    QTest.mouseClick(vp, L, NO, center); app.processEvents()
    check("4. un clic normale piazza la firma", len(w.placements.items) == 1 and not v.placing)

    # --- 5. Firma piazzata e selezionata ---
    placed = w.placements.items[0] if w.placements.items else None
    check("5. firma selezionata", placed is not None and w.placements.selected is placed)
    rect = QRectF(placed.rect)
    space(True)
    check("5. sopra la firma con lo Spazio: mano aperta", shape() == OPEN)
    before = bars()
    drag(center, QPoint(-120, -80))
    check("5. la firma non si sposta, la vista sì", placed.rect == rect and w.placements.selected is placed
          and near(bars(), (before[0] + 120, before[1] + 80)), f"{before} → {bars()}")
    QTest.mouseClick(vp, L, NO, QPoint(30, 30)); app.processEvents()
    check("5. con lo Spazio il clic non deseleziona", w.placements.selected is placed)
    space(False)
    w.placements.clear()

    # --- 10. Bordi ---
    v.go_to_page(0); hbar.setValue(0); app.processEvents()
    space(True)
    drag(center, QPoint(250, 300))
    check("10. oltre l'inizio del documento: barre a 0", bars() == (0, 0), str(bars()))
    vbar.setValue(vbar.maximum()); hbar.setValue(hbar.maximum()); app.processEvents()
    drag(center, QPoint(-250, -300))
    check("10. oltre la fine: barre al massimo", bars() == (hbar.maximum(), vbar.maximum()), str(bars()))
    space(False)
    v.set_fit_mode(FitMode.WIDTH); v.set_scroll_position(5, 0.0); app.processEvents()
    before = bars()
    space(True)
    drag(center, QPoint(-200, -100))
    space(False)
    check("10. Adatta larghezza: nessun movimento orizzontale", hbar.maximum() == 0 and bars()[0] == before[0] == 0
          and bars()[1] == before[1] + 100, f"{before} → {bars()}")

    # --- 9. La finestra perde il focus con lo Spazio premuto ---
    for name, event in (("focus perso", QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.ActiveWindowFocusReason)),
                        ("finestra disattivata", QEvent(QEvent.Type.WindowDeactivate))):
        space(True)
        QApplication.sendEvent(v, event); app.processEvents()
        check(f"9. {name}: la manina si annulla", not v.panning and shape() not in (OPEN, CLOSED), str(shape()))
        before = bars()
        drag(center, QPoint(0, -100))
        check(f"9. {name}: al ritorno il trascinamento non sposta la vista", bars() == before, f"{before} → {bars()}")
    # Anche a metà di un trascinamento.
    space(True)
    QTest.mousePress(vp, L, NO, center)
    QApplication.sendEvent(v, QEvent(QEvent.Type.WindowDeactivate)); app.processEvents()
    before = bars()
    QTest.mouseMove(vp, center + QPoint(0, -100))
    QTest.mouseRelease(vp, L, NO, center + QPoint(0, -100)); app.processEvents()
    check("9. finestra disattivata durante il trascinamento: si ferma", not v.panning and bars() == before)

    # --- 6. Link ---
    w.open_file(str(PDFS / "link.pdf"))
    v.set_scroll_position(0, 0.0)
    check("6. link della prima pagina arrivati", wait(lambda: 0 in v._links))
    link = at(0, 186, 112)  # link interno verso pagina 3 (vedi check_links.py)
    QTest.mouseMove(vp, link); app.processEvents()
    check("6. manina dei link senza Spazio", shape() == Qt.CursorShape.PointingHandCursor)
    space(True)
    check("6. con lo Spazio: mano aperta e niente tooltip", shape() == OPEN and vp.toolTip() == "")
    QTest.mouseClick(vp, L, NO, link); app.processEvents()
    check("6. Spazio + clic sul link: nessuna navigazione", v.current_page == 0 and not w._back,
          f"{v.current_page} {w._back}")
    space(False)
    check("6. rilasciato lo Spazio: di nuovo la manina del link", shape() == Qt.CursorShape.PointingHandCursor)

    # --- 7. Campi modulo ---
    w.open_file(str(PDFS / "modulo.pdf"))
    v.set_scroll_position(0, 0.0)
    fields = {f.name: f for f in w.forms.fields}
    nome = at(0, 275, 110)
    QTest.mouseMove(vp, nome)
    space(True)
    QTest.mouseClick(vp, L, NO, nome); app.processEvents()
    check("7. Spazio + clic su un campo: nessun editor", v.editor is None and v.focus_field is None)
    QTest.mouseClick(vp, L, NO, at(0, 158, 308)); app.processEvents()
    check("7. Spazio + clic sulla casella: non si spunta", fields["Privacy"].value == "Off", fields["Privacy"].value)
    space(False)
    QTest.mouseClick(vp, L, NO, nome); app.processEvents()
    check("7. clic normale: editor del campo", isinstance(v.editor, QLineEdit))
    QTest.keyClicks(v.editor, "Mario")
    QTest.keyClick(v.editor, SPACE)
    QTest.keyClicks(v.editor, "Rossi")
    check("7. nell'editor lo Spazio scrive uno spazio", isinstance(v.editor, QLineEdit)
          and v.editor.text() == "Mario Rossi" and not v.panning, v.editor.text() if v.editor else "")
    for _ in range(3):  # Note, CAP, Privacy
        QTest.keyClick(app.focusWidget() or v, Qt.Key.Key_Tab); app.processEvents()
    check("7. Tab fino alla casella Privacy", v.focus_field is fields["Privacy"] and v.editor is None)
    check("7. il testo è stato confermato", fields["Nome"].value == "Mario Rossi", fields["Nome"].value)
    QTest.keyClick(vp, SPACE); app.processEvents()
    check("7. con la casella raggiunta col Tab lo Spazio la spunta", fields["Privacy"].value not in ("Off", "")
          and not v.panning and shape() != OPEN, fields["Privacy"].value)

    # --- 8. Casella di testo in modifica ---
    w.open_file(str(PDFS / "semplice.pdf"))
    w.act_text.trigger()
    QTest.mouseClick(vp, L, NO, center); app.processEvents()
    check("8. casella di testo in modifica", isinstance(v.editor, QPlainTextEdit) and v.editing_box is not None)
    QTest.keyClicks(v.editor, "Ciao")
    QTest.keyClick(v.editor, SPACE)
    QTest.keyClicks(v.editor, "mondo")
    check("8. lo Spazio scrive uno spazio nella casella", isinstance(v.editor, QPlainTextEdit)
          and v.editor.toPlainText() == "Ciao mondo" and not v.panning,
          v.editor.toPlainText() if v.editor else "")

    w.close_document()
    w.close(); app.processEvents()

    print(f"\n{sum(results)}/{len(results)} ok", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
