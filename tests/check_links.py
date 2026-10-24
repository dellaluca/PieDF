"""Controllo end-to-end dei link cliccabili, con Indietro/Avanti (#24).

    python tests/make_test_pdfs.py
    set QT_QPA_PLATFORM=offscreen
    python tests/check_links.py
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
    from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QUrl
    from PySide6.QtGui import QColor, QDesktopServices, QImage
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox

    import piedf.paths as paths
    data = S / "data_link"
    shutil.rmtree(data, ignore_errors=True)
    data.mkdir(parents=True)
    paths.data_dir = lambda: data

    from piedf.main_window import MAX_HISTORY, MainWindow
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

    def wait(cond, sec=10):
        t = time.perf_counter()
        while not cond() and time.perf_counter() - t < sec:
            app.processEvents(); time.sleep(0.01)
        return cond()

    # La conferma e il browser sono sostituiti: niente dialog né browser veri.
    answers, asked, opened = [], [], []

    def fake_exec(box):
        asked.append(box.text())
        return answers.pop(0)

    QMessageBox.exec = fake_exec
    QDesktopServices.openUrl = staticmethod(lambda url: opened.append(QUrl(url)) or True)

    w = MainWindow(); w.resize(1000, 800); w.show(); pump(0.2)
    v = w.view
    vp = v.viewport()
    L = Qt.MouseButton.LeftButton
    NO = Qt.KeyboardModifier.NoModifier
    ALT = Qt.KeyboardModifier.AltModifier
    HAND = Qt.CursorShape.PointingHandCursor

    def at(page, x, y):
        return v.page_rect_to_viewport(page, QRectF(x, y, 0, 0)).topLeft().toPoint()

    def hover(pos):
        QTest.mouseMove(vp, pos); app.processEvents()

    def click(pos, button=L):
        hover(pos)
        QTest.mouseClick(vp, button, NO, pos)
        app.processEvents()

    def key(k, mod=NO):
        QTest.keyClick(vp, k, mod); app.processEvents()

    def near(pos, page, y, tol=5):
        return pos[0] == page and abs(pos[1] - y) <= tol

    def top():
        v.set_scroll_position(0, 0.0)
        return wait(lambda: 0 in v._links)

    # Centri dei link di link.pdf (punti, pagina 1 come si vede): vedi make_test_pdfs.py.
    INTERNO, SENZA_PUNTO = (186, 112), (186, 172)
    HTTPS, MAILTO, FILE, LAUNCH, JS, GOTOR = (186, 232), (186, 292), (186, 352), (186, 412), (186, 472), (186, 532)
    RUOTATA, NOME = (430, 112), (430, 172)

    w.open_file(str(PDFS / "link.pdf"))
    check("link della prima pagina arrivati", wait(lambda: 0 in v._links), str(list(v._links)))
    check("10 link letti sulla pagina 1", len(v._links.get(0, [])) == 10, str(len(v._links.get(0, []))))
    check("menu Visualizza: Indietro e Avanti", {"I&ndietro", "A&vanti"} <= {
        a.text() for m in w.menuBar().actions() if m.text() == "&Visualizza" for a in m.menu().actions()})
    check("Indietro/Avanti disabilitati all'inizio", not w.act_back.isEnabled() and not w.act_forward.isEnabled())

    # --- 1. link interno con punto di destinazione ---
    pos = at(0, *INTERNO)
    hover(pos)
    check("1. manina sopra il link interno", vp.cursor().shape() == HAND, str(vp.cursor().shape()))
    check("1. tooltip «Vai a pagina 3»", vp.toolTip() == "Vai a pagina 3", vp.toolTip())
    hover(at(0, 450, 300))
    check("1. fuori dal link: cursore normale, niente tooltip", vp.cursor().shape() != HAND and vp.toolTip() == "")
    start = v.scroll_position()
    click(pos)
    check("1. clic → pagina 3", v.current_page == 2, str(v.current_page))
    check("1. nella posizione di destinazione (y 400 ± 5 pt)", near(v.scroll_position(), 2, 400), str(v.scroll_position()))

    # --- 2. Indietro / Avanti ---
    check("2. Indietro abilitato", w.act_back.isEnabled() and not w.act_forward.isEnabled())
    key(Qt.Key.Key_Left, ALT)
    check("2. Alt+← torna alla posizione di partenza", near(v.scroll_position(), *start, tol=0.5)
          and v.current_page == 0, f"{start} → {v.scroll_position()}")
    key(Qt.Key.Key_Right, ALT)
    check("2. Alt+→ torna a pagina 3", near(v.scroll_position(), 2, 400) and v.current_page == 2, str(v.scroll_position()))
    QTest.mouseClick(vp, Qt.MouseButton.BackButton, NO, QPoint(400, 400)); app.processEvents()
    check("2. tasto Indietro del mouse", near(v.scroll_position(), *start, tol=0.5), str(v.scroll_position()))
    QTest.mouseClick(vp, Qt.MouseButton.ForwardButton, NO, QPoint(400, 400)); app.processEvents()
    check("2. tasto Avanti del mouse", near(v.scroll_position(), 2, 400), str(v.scroll_position()))
    key(Qt.Key.Key_Left, ALT)

    # --- link interni: senza punto, verso una pagina ruotata, con nome ---
    top(); click(at(0, *SENZA_PUNTO))
    check("link senza punto: in cima a pagina 3", near(v.scroll_position(), 2, 0), str(v.scroll_position()))
    top(); hover(at(0, *RUOTATA))
    check("tooltip «Vai a pagina 4»", vp.toolTip() == "Vai a pagina 4", vp.toolTip())
    click(at(0, *RUOTATA))
    check("destinazione su pagina ruotata: pagina 4, y 100 come si vede", near(v.scroll_position(), 3, 100),
          str(v.scroll_position()))
    top(); click(at(0, *NOME))
    check("destinazione con nome (LINK_NAMED) su pagina ruotata: pagina 4, y 200", near(v.scroll_position(), 3, 200),
          str(v.scroll_position()))
    check("ogni link seguito va nella cronologia", len(w._back) == 3 and not w._forward, str(w._back))

    # --- 3. link http con conferma ---
    top()
    before = v.scroll_position()
    hover(at(0, *HTTPS))
    check("3. manina e indirizzo nel tooltip", vp.cursor().shape() == HAND
          and vp.toolTip() == "https://example.com/percorso?x=1", vp.toolTip())
    answers.append(QMessageBox.StandardButton.Cancel)
    click(at(0, *HTTPS))
    check("3. conferma chiesta con l'indirizzo per intero", len(asked) == 1
          and "Aprire questo indirizzo nel browser?" in asked[0] and "https://example.com/percorso?x=1" in asked[0],
          str(asked))
    check("3. Annulla → nessuna apertura", opened == [] and not answers)
    answers.append(QMessageBox.StandardButton.Open)
    click(at(0, *HTTPS))
    check("3. Apri → openUrl una volta con l'URL giusto",
          [u.toString() for u in opened] == ["https://example.com/percorso?x=1"], str(opened))
    check("3. la vista non si sposta", v.scroll_position() == before)
    answers.append(QMessageBox.StandardButton.Open)
    click(at(0, *MAILTO))
    check("3. mailto: con conferma", len(asked) == 3 and opened[-1].toString() == "mailto:info@example.com",
          str([u.toString() for u in opened]))

    # --- 4. file:, launch, javascript:, altro PDF ---
    opened.clear(); asked.clear()
    for name, pt in (("file:", FILE), ("launch", LAUNCH), ("javascript:", JS), ("altro PDF", GOTOR)):
        w.statusBar().clearMessage()
        click(at(0, *pt))
        check(f"4. {name}: nessuna apertura, messaggio nella barra di stato",
              not opened and not asked and w.statusBar().currentMessage() == "Link non supportato per sicurezza",
              w.statusBar().currentMessage())
    for uri in ("http://", "https:percorso", "ftp://example.com", "HTTPS://example.com/ok", "mailto:"):
        w.statusBar().clearMessage()
        answers.append(QMessageBox.StandardButton.Cancel)
        w._open_uri(uri)
        blocked = w.statusBar().currentMessage() == "Link non supportato per sicurezza"
        check(f"4. «{uri}» {'aperto con conferma' if uri.startswith('HTTPS') else 'rifiutato'}",
              blocked != uri.startswith("HTTPS"))
        answers.clear()
    asked.clear()

    # --- 5. pagina ruotata: il link è dove si vede ---
    # Il riquadro rosso si cerca nell'immagine della pagina come si vede, non nei dati del link.
    doc = pymupdf.open(PDFS / "link.pdf")
    pix = doc[1].get_pixmap(alpha=False)
    red = [(x, y) for y in range(0, pix.height, 2) for x in range(0, pix.width, 2)
           if pix.pixel(x, y) == (255, 0, 0)]
    doc.close()
    cx = (min(p[0] for p in red) + max(p[0] for p in red)) / 2
    cy = (min(p[1] for p in red) + max(p[1] for p in red)) / 2
    check("5. riquadro rosso trovato nella pagina 2 ruotata", red and pix.width > pix.height, f"{cx}, {cy}")
    v.set_scroll_position(1, 0.0)
    check("5. link della pagina ruotata arrivati", wait(lambda: 1 in v._links))
    hover(at(1, cx, cy))
    check("5. manina sul riquadro visibile", vp.cursor().shape() == HAND and vp.toolTip() == "Vai a pagina 1",
          vp.toolTip())
    click(at(1, cx, cy))
    check("5. clic → pagina 1", near(v.scroll_position(), 0, 0), str(v.scroll_position()))

    # --- 6. trascinamento, posizionamento firma, firma selezionata ---
    top()
    before, history = v.scroll_position(), list(w._back)
    pos = at(0, *INTERNO)
    QTest.mousePress(vp, L, NO, pos)
    for i in range(1, 6):
        QTest.mouseMove(vp, pos + QPoint(12 * i, 4 * i))
    QTest.mouseRelease(vp, L, NO, pos + QPoint(60, 20)); app.processEvents()
    check("6. trascinamento da un link: nessuna navigazione", v.scroll_position() == before and w._back == history)
    QTest.mousePress(vp, L, NO, pos)
    QTest.mouseMove(vp, pos + QPoint(60, 0))
    QTest.mouseMove(vp, pos)
    QTest.mouseRelease(vp, L, NO, pos); app.processEvents()
    check("6. andata e ritorno sul link: nessuna navigazione", v.scroll_position() == before)

    img = QImage(600, 200, QImage.Format.Format_ARGB32)
    img.fill(QColor("#1a237e"))
    sig = w.signatures.add(img, Kind.SIGNATURE)
    w.place_signature(sig)
    hover(pos)
    check("6. in posizionamento: croce, non manina", vp.cursor().shape() == Qt.CursorShape.CrossCursor)
    click(pos)
    check("6. in posizionamento il clic sul link piazza la firma",
          len(w.placements.items) == 1 and v.scroll_position() == before and w._back == history)
    check("6. la firma piazzata è selezionata", w.placements.selected is not None)
    other = at(0, *SENZA_PUNTO)
    hover(other)
    check("6. con una firma selezionata niente manina", vp.cursor().shape() != HAND and vp.toolTip() == "")
    click(other)
    check("6. con una firma selezionata il clic la deseleziona e basta",
          w.placements.selected is None and v.scroll_position() == before)
    click(other)
    check("6. senza selezione il link funziona di nuovo", near(v.scroll_position(), 2, 0))
    w.placements.clear()

    # --- cronologia: massimo 50 voci, svuotata cambiando documento ---
    for i in range(MAX_HISTORY + 10):
        w._follow_link(i % 6, 0.0)
    check("cronologia: al massimo 50 voci", len(w._back) == MAX_HISTORY, str(len(w._back)))
    w.go_back()
    check("Avanti abilitato dopo Indietro", w.act_forward.isEnabled() and len(w._forward) == 1)

    # --- 7. lungo_350.pdf: nessun link, link chiesti solo per le pagine visibili ---
    requests = []
    real = w._renderer.request_links

    def spy(gen, page):
        requests.append((page, page in v._visible_range()))
        real(gen, page)

    w._renderer.request_links = spy
    w.open_file(str(PDFS / "lungo_350.pdf"))
    check("7. aprendo un altro documento la cronologia si svuota",
          not w._back and not w._forward and not w.act_back.isEnabled() and not w.act_forward.isEnabled())
    pump(0.3)
    for page in (10, 11, 200, 349):
        v.go_to_page(page); pump(0.3)
    check("7. pagine renderizzate come prima", wait(lambda: all(p in v._cache for p in v._visible_range())))
    check("7. link chiesti solo per le pagine visibili", requests and all(vis for _, vis in requests),
          str(requests))
    check("7. ogni pagina chiesta una volta sola", len({p for p, _ in requests}) == len(requests) < 15,
          str(sorted(p for p, _ in requests)))
    check("7. nessun link nel documento", wait(lambda: all(p in v._links for p in v._visible_range()))
          and not any(v._links.values()))
    hover(QPoint(vp.width() // 2, vp.height() // 2))
    check("7. nessuna manina", vp.cursor().shape() != HAND and vp.toolTip() == "")
    vbar = v.verticalScrollBar()
    value = vbar.value()
    key(Qt.Key.Key_PageDown)
    check("7. scroll come prima", vbar.value() > value or value == vbar.maximum())
    value = vbar.value()
    key(Qt.Key.Key_Left, ALT)
    check("7. Alt+← senza cronologia: nessun effetto", vbar.value() == value)
    w.close_document()
    w.close(); app.processEvents()

    print(f"\n{sum(results)}/{len(results)} ok", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
