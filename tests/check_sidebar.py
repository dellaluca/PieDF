"""Controllo end-to-end del pannello laterale (#6): miniature, indice e rotazione della vista.

    python tests/make_test_pdfs.py
    set QT_QPA_PLATFORM=offscreen
    python tests/check_sidebar.py
"""
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out"
PDFS = S / "pdfs"
SHOTS = S / "shots"


def main():
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    SHOTS.mkdir(parents=True, exist_ok=True)
    (S / "data").mkdir(parents=True, exist_ok=True)
    import piedf.paths as paths
    paths.data_dir = lambda: S / "data"
    (S / "data" / "settings.ini").unlink(missing_ok=True)
    (S / "data" / "posizioni.json").unlink(missing_ok=True)  # ogni file si apre dall'inizio
    from piedf.document import TextBox
    from piedf.main_window import MainWindow
    from piedf.page_ops import MAIN
    from piedf.sidebar import NO_TOC, PAGES_TAB, TOC_TAB

    app = QApplication(["t"])
    results = []

    def check(name, ok, info=""):
        results.append(bool(ok))
        print(("OK  " if ok else "FAIL"), name, info, flush=True)

    def pump(sec):
        t = time.perf_counter()
        while time.perf_counter() - t < sec:
            app.processEvents(); time.sleep(0.01)

    def wait_for(cond, timeout=15):
        t = time.perf_counter()
        while time.perf_counter() - t < timeout:
            app.processEvents()
            if cond():
                return time.perf_counter() - t
            time.sleep(0.01)
        return None

    def wait_rendered(v, timeout=15):
        def done():
            v.viewport().repaint()
            vis = list(v._visible_range())
            return vis and all(p in v._cache and v._cache[p][0] == v._render_scale(p) for p in vis)
        return wait_for(done, timeout)

    # --- 1. F4 e stato ricordato ------------------------------------------------------
    w = MainWindow(); w.resize(1000, 800); w.show(); app.processEvents()
    v, sb = w.view, w.sidebar
    check("all'inizio il pannello è nascosto", not sb.isVisible() and not w.act_sidebar.isChecked())
    check("Visualizza › Pannello laterale con F4", w.act_sidebar.shortcut().toString() == "F4"
          and w.act_sidebar in w.menuBar().actions()[2].menu().actions(), w.act_sidebar.shortcut().toString())
    QTest.keyClick(w, Qt.Key.Key_F4); app.processEvents()
    check("F4 mostra il pannello", sb.isVisible() and w.act_sidebar.isChecked())
    QTest.keyClick(w, Qt.Key.Key_F4); app.processEvents()
    check("F4 di nuovo lo nasconde", not sb.isVisible() and not w.act_sidebar.isChecked())
    w.act_sidebar.trigger(); app.processEvents()
    w.resizeDocks([sb], [300], Qt.Orientation.Horizontal); app.processEvents()
    sb.tabs.setCurrentIndex(TOC_TAB)
    width = sb.width()
    w.close(); app.processEvents()
    w = MainWindow(); w.resize(1000, 800); w.show(); app.processEvents()
    v, sb = w.view, w.sidebar
    check("riaprendo la finestra: pannello visibile, stessa scheda e larghezza",
          sb.isVisible() and w.act_sidebar.isChecked() and sb.tabs.currentIndex() == TOC_TAB and abs(sb.width() - width) <= 2,
          f"larghezza {sb.width()} (era {width}), scheda {sb.tabs.currentIndex()}")
    sb.tabs.setCurrentIndex(PAGES_TAB)

    # --- 2. lungo_350: miniature pigre ------------------------------------------------
    requested = []
    original = w._renderer.request_thumbs
    def spy(gen, items):
        requested.extend(p for p, _ in items)
        original(gen, items)
    w._renderer.request_thumbs = spy
    w.open_file(str(PDFS / "lungo_350.pdf"))
    wait_rendered(v)
    visible = list(sb.visible_rows())
    dt = wait_for(lambda: visible and all((MAIN, p) in sb.model.thumbs for p in sb.visible_rows()))
    pump(0.3)
    check("lungo: il pannello Pagine ha 350 voci", sb.model.rowCount() == 350, str(sb.model.rowCount()))
    check("lungo: arrivano le miniature visibili", dt is not None, f"{dt and round(dt, 2)}s, visibili {visible}")
    check("lungo: chieste solo le miniature visibili (meno di 30)", 0 < len(set(requested)) < 30,
          f"{len(set(requested))} pagine chieste, {len(sb.model.thumbs)} arrivate")
    thumb = sb.model.thumbs.get((MAIN, 0))
    check("miniatura larga ~113 px (A4, lato massimo 160)", thumb is not None and abs(thumb.width() / sb.devicePixelRatioF() - 113) <= 2,
          thumb and f"{thumb.width()}x{thumb.height()}")
    check("lungo: miniatura della pagina corrente selezionata", sb.pages.currentIndex().row() == 0)
    sb.pages.grab().save(str(SHOTS / "pannello_miniature.png"))

    # --- 3. clic sulla miniatura, la vista porta con sé il pannello -------------------
    index = sb.model.index(119)
    sb.pages.scrollTo(index); app.processEvents()
    rect = sb.pages.visualRect(index)
    QTest.mouseClick(sb.pages.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
    app.processEvents()
    check("clic sulla miniatura 120 → pagina 120", v.current_page == 119 and w._page_box.value() == 120, str(v.current_page))
    check("dopo il clic la miniatura 120 è selezionata", sb.pages.currentIndex().row() == 119)
    t0 = time.perf_counter(); dt = wait_rendered(v)
    check("render della pagina dopo il salto (pannello aperto) < 0,5 s", dt is not None and dt < 0.5, f"{dt and round(dt, 3)}s")
    v.go_to_page(299); app.processEvents()
    rect = sb.pages.visualRect(sb.model.index(299))
    check("vista a pagina 300 → miniatura 300 selezionata e visibile",
          sb.pages.currentIndex().row() == 299 and sb.pages.viewport().rect().contains(rect), str(sb.pages.currentIndex().row()))
    requested.clear()
    dt = wait_for(lambda: all((MAIN, p) in sb.model.thumbs for p in sb.visible_rows()))
    check("le miniature intorno a pagina 300 arrivano", dt is not None and set(sb.visible_rows()) <= set(requested) | {p for (_, p) in sb.model.thumbs},
          f"{dt and round(dt, 2)}s")
    for p in (60, 200, 340):
        v.go_to_page(p); dt = wait_rendered(v)
        check(f"salto a pagina {p + 1}: render < 0,5 s anche con le miniature in coda", dt is not None and dt < 0.5, f"{dt and round(dt, 3)}s")

    # Scorrimento veloce del pannello: la GUI non si ferma.
    gaps, last = [], time.perf_counter()
    bar = sb.pages.verticalScrollBar()
    for i in range(150):
        bar.setValue(bar.value() + (300 if i < 75 else -300))
        app.processEvents(); now = time.perf_counter(); gaps.append(now - last); last = now
    pump(0.5)
    check("scorrimento veloce del pannello: GUI reattiva", max(gaps) < 0.15, f"pausa max {max(gaps) * 1000:.0f} ms")

    # --- 5. Indice --------------------------------------------------------------------
    w.open_file(str(PDFS / "indice.pdf")); wait_rendered(v)
    sb.tabs.setCurrentIndex(TOC_TAB); app.processEvents()
    model = sb.toc_model
    top = [model.item(r).text() for r in range(model.rowCount())]
    children = [model.item(0).child(r).text() for r in range(model.item(0).rowCount())]
    check("indice: voci di 1° livello", top == ["Introduzione", "Metodo", "Conclusioni"], str(top))
    check("indice: voci di 2° livello", children == ["Scopo", "Paragrafo in basso"]
          and model.item(1).child(0).text() == "Pagina ruotata", str(children))
    check("indice: albero mostrato, senza messaggio", sb.toc_stack.currentWidget() is sb.toc)
    item = model.item(0).child(1)
    sb.toc.scrollTo(item.index()); app.processEvents()
    rect = sb.toc.visualRect(item.index())
    QTest.mouseClick(sb.toc.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
    app.processEvents()
    page, y = v.scroll_position()
    check("clic su «Paragrafo in basso» (2° livello) → pagina 3 alla y del segnalibro",
          page == 2 and abs(y - 600) < 2, f"pagina {page + 1}, y {y:.1f}")
    check("il segnalibro entra nella cronologia (Indietro)", w.act_back.isEnabled())
    w.go_back(); app.processEvents()
    check("Indietro torna a prima del segnalibro", v.current_page == 0, str(v.current_page))
    item = model.item(1).child(0)
    sb.toc.expand(model.item(1).index()); app.processEvents()
    QTest.mouseClick(sb.toc.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, sb.toc.visualRect(item.index()).center())
    app.processEvents()
    check("clic su «Pagina ruotata» → pagina 10", v.current_page == 9, str(v.current_page))
    sb.grab().save(str(SHOTS / "pannello_indice.png"))

    w.open_file(str(PDFS / "semplice.pdf")); app.processEvents()
    check("PDF senza indice: messaggio", sb.toc_stack.currentWidget() is sb.no_toc and sb.no_toc.text() == NO_TOC
          and sb.tabs.isTabEnabled(TOC_TAB) and sb.toc_model.rowCount() == 0)

    # --- 6. Ruota vista ---------------------------------------------------------------
    w.open_file(str(PDFS / "indice.pdf")); v.go_to_page(0); wait_rendered(v)
    sb.tabs.setCurrentIndex(PAGES_TAB)
    check("Ruota vista: scorciatoie Ctrl+Shift+Più / Meno",
          w.act_view_right.shortcut().toString() == "Ctrl+Shift++" and w.act_view_left.shortcut().toString() == "Ctrl+Shift+-",
          f"{w.act_view_right.shortcut().toString()} {w.act_view_left.shortcut().toString()}")
    s0 = v._scale()
    before = (v._page_rect(0).width() / s0, v._page_rect(0).height() / s0)
    w.act_view_right.trigger(); dt = wait_rendered(v)
    r = v._page_rect(0)
    check("ruota a destra: A4 largo 842 pt a schermo", v.rotation == 90 and abs(r.width() / v._scale() - 842) < 1.5
          and abs(r.height() / v._scale() - 595) < 1.5, f"{r.width() / v._scale():.1f} x {r.height() / v._scale():.1f} pt")
    check("ruota a destra: file e modelli non cambiano", v.page_sizes[0] == (595.0, 842.0) and not w.modified and "*" not in w.windowTitle())
    img = v._shown_image(0, v._cache[0][1]) if 0 in v._cache else None
    check("ruota a destra: immagine della pagina ruotata", dt is not None and img is not None and img.width() > img.height(),
          img and f"{img.width()}x{img.height()}")
    check("ruota a destra: le miniature ruotano con la vista", sb.model.refs[0].rotate == 90)
    # Conversioni: il centro dell'angolo in alto a sinistra della pagina va in alto a destra.
    corner = v.page_rect_to_viewport(0, QRectF(0, 0, 10, 10))
    page_rect = v.page_rect_to_viewport(0, QRectF(0, 0, 595, 842))
    check("ruota a destra: l'angolo in alto a sinistra della pagina va in alto a destra",
          abs(corner.right() - page_rect.right()) < 1 and abs(corner.top() - page_rect.top()) < 1,
          f"{corner} in {page_rect}")
    for rot in (90, 180, 270):
        v.set_rotation(rot)
        ok = True
        for pt in (QPointF(10, 20), QPointF(500, 800), QPointF(300, 100)):
            view_pt = v.page_rect_to_viewport(0, QRectF(pt, pt)).topLeft()
            back = v.to_page_point(0, view_pt)
            ok = ok and abs(back.x() - pt.x()) < 0.01 and abs(back.y() - pt.y()) < 0.01
        check(f"rotazione {rot}°: punto pagina → vista → pagina", ok)
    v.set_rotation(90); wait_rendered(v)
    v.grab().save(str(SHOTS / "vista_ruotata.png"))
    words = v._words.get(0)
    if words is None:
        wait_for(lambda: 0 in v._words)
        words = v._words.get(0)
    x0, y0, x1, y1 = words.words[0][:4] if words and hasattr(words, "words") else (72, 80, 100, 100)
    pos = v.page_rect_to_viewport(0, QRectF(x0, y0, x1 - x0, y1 - y0)).center()
    check("vista ruotata: la parola sotto il mouse è quella giusta", v._text_word(pos) == (0, 0), str(v._text_word(pos)))
    for _ in range(3):
        w.act_view_right.trigger()
    app.processEvents()
    after = (v._page_rect(0).width() / v._scale(), v._page_rect(0).height() / v._scale())
    check("ruota a destra 4 volte → orientamento iniziale", v.rotation == 0 and all(abs(a - b) < 1.5 for a, b in zip(before, after))
          and sb.model.refs[0].rotate == 0, f"{after}")
    w.act_view_left.trigger(); app.processEvents()
    check("ruota a sinistra = 270°", v.rotation == 270)
    w.act_view_right.trigger()

    # Con modifiche da salvare o uno strumento attivo la vista non si ruota.
    w.annotations.add(TextBox(0, (100.0, 100.0, 200.0, 120.0), text="prova"))
    app.processEvents()
    check("caselle da salvare: Ruota vista disabilitata", w.modified and not w.act_view_right.isEnabled() and not w.act_view_left.isEnabled())
    w.placements.undo_stack.undo(); app.processEvents()
    check("annullata la casella: Ruota vista di nuovo attiva", not w.modified and w.act_view_right.isEnabled())
    w.act_text.trigger(); app.processEvents()
    check("strumento Testo attivo: Ruota vista disabilitata", v.text_tool and not w.act_view_right.isEnabled())
    v.cancel_text_tool(); app.processEvents()
    w.act_view_right.trigger(); app.processEvents()
    check("vista ruotata: Testo raddrizza la vista", v.rotation == 90)
    w.act_text.trigger(); app.processEvents()
    check("…e lo strumento parte con la vista dritta", v.rotation == 0 and v.text_tool)
    v.cancel_text_tool()
    w.act_view_right.trigger()
    w.open_file(str(PDFS / "semplice.pdf")); app.processEvents()
    check("aprendo un altro documento la vista torna dritta", v.rotation == 0 and sb.model.refs[0].rotate == 0)

    w.act_close.trigger(); app.processEvents()
    check("chiudendo il documento il pannello si svuota", sb.model.rowCount() == 0 and sb.toc_model.rowCount() == 0)
    w.close()
    print(f"\n{sum(results)}/{len(results)} ok", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
