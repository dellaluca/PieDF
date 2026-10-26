"""Controllo end-to-end della selezione e copia del testo (#15), copia mai a metà (#49).

    python tests/make_test_pdfs.py
    set QT_QPA_PLATFORM=offscreen
    python tests/check_text_selection.py
"""
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out"
PDFS = S / "pdfs"


def main():
    import pymupdf
    from PySide6.QtCore import QPoint, QRectF, Qt, QTimer
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox, QPushButton

    import piedf.paths as paths
    data = S / "data_text_selection"
    shutil.rmtree(data, ignore_errors=True)
    data.mkdir(parents=True)
    paths.data_dir = lambda: data

    from piedf.annotations import PEN
    from piedf.main_window import COPY_HINT, MainWindow
    from piedf.signatures import Kind
    from piedf.text_selection import END

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

    warnings = []
    QMessageBox.warning = staticmethod(lambda parent, title, text, *a: warnings.append(text))

    w = MainWindow(); w.resize(1000, 800); w.show(); pump(0.2)
    v = w.view
    vp = v.viewport()
    clipboard = QApplication.clipboard()
    L = Qt.MouseButton.LeftButton
    NO = Qt.KeyboardModifier.NoModifier
    CTRL = Qt.KeyboardModifier.ControlModifier
    IBEAM = Qt.CursorShape.IBeamCursor

    def at(page, x, y):
        return v.page_rect_to_viewport(page, QRectF(x, y, 0, 0)).topLeft().toPoint()

    def hover(pos):
        QTest.mouseMove(vp, pos); app.processEvents()

    def drag(a, b, steps=6):
        hover(a)
        QTest.mousePress(vp, L, NO, a); app.processEvents()
        for i in range(1, steps + 1):
            QTest.mouseMove(vp, a + (b - a) * i / steps); app.processEvents()
        QTest.mouseRelease(vp, L, NO, b); app.processEvents()

    def click(pos):
        hover(pos)
        QTest.mouseClick(vp, L, NO, pos); app.processEvents()

    def key(k, mod=NO):
        if QApplication.activeWindow() is not w:
            # Offscreen, chiuso un dialog modale (avanzamento della copia) nessuna finestra torna
            # attiva e le scorciatoie (Ctrl+C, Ctrl+A) non scatterebbero.
            w.activateWindow(); v.setFocus(); app.processEvents()
        QTest.keyClick(vp, k, mod); app.processEvents()

    def copy():
        key(Qt.Key.Key_C, CTRL)
        wait(lambda: not v.copying, 30)
        return clipboard.text()

    def words_ready(*pages):
        return wait(lambda: all(p in v._words for p in pages))

    def joined(words):
        # Come la vista: spazio sulla stessa riga, a capo quando cambiano blocco o riga.
        out, last = [], None
        for w_ in words:
            key_ = (w_[5], w_[6])
            if last is not None:
                out.append(" " if key_ == last else "\n")
            out.append(w_[4])
            last = key_
        return "".join(out)

    def first(page_words, text, block=None):
        return next(x for x in page_words if x[4] == text and (block is None or x[5] == block))

    # --- 1. semplice.pdf: la prima riga ---
    w.open_file(str(PDFS / "semplice.pdf"))
    check("1. parole della pagina arrivate", words_ready(0), str(list(v._words)))
    check("menu Modifica: Copia e Seleziona tutto", {"&Copia", "Seleziona &tutto"} <= {
        a.text() for m in w.menuBar().actions() if m.text() == "&Modifica" for a in m.menu().actions()})
    hover(at(0, 100, 64))
    check("1. cursore I sopra il testo", vp.cursor().shape() == IBEAM, str(vp.cursor().shape()))
    hover(at(0, 400, 400))
    check("1. cursore normale su un punto vuoto", vp.cursor().shape() != IBEAM)
    clipboard.setText("prima")
    drag(at(0, 73, 64), at(0, 335, 64))
    check("1. selezione evidenziata", v.selected_text() == "PieDF - documento semplice", repr(v.selected_text()))
    check("1. Ctrl+C → «PieDF - documento semplice»", copy() == "PieDF - documento semplice", repr(clipboard.text()))
    img = vp.grab().toImage()
    p = at(0, 200, 64)
    color = img.pixelColor(p.x() * img.width() // vp.width(), p.y() * img.height() // vp.height())
    check("1. sfondo azzurro dietro il testo selezionato", color.blue() > color.red() + 30, color.name())
    w.statusBar().clearMessage()
    clipboard.setText("prima")
    w.act_copy.trigger()
    check("1. Modifica › Copia", clipboard.text() == "PieDF - documento semplice", repr(clipboard.text()))
    key(Qt.Key.Key_Escape)
    check("Esc toglie la selezione", v.selected_text() == "")
    drag(at(0, 73, 64), at(0, 180, 105))
    check("due righe: a capo tra le righe", v.selected_text() == "PieDF - documento semplice\nTesto di prova per il",
          repr(v.selected_text()))
    click(at(0, 400, 400))
    check("clic su un punto vuoto: niente selezione", v.selected_text() == "" and v._selection is None)
    drag(at(0, 73, 64), at(0, 335, 64))
    click(at(0, 100, 64))
    check("clic sul testo senza trascinare: niente selezione", v.selected_text() == "")

    # Editor di testo con il focus: Copia vale per lui.
    drag(at(0, 73, 64), at(0, 335, 64))
    w._page_box.setFocus(); w._page_box.findChild(QLineEdit).selectAll(); app.processEvents()
    clipboard.setText("prima")
    w.act_copy.trigger()
    check("Copia con il numero di pagina nel focus: copia il numero", clipboard.text() == "1", repr(clipboard.text()))
    v.setFocus(); app.processEvents()

    # --- 5. firma: clic e trascinamento la spostano, nessuna selezione ---
    img = QImage(600, 200, QImage.Format.Format_ARGB32)
    img.fill(QColor("#1a237e"))
    sig = w.signatures.add(img, Kind.SIGNATURE)
    w.place_signature(sig)
    hover(at(0, 200, 64))
    click(at(0, 200, 64))
    placement = w.placements.items[0] if w.placements.items else None
    check("5. firma piazzata sul testo", placement is not None)
    hover(at(0, 200, 400))
    v.clear_text_selection()
    start = QRectF(placement.rect)
    c = start.center()
    hover(at(0, c.x(), c.y()))
    check("5. sopra la firma la mano, non la I", vp.cursor().shape() == Qt.CursorShape.OpenHandCursor,
          str(vp.cursor().shape()))
    drag(at(0, c.x(), c.y()), at(0, c.x() + 60, c.y() + 40))
    check("5. la firma si è spostata", abs(placement.rect.x() - start.x() - 60) < 2, f"{start} → {placement.rect}")
    check("5. nessuna selezione di testo", v._selection is None and v.selected_text() == "")
    clipboard.setText("prima")
    copy()
    check("5. Ctrl+C non cambia gli appunti", clipboard.text() == "prima")
    # In posizionamento il trascinamento non seleziona.
    w.place_signature(sig)
    drag(at(0, 73, 105), at(0, 220, 105))
    check("5. in posizionamento niente selezione", v._selection is None)
    v.cancel_placing()
    w.placements.clear()

    # Strumenti che usano il clic: Disegna e Testo.
    v.start_draw_tool(PEN)
    hover(at(0, 100, 64))
    check("con la penna niente cursore I", vp.cursor().shape() != IBEAM)
    drag(at(0, 73, 64), at(0, 335, 64))
    check("con la penna il trascinamento disegna e non seleziona", v._selection is None and len(w.annotations.items) == 1)
    v.cancel_draw_tool()
    w.annotations.undo_stack.undo()
    v.start_text_tool()
    drag(at(0, 73, 64), at(0, 335, 64))
    check("in modalità Testo niente selezione", v._selection is None)
    v.commit_editor()
    v.cancel_text_tool()
    w.placements.undo_stack.clear()

    # --- 2. lungo_350.pdf: da «Riga 3:» a metà della riga 5 ---
    doc = pymupdf.open(PDFS / "lungo_350.pdf")
    page0 = [tuple(x) for x in doc[0].get_text("words", sort=True)]
    page9 = [tuple(x) for x in doc[9].get_text("words", sort=True)]
    page10 = [tuple(x) for x in doc[10].get_text("words", sort=True)]
    doc.close()
    requests = []
    real = w._renderer.request_words

    def spy(gen, page):
        requests.append((page, page in v._visible_range()))
        real(gen, page)

    w._renderer.request_words = spy
    w.open_file(str(PDFS / "lungo_350.pdf"))
    check("2. parole arrivate", words_ready(0))
    check("aprendo un altro documento la selezione si svuota", v._selection is None)
    riga3 = first(page0, "Riga", block=3)
    row5 = [x for x in page0 if x[5] == 5]
    mid = (row5[0][0] + row5[-1][2]) / 2
    y5 = (row5[0][1] + row5[0][3]) / 2
    drag(at(0, riga3[0] + 0.5, (riga3[1] + riga3[3]) / 2), at(0, mid, y5))
    expected = joined([x for x in page0 if x[5] in (3, 4)] + [x for x in row5 if (x[0] + x[2]) / 2 <= mid])
    text = copy()
    check("2. tre righe con gli a capo giusti", text == expected and text.count("\n") == 2,
          repr(text[:60]) + " … " + repr(text[-30:]))
    check("2. comincia da «Riga 3:» e finisce a metà della riga 5",
          text.startswith("Riga 3: lorem") and text.splitlines()[-1].startswith("Riga 5:")
          and len(text.splitlines()[-1]) < len(joined(row5)) - 5, repr(text.splitlines()[-1]))

    # --- 4. doppio clic su «lorem», triplo clic, Ctrl+A ---
    lorem = first(page0, "lorem", block=1)
    pos = at(0, (lorem[0] + lorem[2]) / 2, (lorem[1] + lorem[3]) / 2)
    hover(pos)
    QTest.mouseDClick(vp, L, NO, pos); app.processEvents()
    check("4. doppio clic su «lorem» → solo quella parola", v.selected_text() == "lorem", repr(v.selected_text()))
    check("4. Ctrl+C → «lorem»", copy() == "lorem", repr(clipboard.text()))
    QTest.mouseClick(vp, L, NO, pos); app.processEvents()
    row1 = joined([x for x in page0 if x[5] == 1])
    check("triplo clic → tutta la riga", v.selected_text() == row1, repr(v.selected_text()))
    pump(QApplication.doubleClickInterval() / 1000 + 0.1)
    QTest.mouseClick(vp, L, NO, pos); app.processEvents()
    check("un clic dopo l'intervallo non è un triplo clic", v.selected_text() == "")
    key(Qt.Key.Key_A, CTRL)
    check("4. Ctrl+A → tutto il documento", v._ordered_selection() == ((0, 0), (349, END)), str(v._ordered_selection()))

    # --- #49: Ctrl+A e subito Ctrl+C su 350 pagine: testo intero, appunti scritti una volta sola ---
    doc = pymupdf.open(PDFS / "lungo_350.pdf")
    whole = "\n".join(joined([tuple(x) for x in page.get_text("words", sort=True)]) for page in doc)
    doc.close()
    writes = []
    clipboard.dataChanged.connect(lambda: writes.append(clipboard.text()))
    clipboard.setText("prima")
    writes.clear()
    started = time.perf_counter()
    key(Qt.Key.Key_C, CTRL)
    check("#49 Ctrl+C con pagine da leggere: copia in corso", v.copying)
    check("#49 durante la copia: cursore di attesa", QApplication.overrideCursor() is not None
          and QApplication.overrideCursor().shape() == Qt.CursorShape.BusyCursor)
    check("#49 durante la copia: «Copia del testo…» nella barra di stato", w.statusBar().currentMessage() == COPY_HINT,
          repr(w.statusBar().currentMessage()))
    check("#49 appunti invariati finché mancano pagine", clipboard.text() == "prima" and not writes)
    dialog_seen = []
    wait(lambda: dialog_seen.append(w._copy_dialog is not None and w._copy_dialog.isVisible()) or not v.copying, 60)
    elapsed = time.perf_counter() - started
    text = clipboard.text()
    check("#49 Ctrl+A + Ctrl+C: tutto il testo, uguale a quello di PyMuPDF", text == whole,
          f"{len(text)} / {len(whole)} caratteri in {elapsed:.1f} s")
    check("#49 «Pagina 1 di 350» … «Pagina 350 di 350», in ordine",
          text.startswith("Pagina 1 di 350") and 0 < text.find("Pagina 350 di 350") and text.find("Pagina 2 di 350") < text.find("Pagina 350 di 350"))
    check("#49 appunti scritti una volta sola", len(writes) == 1, str(len(writes)))
    check("#49 oltre 0,5 s compare l'avanzamento", any(dialog_seen) or elapsed < 0.6, f"{elapsed:.2f} s")
    check("#49 finita la copia: niente cursore di attesa, niente avanzamento, barra di stato pulita",
          QApplication.overrideCursor() is None and w._copy_dialog is None and w.statusBar().currentMessage() != COPY_HINT)

    # Copia annullata con Esc, con Annulla dell'avanzamento e con una nuova selezione: appunti invariati.
    def cancelled(name, cancel):
        clipboard.setText("prima")
        writes.clear()
        key(Qt.Key.Key_C, CTRL)
        job = v._copy
        ok = v.copying
        cancel()
        ok = ok and not v.copying and not job.process.is_alive()
        pump(1.0)  # niente arriva più tardi
        check(f"#49 copia annullata ({name}): appunti invariati", ok and clipboard.text() == "prima" and not writes,
              repr(clipboard.text()[:30]))

    cancelled("Esc", lambda: key(Qt.Key.Key_Escape))
    check("#49 Esc annulla la copia ma la selezione resta", v._ordered_selection() == ((0, 0), (349, END)))

    def cancel_dialog():
        w._show_copy_dialog()  # come dopo COPY_DIALOG_MS, senza dipendere da quanto dura la copia
        w._copy_dialog.findChild(QPushButton).click(); app.processEvents()

    cancelled("Annulla dell'avanzamento", cancel_dialog)
    check("#49 dopo Annulla niente cursore di attesa né avanzamento", QApplication.overrideCursor() is None and w._copy_dialog is None)
    cancelled("nuova selezione", lambda: click(at(0, 400, 600)))
    key(Qt.Key.Key_A, CTRL)
    cancelled("chiusura del documento", lambda: w.close_document())
    w.open_file(str(PDFS / "lungo_350.pdf"))
    key(Qt.Key.Key_A, CTRL)
    cancelled("apertura di un altro documento", lambda: w.open_file(str(PDFS / "lungo_350.pdf")))
    check("#49 nessun messaggio d'errore annullando", not warnings, str(warnings))

    # Il processo della copia muore a metà: messaggio, niente testo parziale.
    key(Qt.Key.Key_A, CTRL)
    clipboard.setText("prima")
    writes.clear()
    key(Qt.Key.Key_C, CTRL)
    job = v._copy
    wait(lambda: w._copy_done[0] > 0 or not v.copying, 30)
    check("#49 processo della copia ucciso a metà", v.copying and 0 < w._copy_done[0] < w._copy_done[1],
          str(w._copy_done))
    job.process.kill()
    wait(lambda: not v.copying, 10)
    check("#49 processo morto: «Impossibile copiare il testo»", not v.copying and warnings == ["Impossibile copiare il testo."],
          str(warnings))
    check("#49 processo morto: appunti invariati", clipboard.text() == "prima" and not writes, repr(clipboard.text()[:30]))
    warnings.clear()
    reopened = len(requests)  # il documento è stato riaperto: le pagine si richiedono da capo
    v.set_scroll_position(0, 0.0)

    # --- 3. dal fondo di pagina 10 all'inizio di pagina 11 ---
    v.set_scroll_position(9, 600.0)
    check("3. pagine 10 e 11 visibili", {9, 10} <= set(v._visible_range()) and words_ready(9, 10),
          str(list(v._visible_range())))
    riga40 = first(page9, "Riga", block=40)
    title = [x for x in page10 if x[5] == 0]
    drag(at(9, riga40[0] + 0.5, (riga40[1] + riga40[3]) / 2), at(10, title[-1][2] + 5, (title[-1][1] + title[-1][3]) / 2))
    expected = joined([x for x in page9 if x[5] == 40]) + "\n" + "Pagina 11 di 350"
    text = copy()
    check("3. testo di entrambe le pagine, in ordine", text == expected, repr(text))

    # Trascinando sotto la vista, la vista scorre e la selezione la segue.
    v.set_scroll_position(20, 0.0)
    check("pagina 21 pronta", words_ready(20))

    # Le parole arrivano dal processo di render con 1 s di ritardo (#49): la copia non dipende dai tempi.
    def slow(gen, page):
        requests.append((page, page in v._visible_range()))
        QTimer.singleShot(1000, lambda: real(gen, page))

    w._renderer.request_words = slow
    vbar = v.verticalScrollBar()
    value = vbar.value()
    a = at(20, 73, 116)  # riga 1
    hover(a)
    QTest.mousePress(vp, L, NO, a)
    QTest.mouseMove(vp, a + QPoint(0, 30))
    QTest.mouseMove(vp, QPoint(a.x(), vp.height() + 60))
    pump(0.8)
    QTest.mouseRelease(vp, L, NO, QPoint(a.x(), vp.height() + 60)); app.processEvents()
    check("trascinando sotto la vista: scorre da sola", vbar.value() > value + 100, f"{value} → {vbar.value()}")
    sel = v._ordered_selection()
    check("trascinando sotto la vista: la selezione arriva più giù", sel is not None and sel[1] > (20, 200), str(sel))
    value = vbar.value()
    pump(0.2)
    check("rilasciato il tasto non scorre più", vbar.value() == value)
    missing = [p for p in range(sel[0][0], sel[1][0] + 1) if p not in v._words] if sel else []
    clipboard.setText("prima")
    key(Qt.Key.Key_C, CTRL)
    check("#49 parole di qualche pagina non ancora arrivate: appunti invariati", missing and clipboard.text() == "prima",
          f"mancano {missing}")
    wait(lambda: not v.copying, 30)
    text = clipboard.text()
    end = v._ordered_selection()[1] if v._ordered_selection() else None
    check("#49 la fine della selezione stimata senza parole diventa quella del mouse", end is not None and end > sel[1]
          and v._selection_guess == (None, None), f"{sel[1]} → {end}")
    check("selezione su più pagine copiata, con le pagine in ordine",
          text.startswith("Riga 1: lorem") and f"Pagina {sel[1][0] + 1} di 350" in text and "Pagina 22 di 350" in text,
          repr(text[:40]))
    check("parole chieste solo per le pagine visibili (o della selezione)",
          requests and all(vis for _, vis in requests[:5]), str(requests[:10]))
    fresh = requests[reopened:]
    check("ogni pagina chiesta una volta sola", len({p for p, _ in fresh}) == len(fresh), str(sorted(fresh)))
    w._renderer.request_words = real

    # Link: un clic lo segue, trascinando dal link si seleziona.
    w.open_file(str(PDFS / "link.pdf"))
    check("link.pdf: link e parole arrivate", wait(lambda: 0 in v._links) and words_ready(0))
    lw = [tuple(x) for x in pymupdf.open(PDFS / "link.pdf")[0].get_text("words", sort=True)]
    label = first(lw, "interno,")
    before = v.scroll_position()
    drag(at(0, 186, 112), at(0, label[0] - 1, (label[1] + label[3]) / 2))
    check("trascinando da un link: nessuna navigazione", v.scroll_position() == before and not w._back)
    check("trascinando da un link: selezione del testo", v.selected_text().startswith("interno,"), repr(v.selected_text()))
    click(at(0, 186, 112))
    check("clic sul link: lo segue e la selezione sparisce", v.current_page == 2 and v._selection is None)

    # Pagina ruotata: le parole sono dove si vedono, nell'ordine in cui si leggono.
    w.open_file(str(PDFS / "modulo.pdf"))
    v.go_to_page(1)
    check("modulo.pdf: parole della pagina ruotata", words_ready(1))
    pos = at(1, 117, 86)
    hover(pos)
    check("pagina ruotata: cursore I sopra «Pagina»", vp.cursor().shape() == IBEAM, str(vp.cursor().shape()))
    QTest.mouseDClick(vp, L, NO, pos); app.processEvents()
    check("pagina ruotata: doppio clic → «Pagina»", v.selected_text() == "Pagina", repr(v.selected_text()))
    key(Qt.Key.Key_A, CTRL)
    text = copy()
    check("pagina ruotata: Ctrl+A → tutto, finisce con «Pagina ruotata»",
          text.startswith("Modulo di prova") and text.endswith("\nPagina ruotata"), repr(text))
    v.go_to_page(0)
    words_ready(0)
    drag(at(0, 275, 110), at(0, 80, 154))
    check("trascinando da un campo compilabile: si apre il campo, nessuna selezione",
          v.editing_field is not None and v._selection is None)
    v.commit_editor()

    # --- 6. scansionato.pdf: niente da selezionare ---
    w.open_file(str(PDFS / "scansionato.pdf"))
    check("6. parole (nessuna) arrivate", words_ready(0) and len(v._words[0]) == 0)
    hover(at(0, 300, 400))
    check("6. niente cursore I", vp.cursor().shape() != IBEAM)
    clipboard.setText("prima")
    drag(at(0, 72, 72), at(0, 500, 600))
    check("6. trascinamento: nessuna selezione", v.selected_text() == "")
    key(Qt.Key.Key_A, CTRL)
    check("6. Ctrl+A: nessuna selezione", v.selected_text() == "")
    copy()
    check("6. appunti invariati", clipboard.text() == "prima", repr(clipboard.text()))

    w.close_document()
    check("chiudendo il documento la selezione si svuota", v._selection is None and not v._words)
    w.close(); app.processEvents()

    print(f"\n{sum(results)}/{len(results)} ok", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
