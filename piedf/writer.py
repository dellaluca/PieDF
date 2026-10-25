"""Scrittura delle firme piazzate, dei campi compilati e delle annotazioni di PieDF nel PDF
(senza Qt nella logica: i rettangoli arrivano da `Placement`).

Ogni firma diventa un'immagine sopra la pagina (`page.insert_image`); i campi restano
campi (si scrive solo il valore e il suo aspetto); le caselle di testo restano annotazioni
FreeText modificabili. Il resto del documento non viene toccato né rasterizzato.
"""

import os
from collections.abc import Iterable
from pathlib import Path

import pymupdf

from piedf.document import TextBox, add_text_box, set_field_value

TMP_SUFFIX = ".piedf-tmp"


class SaveError(Exception):
    """Il salvataggio non è riuscito; il messaggio è pronto per l'utente."""


def has_digital_signatures(path: str, password: str | None = None) -> bool:
    """True se il PDF ha firme digitali con certificato (che una modifica invaliderebbe)."""
    try:
        doc = pymupdf.open(path)
    except Exception:
        return False
    try:
        if doc.needs_pass:
            doc.authenticate(password or "")
        return doc.get_sigflags() == 3
    finally:
        doc.close()


def save_with_signatures(
    src: str, password: str | None, placements: Iterable, dest: str,
    streams: dict[Path, bytes] | None = None, fields: Iterable[tuple[int, int, str]] = (),
    annotations: tuple[list[tuple[int, int]], list] | None = None,
) -> None:
    """Scrive `src` con le firme e i campi in `dest` (anche lo stesso file). Solleva `SaveError`.

    `streams` dà il PNG di una firma il cui file non c'è più (eliminata da "Le mie firme").
    `fields` sono i valori cambiati dei campi modulo: (pagina, xref del widget, valore).
    `annotations` (da `Annotations.pending`): le annotazioni di PieDF lette dal file,
    (pagina, xref), si cancellano e si riscrivono tutte quelle del modello; None = non cambiate.
    Il file sorgente non deve essere aperto da altri processi (render) se `dest` è `src`.
    """
    streams = streams or {}
    same_file = os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dest))
    tmp = dest + TMP_SUFFIX
    doc = None
    try:
        try:
            doc = pymupdf.open(src)
        except Exception as e:
            raise SaveError(f"Impossibile rileggere il documento originale.\n\nDettagli: {e}") from e
        if doc.needs_pass and not doc.authenticate(password or ""):
            raise SaveError("Password del documento non valida.")

        for page, xref, value in fields:
            set_field_value(doc[page], xref, value)

        if annotations is not None:
            # Tutte da capo: modifiche, spostamenti ed eliminazioni restano coerenti.
            original, items = annotations
            for index, xref in original:
                page = doc[index]
                annot = page.load_annot(xref)
                if annot is not None:
                    page.delete_annot(annot)
            for item in items:
                if isinstance(item, TextBox):
                    add_text_box(doc[item.page], item)

        xrefs: dict[Path, int] = {}  # stesso PNG piazzato più volte → una sola copia nel file
        for pl in placements:
            page = doc[pl.page]
            visible = pymupdf.Rect(pl.rect.left(), pl.rect.top(), pl.rect.right(), pl.rect.bottom())
            if pl.image_path in xrefs:
                source = {"xref": xrefs[pl.image_path]}
            elif pl.image_path.exists():
                source = {"filename": str(pl.image_path)}
            elif pl.image_path in streams:
                source = {"stream": streams[pl.image_path]}
            else:
                raise SaveError(f"L'immagine della firma «{pl.image_path.name}» non esiste più.")
            xrefs[pl.image_path] = page.insert_image(
                visible * page.derotation_matrix,  # da coordinate "come si vede" a quelle della pagina
                rotate=page.rotation,  # tiene la firma dritta sulle pagine ruotate
                keep_proportion=True, overlay=True,
                **source,
            )

        if same_file and doc.can_save_incrementally():
            # Aggiunta in coda al file: le revisioni precedenti (e le firme digitali) restano intatte.
            # Come saveIncr(), ma con deflate: senza, il PNG finirebbe nel file non compresso.
            try:
                doc.save(dest, incremental=True, deflate=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
            except Exception as e:
                raise SaveError(_write_message(dest, e)) from e
            return

        try:
            doc.save(tmp, garbage=1, deflate=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
        except Exception as e:
            raise SaveError(_write_message(dest, e)) from e
        doc.close()  # su Windows os.replace non può sostituire un file ancora aperto
        doc = None
        try:
            os.replace(tmp, dest)
        except OSError as e:
            raise SaveError(_write_message(dest, e)) from e
    except SaveError:
        raise
    except Exception as e:
        raise SaveError(f"Errore durante la scrittura delle firme.\n\nDettagli: {e}") from e
    finally:
        if doc is not None:
            doc.close()
        try:
            os.remove(tmp)
        except OSError:
            pass


def _write_message(dest: str, error: Exception) -> str:
    return (
        f"Impossibile scrivere «{Path(dest).name}».\n\n"
        "La cartella potrebbe non essere scrivibile oppure il file è aperto in un altro programma.\n\n"
        f"Dettagli: {error}"
    )
