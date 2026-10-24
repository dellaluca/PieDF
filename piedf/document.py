from dataclasses import dataclass, field

import pymupdf

A4 = (595.0, 842.0)
OFF = "Off"  # valore di una casella o di un gruppo radio senza spunta

TEXT = pymupdf.PDF_WIDGET_TYPE_TEXT
CHECKBOX = pymupdf.PDF_WIDGET_TYPE_CHECKBOX
RADIO = pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON
COMBO = pymupdf.PDF_WIDGET_TYPE_COMBOBOX
LIST = pymupdf.PDF_WIDGET_TYPE_LISTBOX
SIGNATURE = pymupdf.PDF_WIDGET_TYPE_SIGNATURE
BUTTON = pymupdf.PDF_WIDGET_TYPE_BUTTON


class OpenError(Exception):
    """Il file non si può aprire (non è un PDF, è danneggiato oltre la riparazione, non esiste…)."""


class PasswordRequired(Exception):
    def __init__(self, wrong: bool) -> None:
        super().__init__("Password errata" if wrong else "Password richiesta")
        self.wrong = wrong


@dataclass(eq=False)
class FormField:
    """Un widget di un campo modulo. Il valore è sempre testo: per caselle e radio è lo stato
    "acceso" (`on_state`) oppure `OFF`; per i radio è il valore di tutto il gruppo."""

    xref: int  # del widget
    page: int  # indice da 0
    name: str
    type: int  # pymupdf.PDF_WIDGET_TYPE_*
    # Punti PDF (x0, y0, x1, y1), pagina come si vede: lo stesso spazio di `Placement.rect`.
    rect: tuple[float, float, float, float]
    value: str
    choices: list[tuple[str, str]] = field(default_factory=list)  # (valore, testo) di combo e list
    readonly: bool = False
    multiline: bool = False
    password: bool = False
    editable: bool = False  # combo in cui si può anche scrivere
    max_len: int = 0  # 0 = senza limite
    font_size: float = 0.0  # 0 = automatico
    align: int = 0  # 0 sinistra, 1 centro, 2 destra
    on_state: str | None = None  # caselle e radio
    # xref del campo a cui appartiene il widget: i widget dello stesso gruppo (radio,
    # o lo stesso campo mostrato due volte) cambiano insieme.
    group: int = 0

    @property
    def fillable(self) -> bool:
        """Si può compilare (i campi firma e i pulsanti no, quelli in sola lettura nemmeno)."""
        return not self.readonly and self.type in (TEXT, CHECKBOX, RADIO, COMBO, LIST)

    @property
    def checked(self) -> bool:
        return self.on_state is not None and self.value == self.on_state


@dataclass
class PdfDocument:
    path: str
    password: str | None
    # Dimensioni delle pagine in punti PDF, con la rotazione della pagina già applicata.
    page_sizes: list[tuple[float, float]]
    repaired: bool
    fields: list[FormField] = field(default_factory=list)
    xfa: bool = False  # modulo XFA: i campi non si leggono


def open_document(path: str, password: str | None = None) -> PdfDocument:
    """Apre il file solo per leggerne la struttura; il render avviene nel processo dedicato."""
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        raise OpenError(str(e)) from e

    try:
        if doc.needs_pass:
            if password is None:
                raise PasswordRequired(wrong=False)
            if not doc.authenticate(password):
                raise PasswordRequired(wrong=True)
        if doc.page_count == 0:
            raise OpenError("Il documento non contiene pagine.")
        sizes = []
        broken = 0
        for i in range(doc.page_count):
            try:
                rect = doc[i].rect
                sizes.append((rect.width, rect.height))
            except Exception:
                # Una pagina illeggibile non deve impedire di leggere le altre:
                # si tiene il posto con la misura della precedente (o A4).
                broken += 1
                sizes.append(sizes[-1] if sizes else A4)
        if broken == doc.page_count:
            raise OpenError("Nessuna pagina leggibile.")
        xfa = _has_xfa(doc)
        fields = [] if xfa else _read_fields(doc)
        return PdfDocument(path, password, sizes, doc.is_repaired or broken > 0, fields, xfa)
    finally:
        # Chiudere subito evita di tenere il file bloccato su Windows.
        doc.close()


def _has_xfa(doc) -> bool:
    try:
        return bool(doc.is_form_pdf) and doc.xref_get_key(doc.pdf_catalog(), "AcroForm/XFA")[0] != "null"
    except Exception:
        return False


def _read_fields(doc) -> list[FormField]:
    """Campi di tutte le pagine; un widget illeggibile si salta senza perdere gli altri."""
    if not doc.is_form_pdf:
        return []
    fields = []
    for index in range(doc.page_count):
        try:
            page = doc[index]
            widgets = list(page.widgets())
        except Exception:
            continue
        for widget in widgets:
            try:
                fields.append(_read_field(doc, page, index, widget))
            except Exception:
                pass
    # Il valore di un gruppo radio è lo stato del pulsante acceso (se c'è).
    groups: dict[int, str] = {}
    for f in fields:
        if f.type == RADIO and f.value not in ("", OFF):
            groups[f.group] = f.value
    for f in fields:
        if f.type == RADIO:
            f.value = groups.get(f.group, OFF)
    return fields


def _read_field(doc, page, index: int, widget) -> FormField:
    flags = widget.field_flags or 0
    kind = widget.field_type
    on_state = None
    if kind in (CHECKBOX, RADIO):
        on_state = str(widget.on_state())
        value = widget.field_value
        value = on_state if value is True or value == on_state else OFF
    else:
        value = "" if widget.field_value in (None, False) else str(widget.field_value)
    choices = []
    for choice in widget.choice_values or []:
        if isinstance(choice, (list, tuple)):
            choices.append((str(choice[0]), str(choice[1])))
        else:
            choices.append((str(choice), str(choice)))
    group = widget.xref
    if doc.xref_get_key(widget.xref, "T")[0] == "null":
        kind_parent, parent = doc.xref_get_key(widget.xref, "Parent")
        if kind_parent == "xref":
            group = int(parent.split()[0])
    align = doc.xref_get_key(widget.xref, "Q")
    rect = widget.rect * page.rotation_matrix
    return FormField(
        xref=widget.xref,
        page=index,
        name=widget.field_name or "",
        type=kind,
        rect=(rect.x0, rect.y0, rect.x1, rect.y1),
        value=value,
        choices=choices,
        readonly=bool(flags & pymupdf.PDF_FIELD_IS_READ_ONLY),
        multiline=kind == TEXT and bool(flags & pymupdf.PDF_TX_FIELD_IS_MULTILINE),
        password=kind == TEXT and bool(flags & pymupdf.PDF_TX_FIELD_IS_PASSWORD),
        editable=kind == COMBO and bool(flags & pymupdf.PDF_CH_FIELD_IS_EDIT),
        max_len=widget.text_maxlen or 0 if kind == TEXT else 0,
        font_size=widget.text_fontsize or 0.0,
        align=int(align[1]) if align[0] == "int" else 0,
        on_state=on_state,
        group=group,
    )


def set_field_value(page, xref: int, value: str) -> None:
    """Scrive il valore di un widget della pagina e ne rigenera l'aspetto.

    Usato dal processo di render (documento in memoria) e dal salvataggio. Per testo e
    scelte si passa da MuPDF senza `Widget.update()`, che riscriverebbe il font (/DA) con
    Helvetica; per caselle e radio si accende lo stato giusto in tutti i widget del gruppo.
    """
    doc = page.parent
    widget = page.load_widget(xref)
    if widget is None:
        raise ValueError(f"Campo {xref} non trovato a pagina {page.number + 1}")
    if widget.field_type in (CHECKBOX, RADIO):
        _set_button(doc, widget, value)
        return
    from pymupdf import mupdf

    annot = widget._annot.this
    obj = mupdf.pdf_annot_obj(annot)
    mupdf.pdf_set_field_value(mupdf.pdf_get_bound_document(obj), obj, value, 1)
    if widget.field_type in (COMBO, LIST):
        mupdf.pdf_dict_del(obj, mupdf.pdf_new_name("I"))  # indice della scelta: vale il nuovo /V
    mupdf.pdf_update_annot(annot)


def _set_button(doc, widget, value: str) -> None:
    kids = [widget.xref]
    owner = widget.xref
    if doc.xref_get_key(widget.xref, "T")[0] == "null":
        kind, parent = doc.xref_get_key(widget.xref, "Parent")
        if kind == "xref":
            owner = int(parent.split()[0])
            kind, array = doc.xref_get_key(owner, "Kids")
            if kind == "array":
                kids = [int(x) for x in array[1:-1].replace("0 R", "").split()]
    name = OFF if value in ("", OFF) else value
    for kid in kids:
        # Si accende solo il widget che ha quello stato: gli altri pulsanti del gruppo si spengono.
        doc.xref_set_key(kid, "AS", f"/{name}" if name in _states(doc, kid) else f"/{OFF}")
    doc.xref_set_key(owner, "V", f"/{name}")


def _states(doc, xref: int) -> set[str]:
    """Nomi degli aspetti normali (/AP /N) di un pulsante, come `Widget.button_states()`."""
    kind, value = doc.xref_get_key(xref, "AP/N")
    if kind == "xref":
        value = doc.xref_object(int(value.split()[0]))
    elif kind != "dict":
        return set()
    return {part.split()[0].rstrip(">") for part in value.split("/")[1:] if part.split()}
