"""Controllo end-to-end della creazione firme (#3).

    set QT_QPA_PLATFORM=offscreen
    python tests/check_signatures.py
"""
import random
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out"


def main():
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QColor, QImage, QPainter, QPen
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QDialogButtonBox, QMessageBox

    import piedf.paths as paths
    data = OUT / "data_firme"
    shutil.rmtree(data, ignore_errors=True)
    data.mkdir(parents=True)
    paths.data_dir = lambda: data
    shots = OUT / "shots"
    shots.mkdir(parents=True, exist_ok=True)

    from piedf.signature_dialog import SignatureDialog, render_text, script_fonts
    from piedf.signature_manager import SignatureManager
    from piedf.signatures import Kind, SignatureStore, content_rect, finalize, remove_background

    app = QApplication(["t"])
    results = []

    def check(name, ok, info=""):
        results.append(bool(ok))
        print(("OK  " if ok else "FAIL"), name, info, flush=True)

    def alpha(img, x, y):
        return QColor.fromRgba(img.pixel(x, y)).alpha()

    def draw_zigzag(pad, x0=60, y0=80, n=12):
        QTest.mousePress(pad, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(x0, y0))
        for i in range(1, n):
            QTest.mouseMove(pad, QPoint(x0 + i * 25, y0 + (30 if i % 2 else -30)))
        QTest.mouseRelease(pad, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(x0 + n * 25, y0))

    store = SignatureStore()
    check("archivio in <dati>/firme", store.directory == data / "firme" and store.list() == [])

    # --- Disegna ---
    dialog = SignatureDialog(store, Kind.SIGNATURE)
    dialog.show()
    save = dialog.buttons.button(QDialogButtonBox.StandardButton.Save)
    check("Salva disabilitato con firma vuota", not save.isEnabled())
    pad = dialog.draw_tab.pad
    draw_zigzag(pad)
    QTest.mouseClick(pad, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(420, 60))  # puntino
    check("Salva abilitato dopo il disegno", save.isEnabled() and len(pad._strokes) == 2, f"tratti={len(pad._strokes)}")
    dialog.draw_tab.color_box.setCurrentText("Blu")
    dialog.draw_tab.width_box.setCurrentText("Spesso")
    dialog.grab().save(str(shots / "firma_disegna.png"))
    img = dialog.draw_tab.to_image()
    pad_w = pad.width() * 3
    check("disegno: ritagliato e trasparente", img is not None and img.width() < pad_w and alpha(img, 0, 0) == 0,
          f"{img.width()}x{img.height()} (area {pad_w}x{pad.height() * 3})")
    blue = [QColor.fromRgba(img.pixel(x, y)) for x in range(img.width()) for y in range(0, img.height(), 3)]
    blue = [c for c in blue if c.alpha() == 255]
    check("disegno: colore blu applicato", blue and all(c.blue() > c.red() for c in blue[:50]))
    pad.undo()
    check("annulla tratto", len(pad._strokes) == 1)
    pad.clear()
    check("cancella", pad.is_empty() and not save.isEnabled())
    draw_zigzag(pad)
    dialog.accept()
    check("salva firma disegnata", dialog.saved is not None and dialog.saved.kind is Kind.SIGNATURE and dialog.saved.path.exists(),
          dialog.saved.path.name if dialog.saved else "")

    # --- Scrivi ---
    fonts = script_fonts()
    check("font calligrafici di Windows disponibili", len(fonts) >= 3, str(fonts))
    dialog = SignatureDialog(store, Kind.INITIALS)
    dialog.show()
    dialog.tabs.setCurrentWidget(dialog.type_tab)
    save = dialog.buttons.button(QDialogButtonBox.StandardButton.Save)
    check("scrivi: Salva disabilitato senza nome", not save.isEnabled())
    QTest.keyClicks(dialog.type_tab.name, "Mario Rossi")
    check("scrivi: anteprima nei font", dialog.type_tab.fonts.item(0).text() == "Mario Rossi" and save.isEnabled())
    for i, family in enumerate(fonts):
        im = render_text("Mario Rossi", family, QColor("#111111"))
        check(f"scrivi: {family}", im is not None and im.width() > im.height() and alpha(im, 0, 0) == 0,
              f"{im.width()}x{im.height()}")
    dialog.type_tab.fonts.setCurrentRow(1)
    dialog.grab().save(str(shots / "firma_scrivi.png"))
    check("scrivi: tipo iniziali preselezionato", dialog.kind is Kind.INITIALS)
    dialog.type_tab.name.setText("M.R.")
    dialog.accept()
    check("salva iniziali scritte", dialog.saved is not None and dialog.saved.kind is Kind.INITIALS)
    check("render_text vuoto", render_text("   ", fonts[0], QColor("black")) is None)

    # --- Immagine ---
    random.seed(3)
    photo = QImage(1600, 700, QImage.Format.Format_RGB32)
    photo.fill(QColor(236, 233, 226))  # carta un po' grigia
    p = QPainter(photo)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(25, 30, 70), 9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    pts = [QPoint(300 + i * 70, 350 + random.randint(-90, 90)) for i in range(14)]
    for a, b in zip(pts, pts[1:]):
        p.drawLine(a, b)
    for _ in range(3000):  # rumore della carta
        g = random.randint(200, 250)
        p.setPen(QColor(g, g, g))
        p.drawPoint(random.randrange(1600), random.randrange(700))
    p.end()
    photo_path = OUT / "foto_firma.jpg"
    photo.save(str(photo_path), "JPG", 85)

    dialog = SignatureDialog(store)
    dialog.show()
    dialog.tabs.setCurrentWidget(dialog.image_tab)
    t = time.perf_counter()
    ok = dialog.image_tab.load(str(photo_path))
    im = dialog.image_tab.to_image()
    dt = time.perf_counter() - t
    dialog.grab().save(str(shots / "firma_immagine.png"))
    rect = content_rect(im) if im else None
    check("immagine: sfondo trasparente e ritaglio", ok and im is not None and alpha(im, 0, 0) == 0 and im.width() < 1100 and im.height() < 300,
          f"{im.width()}x{im.height()} in {dt:.2f}s")
    opaque = sum(1 for x in range(0, im.width(), 4) for y in range(0, im.height(), 4) if alpha(im, x, y) > 200)
    check("immagine: inchiostro opaco presente", opaque > 50, f"{opaque} campioni")
    dialog.image_tab.transparent.setChecked(False)
    im2 = dialog.image_tab.to_image()
    check("immagine: senza trasparenza resta opaca (ridotta a 1200 px)", alpha(im2, 0, 0) == 255 and im2.width() == 1200, f"{im2.width()}x{im2.height()}")
    dialog.image_tab.transparent.setChecked(True)
    dialog.accept()
    check("salva firma da immagine", dialog.saved is not None and dialog.saved.kind is Kind.SIGNATURE)

    # PNG già trasparente con sfondo nero trasparente: deve restare trasparente
    png = QImage(400, 200, QImage.Format.Format_ARGB32)
    png.fill(Qt.GlobalColor.transparent)
    p = QPainter(png); p.setPen(QPen(QColor("black"), 6)); p.drawLine(50, 100, 350, 120); p.end()
    out = finalize(remove_background(png))
    check("PNG trasparente resta trasparente", out is not None and alpha(out, 0, 0) == 0 and out.height() < 60, f"{out.width()}x{out.height()}")

    big = QImage(6000, 3000, QImage.Format.Format_RGB32); big.fill(QColor("white"))
    p = QPainter(big); p.setPen(QPen(QColor("black"), 30)); p.drawLine(500, 1500, 5500, 1600); p.end()
    big_path = OUT / "foto_grande.png"; big.save(str(big_path))
    dialog = SignatureDialog(store)
    t = time.perf_counter(); dialog.image_tab.load(str(big_path)); im = dialog.image_tab.to_image(); dt = time.perf_counter() - t
    check("foto grande: ridotta e veloce", im.width() <= 1200 and dt < 3, f"{im.width()}x{im.height()} in {dt:.2f}s")
    blank = QImage(300, 100, QImage.Format.Format_RGB32); blank.fill(QColor("white"))
    check("immagine tutta bianca -> niente firma", finalize(remove_background(blank)) is None)

    warnings = []
    QMessageBox.warning = staticmethod(lambda *a: warnings.append(a[2]))
    bad = OUT / "non_immagine.png"; bad.write_bytes(b"non sono un png")
    check("file immagine non valido: avviso", dialog.image_tab.load(str(bad)) is False and warnings, warnings[-1] if warnings else "")

    # --- Persistenza ("riavvio") ---
    reopened = SignatureStore()
    firme, iniziali = reopened.list(Kind.SIGNATURE), reopened.list(Kind.INITIALS)
    check("dopo il riavvio le firme ci sono", len(firme) == 2 and len(iniziali) == 1, f"firme={len(firme)} iniziali={len(iniziali)}")
    check("PNG salvati con trasparenza", all(s.image().hasAlphaChannel() for s in reopened.list()))

    # --- Gestione ---
    manager = SignatureManager(reopened)
    manager.show()
    manager.grab().save(str(shots / "firme_gestione.png"))
    lf, li = manager.lists[Kind.SIGNATURE], manager.lists[Kind.INITIALS]
    check("gestione: miniature", lf.count() == 2 and li.count() == 1)
    check("gestione: Elimina disabilitato senza selezione", not manager.delete_button.isEnabled())
    lf.item(0).setSelected(True)
    li.item(0).setSelected(True)
    check("gestione: selezione unica tra i due elenchi", not lf.selectedItems() and manager.selected().kind is Kind.INITIALS)
    QMessageBox.question = staticmethod(lambda *a: QMessageBox.StandardButton.No)
    manager.delete_selected()
    check("gestione: elimina annullato", len(reopened.list()) == 3)
    li.item(0).setSelected(True)
    QMessageBox.question = staticmethod(lambda *a: QMessageBox.StandardButton.Yes)
    manager.delete_selected()
    check("gestione: elimina confermato", len(reopened.list(Kind.INITIALS)) == 0 and li.count() == 1 and not (li.item(0).flags() & Qt.ItemFlag.ItemIsSelectable),
          li.item(0).text())

    # --- Menu ---
    from piedf.main_window import MainWindow
    w = MainWindow()
    menus = {a.text(): [x.text() for x in a.menu().actions()] for a in w.menuBar().actions()}
    check("menu Firma", menus.get("F&irma") == ["Crea &firma…", "Crea &iniziali…", "", "&Gestisci firme…"], str(menus.get("F&irma")))
    w.close()

    print(f"\n{sum(results)}/{len(results)} ok", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
