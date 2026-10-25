"""Genera i PDF di prova in tests/out/pdfs: semplice, lungo, cifrato, scansionato, pesante, danneggiati, ruotati, firmato digitalmente, link, senza permesso di stampa."""
import random, re, sys
from pathlib import Path
import pymupdf

out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "out" / "pdfs"
out.mkdir(parents=True, exist_ok=True)

d = pymupdf.open()
p = d.new_page()
p.insert_text((72, 72), "PieDF - documento semplice", fontsize=20)
p.insert_text((72, 110), "Testo di prova per il viewer.", fontsize=12)
d.save(out / "semplice.pdf")

d = pymupdf.open()
for i in range(350):
    p = d.new_page(width=595, height=842)
    p.insert_text((72, 72), f"Pagina {i + 1} di 350", fontsize=28)
    for j in range(40):
        p.insert_text((72, 120 + j * 17), f"Riga {j + 1}: lorem ipsum dolor sit amet " * 2, fontsize=9)
d.save(out / "lungo_350.pdf")

d = pymupdf.open(out / "semplice.pdf")
d.save(out / "cifrato.pdf", encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pie", owner_pw="owner")

# Si apre senza password ma i permessi vietano la stampa (#25)
d = pymupdf.open(out / "semplice.pdf")
d.save(out / "senza_stampa.pdf", encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner",
       permissions=pymupdf.PDF_PERM_ACCESSIBILITY | pymupdf.PDF_PERM_COPY)

# "Scansione": immagine rumorosa grande a pagina intera, 5 pagine
random.seed(1)
d = pymupdf.open()
for i in range(5):
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 2480, 3508), False)
    pix.set_rect(pix.irect, (235, 232, 225))
    for _ in range(400):
        x, y = random.randrange(0, 2400), random.randrange(0, 3400)
        pix.set_rect(pymupdf.IRect(x, y, x + random.randrange(5, 80), y + 6), (40, 40, 40))
    p = d.new_page(width=595, height=842)
    p.insert_image(p.rect, stream=pix.tobytes("jpeg"))
d.save(out / "scansionato.pdf")

# Pagina vettoriale pesante, per misurare il tempo di render
d = pymupdf.open()
p = d.new_page()
sh = p.new_shape()
for _ in range(60000):
    a = pymupdf.Point(random.uniform(0, 595), random.uniform(0, 842))
    sh.draw_line(a, a + (random.uniform(-30, 30), random.uniform(-30, 30)))
sh.finish(width=0.3, color=(0, 0, 0.5))
sh.commit()
d.save(out / "pesante.pdf")

# Pagine con orientamenti misti (A4 verticale, orizzontale, ruotata)
d = pymupdf.open()
d.new_page(width=595, height=842).insert_text((72, 72), "Verticale", fontsize=30)
d.new_page(width=842, height=595).insert_text((72, 72), "Orizzontale", fontsize=30)
p = d.new_page(width=595, height=842)
p.insert_text((72, 72), "Ruotata 90", fontsize=30)
p.set_rotation(90)
d.save(out / "orientamenti.pdf")

# Quattro pagine A4 con /Rotate 0, 90, 180, 270 (salvataggio delle firme, #5)
d = pymupdf.open()
for rot in (0, 90, 180, 270):
    p = d.new_page(width=595, height=842)
    p.insert_text((72, 72), f"Rotate {rot}", fontsize=30)
    p.set_rotation(rot)
d.save(out / "ruotate.pdf")

# "Firmato digitalmente": PyMuPDF non crea firme con certificato, ma un campo firma
# basta perché get_sigflags() risponda 3 come in un PDF firmato davvero.
d = pymupdf.open(out / "semplice.pdf")
wdg = pymupdf.Widget()
wdg.field_type = pymupdf.PDF_WIDGET_TYPE_SIGNATURE
wdg.field_name = "FirmaDigitale"
wdg.rect = pymupdf.Rect(300, 700, 500, 760)
d[0].add_widget(wdg)
d.save(out / "firmato_digitale.pdf")

# Link (#24). I rettangoli sono nello spazio della pagina come si vede; dove c'è un link
# si disegna un riquadro rosso, così il controllo trova il punto da cliccare nell'immagine.
def raw_link(page, visible, action):
    """Link scritto a mano: serve per /Fit, destinazioni con nome, Launch, JavaScript."""
    r = visible * page.derotation_matrix * page.transformation_matrix  # → coordinate PDF
    xref = d.get_new_xref()
    d.update_object(xref, f"<</Type/Annot/Subtype/Link/Rect[{r.x0:g} {r.y0:g} {r.x1:g} {r.y1:g}]/Border[0 0 0]/A{action}>>")
    kind, annots = d.xref_get_key(page.xref, "Annots")
    d.xref_set_key(page.xref, "Annots", (annots[:-1] if kind == "array" else "[") + f" {xref} 0 R]")

def box(page, visible, label):
    page.draw_rect(visible * page.derotation_matrix, color=(1, 0, 0), fill=(1, 0, 0))
    page.insert_text((visible * page.derotation_matrix).bl + (0, 14), label, fontsize=10, rotate=page.rotation)

d = pymupdf.open()
for _ in range(6):
    d.new_page(width=595, height=842)
pages = [d[i] for i in range(6)]
pages[1].set_rotation(90)
pages[3].set_rotation(90)
for i, p in enumerate(pages):
    p.insert_text(pymupdf.Point(72, 60) * p.derotation_matrix, f"Pagina {i + 1}", fontsize=24, rotate=p.rotation)
p0 = pages[0]
p0.insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(72, 100, 300, 125), "page": 2, "to": pymupdf.Point(0, 400)})
box(p0, pymupdf.Rect(72, 100, 300, 125), "interno, pagina 3 a y 400")
raw_link(p0, pymupdf.Rect(72, 160, 300, 185), f"<</S/GoTo/D[{pages[2].xref} 0 R/Fit]>>")
box(p0, pymupdf.Rect(72, 160, 300, 185), "interno, pagina 3 senza punto")
p0.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 220, 300, 245), "uri": "https://example.com/percorso?x=1"})
box(p0, pymupdf.Rect(72, 220, 300, 245), "https")
p0.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 280, 300, 305), "uri": "mailto:info@example.com"})
box(p0, pymupdf.Rect(72, 280, 300, 305), "mailto")
p0.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 340, 300, 365), "uri": "file:///C:/Windows/System32/calc.exe"})
box(p0, pymupdf.Rect(72, 340, 300, 365), "file:")
raw_link(p0, pymupdf.Rect(72, 400, 300, 425), "<</S/Launch/F(calc.exe)>>")
box(p0, pymupdf.Rect(72, 400, 300, 425), "launch")
p0.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 460, 300, 485), "uri": "javascript:app.alert(1)"})
box(p0, pymupdf.Rect(72, 460, 300, 485), "javascript:")
raw_link(p0, pymupdf.Rect(72, 520, 300, 545), "<</S/GoToR/F(altro.pdf)/D[0/Fit]>>")
box(p0, pymupdf.Rect(72, 520, 300, 545), "altro PDF")
# Destinazione su una pagina ruotata: (100, 300) non ruotato è (542, 100) come si vede.
p0.insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(320, 100, 540, 125), "page": 3, "to": pymupdf.Point(100, 300)})
box(p0, pymupdf.Rect(320, 100, 540, 125), "pagina 4 ruotata, y 100")
raw_link(p0, pymupdf.Rect(320, 160, 540, 185), "<</S/GoTo/D(capitolo)>>")
box(p0, pymupdf.Rect(320, 160, 540, 185), "con nome, pagina 4 y 200")
names = d.get_new_xref()
# XYZ 200 542 = (200, 300) non ruotato dall'alto = (542, 200) come si vede.
d.update_object(names, f"<</Names[(capitolo)[{pages[3].xref} 0 R/XYZ 200 542 0]]>>")
d.xref_set_key(d.pdf_catalog(), "Names", f"<</Dests {names} 0 R>>")
# Link sulla pagina ruotata, nel punto in cui si vede
raw_link(pages[1], pymupdf.Rect(500, 300, 700, 340), f"<</S/GoTo/D[{p0.xref} 0 R/Fit]>>")
box(pages[1], pymupdf.Rect(500, 300, 700, 340), "ruotata, verso pagina 1")
d.save(out / "link.pdf")

data = (out / "lungo_350.pdf").read_bytes()
(out / "danneggiato.pdf").write_bytes(data[: len(data) // 2])  # troncato a metà: irrecuperabile
i = data.rfind(b"xref")
(out / "senza_xref.pdf").write_bytes(data[:i])  # manca xref/trailer: MuPDF ripara
table = re.sub(rb"(\d{10}) (\d{5}) n", lambda m: b"%010d %s n" % (int(m.group(1)) + 37, m.group(2)), data[i:])
(out / "xref_corrotta.pdf").write_bytes(data[:i] + table)  # offset sbagliati
(out / "non_pdf.pdf").write_bytes(b"questo non e' un pdf\n" * 50)
print("ok", sorted(f.name for f in out.iterdir()))
