# PieDF

Lettore PDF leggero e portatile per Windows, con firma visiva dei documenti (come "Firma" di Acrobat). Software libero, licenza [AGPL-3.0](LICENSE).

## Scaricare e usare

1. Scarica lo zip `PieDF-<versione>-win64.zip` dalle [Release](https://github.com/dellaluca/PieDF/releases) (oppure, per una versione in prova, dagli artifact della run "Build" di una PR) e scompattalo dove vuoi (Documenti, Desktop, una chiavetta USB).
2. Avvia `PieDF\PieDF.exe`. Non serve installare niente e non si scrive nulla nel registro.

Firme e impostazioni finiscono in `PieDF\data\`, accanto all'exe: copiando la cartella si portano dietro anche quelle. Se la cartella non è scrivibile (per esempio `C:\Program Files\PieDF`) si usa `%APPDATA%\PieDF`.

**Aprire i PDF con PieDF**: tasto destro su un PDF → *Apri con* → *Scegli un'altra app* → *Cerca un'altra app in questo PC* e scegli `PieDF.exe`. Spuntando *Usa sempre quest'app* PieDF diventa il lettore predefinito. Si può anche trascinare un PDF sull'exe o nella finestra.

## Costruire l'exe

Ogni PR lo costruisce e lo prova da sola nel controllo "Build" (lo zip è tra gli artifact della run). A mano:

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
| Spazio + trascina / tasto centrale | Sposta il documento |

Lo sviluppo procede a ticket: vedi le [issue](https://github.com/dellaluca/PieDF/issues).

## Licenza

PieDF è software libero: puoi usarlo, studiarlo, modificarlo e ridistribuirlo secondo la [GNU Affero General Public License v3.0](LICENSE). Chi distribuisce una versione modificata deve renderne disponibile il codice sorgente con la stessa licenza.

PieDF usa [PyMuPDF](https://pymupdf.readthedocs.io/) (AGPL-3.0, Artifex) e [Qt for Python / PySide6](https://doc.qt.io/qtforpython-6/) (LGPL-3.0).

## Firma del codice

Gli exe delle Release sono firmati con la firma gratuita per progetti open source: free code signing provided by [SignPath.io](https://about.signpath.io/), certificate by [SignPath Foundation](https://signpath.org/). Si firma solo quello che il workflow "Release" costruisce dal codice di questo repo. Dettagli e configurazione: [docs/firma-codice.md](docs/firma-codice.md).

- Autori e revisori (committer e approver): [@dellaluca](https://github.com/dellaluca). Le modifiche proposte dagli assistenti automatici del repo diventano PR che vengono riviste prima del merge.
- Chi approva le firme: [@dellaluca](https://github.com/dellaluca).

**Privacy**: PieDF non invia dati in rete. L'unico collegamento verso l'esterno è l'apertura nel browser di un link contenuto in un PDF, solo dopo aver cliccato il link e confermato l'indirizzo.

## Pubblicare una versione

1. Aggiorna `__version__` in `piedf/__init__.py` con una PR e uniscila.
2. Crea e invia il tag corrispondente:
   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```
3. Il workflow "Release" costruisce l'exe su Windows, lo prova e crea la Release con lo zip allegato. Se il tag non corrisponde a `__version__` si ferma con un errore.
