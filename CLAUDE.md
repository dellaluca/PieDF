# PieDF

Lettore PDF desktop leggero e portatile con firma visiva stile Acrobat "Firma".

## Decisioni

- **Stack**: Python 3.12 + PySide6 (`PySide6-Essentials`, solo i moduli base per tenere l'exe piccolo) + PyMuPDF per render e scrittura: usare `import pymupdf` (il vecchio nome `fitz` è deprecato in PyMuPDF 2.0).
- **Piattaforma**: solo Windows. Distribuzione come exe portatile con PyInstaller (#8): niente installer, niente registro.
- **Firma**: solo visiva (disegnata / scritta in corsivo / immagine), inserita come immagine con `page.insert_image`. Niente firme con certificato.
- **Mai rasterizzare le pagine** al salvataggio: il resto del PDF deve restare intatto.
- **Dati utente** (firme, impostazioni): `data/` accanto all'exe se scrivibile, altrimenti `%APPDATA%\PieDF`.
- **Exe** (`piedf.spec`, `python tools/build_exe.py`): PyInstaller **onedir** distribuito in uno zip (`dist/PieDF-<versione>-win64.zip`), non onefile (si scompatta a ogni avvio e il processo di render rilancia l'exe). Nell'exe il processo di render è l'exe stesso con `--multiprocessing-fork`: `__main__.py` chiama `freeze_support()` prima di importare `piedf.app`, così il figlio non carica Qt. Lo spec esclude i moduli Qt non usati (se ne aggiungi uno a `piedf`, toglilo da `EXCLUDES`), i plugin immagine tranne jpeg/ico, le traduzioni tranne `qtbase_it` e `pymupdf.layout` (onnxruntime, ~30 MB). Le risorse stanno in `_internal/piedf/assets`, dove punta già `Path(__file__).parent`. Versione dell'exe da `piedf.__version__`.
- **Licenza**: PyMuPDF è AGPL; ok finché uso personale / repo privato.
- **Render in un processo separato**: PyMuPDF tiene il GIL durante il render (una pagina pesante bloccava la GUI per 2 s anche da un thread), quindi le pagine si disegnano in un processo `multiprocessing` "spawn". Per questo `__main__.py` ha la guardia `if __name__ == "__main__"` e `freeze_support()`: non toglierli (servono anche a PyInstaller). Il processo di render non importa Qt.
- **Font per "Scrivi"**: si usano i font calligrafici presenti in ogni Windows 10/11 (Segoe Script, Segoe Print, Ink Free, Gabriola), registrati da `%WINDIR%\Fonts` se Qt non li vede. Nessun font incluso nell'exe: la firma diventa comunque un PNG.
- **Elaborazione immagini senza numpy**: solo Qt + `bytes.translate` (velocità C). Non aggiungere numpy per questo.
- **Documento nella GUI**: la GUI apre il PDF solo per leggere dimensioni pagine e password, poi lo chiude (niente file bloccati su Windows).
- **Coordinate delle firme piazzate**: `Placement.rect` è in punti PDF, nello spazio della pagina come si vede (dopo /Rotate), origine in alto a sinistra: lo stesso di `page.rect` in PyMuPDF e di `PdfDocument.page_sizes`. Le conversioni verso lo schermo sono in `PdfView` (`page_at`, `to_page_point`, `page_rect_to_viewport`). I posizionamenti stanno in `Placements` (modello senza widget, con `QUndoStack`: "modificato" = stack non pulito); si svuotano aprendo o chiudendo un documento.
- **Posizione di lettura** (`positions.py`): `posizioni.json` in `data_dir()`, non `settings.ini` (con molti file l'INI cresce e QSettings lo riscrive tutto a ogni modifica). Chiave = percorso `normcase(abspath)`, massimo 500 file (via i meno recenti), scrittura atomica, JSON illeggibile = vuoto. `y` è in punti PDF dentro la pagina (`PdfView.scroll_position` / `set_scroll_position`), così regge zoom e finestra diversi. Salvata a ogni chiusura del documento (altro file, Ctrl+W, uscita) e ogni 30 s; ripristinata in `open_file` dopo `set_document`. Con Adatta larghezza/pagina il ridimensionamento della finestra mantiene la posizione.
- **Salvataggio** (`writer.py`): ogni firma è un `page.insert_image(rect * page.derotation_matrix, rotate=page.rotation, overlay=True)` (formula provata su 0/90/180/270°); lo stesso PNG si scrive una volta sola (riuso dell'xref). Su un file nuovo: salvataggio completo su `<dest>.piedf-tmp` e poi `os.replace`, mai un file a metà. Sovrascrivendo lo stesso file, se si può, salvataggio incrementale (con `deflate=True`, altrimenti il PNG finisce non compresso). Sempre `encryption=PDF_ENCRYPT_KEEP`: un PDF cifrato resta cifrato con le stesse password (anche l'owner). Prima di sovrascrivere il file aperto, `Renderer.close(gen, wait=True)` aspetta che il processo di render lo rilasci (su Windows altrimenti è bloccato). Dopo il salvataggio la GUI riapre il file salvato alla stessa pagina e zoom e svuota i posizionamenti: le firme non sono più modificabili. Il primo "Salva" di un documento propone `<nome>_firmato.pdf`.
- **Moduli PDF** (`forms.py`, `document.FormField`): i campi si leggono in `open_document` (con XFA nessun campo, solo il messaggio). Rettangolo "come si vede" = `widget.rect * page.rotation_matrix`, lo stesso spazio di `Placement.rect`. Valori sempre testo: caselle e radio = stato acceso o `"Off"`, per i radio il valore è del gruppo (`FormField.group` = xref del campo padre; i widget dello stesso gruppo cambiano insieme). `FormValues` usa lo **stesso `QUndoStack`** di `Placements` (un passo per ogni modifica confermata). Aspetto vero senza salvare: il messaggio `("field", gen, page, xref, value)` cambia il documento **in memoria** del processo di render, che risponde `("field", gen, page)`; la vista marca vecchio il render della pagina e lo richiede (i valori si rimandano se il processo riapre il file). `document.set_field_value` (usato da worker e `writer.py`) passa da `pdf_set_field_value` di MuPDF e non da `Widget.update()`, che riscriverebbe il font del campo con Helvetica; caselle e radio cambiano `/AS` dei widget e `/V` del campo. Il clic su un campo compilabile ha la precedenza sulle firme piazzate (non sulle maniglie della firma selezionata né durante il posizionamento); campi firma, pulsanti e sola lettura si ignorano, anche col Tab.

## Struttura

- `piedf/__main__.py` – entry point (`python -m piedf [file.pdf]`)
- `piedf/app.py` – crea `QApplication` e la finestra, gestisce gli argomenti
- `piedf/main_window.py` – finestra principale: menu, toolbar, barra dei moduli (Evidenzia campi), password, drag & drop, file recenti
- `piedf/viewer.py` – `PdfView`: scorrimento continuo, zoom, cache delle pagine visibili, evidenziazione ed editor sul posto dei campi
- `piedf/renderer.py` – lato GUI del processo di render (segnali Qt, riavvio se il processo muore)
- `piedf/render_worker.py` – processo di render (solo PyMuPDF), scarta le richieste vecchie
- `piedf/document.py` – apertura del PDF: password, file danneggiati, dimensioni pagine, campi modulo (`FormField`, `set_field_value`)
- `piedf/paths.py` – cartella dati portatile e `settings.ini`
- `piedf/signatures.py` – archivio firme (`<dati>/firme/*.png`, tipo nel prefisso `firma_` / `iniziali_`) e trattamento immagini (sfondo trasparente, ritaglio)
- `piedf/signature_dialog.py` – dialog "Crea firma": Disegna / Scrivi / Immagine
- `piedf/signature_manager.py` – dialog "Le mie firme": elenco, nuova, elimina
- `piedf/forms.py` – valori dei campi modulo (`FormValues`), ordine del Tab, comandi annulla/ripeti
- `piedf/placements.py` – firme piazzate sul documento (`Placement`, `Placements`), comandi annulla/ripeti, dimensione predefinita
- `piedf/positions.py` – posizione di lettura per file (`posizioni.json`, senza Qt)
- `piedf/writer.py` – scrittura delle firme e dei campi compilati nel PDF (`save_with_signatures`, `SaveError`), controllo firme digitali esistenti
- `piedf/assets/` – icona e risorse incluse nell'exe
- `piedf.spec` – spec di PyInstaller (onedir; `-- --onefile` per il confronto), esclusioni, icona, informazioni di versione
- `tools/build_exe.py` – lancia PyInstaller, crea lo zip e stampa le dimensioni; `requirements-build.txt` – solo per costruire (PyInstaller)
- `tests/` – `make_test_pdfs.py` genera PDF di prova in `tests/out/` (ignorata da git); `check_<area>.py` sono i controlli end-to-end offscreen (`check_viewer.py`, `check_signatures.py`, …): uscita 1 se qualcosa fallisce, i controlli solo-Windows si saltano altrove (`check_exe.py` richiede anche la build in `dist/`)
- `.github/claude/` – istruzioni di orchestratore ed esecutore; `.github/workflows/` – worker e build (li modifica solo la sessione locale)
- `docs/automazione.md` – come funzionano e si configurano i worker

## Workflow

Un ticket GitHub = un branch (`feat/<n>-<slug>`) = una PR verso `main`. Mai push diretti su `main`.
I ticket li eseguono i worker su GitHub Actions (orchestratore → esecutore → PR, vedi `docs/automazione.md`). In sessione locale si scrivono ticket completi e si curano automazione e workflow, che i worker non possono modificare.
Ogni funzione ha il suo controllo end-to-end in `tests/check_<area>.py`; il build della PR li esegue tutti su Windows.
Testi dell'interfaccia in italiano.
