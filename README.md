# PieDF

Lettore PDF leggero e portatile per Windows, con firma visiva dei documenti (come "Firma" di Acrobat). Gratuito per uso personale.

## Scaricare e usare

1. Scarica lo zip `PieDF-<versione>-win64.zip` e scompattalo dove vuoi (Documenti, Desktop, una chiavetta USB).
2. Avvia `PieDF\PieDF.exe`. Non serve installare niente e non si scrive nulla nel registro.

Firme e impostazioni finiscono in `PieDF\data\`, accanto all'exe: copiando la cartella si portano dietro anche quelle. Se la cartella non è scrivibile (per esempio `C:\Program Files\PieDF`) si usa `%APPDATA%\PieDF`.

**Aprire i PDF con PieDF**: tasto destro su un PDF → *Apri con* → *Scegli un'altra app* → *Cerca un'altra app in questo PC* e scegli `PieDF.exe`. Spuntando *Usa sempre quest'app* PieDF diventa il lettore predefinito. Si può anche trascinare un PDF sull'exe o nella finestra.

## Costruire l'exe

Su Windows, con Python 3.12:

```bash
pip install -r requirements.txt -r requirements-build.txt
python tools/build_exe.py             # dist\PieDF\PieDF.exe e dist\PieDF-<versione>-win64.zip
python tools/build_exe.py --onefile   # dist\PieDF.exe, un solo file (solo per confronto)
python tests/make_test_pdfs.py
python tests/check_exe.py             # avvia l'exe e controlla processi, dati portatili, chiusura
```

Si distribuisce la versione a cartella (onedir) dentro lo zip: il onefile si scompatta in una cartella temporanea a ogni avvio, quindi parte più lento. Le opzioni di build (moduli Qt esclusi, icona, informazioni di versione) sono in `piedf.spec`.

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
