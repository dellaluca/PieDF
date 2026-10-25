"""Controllo end-to-end di "Organizza pagine" (#26): ruotare, eliminare, spostare, inserire,
unire, estrarre, e il documento che risulta modificato fino al salvataggio.

    python tests/make_test_pdfs.py
    set QT_QPA_PLATFORM=offscreen
    python tests/check_pages.py
"""
import hashlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

S = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out"
PDFS = S / "pdfs"
SHOTS = S / "shots"


def main():
    import pymupdf
    from PySide6.QtCore import QPoint, QRectF, Qt, QTimer
    from PySide6.QtGui import QColor, QImage, QPainter, QPen
    from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QInputDialog, QMessageBox

    import piedf.paths as paths
    data = S / "data_pagine"
    work = S / "pagine"
    tmp = S / "tmp_pagine"  # i temporanei finiscono qui, per vedere se restano
    for d in (data, work, tmp):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    paths.data_dir = lambda: data
    tempfile.tempdir = str(tmp)
    SHOTS.mkdir(parents=True, exist_ok=True)

    from piedf.main_window import SAVE_FIRST, MainWindow
    from piedf.page_ops import MAIN
    from piedf.page_organizer import PageOrganizer
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
    save_answers, save_asked = [], []

    def fake_save(parent, title, proposed, *a, **k):
        save_asked.append(proposed)
        return (save_answers.pop(0) if save_answers else ""), ""
    QFileDialog.getSaveFileName = staticmethod(fake_save)
    open_answers = []
    QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: ((open_answers.pop(0) if open_answers else ""), ""))
    QFileDialog.getOpenFileNames = staticmethod(lambda *a, **k: ((open_answers.pop(0) if open_answers else []), ""))
    box_answers, boxes = [], []

    def fake_exec(box):
        boxes.append(box.text())
        return box_answers.pop(0) if box_answers else QMessageBox.StandardButton.Ok
    QMessageBox.exec = fake_exec
    errors = []
    QMessageBox.critical = staticmethod(lambda parent, title, text, *a: errors.append(text))
    QMessageBox.warning = staticmethod(lambda parent, title, text, *a: errors.append(text))
    passwords = []
    QInputDialog.getText = staticmethod(lambda *a, **k: (passwords.append(a[2]) or "pie", True))
    Button = QMessageBox.StandardButton

    # La finestra "Organizza pagine" senza exec(): `actions(dialog)` fa le modifiche e chiude.
    organizers = []
    actions = [lambda dlg: dlg.reject()]

    def fake_organizer_exec(dlg):
        organizers.append(dlg)
        dlg.show()
        pump(0.05)
        actions[0](dlg)
        return dlg.result()
    PageOrganizer.exec = fake_organizer_exec

    w = MainWindow(); w.resize(1000, 800); w.show()
    pump(0.2)

    def wait_job(limit=60.0, on_pump=None):
        t = time.perf_counter()
        while w._page_job is not None and time.perf_counter() - t < limit:
            app.processEvents(); time.sleep(0.005)
            if on_pump:
                on_pump()
        pump(0.1)
        return w._page_job is None

    def organize(fn):
        actions[0] = fn
        w.organize_pages()
        return wait_job()

    def numbers(path, password=None):
        doc = pymupdf.open(path)
        if doc.needs_pass:
            doc.authenticate(password or "")
        out = [doc[i].get_text().split("\n")[0].strip() for i in range(doc.page_count)]
        doc.close()
        return out

    def sha(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def tmp_files():
        return sorted(p.name for p in tmp.iterdir())

    numerato = str(PDFS / "numerato_10.pdf")

    # --- 1 e 2. Spostare, eliminare, ruotare; prima di Applica non cambia nulla ---
    w.open_file(numerato)
    before = sha(numerato)
    seen = {}

    def edit_numerato(dlg):
        seen["open_ok"] = dlg.isVisible() and len(dlg.refs) == 10
        t = time.perf_counter()
        while len(dlg.model.thumbs) < 4 and time.perf_counter() - t < 15:
            pump(0.05)
        seen["thumbs"] = len(dlg.model.thumbs)
        img = dlg.grid.viewport().grab().toImage()
        img.save(str(SHOTS / "pagine_griglia.png"))
        grid = dlg.grid
        r3 = grid.visualRect(dlg.model.index(3))
        seen["drop"] = (grid.drop_position(QPoint(r3.left() + 5, r3.center().y())),
                        grid.drop_position(QPoint(r3.right() - 5, r3.center().y())),
                        grid.drop_position(QPoint(5, grid.viewport().height() - 2)))
        dlg.select_rows([9])
        grid.moved.emit(0)  # come il rilascio del trascinamento prima della prima pagina
        seen["moved"] = [r.page for r in dlg.refs][:3]
        dlg.select_rows([5])  # la "5"
        dlg.delete_selected()
        seen["deleted"] = len(dlg.refs)
        dlg.act_undo.trigger()
        seen["undo"] = len(dlg.refs)
        dlg.act_redo.trigger()
        dlg.select_rows([2])  # la "2"
        dlg.act_rotate_right.trigger()
        seen["rotate"] = dlg.refs[2].rotate
        pump(0.1)
        dlg.grid.viewport().grab().toImage().save(str(SHOTS / "pagine_modifiche.png"))
        seen["unchanged"] = (w._path == numerato and not w.modified and "*" not in w.windowTitle()
                             and sha(numerato) == before and w.view.page_count == 10)
        dlg.accept()

    check("1. applicato", organize(edit_numerato), str(errors))
    check("10. finestra aperta", seen.get("open_ok"))
    check("miniature dal processo dedicato", seen.get("thumbs", 0) >= 4, str(seen.get("thumbs")))
    check("indicatore: prima/dopo la cella, in fondo", seen.get("drop") == (3, 4, 10), str(seen.get("drop")))
    check("1. trascinamento: la 10 in testa", seen.get("moved") == [9, 0, 1], str(seen.get("moved")))
    check("1. eliminata, annulla e ripeti", seen.get("deleted") == 9 and seen.get("undo") == 10)
    check("1. ruotata a destra", seen.get("rotate") == 90)
    check("2. prima di Applica il documento non cambia", seen.get("unchanged"))
    check("1. documento modificato con *", w.modified and w.windowTitle().startswith("* numerato_10.pdf"), w.windowTitle())
    check("1. nella vista 9 pagine", w.view.page_count == 9 and w._path != numerato)
    check("1. originale intatto", sha(numerato) == before)
    check("1. un temporaneo aperto", len(tmp_files()) == 1, str(tmp_files()))
    dest = str(work / "numerato_modificato.pdf")
    save_answers.append(dest)
    check("1. salvato", w.save(), str(errors))
    check("1. nome proposto <nome>_modificato.pdf", save_asked[-1:] == [str(PDFS / "numerato_10_modificato.pdf")],
          str(save_asked[-1:]))
    got = numbers(dest)
    check("1. 9 pagine nell'ordine atteso", got == ["10", "1", "2", "3", "4", "6", "7", "8", "9"], str(got))
    doc = pymupdf.open(dest)
    check("1. vecchia pagina 2 con rotation 90", doc[2].rotation == 90 and all(doc[i].rotation == 0 for i in (0, 1, 3)))
    doc[2].get_pixmap(matrix=pymupdf.Matrix(0.3, 0.3)).save(str(SHOTS / "pagine_ruotata.png"))
    doc.close()
    check("1. dopo il salvataggio non modificato", not w.modified and w._path == dest and "*" not in w.windowTitle())
    pump(0.3)
    w._remove_temps()
    check("1. temporaneo cancellato", tmp_files() == [], str(tmp_files()))

    # --- 2. Annulla della finestra ---
    w.open_file(numerato)

    def cancel(dlg):
        dlg.select_rows([0, 1])
        dlg.delete_selected()
        dlg.reject()
    n_org = len(organizers)
    organize(cancel)
    check("2. Annulla: nessun cambiamento", len(organizers) == n_org + 1 and w._path == numerato
          and not w.modified and "*" not in w.windowTitle() and w.view.page_count == 10)
    check("2. Annulla: nessun temporaneo", tmp_files() == [], str(tmp_files()))

    # Applica senza modifiche: niente da fare
    organize(lambda dlg: dlg.accept())
    check("Applica senza modifiche: niente temporaneo", not w.modified and w._path == numerato and tmp_files() == [])

    # --- 3. Inserisci da file dopo la pagina 3 ---
    def insert(dlg):
        dlg.select_rows([2])
        open_answers.append(str(PDFS / "semplice.pdf"))
        dlg.choose_insert_file()
        seen["inserted"] = (dlg.selected_rows(), dlg.refs[3].source != MAIN)
        dlg.accept()
    organize(insert)
    got = numbers(w._path)
    check("3. 11 pagine, semplice in posizione 4", len(got) == 11 and got[3].startswith("PieDF - documento semplice")
          and got[2] == "3" and got[4] == "4", str(got))
    check("3. pagine inserite selezionate ed evidenziate", seen.get("inserted") == ([3], True), str(seen.get("inserted")))
    check("3. vista aggiornata", w.view.page_count == 11)

    # --- 4. Unisci PDF (anche cifrato) ---
    box_answers.append(Button.Discard)  # le pagine inserite qui sopra non si salvano
    w.open_file(numerato)
    check("riaprendo un altro file si chiede per le pagine", boxes[-1:] and "non sono state salvate" in boxes[-1])
    pump(0.2)
    w._remove_temps()
    check("scartate: temporaneo cancellato", tmp_files() == [], str(tmp_files()))
    files = [str(PDFS / n) for n in ("semplice.pdf", "orientamenti.pdf", "cifrato.pdf")]
    open_answers.append(files)
    n_pw = len(passwords)
    actions[0] = lambda dlg: (seen.__setitem__("merge", len(dlg.refs)), dlg.accept())
    w.merge_files()
    check("4. password chiesta per cifrato.pdf", len(passwords) == n_pw + 1)
    check("4. Organizza pagine con le pagine in fondo", seen.get("merge") == 15, str(seen.get("merge")))
    check("4. applicato", wait_job() and w.view.page_count == 15, str(errors))
    doc = pymupdf.open(w._path)
    expect = []
    for f in files:
        src = pymupdf.open(f)
        if src.needs_pass:
            src.authenticate("pie")
        expect += [(tuple(p.rect), p.rotation) for p in src]
        src.close()
    tail = [(tuple(doc[i].rect), doc[i].rotation) for i in range(10, 15)]
    check("4. 1+3+1 in fondo, con dimensioni e rotazioni originali", tail == expect, str(tail))
    check("4. testi giusti", numbers(w._path)[10:12] == ["PieDF - documento semplice", "Verticale"], str(numbers(w._path)[10:]))
    doc.close()

    # --- 5. Estrai pagine 2, 4, 6 ---
    box_answers.append(Button.Discard)
    w.open_file(numerato)
    extracted = str(work / "estratto.pdf")

    def extract(dlg):
        dlg.select_rows([1, 3, 5])
        save_answers.append(extracted)
        dlg.act_extract.trigger()
        seen["proposed"] = save_asked[-1]
        t = time.perf_counter()
        while dlg._job is not None and time.perf_counter() - t < 30:
            pump(0.02)
        seen["still_open"] = dlg.isVisible()
        dlg.reject()
    organize(extract)
    check("5. nuovo file di 3 pagine", Path(extracted).exists() and numbers(extracted) == ["2", "4", "6"],
          str(numbers(extracted) if Path(extracted).exists() else None))
    check("5. nome proposto <nome>_estratto.pdf", seen.get("proposed") == str(PDFS / "numerato_10_estratto.pdf"),
          str(seen.get("proposed")))
    check("5. la finestra resta aperta durante l'estrazione", seen.get("still_open"))
    check("5. documento aperto: 10 pagine, non modificato", w.view.page_count == 10 and not w.modified and w._path == numerato)

    # --- 6. Firma non salvata: Salva/Scarta/Annulla ---
    img = QImage(300, 100, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img); p.setPen(QPen(QColor("#1a237e"), 8)); p.drawLine(5, 90, 295, 10); p.end()
    sig = w.signatures.add(img, Kind.SIGNATURE)
    w.placements.add(Placement(0, QRectF(100, 100, 150, 50), sig.path, Kind.SIGNATURE))
    n_org, n_box = len(organizers), len(boxes)
    box_answers.append(Button.Cancel)
    w.act_organize.trigger()
    check("6. chiede Salva/Scarta/Annulla", boxes[n_box:] == [SAVE_FIRST], str(boxes[n_box:]))
    check("6. Annulla: la finestra non si apre", len(organizers) == n_org and w.placements.modified)
    box_answers.append(Button.Cancel)
    w.act_rotate_right.trigger()
    check("6. anche Ruota pagina corrente", boxes[-1] == SAVE_FIRST and w._page_job is None and w.placements.modified)
    box_answers.append(Button.Discard)
    organize(lambda dlg: dlg.reject())
    check("6. Scarta: firma tolta e finestra aperta", len(organizers) == n_org + 1 and not w.placements.items
          and not w.modified)

    # --- 7. Annotazione e campo seguono la pagina ---
    struttura = str(PDFS / "struttura_5.pdf")
    w.open_file(struttura)

    def to_end(dlg):
        dlg.select_rows([2])
        dlg.move_selected(5)
        dlg.accept()
    organize(to_end)
    check("7. campo letto sull'ultima pagina", [(f.name, f.page) for f in w.forms.fields] == [("CampoPagina3", 4)],
          str([(f.name, f.page) for f in w.forms.fields]))
    dest = str(work / "struttura_spostata.pdf")
    save_answers.append(dest)
    w.save()
    doc = pymupdf.open(dest)
    last = doc[4]
    check("7. pagina 3 in fondo", numbers(dest)[4].startswith("3"), str(numbers(dest)))
    check("7. annotazione sull'ultima pagina", [a.type[1] for a in last.annots()] == ["Text"]
          and all(not list(doc[i].annots()) for i in range(4)))
    check("7. campo sull'ultima pagina", [x.field_name for x in last.widgets()] == ["CampoPagina3"])
    doc.close()

    # --- 8. Indice: elimino la pagina di una voce ---
    w.open_file(struttura)
    organize(lambda dlg: (dlg.select_rows([2]), dlg.delete_selected(), dlg.accept()))
    doc = pymupdf.open(w._path)
    toc = doc.get_toc()
    check("8. indice di 2 voci sulle pagine giuste", toc == [[1, "Inizio", 1], [1, "Fine", 4]], str(toc))
    check("8. le pagine sono quelle", [doc[p - 1].get_text().split()[0] for _, _, p in toc] == ["1", "5"])
    doc.close()

    # --- 9. Cifrato: dopo il salvataggio chiede ancora la password ---
    box_answers.append(Button.Discard)
    w.open_file(str(PDFS / "cifrato.pdf"))  # password "pie" dal dialog finto
    w.rotate_current(90)
    check("9. Ruota pagina corrente", wait_job() and w.modified, str(errors))
    dest = str(work / "cifrato_modificato.pdf")
    save_answers.append(dest)
    w.save()
    doc = pymupdf.open(dest)
    check("9. chiede ancora la password", doc.needs_pass and not doc.authenticate("") and doc.authenticate("pie"))
    check("9. ruotata", doc[0].rotation == 90)
    doc.close()
    doc = pymupdf.open(dest)
    check("9. owner password originale conservata", doc.authenticate("owner") & 4)
    doc.close()

    # --- 10. lungo_350.pdf: apertura veloce, GUI reattiva applicando ---
    w.open_file(str(PDFS / "lungo_350.pdf"))
    pump(0.3)
    timing = {}

    def delete_100(dlg):
        timing["first_paint"] = time.perf_counter() - timing["t0"]
        dlg.select_rows(list(range(100)))
        dlg.delete_selected()
        dlg.accept()

    actions[0] = delete_100
    ticks = []
    timer = QTimer(); timer.setInterval(10); timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
    timing["t0"] = time.perf_counter()
    w.organize_pages()
    timer.start()
    t1 = time.perf_counter()
    done = wait_job(limit=120)
    timer.stop()
    secs = time.perf_counter() - t1
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    check("10. finestra aperta in meno di 1 s", timing.get("first_paint", 9) < 1.0, f"{timing.get('first_paint', 0):.2f} s")
    check("10. 250 pagine", done and w.view.page_count == 250, f"{secs:.1f} s")
    check("10. pausa massima della GUI < 150 ms", gaps and max(gaps) < 0.150, f"{max(gaps or [0]) * 1000:.0f} ms")
    check("10. resta la pagina 101", numbers(w._path)[0] == "Pagina 101 di 350")

    # --- 11. Non si possono eliminare tutte le pagine ---
    box_answers.append(Button.Discard)
    w.open_file(numerato)

    def all_pages(dlg):
        dlg.grid.selectAll()
        pump(0.05)
        seen["all"] = (len(dlg.selected_rows()), dlg.act_delete.isEnabled())
        dlg.delete_selected()
        seen["after"] = len(dlg.refs)
        dlg.select_rows([0])
        seen["one"] = dlg.act_delete.isEnabled()
        dlg.reject()
    organize(all_pages)
    check("11. tutte selezionate: Elimina disattivato", seen.get("all") == (10, False) and seen.get("after") == 10,
          str(seen.get("all")))
    check("11. una sola: Elimina attivo", seen.get("one"))

    # Pagina vuota della dimensione della pagina selezionata
    w.open_file(str(PDFS / "orientamenti.pdf"))
    organize(lambda dlg: (dlg.select_rows([1]), dlg.act_insert_blank.trigger(), dlg.accept()))
    doc = pymupdf.open(w._path)
    check("pagina vuota dopo la selezionata, stessa dimensione", doc.page_count == 4 and tuple(doc[2].rect) == (0, 0, 842, 595)
          and not doc[2].get_text().strip(), str([tuple(p.rect) for p in doc]))
    doc.close()

    # Menu e scorciatoie
    menus = {a.text(): a.menu() for a in w.menuBar().actions()}
    pages_menu = menus.get("&Pagine")
    check("menu Pagine", pages_menu is not None and [a.text() for a in pages_menu.actions() if a.text()] == [
        "&Organizza pagine…", "&Unisci PDF…", "Ruota pagina corrente a &sinistra", "Ruota pagina corrente a &destra"])
    check("Ctrl+Shift+O", w.act_organize.shortcut().toString() == "Ctrl+Shift+O")
    dlg = PageOrganizer(numerato, None, [(595, 842)] * 3, numerato, w)
    check("Ctrl+[ / Ctrl+]", dlg.act_rotate_left.shortcut().toString() == "Ctrl+["
          and dlg.act_rotate_right.shortcut().toString() == "Ctrl+]")
    dlg.reject()
    process = dlg._thumb_process
    deadline = time.monotonic() + 5
    while process.is_alive() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.05)
    # exitcode 0: uscito da solo col messaggio di uscita, non con terminate() (-15)
    check("10. processo delle miniature chiuso", not process.is_alive() and process.exitcode == 0,
          f"vivo={process.is_alive()} exitcode={process.exitcode}")

    # Chiusura mentre le miniature sono in corso e nessuno le legge più
    dlg = PageOrganizer(str(PDFS / "lungo_350.pdf"), None, [(595, 842)] * 350, str(PDFS / "lungo_350.pdf"), w)
    dlg._poll_timer.stop()  # le miniature restano nella pipe
    dlg.resize(1200, 900); dlg.show()
    pump(2.0)
    dlg.reject()
    process = dlg._thumb_process
    check("10. processo chiuso anche con miniature non lette", not process.is_alive() and process.exitcode == 0,
          f"vivo={process.is_alive()} exitcode={process.exitcode}")

    positions = json.loads((data / "posizioni.json").read_text("utf-8")) if (data / "posizioni.json").exists() else {}
    check("nessuna posizione per i temporanei", not any("piedf-pagine" in k for k in positions), str(list(positions)))
    check("nessun errore", not errors, str(errors))

    # Uscita con pagine non salvate (Scarta): niente temporanei rimasti
    box_answers.append(Button.Discard)
    w.close(); app.processEvents()
    check("uscita: temporanei cancellati", tmp_files() == [], str(tmp_files()))

    print(f"\n{sum(results)}/{len(results)} ok", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
