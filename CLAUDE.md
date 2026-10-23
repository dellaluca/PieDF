# PieDF

Lettore PDF desktop leggero e portatile con firma visiva stile Acrobat "Firma".

## Decisioni

- **Stack**: Python 3.12 + PySide6 (`PySide6-Essentials`, solo i moduli base per tenere l'exe piccolo) + PyMuPDF per render e scrittura: usare `import pymupdf` (il vecchio nome `fitz` è deprecato in PyMuPDF 2.0).
- **Piattaforma**: solo Windows. Distribuzione come exe portatile con PyInstaller (#8): niente installer, niente registro.
- **Firma**: solo visiva (disegnata / scritta in corsivo / immagine), inserita come immagine con `page.insert_image`. Niente firme con certificato.
- **Mai rasterizzare le pagine** al salvataggio: il resto del PDF deve restare intatto.
- **Dati utente** (firme, impostazioni): `data/` accanto all'exe se scrivibile, altrimenti `%APPDATA%\PieDF`.
- **Licenza**: PyMuPDF è AGPL; ok finché uso personale / repo privato.

## Struttura

- `piedf/__main__.py` – entry point (`python -m piedf [file.pdf]`)
- `piedf/app.py` – crea `QApplication` e la finestra, gestisce gli argomenti
- `piedf/main_window.py` – finestra principale e menu
- `piedf/assets/` – icona e risorse incluse nell'exe

## Workflow

Un ticket GitHub = un branch (`feat/<n>-<slug>`) = una PR verso `main`. Mai push diretti su `main`.
Testi dell'interfaccia in italiano.
