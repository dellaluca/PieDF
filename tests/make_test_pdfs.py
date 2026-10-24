"""Genera i PDF di prova in tests/out/pdfs: semplice, lungo, cifrato, scansionato, pesante, danneggiati, ruotati, firmato digitalmente."""
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

data = (out / "lungo_350.pdf").read_bytes()
(out / "danneggiato.pdf").write_bytes(data[: len(data) // 2])  # troncato a metà: irrecuperabile
i = data.rfind(b"xref")
(out / "senza_xref.pdf").write_bytes(data[:i])  # manca xref/trailer: MuPDF ripara
table = re.sub(rb"(\d{10}) (\d{5}) n", lambda m: b"%010d %s n" % (int(m.group(1)) + 37, m.group(2)), data[i:])
(out / "xref_corrotta.pdf").write_bytes(data[:i] + table)  # offset sbagliati
(out / "non_pdf.pdf").write_bytes(b"questo non e' un pdf\n" * 50)
print("ok", sorted(f.name for f in out.iterdir()))
