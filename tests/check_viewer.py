"""Controllo end-to-end del viewer (#2) sui PDF di tests/make_test_pdfs.py.

    python tests/make_test_pdfs.py
    set QT_QPA_PLATFORM=offscreen
    python tests/check_viewer.py
"""
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out"
PDFS = S / "pdfs"
SHOTS = S / "shots"


def main():
    from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, Qt, QUrl
    from PySide6.QtGui import QDropEvent, QKeyEvent, QWheelEvent
    from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

    (S / "shots").mkdir(parents=True, exist_ok=True)
    (S / "data").mkdir(parents=True, exist_ok=True)
    import piedf.paths as paths
    paths.data_dir = lambda: S / "data"
    (S / "data" / "settings.ini").unlink(missing_ok=True)
    from piedf.main_window import MainWindow
    from piedf.viewer import FitMode

    app = QApplication(["t"])
    w = MainWindow(); w.resize(1000, 800); w.show()
    v = w.view
    results = []

    def check(name, ok, info=""):
        results.append(bool(ok))
        print(("OK  " if ok else "FAIL"), name, info, flush=True)

    def wait_rendered(timeout=15):
        t = time.perf_counter()
        while time.perf_counter() - t < timeout:
            app.processEvents()
            v.viewport().repaint()
            vis = list(v._visible_range())
            if vis and all(p in v._cache and v._cache[p][0] == v._render_scale(p) for p in vis):
                return time.perf_counter() - t
            time.sleep(0.01)
        return None

    def pump(sec):
        t = time.perf_counter()
        while time.perf_counter() - t < sec:
            app.processEvents(); time.sleep(0.01)

    check("finestra vuota", w.windowTitle() == "PieDF" and not w.act_close.isEnabled())

    ok = w.open_file(str(PDFS / "semplice.pdf"))
    dt = wait_rendered()
    check("semplice: apertura + render", ok and dt is not None, f"{dt and round(dt, 2)}s (include avvio processo)")
    check("semplice: adatta larghezza di default", v.fit_mode == FitMode.WIDTH and w._zoom_box.currentText() == "Adatta larghezza", w._zoom_box.currentText())
    v.grab().save(str(SHOTS / "semplice.png"))

    t0 = time.perf_counter(); ok = w.open_file(str(PDFS / "lungo_350.pdf")); topen = time.perf_counter() - t0
    dt = wait_rendered()
    check("lungo: apertura", ok and v.page_count == 350 and w._page_total.text() == " / 350 ", f"struttura {topen:.2f}s, primo render {dt and round(dt, 2)}s")
    v.go_to_page(199); dt = wait_rendered()
    check("lungo: vai a pagina 200", v.current_page == 199 and w._page_box.value() == 200 and dt is not None, f"render {dt and round(dt, 2)}s")
    w._page_box.setValue(300); wait_rendered()
    check("lungo: spinbox -> pagina 300", v.current_page == 299, str(v.current_page))
    for _ in range(120):
        v.verticalScrollBar().setValue(v.verticalScrollBar().value() + 300)
        app.processEvents(); v.viewport().repaint()
    dt = wait_rendered()
    check("lungo: scroll veloce, poi render delle pagine visibili", dt is not None and dt < 3, f"{dt and round(dt, 2)}s, cache={len(v._cache)} pagine, {v._cache_bytes // 2**20} MB")
    check("lungo: pagina corrente segue lo scroll", w._page_box.value() == v.current_page + 1, str(v.current_page + 1))
    v.verticalScrollBar().setValue(v.verticalScrollBar().maximum()); app.processEvents()
    check("lungo: fine documento = pagina 350", v.current_page == 349)
    v.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Home, Qt.KeyboardModifier.NoModifier))
    app.processEvents()
    check("lungo: tasto Home", v.current_page == 0)

    v.set_zoom(2.0); dt = wait_rendered()
    check("zoom 200%", abs(v.zoom - 2.0) < 1e-9 and w._zoom_box.currentText() == "200%" and dt is not None, w._zoom_box.currentText())
    v.grab().save(str(SHOTS / "zoom200.png"))
    w.act_zoom_in.trigger(); check("zoom avanti", abs(v.zoom - 2.5) < 1e-9, w._zoom_box.currentText())
    w.act_zoom_out.trigger(); w.act_zoom_out.trigger(); check("zoom indietro x2", abs(v.zoom - 1.6) < 1e-9, w._zoom_box.currentText())
    w._apply_zoom_text("75"); check("zoom digitato 75", abs(v.zoom - 0.75) < 1e-9 and w._zoom_box.currentText() == "75%")
    w._apply_zoom_text("abc"); check("zoom testo non valido ignorato", abs(v.zoom - 0.75) < 1e-9 and w._zoom_box.currentText() == "75%")
    w.act_fit_page.trigger(); wait_rendered()
    r = v._page_rect(v.current_page)
    check("adatta pagina", v.fit_mode == FitMode.PAGE and r.height() <= v.viewport().height(), f"pagina {r.height()}px, vista {v.viewport().height()}px")
    v.grab().save(str(SHOTS / "fit_page.png"))
    v.set_zoom(1.0)
    ev = QWheelEvent(QPointF(400, 300), QPointF(400, 300), QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton,
                     Qt.KeyboardModifier.ControlModifier, Qt.ScrollPhase.NoScrollPhase, False)
    v.wheelEvent(ev)
    check("ctrl+rotella zoom", abs(v.zoom - 1.1) < 1e-9, f"{v.zoom:.2f}")
    v.set_zoom(8.0); dt = wait_rendered()
    big = max(img.width() * img.height() for _, img in v._cache.values())
    check("zoom 800%: render limitato", dt is not None and big <= 25_000_000, f"max {big/1e6:.1f} MP")
    v.set_zoom(0.05); check("zoom minimo 10%", abs(v.zoom - 0.1) < 1e-9)
    w.act_fit_width.trigger()
    w.resize(700, 800); app.processEvents(); pump(0.2)
    check("ridimensionamento ricalcola adatta larghezza", abs(v._page_rect(0).width() - (v.viewport().width() - 24)) <= 1, f"{v._page_rect(0).width()} vs {v.viewport().width()}")
    w.resize(1000, 800); app.processEvents()

    w.open_file(str(PDFS / "pesante.pdf"))
    v.set_zoom(4.0)
    gaps, last = [], time.perf_counter(); t0 = last
    while time.perf_counter() - t0 < 4:
        app.processEvents(); time.sleep(0.005); now = time.perf_counter(); gaps.append(now - last); last = now
    check("GUI reattiva durante render pesante", max(gaps) < 0.15, f"pausa max {max(gaps)*1000:.0f} ms")
    wait_rendered(30)

    ok = w.open_file(str(PDFS / "scansionato.pdf")); dt = wait_rendered()
    check("scansionato", ok and v.page_count == 5 and dt is not None, f"{dt and round(dt, 2)}s")
    v.grab().save(str(SHOTS / "scansionato.png"))

    ok = w.open_file(str(PDFS / "orientamenti.pdf")); wait_rendered()
    sizes = [tuple(round(x) for x in s) for s in v._sizes]
    check("orientamenti: dimensioni con rotazione", sizes == [(595, 842), (842, 595), (842, 595)], str(sizes))
    v.set_zoom(0.5); wait_rendered(); v.grab().save(str(SHOTS / "orientamenti.png"))

    answers = iter([("sbagliata", True), ("pie", True)])
    prompts = []
    def fake_get_text(parent, title, label, echo):
        prompts.append(label); return next(answers)
    QInputDialog.getText = staticmethod(fake_get_text)
    ok = w.open_file(str(PDFS / "cifrato.pdf")); dt = wait_rendered()
    check("cifrato: password errata poi corretta", ok and dt is not None and len(prompts) == 2 and "errata" in prompts[1], str(prompts))
    v.grab().save(str(SHOTS / "cifrato.png"))
    QInputDialog.getText = staticmethod(lambda *a: ("", False))
    ok = w.open_file(str(PDFS / "cifrato.pdf"))
    check("cifrato: annulla", ok is False and w._path.endswith("cifrato.pdf"))

    errors = []
    QMessageBox.critical = staticmethod(lambda parent, title, text: errors.append(text))
    ok = w.open_file(str(PDFS / "danneggiato.pdf"))
    check("troncato a metà (irrecuperabile): messaggio", ok is False and len(errors) == 1, errors[0][-80:].replace("\n", " ") if errors else "")
    ok = w.open_file(str(PDFS / "senza_xref.pdf")); dt = wait_rendered()
    check("senza xref/trailer: riparato", ok and v.page_count == 350 and dt is not None and "riparato" in w.statusBar().currentMessage(),
          f"stato='{w.statusBar().currentMessage()}'")
    ok = w.open_file(str(PDFS / "xref_corrotta.pdf")); v.go_to_page(120); dt = wait_rendered()
    check("xref corrotta: legge pagina 121", ok and dt is not None, f"{dt and round(dt, 2)}s")
    v.grab().save(str(SHOTS / "xref_corrotta.png"))
    errors.clear()
    ok = w.open_file(str(PDFS / "non_pdf.pdf"))
    check("non PDF: messaggio d'errore", ok is False and errors and "non riesce" in errors[0], errors[0][:120].replace("\n", " ") if errors else "")
    ok = w.open_file(str(PDFS / "inesistente.pdf"))
    check("file inesistente: messaggio", ok is False and len(errors) == 2)

    mime = QMimeData(); mime.setUrls([QUrl.fromLocalFile(str(PDFS / "semplice.pdf"))])
    drop = QDropEvent(QPointF(100, 100), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    w.dropEvent(drop)
    check("drag & drop apre il file", w._path.endswith("semplice.pdf") and w.windowTitle() == "semplice.pdf – PieDF", w.windowTitle())

    recent = [Path(p).name for p in w._recent()]
    check("file recenti (senza duplicati, senza file falliti)",
          recent[:4] == ["semplice.pdf", "xref_corrotta.pdf", "senza_xref.pdf", "cifrato.pdf"]
          and not {"non_pdf.pdf", "danneggiato.pdf", "inesistente.pdf"} & set(recent) and len(recent) == len(set(recent)), str(recent))
    w._fill_recent_menu()
    labels = [a.text() for a in w._recent_menu.actions()]
    check("menu recenti", labels[0].endswith("semplice.pdf") and labels[-1] == "Svuota elenco", str(labels[:2]))

    w.act_close.trigger()
    check("chiudi documento", w._path is None and w.windowTitle() == "PieDF" and not w.act_zoom_in.isEnabled() and v.page_count == 0)
    v.grab().save(str(SHOTS / "vuota.png"))

    w.close()
    print(f"\n{sum(results)}/{len(results)} ok", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
