"""Controllo end-to-end del posizionamento firme (#4).

    python tests/make_test_pdfs.py
    set QT_QPA_PLATFORM=offscreen
    python tests/check_placement.py
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
    from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter, QPen
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox, QWidgetAction

    import piedf.paths as paths
    data = S / "data_posizionamento"
    shutil.rmtree(data, ignore_errors=True)
    data.mkdir(parents=True)
    paths.data_dir = lambda: data
    SHOTS.mkdir(parents=True, exist_ok=True)

    from piedf.main_window import MainWindow, PLACING_HINT
    from piedf.signatures import Kind

    app = QApplication(["t"])
    results = []

    def check(name, ok, info=""):
        results.append(bool(ok))
        print(("OK  " if ok else "FAIL"), name, info, flush=True)

    def near(a, b, tol):
        return abs(a - b) <= tol

    def pump(sec=0.05):
        t = time.perf_counter()
        while time.perf_counter() - t < sec:
            app.processEvents(); time.sleep(0.01)

    def fmt(r):
        return f"({r.x():.1f}, {r.y():.1f}, {r.width():.1f}×{r.height():.1f})"

    w = MainWindow(); w.resize(1000, 800); w.show()
    v, model = w.view, w.placements
    vp = v.viewport()
    L = Qt.MouseButton.LeftButton
    NO = Qt.KeyboardModifier.NoModifier

    def click(pos, button=L):
        QTest.mouseMove(vp, pos)
        QTest.mouseClick(vp, button, NO, pos)
        app.processEvents()

    def drag(start, end, steps=5):
        QTest.mousePress(vp, L, NO, start)
        for i in range(1, steps + 1):
            QTest.mouseMove(vp, start + (end - start) * i / steps)
        QTest.mouseRelease(vp, L, NO, end)
        app.processEvents()

    def key(k, mod=NO):
        QTest.keyClick(vp, k, mod)
        app.processEvents()

    def center_of(page):
        r = v.page_rect_to_viewport(page, QRectF(QPointF(0, 0), QPointF(*v._sizes[page])))
        return r, r.center().toPoint()

    # Firma e iniziali di prova (proporzioni 3:1 e 1:1) nell'archivio temporaneo.
    def make(kind, width, height):
        img = QImage(width, height, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img); p.setPen(QPen(QColor("#1a237e"), 8)); p.drawLine(10, height - 20, width - 10, 20); p.end()
        return w.signatures.add(img, kind)

    sig = make(Kind.SIGNATURE, 600, 200)

    # --- Pulsante e menu ---
    check("Firma ▾ disabilitato senza documento", not w.sign_button.isEnabled() and w.sign_button.text() == "Firma ▾")
    menus = [a.text() for a in w.menuBar().actions()]
    check("menu Modifica tra File e Visualizza", menus[:3] == ["&File", "&Modifica", "&Visualizza"], str(menus))
    w.signatures.delete(sig)
    w._fill_sign_menu()
    labels = [a.text() for a in w._sign_menu.actions()]
    check("menu senza firme: solo Nuova…", labels == ["Nuova firma…", "Nuove iniziali…"], str(labels))
    sig = make(Kind.SIGNATURE, 600, 200)
    ini = make(Kind.INITIALS, 200, 200)
    w._fill_sign_menu()
    acts = w._sign_menu.actions()
    labels = ["<firma>" if isinstance(a, QWidgetAction) else a.text() or "|" for a in acts]
    check("menu con firme", labels == ["<firma>", "|", "<firma>", "|", "Nuova firma…", "Nuove iniziali…", "Gestisci firme…"], str(labels))
    check("menu: tipo nel tooltip", [acts[0].toolTip(), acts[2].toolTip()] == ["Firma", "Iniziali"])

    # --- 1. Clic al centro di semplice.pdf al 100% ---
    w.open_file(str(PDFS / "semplice.pdf"))
    v.set_zoom(1.0); pump()
    check("Firma ▾ abilitato con documento", w.sign_button.isEnabled())
    acts[0].trigger()  # la prima firma del menu
    check("modalità posizionamento", v.placing and vp.cursor().shape() == Qt.CursorShape.CrossCursor
          and w.statusBar().currentMessage() == PLACING_HINT, w.statusBar().currentMessage())
    page_r, c = center_of(0)
    QTest.mouseMove(vp, c); app.processEvents()
    preview = v._placing_rect()
    check("anteprima sotto il cursore", preview is not None and preview[0] == 0)
    v.grab().save(str(SHOTS / "posizionamento_anteprima.png"))
    click(c)
    pw, ph = v._sizes[0]
    p0 = model.items[0] if model.items else None
    pt = v.to_page_point(0, c)
    check("1. un posizionamento centrato, larghezza 150 pt",
          len(model.items) == 1 and near(p0.rect.center().x(), pt.x(), 1) and near(p0.rect.center().y(), pt.y(), 1)
          and near(p0.rect.width(), 150, 0.01) and near(p0.rect.height(), 50, 0.01)
          and near(pt.x(), pw / 2, 1) and near(pt.y(), ph / 2, 1),
          fmt(p0.rect) if p0 else "")
    check("1. firma selezionata, fuori dalla modalità", model.selected is p0 and not v.placing
          and w.statusBar().currentMessage() != PLACING_HINT and p0.image_path == sig.path and p0.kind is Kind.SIGNATURE)
    v.grab().save(str(SHOTS / "posizionamento_selezionata.png"))

    # --- 10a. Titolo con asterisco ---
    check("10. titolo con *", w.windowTitle() == "* semplice.pdf – PieDF", w.windowTitle())

    # --- 2. Clic vicino al bordo destro ---
    w.place_signature(sig)
    edge = QPoint(round(page_r.right()) - 3, round(page_r.top()) + 5)
    click(edge)
    p1 = model.items[-1]
    check("2. vicino al bordo: dentro la pagina", len(model.items) == 2 and p1.rect.left() >= 0 and p1.rect.top() >= 0
          and p1.rect.right() <= pw + 1e-6 and p1.rect.bottom() <= ph + 1e-6, fmt(p1.rect))

    # --- 3. Zoom: rect in punti invariato, vista scalata ---
    before = QRectF(p0.rect)
    ok = True; info = []
    for name, action in (("50%", lambda: v.set_zoom(0.5)), ("200%", lambda: v.set_zoom(2.0)),
                         ("adatta pagina", w.act_fit_page.trigger)):
        action(); pump()
        r = v.page_rect_to_viewport(0, p0.rect)
        s = v.zoom * 96 / 72
        ok &= p0.rect == before and near(r.width(), 150 * s, 0.01) and near(r.height(), 50 * s, 0.01)
        info.append(f"{name}: {r.width():.1f}px")
    check("3. zoom: rect invariato, vista scalata", ok, ", ".join(info))

    # --- 4. Trascinamento di 100 px al 200% ---
    v.set_zoom(2.0); pump()
    v.verticalScrollBar().setValue(round(v.page_rect_to_viewport(0, p0.rect).top() + v.verticalScrollBar().value()) - 200)
    v.horizontalScrollBar().setValue(round(v.page_rect_to_viewport(0, p0.rect).left() + v.horizontalScrollBar().value()) - 200)
    app.processEvents()
    model.select(p0)
    start = v.page_rect_to_viewport(0, p0.rect).center().toPoint()
    QTest.mouseMove(vp, start); app.processEvents()
    check("cursore mano aperta sopra la firma", vp.cursor().shape() == Qt.CursorShape.OpenHandCursor)
    steps_before = model.undo_stack.count()
    drag(start, start + QPoint(100, 0))
    moved = p0.rect.x() - before.x()
    check("4. trascinamento 100 px al 200% = 37,5 pt", near(moved, 37.5, 0.01) and near(p0.rect.y(), before.y(), 0.01), f"{moved:.3f} pt")
    check("6. un trascinamento = un solo passo", model.undo_stack.count() == steps_before + 1
          and w.act_undo.text() == "Annulla spostamento firma", w.act_undo.text())
    key(Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    check("6. Ctrl+Z annulla l'intero trascinamento", p0.rect == before, fmt(p0.rect))
    key(Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
    check("6. Ctrl+Y lo ripete", near(p0.rect.x() - before.x(), 37.5, 0.01))
    key(Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    check("Ctrl+Shift+Z = ripeti (niente da ripetere)", near(p0.rect.x() - before.x(), 37.5, 0.01))

    # Trascinamento oltre il bordo: resta nella pagina.
    start = v.page_rect_to_viewport(0, p0.rect).center().toPoint()
    drag(start, start + QPoint(-3000, -3000))
    check("trascinamento oltre il bordo: resta nella pagina", near(p0.rect.x(), 0, 1e-6) and near(p0.rect.y(), 0, 1e-6), fmt(p0.rect))
    key(Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)

    # --- 5. Ridimensionamento dall'angolo in basso a destra ---
    model.select(p0)
    r0 = QRectF(p0.rect)
    corner = v.page_rect_to_viewport(0, p0.rect).bottomRight().toPoint()
    QTest.mouseMove(vp, corner); app.processEvents()
    check("cursore diagonale sulla maniglia", vp.cursor().shape() == Qt.CursorShape.SizeFDiagCursor)
    drag(corner, corner + QPoint(80, 10))
    ratio = p0.rect.width() / p0.rect.height()
    check("5. ridimensiona: proporzioni e angolo fermo",
          p0.rect.width() > r0.width() and near(ratio, 3, 3 * 0.005) and p0.rect.topLeft() == r0.topLeft()
          and w.act_undo.text() == "Annulla ridimensionamento firma", fmt(p0.rect))
    corner = v.page_rect_to_viewport(0, p0.rect).bottomRight().toPoint()
    drag(corner, corner - QPoint(2000, 2000))
    check("5. sotto il minimo: altezza 8 pt", near(p0.rect.height(), 8, 0.01) and near(p0.rect.width(), 24, 0.01)
          and p0.rect.topLeft() == r0.topLeft(), fmt(p0.rect))
    key(Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    key(Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    check("annulla i ridimensionamenti", p0.rect == r0, fmt(p0.rect))

    # Clic su area vuota → deseleziona; clic su un'altra firma → seleziona quella.
    v.set_zoom(1.0); pump()
    page_r, c = center_of(0)
    click(QPoint(round(page_r.left()) + 20, round(page_r.bottom()) - 20))
    check("clic su area vuota deseleziona", model.selected is None)
    click(v.page_rect_to_viewport(0, p1.rect).center().toPoint())
    check("clic su un'altra firma la seleziona", model.selected is p1)

    # --- 6. Canc, Ctrl+Z, Ctrl+Y ---
    model.select(p0)
    key(Qt.Key.Key_Delete)
    check("6. Canc elimina", p0 not in model.items and len(model.items) == 1 and w.act_undo.text() == "Annulla eliminazione firma")
    key(Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    check("6. Ctrl+Z la ripristina", p0 in model.items and p0.rect == r0)
    key(Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
    check("6. Ctrl+Y la rielimina", p0 not in model.items)
    key(Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    model.select(p1)
    key(Qt.Key.Key_Backspace)
    check("Backspace elimina", p1 not in model.items)
    key(Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)

    # --- 7. Esc e clic destro annullano, sfondo non fa niente ---
    n = len(model.items)
    w.place_signature(sig); key(Qt.Key.Key_Escape)
    check("7. Esc in posizionamento non crea niente", len(model.items) == n and not v.placing)
    w.place_signature(sig); click(c, Qt.MouseButton.RightButton)
    check("clic destro annulla", len(model.items) == n and not v.placing)
    w.place_signature(sig); click(QPoint(3, 3))
    check("clic sullo sfondo: niente", len(model.items) == n and v.placing and v.page_at(QPoint(3, 3)) is None)
    key(Qt.Key.Key_Escape)

    # Iniziali: 50 pt.
    w.place_signature(ini); click(c)
    check("iniziali: larghezza 50 pt", near(model.items[-1].rect.width(), 50, 0.01) and model.items[-1].kind is Kind.INITIALS,
          fmt(model.items[-1].rect))

    # PNG eliminato da "Gestisci firme": il posizionamento resta con la sua immagine.
    w.signatures.delete(ini)
    v.viewport().repaint()
    check("PNG eliminato: l'immagine resta in memoria", not model.items[-1].image_path.exists()
          and not model.image(model.items[-1].image_path).isNull())

    # Ridimensionamento finestra e adatta larghezza: i punti non cambiano.
    rects = [QRectF(p.rect) for p in model.items]
    w.act_fit_width.trigger(); w.resize(700, 700); pump(0.2)
    r = v.page_rect_to_viewport(0, model.items[0].rect)
    check("ridimensionamento finestra: rect invariati", [p.rect for p in model.items] == rects
          and near(r.width(), model.items[0].rect.width() * v.zoom * 96 / 72, 0.01))
    w.resize(1000, 800); pump(0.2)

    # --- 10b. Conferma aprendo un altro file ---
    answers = []
    texts = []
    def fake_exec(box):
        texts.append((box.text(), [b.text() for b in box.buttons()]))
        return answers.pop(0)
    QMessageBox.exec = fake_exec
    answers.append(QMessageBox.StandardButton.Cancel)
    ok = w.open_file(str(PDFS / "lungo_350.pdf"))
    check("10. conferma con Annulla: resta aperto", ok is False and w._path.endswith("semplice.pdf") and len(model.items) == 3
          and texts and texts[0][0] == "Le firme aggiunte a «semplice.pdf» non sono state salvate."
          and sorted(texts[0][1]) == ["Annulla", "Scarta"], str(texts[:1]))
    answers.append(QMessageBox.StandardButton.Cancel)
    w.act_close.trigger()
    check("Ctrl+W con Annulla: resta aperto", w._path is not None and len(texts) == 2)
    answers.append(QMessageBox.StandardButton.Discard)
    ok = w.open_file(str(PDFS / "lungo_350.pdf"))
    check("10. conferma con Scarta: si apre il nuovo, posizionamenti vuoti",
          ok and w._path.endswith("lungo_350.pdf") and model.items == [] and model.undo_stack.count() == 0
          and w.windowTitle() == "lungo_350.pdf – PieDF", w.windowTitle())

    # --- 8. lungo_350.pdf, pagina 200 ---
    n_conf = len(texts)
    v.set_zoom(1.0); v.go_to_page(199); pump()
    _, c = center_of(199)
    w.place_signature(sig); click(c)
    check("8. pagina 200 → page == 199", len(model.items) == 1 and model.items[0].page == 199, str(model.items[0].page if model.items else None))
    w.placements.undo_stack.undo()
    check("annullato tutto: niente *, niente conferma", w.windowTitle() == "lungo_350.pdf – PieDF")

    # --- 9. orientamenti.pdf, pagina 3 ruotata ---
    w.open_file(str(PDFS / "orientamenti.pdf"))
    check("nessuna conferma senza modifiche", len(texts) == n_conf)
    v.set_zoom(1.0); v.go_to_page(2); pump()
    r2, _ = center_of(2)
    w.place_signature(sig)
    click(QPoint(round(r2.right()) - 2, round(r2.bottom()) - 2) if r2.bottom() < vp.height() else QPoint(round(r2.right()) - 2, round(r2.top()) + 2))
    p2 = model.items[-1] if model.items else None
    w3, h3 = v._sizes[2]
    check("9. pagina ruotata: dentro page_sizes[2] (842×595)",
          p2 is not None and p2.page == 2 and (round(w3), round(h3)) == (842, 595)
          and p2.rect.left() >= 0 and p2.rect.top() >= 0 and p2.rect.right() <= w3 + 1e-6 and p2.rect.bottom() <= h3 + 1e-6,
          fmt(p2.rect) if p2 else "")
    v.grab().save(str(SHOTS / "posizionamento_ruotata.png"))

    # Pagina stretta: al massimo il 45% della larghezza.
    from piedf.placements import default_rect
    img = QImage(600, 200, QImage.Format.Format_ARGB32)
    r = default_rect(Kind.SIGNATURE, img, (200, 300), QPointF(100, 150))
    check("pagina stretta: 45% della larghezza", near(r.width(), 90, 0.01) and near(r.height(), 30, 0.01), fmt(r))

    # Uscita: conferma prima della chiusura del renderer.
    answers.append(QMessageBox.StandardButton.Cancel)
    w.close(); app.processEvents()
    check("uscita con Annulla: la finestra resta, renderer vivo", w.isVisible() and w._renderer._process.is_alive())
    answers.append(QMessageBox.StandardButton.Discard)
    w.close(); app.processEvents()
    check("uscita con Scarta: chiusa", not w.isVisible())

    print(f"\n{sum(results)}/{len(results)} ok", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
