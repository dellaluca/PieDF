# Sviluppo

Le informazioni tecniche per chi lavora sul codice di PieDF. Le decisioni di progetto sono in [CLAUDE.md](../CLAUDE.md), l'automazione dei ticket in [automazione.md](automazione.md).

## Ambiente

Serve Python 3.12, su Windows.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m piedf            # finestra vuota
python -m piedf file.pdf   # apre direttamente un file
```

## Controlli automatici

I controlli end-to-end stanno in `tests/check_<area>.py` e girano su PDF di prova generati (semplice, 350 pagine, cifrato, scansionato, danneggiati…):

```bash
python tests/run_all.py                # genera i PDF e li esegue tutti
python tests/run_all.py pages forms    # solo check_pages.py e check_forms.py
```

Ogni PR li esegue su Windows nel workflow "Build".

## Costruire l'exe

Ogni PR costruisce e prova l'exe nel controllo "Build" (lo zip è tra gli artifact della run). A mano:

```bash
pip install -r requirements.txt -r requirements-build.txt
python tools/build_exe.py             # dist\PieDF\PieDF.exe e dist\PieDF-<versione>-win64.zip
python tools/build_exe.py --onefile   # dist\PieDF.exe, un solo file (solo per confronto)
python tests/run_all.py exe           # avvia l'exe e controlla processi, dati portatili, chiusura
```

Si distribuisce la versione a cartella (onedir) dentro lo zip: il onefile si scompatta in una cartella temporanea a ogni avvio, quindi parte più lento. Le opzioni di build (moduli Qt esclusi, icona, informazioni di versione) sono in `piedf.spec`.

## Screenshot del README

Le immagini in `docs/img/` le genera `python tools/screenshots.py`, sui PDF di prova di `tests/make_test_pdfs.py`. Rigenerale quando l'interfaccia cambia in modo visibile.

## Logo e icona

Il logo è `piedf/assets/piedf.svg`. L'icona di finestra ed exe (`piedf/assets/piedf.ico`, 10 dimensioni da 16 a 256 px) si rigenera con `python tools/make_icon.py` ogni volta che il logo cambia.

## Pubblicare una versione

1. Aggiorna `__version__` in `piedf/__init__.py` con una PR e uniscila.
2. Crea e invia il tag corrispondente:
   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```
3. Il workflow "Release" costruisce l'exe su Windows, lo prova e crea la Release con lo zip allegato, anche con il nome fisso `PieDF-win64.zip` per il link diretto all'ultima versione (`https://github.com/dellaluca/PieDF/releases/latest/download/PieDF-win64.zip`). Se il tag non corrisponde a `__version__` si ferma con un errore.
