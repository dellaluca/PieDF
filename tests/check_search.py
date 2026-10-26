"""Controllo end-to-end della ricerca testo (#14) sui PDF di tests/make_test_pdfs.py.

    python tests/make_test_pdfs.py
    set QT_QPA_PLATFORM=offscreen
    python tests/check_search.py
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
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

    import piedf.paths as paths
    data = S / "data_ricerca"
    shutil.rmtree(data, ignore_errors=True)
    data.mkdir(parents=True)
    paths.data_dir = lambda: data
    SHOTS.mkdir(parents=True, exist_ok=True)

    from piedf.document import TextBox
    from piedf.search_bar import DELAY_MS, NO_RESULTS, NO_TEXT

    app = QApplication(["t"])
    results = []

    def check(name, ok, info=""):
        results.append(bool(ok))
        print(("OK  " if ok else "FAIL"), name, info, flush=True)

    def pump(sec=0.05):
        t = time.perf_counter()
        while time.perf_counter() - t < sec:
            app.processEvents(); time.sleep(0.01)

    QMessageBox.exec = lambda box: QMessageBox.StandardButton.Discard
    QInputDialog.getText = staticmethod(lambda *a, **k: ("", False))

    from piedf.main_window import MainWindow
    w = MainWindow(); w.resize(1000, 800); w.show()
    v, bar = w.view, w.search_bar
    field = bar.field
    NO = Qt.KeyboardModifier.NoModifier
    SHIFT = Qt.KeyboardModifier.ShiftModifier

    def wait_rendered(timeout=15):
        t = time.perf_counter()
        while time.perf_counter() - t < timeout:
            app.processEvents()
            v.viewport().repaint()
            vis = list(v._visible_range())
            if vis and all(p in v._cache and v._cache[p][0] == v._render_scale(p) for p in vis):
                return time.perf_counter() - t
            time.sleep(0.005)
        return None

    def wait_done(timeout=30):
        t = time.perf_counter()
        while time.perf_counter() - t < timeout:
            app.processEvents()
            if not bar.searching and not bar._timer.isActive() and not bar._rebuild_timer.isActive():
                return time.perf_counter() - t
            time.sleep(0.005)
        return None

    def search(text, case=False):
        """Scrive il testo nel campo come l'utente e aspetta la fine della ricerca."""
        if bar.case_box.isChecked() != case:
            bar.case_box.setChecked(case)
        field.selectAll()
        QTest.keyClicks(field, text)
        return wait_done()

    def label():
        return bar.count_label.text()

    def key(k, mod=NO, widget=None):
        QTest.keyClick(widget or app.focusWidget() or w, k, mod)
        app.processEvents()

    # --- 1. Un risultato, salto alla pagina ----------------------------------------------
    w.open_file(str(PDFS / "lungo_350.pdf")); wait_rendered()
    check("Trova abilitato con un documento", w.act_find.isEnabled() and w.act_find_next.isEnabled())
    key(Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier, v)
    check("Ctrl+F apre la barra col fuoco nel campo", bar.isVisible() and app.focusWidget() is field)
    vp = v.viewport()
    check("la barra sta sopra la vista, in alto", vp.geometry().contains(bar.geometry().topLeft()) and bar.y() < vp.y() + 20,
          f"{bar.geometry()} in {vp.geometry()}")
    QTest.keyClicks(field, "Pagina 200 di")
    pump(DELAY_MS / 1000 * 0.5)
    check("ritardo di 300 ms prima di cercare", not bar.searching and not bar.hits and bar._timer.isActive())
    dt = wait_done()
    wait_rendered()
    hits = bar.hits
    check("'Pagina 200 di': 1 risultato", dt is not None and len(hits) == 1 and label() == "1 di 1", f"{len(hits)} «{label()}»")
    check("'Pagina 200 di': vista a pagina 200", v.current_page == 199 and w._page_box.value() == 200, str(v.current_page + 1))
    page, rect = hits[0] if hits else (-1, QRectF())
    check("rettangolo nella zona del titolo", page == 199 and 60 < rect.left() < 90 and 30 < rect.top() < 60 and rect.bottom() < 90,
          f"{rect}")
    on_screen = v.page_rect_to_viewport(page, rect)
    check("risultato evidenziato e in vista", v._search_current == (page, rect) and QRectF(vp.rect()).contains(on_screen),
          f"{on_screen}")
    img = v.viewport().grab().toImage()
    c = img.pixelColor(on_screen.center().toPoint() + QPointF(0, on_screen.height() * 0.45).toPoint())
    check("evidenziazione arancione disegnata", c.red() > 200 and c.blue() < 150, c.name())
    check("Indietro torna a prima del salto", w.act_back.isEnabled())
    v.grab().save(str(SHOTS / "ricerca_pagina200.png"))

    # --- 2. Molti risultati, F3 / Maiusc+F3 ----------------------------------------------
    v.go_to_page(0); wait_rendered()
    # Ogni riga è "Riga N: lorem ipsum dolor sit amet " ripetuto due volte: 2 risultati per pagina.
    dt = search("Riga 7:")
    check("'Riga 7:': 700 risultati, 2 per pagina su tutte le 350", len(bar.hits) == 700 and len({p for p, _ in bar.hits}) == 350,
          f"{len(bar.hits)} in {dt and round(dt, 2)}s «{label()}»")
    check("salto al primo risultato dalla pagina corrente", bar.current_index == 0 and label() == "1 di 700", label())
    by_page = [p for p, _ in bar.hits[:4]]
    check("ordine: pagina, poi da sinistra a destra", by_page == [0, 0, 1, 1] and bar.hits[0][1].left() < bar.hits[1][1].left(),
          str(by_page))
    for _ in range(10):
        key(Qt.Key.Key_F3, NO, field)
    check("F3 dieci volte: risultato 11", bar.current_index == 10 and label() == "11 di 700" and v.current_page == 5,
          f"«{label()}» pagina {v.current_page + 1}")
    key(Qt.Key.Key_F3, SHIFT, field)
    check("Maiusc+F3: risultato 10", bar.current_index == 9 and label() == "10 di 700", label())
    key(Qt.Key.Key_Return, NO, field)
    check("Invio: successivo", bar.current_index == 10, label())
    key(Qt.Key.Key_Return, SHIFT, field)
    check("Maiusc+Invio: precedente", bar.current_index == 9, label())
    v.setFocus(); app.processEvents()
    key(Qt.Key.Key_F3, NO, v)
    check("F3 anche con il fuoco nella vista", bar.current_index == 10, label())
    bar.previous(); bar.previous()
    for _ in range(9):
        bar.previous()
    check("◀ dal primo va all'ultimo", bar.current_index == 699 and v.current_page == 349, label())

    # --- 3. GUI reattiva e render durante la ricerca -------------------------------------
    v.go_to_page(0); wait_rendered()
    field.setText("lorem")  # 80 risultati per pagina: 28 000 in tutto
    bar._start()
    gaps, last = [], time.perf_counter()
    t0 = last
    while bar.searching and time.perf_counter() - t0 < 30:
        app.processEvents(); time.sleep(0.005); now = time.perf_counter(); gaps.append(now - last); last = now
    total = time.perf_counter() - t0
    check("GUI reattiva durante la ricerca su 350 pagine", gaps and max(gaps) < 0.15,
          f"pausa max {max(gaps) * 1000:.0f} ms, ricerca {total:.2f}s, {len(bar.hits)} risultati")
    wait_done()
    check("'lorem': tutti i risultati", len(bar.hits) == 350 * 80, str(len(bar.hits)))
    # Render di una pagina appena raggiunta mentre la ricerca è in corso.
    v.go_to_page(0); wait_rendered()
    field.setText("ipsum")
    bar._start()
    pump(0.02)
    v.go_to_page(300)
    dt = wait_rendered()
    check("render di una pagina raggiunta durante la ricerca < 0,5 s", dt is not None and dt < 0.5,
          f"{dt and round(dt, 3)}s, ricerca ancora in corso: {bar.searching}")
    wait_done()

    # --- 4. Una ricerca nuova annulla la precedente -------------------------------------
    received = []
    w._renderer.search_hits.connect(lambda gen, sid, page, rects: received.append(sid))
    v.go_to_page(0); wait_rendered()
    field.setText("Riga 1"); bar._start()
    first_id = bar._id
    field.setText("Pagina 3 di"); bar._start()
    wait_done(); pump(0.3)
    old = received.count(first_id)
    check("nuova ricerca: solo i risultati della nuova", len(bar.hits) == 1 and bar.hits[0][0] == 2 and label() == "1 di 1",
          f"{[(p + 1, round(r.top())) for p, r in bar.hits[:5]]} «{label()}»")
    check("la ricerca vecchia si ferma nel processo", old < 350, f"{old} pagine della vecchia arrivate")

    # --- 5. Maiuscole/minuscole ---------------------------------------------------------
    search("riga 7:")
    check("'riga 7:' senza Maiuscole: 700", len(bar.hits) == 700, label())
    search("riga 7:", case=True)
    check("'riga 7:' con Maiuscole: 0 e bordo rosso", not bar.hits and label() == NO_RESULTS and "#d93025" in field.styleSheet(),
          f"«{label()}»")
    search("Riga 7:", case=True)
    check("'Riga 7:' con Maiuscole: 700", len(bar.hits) == 700, label())
    search("Riga 7: lorem", case=True)
    check("Maiuscole: anche testo con spazi", len(bar.hits) == 700, label())
    search("xyzzy")
    check("nessun risultato: messaggio e bordo rosso", label() == NO_RESULTS and "#d93025" in field.styleSheet(), label())
    search("Pagina 2 di")
    check("risultato trovato: bordo normale", bar.hits and field.styleSheet() == "", label())
    bar.case_box.setChecked(False)

    # --- 6. Documento senza testo -------------------------------------------------------
    w.open_file(str(PDFS / "scansionato.pdf")); wait_rendered()
    check("aprire un documento azzera la ricerca", not bar.isVisible() and not bar.hits and not v._search_hits)
    w.act_find.trigger(); app.processEvents()
    check("Ctrl+F riprende il testo precedente, selezionato", field.text() == "Pagina 2 di" and field.selectedText() == "Pagina 2 di")
    search("Pagina")
    check("scansionato: non contiene testo ricercabile", label() == NO_TEXT and not bar.hits, f"«{label()}»")

    # --- 7. Pagina ruotata --------------------------------------------------------------
    w.open_file(str(PDFS / "orientamenti.pdf")); wait_rendered()
    w.act_find.trigger()
    search("Ruotata")
    hit = bar.hits[0] if len(bar.hits) == 1 else (-1, QRectF())
    page, rect = hit
    pw, ph = v.page_sizes[2]
    check("ruotata: un risultato a pagina 3, dentro la pagina", page == 2 and QRectF(0, 0, pw, ph).contains(rect), f"{rect}")
    # Il testo visibile: pixel scuri della pagina disegnata da MuPDF (come si vede).
    pix = pymupdf.open(PDFS / "orientamenti.pdf")[2].get_pixmap()
    dark = inside = 0
    for y in range(pix.height):
        for x in range(0, pix.width, 2):
            if pix.pixel(x, y)[0] < 128:
                dark += 1
                inside += rect.adjusted(-2, -2, 2, 2).contains(QPointF(x + 0.5, y + 0.5))
    check("ruotata: il rettangolo copre il testo visibile (verticale)",
          rect.height() > rect.width() and inside > 0.5 * dark, f"{inside}/{dark} pixel scuri dentro")
    v.grab().save(str(SHOTS / "ricerca_ruotata.png"))

    # --- 8. Esc chiude e toglie le evidenziazioni ---------------------------------------
    field.setFocus()
    key(Qt.Key.Key_Escape, NO, field)
    check("Esc chiude la barra e toglie le evidenziazioni", not bar.isVisible() and not v._search_hits and v._search_current is None
          and app.focusWidget() is v)
    w.act_find.trigger(); wait_done()
    v.setFocus(); app.processEvents()
    key(Qt.Key.Key_Escape, NO, v)
    check("Esc dalla vista chiude la barra", not bar.isVisible() and not v._search_hits)

    # --- Caselle di testo di PieDF ------------------------------------------------------
    w.open_file(str(PDFS / "semplice.pdf")); wait_rendered()
    box = TextBox(0, (100.0, 300.0, 260.0, 314.0), text="Casella cercabile")
    w.annotations.add(box)
    w.act_find.trigger()
    search("CERCABILE")
    check("ricerca anche nelle caselle di testo (non salvate)", len(bar.hits) == 1 and bar.hits[0] == (0, QRectF(100, 300, 160, 14)),
          f"{bar.hits}")
    search("Testo di prova")
    check("caselle e testo della pagina insieme", len(bar.hits) == 1, label())
    w.annotations.add(TextBox(0, (100.0, 50.0, 260.0, 64.0), text="Testo di prova anche qui"))
    app.processEvents()
    check("casella aggiunta con la barra aperta: risultato in ordine", len(bar.hits) == 2 and bar.hits[0][1].top() == 50,
          f"{[(p, r.top()) for p, r in bar.hits]}")
    w.annotations.update(w.annotations.items[-1], "modifica testo", text="altro")
    app.processEvents()
    check("casella modificata: il risultato sparisce", len(bar.hits) == 1, label())
    w.placements.undo_stack.setClean()
    bar.close_bar()

    # Una casella lontana non fa saltare la vista prima che arrivino le pagine vicine.
    w.open_file(str(PDFS / "lungo_350.pdf")); wait_rendered()
    w.annotations.add(TextBox(299, (100.0, 300.0, 260.0, 314.0), text="Riga 7: nella casella"))
    start = v.current_page
    w.act_find.trigger()
    search("Riga 7:")
    current = bar.hits[bar.current_index] if bar.current_index >= 0 else (-1, QRectF())
    check("casella lontana: primo salto alla pagina corrente", start < 299 and current[0] == start == v.current_page,
          f"da pagina {start + 1} a {current[0] + 1}, {label()}")
    w.placements.undo_stack.setClean()
    bar.close_bar()

    # --- Clic su link con la ricerca aperta ---------------------------------------------
    w.open_file(str(PDFS / "link.pdf")); wait_rendered()
    w.act_find.trigger()
    search("interno")
    gotos = []
    v.link_goto.connect(lambda page, y: gotos.append(page))
    for _ in range(50):
        app.processEvents(); time.sleep(0.01)
        if 0 in v._links:
            break
    pos = v.page_rect_to_viewport(0, QRectF(180, 112, 0, 0)).topLeft().toPoint()
    QTest.mouseMove(vp, pos)
    QTest.mouseClick(vp, Qt.MouseButton.LeftButton, NO, pos)
    app.processEvents()
    check("con la barra aperta i link funzionano", gotos == [2] and bar.isVisible(), str(gotos))

    w.act_close.trigger()
    check("chiudere il documento azzera la ricerca", not bar.isVisible() and not w.act_find.isEnabled())

    w.close()
    print(f"\n{sum(results)}/{len(results)} ok", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
