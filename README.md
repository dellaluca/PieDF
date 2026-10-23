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

Lo sviluppo procede a ticket: vedi le [issue](https://github.com/dellaluca/PieDF/issues).

## Licenza

PieDF usa [PyMuPDF](https://pymupdf.readthedocs.io/), rilasciato sotto AGPL-3.0.
