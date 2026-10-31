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

`check_lock.py` («Salva e blocca», #70) non usa mai il servizio di marca temporale vero: avvia un servizio finto su `127.0.0.1` con le TSA di prova di pyHanko. Fuori da Windows `piedf/sealing.py` non ha DPAPI e protegge la chiave del sigillo con una chiave locale (stesso comportamento, nessuna protezione vera): serve solo per sviluppo e controlli.

## Costruire l'exe

Ogni PR costruisce e prova l'exe nel controllo "Build" (senza allegare lo zip). A mano:

```bash
pip install -r requirements.txt -r requirements-build.txt
python tools/build_exe.py             # dist\PieDF\PieDF.exe e dist\PieDF-<versione>-win64.zip
python tools/build_exe.py --onefile   # dist\PieDF.exe, un solo file (solo per confronto)
python tests/run_all.py exe           # avvia l'exe e controlla processi, dati portatili, chiusura, sigillo
```

`check_exe.py` prova il sigillo nell'exe con un'opzione nascosta, senza finestra: `PieDF.exe --piedf-sigilla <pdf> <destinazione> <cartella del sigillo> <nome>` (uscita 0 se riuscito, altrimenti l'errore in `<destinazione>.errore.txt`).

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
3. Il workflow "Release" costruisce l'exe su Windows, lo prova e crea la Release con le note e il codice sorgente. Lo zip compilato **non** è allegato alla Release (la versione pronta si vende a parte): finisce in una release in **bozza** `v<versione>-zip`, che vedono solo i collaboratori del repo (non pubblicarla). Niente artifact: in un repo pubblico li scarica chiunque. Se il tag non corrisponde a `__version__` si ferma con un errore.
