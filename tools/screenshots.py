"""Screenshot del README: `python tools/screenshots.py` → docs/img/*.png.

Crea un contratto di esempio, apre PieDF con una cartella dati temporanea (le firme vere
dell'utente non si toccano) e fotografa la finestra in alcune situazioni tipiche.
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "img"
sys.path.insert(0, str(ROOT))

PARAGRAPHS = [
    ("1. Oggetto", "Il Collaboratore si impegna a progettare e realizzare il nuovo sito del Committente, "
     "comprese le pagine dei prodotti, il modulo dei contatti e la versione per smartphone, secondo le "
     "indicazioni concordate nella riunione iniziale e riportate nell'allegato A."),
    ("2. Durata", "Il presente contratto decorre dalla data di sottoscrizione e termina alla consegna del "
     "sito, prevista entro novanta giorni. Le parti possono prorogare il termine con un accordo scritto."),
    ("3. Compenso", "Per l'attività descritta all'articolo 1 il Committente riconosce al Collaboratore un "
     "compenso di 4.800 euro, pagato in tre rate: alla firma, alla consegna della prima versione e al "
     "collaudo finale. Il compenso comprende tutte le spese sostenute dal Collaboratore."),
    ("4. Riservatezza", "Il Collaboratore si impegna a non divulgare informazioni, documenti e dati del "
     "Committente di cui venga a conoscenza durante l'incarico, anche dopo la conclusione del contratto."),
    ("5. Proprietà del lavoro", "Al pagamento del compenso il Committente acquisisce tutti i diritti sul "
     "sito realizzato, sui testi e sulle immagini prodotti per l'incarico."),
    ("6. Recesso", "Ciascuna parte può recedere dal contratto con un preavviso scritto di quindici giorni. "
     "In caso di recesso il Committente paga il compenso per l'attività già svolta."),
    ("7. Controversie", "Per ogni controversia relativa al presente contratto è competente il foro di Milano."),
]


def make_contract(path: Path) -> None:
    import pymupdf

    doc = pymupdf.open()
    css = "* {font-family: sans-serif; font-size: 11pt; line-height: 1.45; color: #222;} h1 {font-size: 17pt;} " \
          "h2 {font-size: 12pt; margin-top: 14pt;} p {text-align: justify;}"
    pages = [PARAGRAPHS[:4], PARAGRAPHS[4:]]
    toc = []
    for n, chunk in enumerate(pages):
        page = doc.new_page(width=595, height=842)
        html = ""
        if n == 0:
            html += ("<h1>Contratto di collaborazione</h1><p>Tra <b>Studio Bianchi S.r.l.</b>, con sede a Milano, "
                     "via Roma 12 (il «Committente»), e <b>Mario Rossi</b>, nato a Torino il 4 maggio 1988 "
                     "(il «Collaboratore»), si conviene quanto segue.</p>")
        for title, text in chunk:
            html += f"<h2>{title}</h2><p>{text}</p>"
            toc.append([1, title, n + 1])
        if n == len(pages) - 1:
            html += "<p style='margin-top: 30pt'>Milano, 12 ottobre 2026</p>"
        page.insert_htmlbox(pymupdf.Rect(70, 70, 525, 700), html, css=css)
        page.insert_text((70, 805), f"Pagina {n + 1} di {len(pages)}", fontsize=9, color=(0.4, 0.4, 0.4))
    last = doc[-1]
    for x, label in ((70, "Il Committente"), (330, "Il Collaboratore")):
        last.draw_line((x, 640), (x + 195, 640), color=(0.3, 0.3, 0.3), width=0.6)
        last.insert_text((x, 655), label, fontsize=10, color=(0.3, 0.3, 0.3))
    doc.set_toc(toc)
    doc.save(path)


def signature_image():
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter

    img = QImage(900, 260, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QColor("#1d2f8f"))
    font = QFont("Segoe Script")
    font.setPixelSize(120)
    p.setFont(font)
    p.drawText(img.rect(), Qt.AlignmentFlag.AlignCenter, "Mario Rossi")
    p.end()
    return img


def main() -> int:
    from PySide6.QtCore import QRectF
    from PySide6.QtWidgets import QApplication

    import piedf.paths as paths

    work = Path(tempfile.mkdtemp(prefix="piedf-screenshot-"))
    data = work / "data"
    data.mkdir()
    paths.data_dir = lambda: data
    pdf = work / "Contratto di collaborazione.pdf"
    make_contract(pdf)
    OUT.mkdir(parents=True, exist_ok=True)

    from piedf.main_window import MainWindow
    from piedf.placements import Placement
    from piedf.signatures import Kind
    
    app = QApplication.instance() or QApplication(sys.argv)
    w = MainWindow()
    w.resize(1280, 800)
    w.move(-4000, -4000)  # fuori dallo schermo: la finestra si fotografa con grab()
    w.show()

    def pump(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)

    def shot(name: str) -> None:
        # La vista chiede le pagine quando si disegna: un primo grab() le fa chiedere al
        # processo di render, il secondo le trova pronte.
        w.grab()
        pump(3)
        w.grab().save(str(OUT / name))
        print("scritto", OUT / name)

    w.open_file(str(pdf))
    w._set_sidebar(True)
    pump(1)
    w.view.set_zoom(1.0)
    shot("lettura.png")

    sig = w.signatures.add(signature_image(), Kind.SIGNATURE)
    w.placements.add(Placement(1, QRectF(335, 585, 180, 52), sig.path, Kind.SIGNATURE))
    w.view.set_scroll_position(1, 250)
    shot("firma.png")

    w.view.set_scroll_position(0, 0)
    w.search_bar.open_bar()
    w.search_bar.field.setText("compenso")
    w.search_bar._start()
    shot("ricerca.png")

    w._confirm_discard = lambda: True  # la firma di prova non si salva
    w.close()
    pump(0.5)
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main())
