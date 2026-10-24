# PieDF

Lettore PDF leggero e portatile per Windows, con firma visiva dei documenti (come "Firma" di Acrobat). Gratuito per uso personale.

## Sviluppo

Serve Python 3.12.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m piedf            # finestra vuota
python -m piedf file.pdf   # apre direttamente un file
```

Controlli automatici del viewer e delle firme, su PDF di prova (semplice, 350 pagine, cifrato, scansionato, danneggiati…):

```bash
python tests/make_test_pdfs.py
set QT_QPA_PLATFORM=offscreen
python tests/check_viewer.py
python tests/check_signatures.py
```

## Scorciatoie

| Tasto | Azione |
|---|---|
| Ctrl+O | Apri |
| Ctrl+W | Chiudi documento |
| Ctrl+rotella, Ctrl++ / Ctrl+− | Zoom |
| Ctrl+1 / Ctrl+0 / Ctrl+2 | Dimensione reale / Adatta pagina / Adatta larghezza |
| Ctrl+PgSu / Ctrl+PgGiù | Pagina precedente / successiva |
| Ctrl+G | Vai a pagina |
| Home / Fine | Inizio / fine documento |

Lo sviluppo procede a ticket: vedi le [issue](https://github.com/dellaluca/PieDF/issues).

## Licenza

PieDF usa [PyMuPDF](https://pymupdf.readthedocs.io/), rilasciato sotto AGPL-3.0.
