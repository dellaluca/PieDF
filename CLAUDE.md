# PieDF

Lettore PDF desktop leggero e portatile con firma visiva stile Acrobat "Firma".

## Decisioni

- **Stack**: Python 3.12 + PySide6 (`PySide6-Essentials`, solo i moduli base per tenere l'exe piccolo) + PyMuPDF per render e scrittura: usare `import pymupdf` (il vecchio nome `fitz` è deprecato in PyMuPDF 2.0).
- **Piattaforma**: solo Windows. Distribuzione come exe portatile con PyInstaller (#8): niente installer, niente registro.
- **Firma**: solo visiva (disegnata / scritta in corsivo / immagine), inserita come immagine con `page.insert_image`. Niente firme con certificato.
- **Mai rasterizzare le pagine** al salvataggio: il resto del PDF deve restare intatto.
- **Dati utente** (firme, impostazioni): `data/` accanto all'exe se scrivibile, altrimenti `%APPDATA%\PieDF`.
- **Licenza**: PyMuPDF è AGPL; ok finché uso personale / repo privato.
- **Render in un processo separato**: PyMuPDF tiene il GIL durante il render (una pagina pesante bloccava la GUI per 2 s anche da un thread), quindi le pagine si disegnano in un processo `multiprocessing` "spawn". Per questo `__main__.py` ha la guardia `if __name__ == "__main__"` e `freeze_support()`: non toglierli (servono anche a PyInstaller). Il processo di render non importa Qt.
- **Font per "Scrivi"**: si usano i font calligrafici presenti in ogni Windows 10/11 (Segoe Script, Segoe Print, Ink Free, Gabriola), registrati da `%WINDIR%\Fonts` se Qt non li vede. Nessun font incluso nell'exe: la firma diventa comunque un PNG.
- **Elaborazione immagini senza numpy**: solo Qt + `bytes.translate` (velocità C). Non aggiungere numpy per questo.
- **Documento nella GUI**: la GUI apre il PDF solo per leggere dimensioni pagine e password, poi lo chiude (niente file bloccati su Windows).
- **Coordinate delle firme piazzate**: `Placement.rect` è in punti PDF, nello spazio della pagina come si vede (dopo /Rotate), origine in alto a sinistra: lo stesso di `page.rect` in PyMuPDF e di `PdfDocument.page_sizes`. Le conversioni verso lo schermo sono in `PdfView` (`page_at`, `to_page_point`, `page_rect_to_viewport`). I posizionamenti stanno in `Placements` (modello senza widget, con `QUndoStack`: "modificato" = stack non pulito); si svuotano aprendo o chiudendo un documento.
- **Salvataggio** (`writer.py`): ogni firma è un `page.insert_image(rect * page.derotation_matrix, rotate=page.rotation, overlay=True)` (formula provata su 0/90/180/270°); lo stesso PNG si scrive una volta sola (riuso dell'xref). Su un file nuovo: salvataggio completo su `<dest>.piedf-tmp` e poi `os.replace`, mai un file a metà. Sovrascrivendo lo stesso file, se si può, salvataggio incrementale (con `deflate=True`, altrimenti il PNG finisce non compresso). Sempre `encryption=PDF_ENCRYPT_KEEP`: un PDF cifrato resta cifrato con le stesse password (anche l'owner). Prima di sovrascrivere il file aperto, `Renderer.close(gen, wait=True)` aspetta che il processo di render lo rilasci (su Windows altrimenti è bloccato). Dopo il salvataggio la GUI riapre il file salvato alla stessa pagina e zoom e svuota i posizionamenti: le firme non sono più modificabili. Il primo "Salva" di un documento propone `<nome>_firmato.pdf`.

## Struttura

- `piedf/__main__.py` – entry point (`python -m piedf [file.pdf]`)
- `piedf/app.py` – crea `QApplication` e la finestra, gestisce gli argomenti
- `piedf/main_window.py` – finestra principale: menu, toolbar, password, drag & drop, file recenti
- `piedf/viewer.py` – `PdfView`: scorrimento continuo, zoom, cache delle pagine visibili
- `piedf/renderer.py` – lato GUI del processo di render (segnali Qt, riavvio se il processo muore)
- `piedf/render_worker.py` – processo di render (solo PyMuPDF), scarta le richieste vecchie
- `piedf/document.py` – apertura del PDF: password, file danneggiati, dimensioni pagine
- `piedf/paths.py` – cartella dati portatile e `settings.ini`
- `piedf/signatures.py` – archivio firme (`<dati>/firme/*.png`, tipo nel prefisso `firma_` / `iniziali_`) e trattamento immagini (sfondo trasparente, ritaglio)
- `piedf/signature_dialog.py` – dialog "Crea firma": Disegna / Scrivi / Immagine
- `piedf/signature_manager.py` – dialog "Le mie firme": elenco, nuova, elimina
- `piedf/placements.py` – firme piazzate sul documento (`Placement`, `Placements`), comandi annulla/ripeti, dimensione predefinita
- `piedf/writer.py` – scrittura delle firme nel PDF (`save_with_signatures`, `SaveError`), controllo firme digitali esistenti
- `piedf/assets/` – icona e risorse incluse nell'exe
- `tests/` – `make_test_pdfs.py` genera PDF di prova in `tests/out/` (ignorata da git); `check_<area>.py` sono i controlli end-to-end offscreen (`check_viewer.py`, `check_signatures.py`, …): uscita 1 se qualcosa fallisce, i controlli solo-Windows si saltano altrove
- `.github/claude/` – istruzioni di orchestratore ed esecutore; `.github/workflows/` – worker e build (li modifica solo la sessione locale)
- `docs/automazione.md` – come funzionano e si configurano i worker

## Workflow

Un ticket GitHub = un branch (`feat/<n>-<slug>`) = una PR verso `main`. Mai push diretti su `main`.
I ticket li eseguono i worker su GitHub Actions (orchestratore → esecutore → PR, vedi `docs/automazione.md`). In sessione locale si scrivono ticket completi e si curano automazione e workflow, che i worker non possono modificare.
Ogni funzione ha il suo controllo end-to-end in `tests/check_<area>.py`; il build della PR li esegue tutti su Windows.
Testi dell'interfaccia in italiano.
