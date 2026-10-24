"""Genera i PDF di prova in tests/out/pdfs: semplice, lungo, cifrato, scansionato, pesante, danneggiati, ruotati, firmato digitalmente, moduli."""
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

# Moduli compilabili (#21)
def widget(page, kind, name, rect, **attrs):
    w = pymupdf.Widget()
    w.field_type, w.field_name, w.rect = kind, name, pymupdf.Rect(rect)
    w.border_color, w.border_width, w.text_fontsize = (0.4, 0.4, 0.4), 1, 11
    for key, value in attrs.items():
        setattr(w, key, value)
    page.add_widget(w)
    return w


d = pymupdf.open()
p = d.new_page(width=595, height=842)
p.insert_text((72, 60), "Modulo di prova", fontsize=20)
for y, label in [(114, "Nome"), (154, "Note"), (234, "CAP"), (274, "Codice"), (312, "Privacy"),
                 (352, "Scelta"), (394, "Provincia"), (434, "Colore"), (534, "Firma")]:
    p.insert_text((72, y), label, fontsize=11)
T = pymupdf.PDF_WIDGET_TYPE_TEXT
widget(p, T, "Nome", (150, 100, 400, 120))
widget(p, T, "Note", (150, 140, 400, 200), field_flags=pymupdf.PDF_TX_FIELD_IS_MULTILINE)
widget(p, T, "CAP", (150, 220, 230, 240), text_maxlen=5)
widget(p, T, "Codice", (150, 260, 300, 280), field_value="ABC-123", field_flags=pymupdf.PDF_FIELD_IS_READ_ONLY)
widget(p, pymupdf.PDF_WIDGET_TYPE_CHECKBOX, "Privacy", (150, 300, 166, 316), field_value=False)
for i, state in enumerate(["Uno", "Due", "Tre"]):  # lo stato "acceso" prende il nome dal valore
    widget(p, pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON, "Scelta", (150 + 50 * i, 340, 166 + 50 * i, 356), field_value=state)
widget(p, pymupdf.PDF_WIDGET_TYPE_COMBOBOX, "Provincia", (150, 380, 300, 400),
       choice_values=["Milano", "Roma", "Napoli", "Torino"], field_value="Milano")
widget(p, pymupdf.PDF_WIDGET_TYPE_LISTBOX, "Colore", (150, 420, 300, 480),
       choice_values=["Rosso", "Verde", "Blu"], field_value="Rosso")
widget(p, pymupdf.PDF_WIDGET_TYPE_SIGNATURE, "Firma", (150, 520, 400, 580))
p = d.new_page(width=595, height=842)
p.set_rotation(90)
p.insert_text(pymupdf.Point(100, 90) * p.derotation_matrix, "Pagina ruotata", fontsize=11, rotate=90)
widget(p, T, "Citta", pymupdf.Rect(100, 100, 400, 120) * p.derotation_matrix)
d.save(out / "modulo.pdf")
# Creando i radio l'ultimo resta acceso: si parte con il gruppo spento.
d = pymupdf.open(out / "modulo.pdf")
for w in d[0].widgets():
    if w.field_name == "Scelta":
        w.field_value = False
        w.update()
d.saveIncr()
d.close()
d = pymupdf.open(out / "modulo.pdf")
d.save(out / "modulo_cifrato.pdf", encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pie", owner_pw="owner")
# XFA: basta la voce /XFA nell'AcroForm perché PieDF lo riconosca.
d.xref_set_key(d.pdf_catalog(), "AcroForm/XFA", "[(template) ()]")
d.save(out / "modulo_xfa.pdf")

d = pymupdf.open()
for i in range(50):
    p = d.new_page(width=595, height=842)
    p.insert_text((72, 40), f"Modulo lungo, pagina {i + 1}", fontsize=14)
    for j in range(20):
        widget(p, T, f"p{i + 1}_campo{j + 1}", (150, 60 + 38 * j, 450, 80 + 38 * j), field_value=f"Valore {j + 1}")
d.save(out / "modulo_lungo.pdf")

data = (out / "lungo_350.pdf").read_bytes()
(out / "danneggiato.pdf").write_bytes(data[: len(data) // 2])  # troncato a metà: irrecuperabile
i = data.rfind(b"xref")
(out / "senza_xref.pdf").write_bytes(data[:i])  # manca xref/trailer: MuPDF ripara
table = re.sub(rb"(\d{10}) (\d{5}) n", lambda m: b"%010d %s n" % (int(m.group(1)) + 37, m.group(2)), data[i:])
(out / "xref_corrotta.pdf").write_bytes(data[:i] + table)  # offset sbagliati
(out / "non_pdf.pdf").write_bytes(b"questo non e' un pdf\n" * 50)
print("ok", sorted(f.name for f in out.iterdir()))
