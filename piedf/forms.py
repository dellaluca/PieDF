"""Valori dei campi modulo del documento aperto, ancora in memoria (li scrive nel PDF `writer.py`).

Il modello condivide il `QUndoStack` di `Placements`: Ctrl+Z, "modificato" e la conferma
alla chiusura valgono per firme e campi insieme. Ogni modifica confermata è un passo di annulla.
"""

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QUndoCommand, QUndoStack

from piedf.document import CHECKBOX, OFF, RADIO, FormField


class FormValues(QObject):
    """Campi del documento, valori attuali e ordine del giro con Tab."""

    # Widget il cui valore è cambiato (anche per annulla/ripeti): da ridisegnare.
    values_changed = Signal(list)  # list[FormField]
    fields_reset = Signal()

    def __init__(self, undo_stack: QUndoStack, parent=None) -> None:
        super().__init__(parent)
        self.undo_stack = undo_stack
        self.fields: list[FormField] = []
        self.tab_order: list[FormField] = []
        self._pages: dict[int, list[FormField]] = {}
        self._groups: dict[int, list[FormField]] = {}
        self._original: dict[int, str] = {}  # xref → valore letto dal file

    def set_fields(self, fields: list[FormField]) -> None:
        """Nuovo documento (o nessuno): i valori ripartono da quelli del file."""
        self.fields = fields
        self._pages = {}
        self._groups = {}
        for f in fields:
            self._pages.setdefault(f.page, []).append(f)
            self._groups.setdefault(f.group, []).append(f)
        self._original = {f.xref: f.value for f in fields}
        # Dall'alto in basso, da sinistra a destra, pagina per pagina.
        self.tab_order = sorted(
            (f for f in fields if f.fillable), key=lambda f: (f.page, round(f.rect[1]), f.rect[0])
        )
        self.fields_reset.emit()

    def on_page(self, page: int) -> list[FormField]:
        return self._pages.get(page, [])

    def changed(self) -> list[tuple[int, int, str]]:
        """(pagina, xref, valore) dei widget cambiati rispetto al file."""
        return [(f.page, f.xref, f.value) for f in self.fields if f.value != self._original[f.xref]]

    # --- Modifiche (ognuna è un passo di annulla) --------------------------------------

    def set_value(self, field: FormField, value: str) -> None:
        if field.value != value and field.fillable:
            self.undo_stack.push(_ValueCommand(self, self._groups[field.group], field.value, value, _text(field)))

    def toggle(self, field: FormField) -> None:
        """Clic su una casella (alterna) o su un radio (lo accende, spegnendo gli altri del gruppo)."""
        if field.type == CHECKBOX:
            self.set_value(field, OFF if field.checked else field.on_state)
        elif field.type == RADIO and not field.checked:
            self.set_value(field, field.on_state)

    # --- Usato dai comandi -------------------------------------------------------------

    def _apply(self, fields: list[FormField], value: str) -> None:
        for f in fields:
            f.value = value
        self.values_changed.emit(fields)


def _text(field: FormField) -> str:
    return {CHECKBOX: "casella", RADIO: "scelta"}.get(field.type, "campo") + f" «{field.name}»"


class _ValueCommand(QUndoCommand):
    def __init__(self, model: FormValues, fields: list[FormField], old: str, new: str, text: str) -> None:
        super().__init__(text)
        self._model, self._fields = model, fields
        self._old, self._new = old, new

    def redo(self) -> None:
        self._model._apply(self._fields, self._new)

    def undo(self) -> None:
        self._model._apply(self._fields, self._old)
