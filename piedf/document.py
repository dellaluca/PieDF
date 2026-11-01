import math
import re
from dataclasses import dataclass, field

import pymupdf

from piedf import fonts
from piedf.fonts import EmbeddedFont
from piedf.ink import bounds as ink_bounds

A4 =(595.0, 842.0)
MAX_TOC = 20_000  # voci dell'indice: oltre si tagliano (un indice ciclico non finirebbe mai)
MAX_TOC_LEVEL = 50
OFF = "Off"  # valore di una casella o di un gruppo radio senza spunta

# Annotazioni modificabili di PieDF: si riconoscono dal titolo (/T).
PIEDF_TITLE = "PieDF"
FONTS = ("Helv", "TiRo", "Cour")  # Helvetica, Times, Courier: font standard PDF, niente da incorporare
LINE_HEIGHT = 1.2  # interlinea di MuPDF nelle FreeText, in multipli della dimensione del font

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


@dataclass(eq=False)  # confronto per identità, come Placement
class Annotation:
    """Annotazione modificabile di PieDF (titolo "PieDF"). Base comune: il disegno (#22) aggiunge Ink."""

    page: int  # indice da 0
    # Punti PDF (x0, y0, x1, y1), pagina come si vede: lo stesso spazio di `Placement.rect`.
    rect: tuple[float, float, float, float]
    xref: int = 0  # 0 = non ancora nel file


@dataclass(eq=False)
class TextBox(Annotation):
    """Casella di testo, salvata come annotazione FreeText."""

    subtype = "FreeText"
    text: str = ""
    font: str = "Helv"  # uno di FONTS, oppure la famiglia di un font installato (#59)
    size: float = 11.0
    color: str = "#000000"
    align: int = 0  # 0 sinistra, 1 centro, 2 destra
    # Font già incorporato nel file (riletto alla riapertura): si riusa se ha tutti i caratteri.
    embedded: EmbeddedFont | None = None


@dataclass(eq=False)
class Ink(Annotation):
    """Tratto a mano libera (penna o evidenziatore), salvato come annotazione Ink.

    `rect` contiene i punti più metà spessore (quello che si vede): si ricalcola con `ink_rect`.
    """

    subtype = "Ink"
    # Punti PDF (x, y), pagina come si vede, già levigati e semplificati. Un tratto di PieDF
    # è una Ink sola con un tratto; una Ink con più tratti si muove e si cancella tutta insieme.
    strokes: tuple[tuple[tuple[float, float], ...], ...] = ()
    color: str = "#000000"
    width: float = 2.0  # punti
    opacity: float = 1.0  # 0,35 per l'evidenziatore


def ink_rect(strokes, width: float) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = ink_bounds(strokes)
    r = width / 2
    return (x0 - r, y0 - r, x1 + r, y1 + r)


@dataclass(frozen=True)
class TocEntry:
    """Voce dell'indice (#6): livello da 1, destinazione nello spazio della pagina come si vede."""

    level: int
    title: str
    page: int  # da 0; -1 = nessuna destinazione nel documento
    y: float | None = None  # punti PDF nella pagina; None = in cima


@dataclass
class PdfDocument:
    path: str
    password: str | None
    # Dimensioni delle pagine in punti PDF, con la rotazione della pagina già applicata.
    page_sizes: list[tuple[float, float]]
    repaired: bool
    fields: list[FormField] = field(default_factory=list)
    xfa: bool = False  # modulo XFA: i campi non si leggono
    annotations: list[Annotation] = field(default_factory=list)  # solo quelle di PieDF
    toc: list[TocEntry] = field(default_factory=list)  # indice (segnalibri), in ordine


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
        annotations = _read_annotations(doc)
        toc = _read_toc(doc, sizes)
        return PdfDocument(path, password, sizes, doc.is_repaired or broken > 0, fields, xfa, annotations, toc)
    finally:
        # Chiudere subito evita di tenere il file bloccato su Windows.
        doc.close()


def _read_toc(doc, sizes: list[tuple[float, float]]) -> list[TocEntry]:
    """Indice del PDF. Come per i link, `doc.resolve_link` dà la destinazione già nello spazio
    della pagina come si vede (get_toc lascia le coordinate così come sono scritte)."""
    entries = []

    def walk(item, level: int) -> None:
        while item is not None and len(entries) < MAX_TOC:
            page, y = -1, None
            try:
                if item.uri and not item.is_external:
                    page, _, y = doc.resolve_link(item.uri)
                    y = None if math.isnan(y) or not 0 <= page < len(sizes) else max(0.0, min(y, sizes[page][1]))
            except Exception:
                page, y = -1, None
            title = " ".join((item.title or "").split())
            entries.append(TocEntry(level, title, page if 0 <= page < len(sizes) else -1, y))
            if item.down is not None and level < MAX_TOC_LEVEL:
                walk(item.down, level + 1)
            item = item.next

    try:
        walk(doc.outline, 1)
    except Exception:
        pass  # un indice rotto non impedisce di leggere il documento
    return entries


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


# --- Annotazioni di PieDF ---------------------------------------------------------------


PIEDF_TYPES = (pymupdf.PDF_ANNOT_FREE_TEXT, pymupdf.PDF_ANNOT_INK)


def is_piedf_annot(annot) -> bool:
    """Annotazione creata da PieDF (e quindi modificabile qui): caselle di testo e tratti."""
    return annot.type[0] in PIEDF_TYPES and annot.info.get("title") == PIEDF_TITLE


def piedf_annots(page) -> list:
    return [a for a in page.annots() if is_piedf_annot(a)]


def _read_annotations(doc) -> list[Annotation]:
    """Annotazioni di PieDF di tutte le pagine; quelle di altri programmi restano nel render."""
    items = []
    embedded: dict[int, tuple[str, set[int], list[TextBox]]] = {}  # xref del font → famiglia, glifi, caselle
    for index in range(doc.page_count):
        try:
            # Senza /Annots la pagina non si carica nemmeno: veloce anche con centinaia di pagine.
            if doc.xref_get_key(doc.page_xref(index), "Annots")[0] == "null":
                continue
            page = doc[index]
            annots = piedf_annots(page)
        except Exception:
            continue
        for annot in annots:
            try:
                if annot.type[0] == pymupdf.PDF_ANNOT_INK:
                    items.append(_read_ink(page, index, annot))
                else:
                    box = _read_text_box(doc, page, index, annot)
                    items.append(box)
                    if box.font not in FONTS:
                        _note_font(doc, annot, box, embedded)
            except Exception:
                pass
    # Un font incorporato può servire più caselle: si legge una volta, con i glifi di tutte.
    for xref, (family, used, boxes) in embedded.items():
        font = fonts.read_embedded(doc, xref, family, used)
        for box in boxes:
            box.embedded = font
    return items


def _note_font(doc, annot, box: TextBox, embedded: dict) -> None:
    """Annota il font incorporato nell'aspetto della casella e i glifi che usa."""
    kind, ap = doc.xref_get_key(annot.xref, "AP/N")
    if kind != "xref":
        return
    ap = int(ap.split()[0])
    kind, ref = doc.xref_get_key(ap, f"Resources/Font/{fonts.RESOURCE}")
    if kind != "xref":
        return
    family, used, boxes = embedded.setdefault(int(ref.split()[0]), (box.font, set(), []))
    if family == box.font:
        used |= fonts.used_glyphs(doc.xref_stream(ap) or b"")
        boxes.append(box)


def _read_ink(page, index: int, annot) -> Ink:
    m = page.rotation_matrix
    strokes = tuple(
        tuple((q.x, q.y) for q in (pymupdf.Point(p) * m for p in stroke))
        for stroke in annot.vertices or () if stroke
    )
    if not strokes:
        raise ValueError("Ink senza punti")
    stroke = annot.colors.get("stroke") or (0.0, 0.0, 0.0)
    width = annot.border.get("width", -1)
    width = width if width and width > 0 else 1.0
    opacity = annot.opacity
    # PyMuPDF legge /CA come float a 32 bit (0,35 → 0,3499…): bastano due decimali.
    opacity = round(opacity, 2) if 0 <= opacity < 1 else 1.0
    return Ink(
        page=index,
        rect=ink_rect(strokes, width),
        xref=annot.xref,
        strokes=strokes,
        color=_hex(stroke),
        width=width,
        opacity=opacity,
    )


def _read_text_box(doc, page, index: int, annot) -> TextBox:
    da = doc.xref_get_key(annot.xref, "DA")[1]
    font_match = re.search(r"/(\w+)\s+([\d.]+)\s+Tf", da)
    font = font_match.group(1) if font_match and font_match.group(1) in FONTS else FONTS[0]
    size = float(font_match.group(2)) if font_match else 11.0
    kind, ds = doc.xref_get_key(annot.xref, "DS")
    family = fonts.style_family(ds) if kind == "string" else None
    if family and font_match and font_match.group(1) == fonts.RESOURCE:
        font = family  # font da file, incorporato nell'aspetto (#59)
    q = doc.xref_get_key(annot.xref, "Q")
    rect = annot.rect * page.rotation_matrix
    return TextBox(
        page=index,
        rect=(rect.x0, rect.y0, rect.x1, rect.y1),
        xref=annot.xref,
        text=annot.info.get("content", ""),
        font=font,
        size=size,
        color=_da_color(da),
        align=int(q[1]) if q[0] == "int" and q[1] in ("0", "1", "2") else 0,
    )


def _da_color(da: str) -> str:
    rgb = re.search(r"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+rg", da)
    if rgb:
        values = [float(v) for v in rgb.groups()]
    else:
        gray = re.search(r"([\d.]+)\s+g\b", da)
        values = [float(gray.group(1))] * 3 if gray else [0.0] * 3
    return _hex(values)


def _hex(values) -> str:
    if len(values) == 1:  # grigio
        values = list(values) * 3
    elif len(values) != 3:  # CMYK o altro: nero
        values = [0.0] * 3
    return "#" + "".join(f"{max(0, min(255, round(v * 255))):02x}" for v in values)


def _rgb(color: str) -> tuple[float, float, float]:
    return tuple(int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))


def add_text_box(page, box: TextBox, font_xref: int = 0):
    """Scrive la casella come FreeText di PieDF sulla pagina (rettangolo come si vede, testo dritto).

    Con un font da file (#59) l'aspetto lo riscrive PieDF col font incorporato: `font_xref` è
    il /Font già nel documento (da `embed_fonts`); 0 = si incorpora qui il font intero, che va
    bene solo per l'immagine della vista.
    """
    x0, y0, x1, y1 = box.rect
    face = fonts.resolve(box.font, box.embedded, box.text)
    annot = page.add_freetext_annot(
        pymupdf.Rect(x0, y0, x1, y1) * page.derotation_matrix,  # come le firme in writer.py
        box.text,
        fontsize=box.size,
        fontname=box.font if box.font in FONTS else FONTS[0],
        text_color=_rgb(box.color),
        align=box.align,
        rotate=page.rotation,  # testo dritto sulle pagine ruotate
    )
    annot.set_info(title=PIEDF_TITLE)
    annot.set_flags(pymupdf.PDF_ANNOT_IS_PRINT)
    # PyMuPDF scrive una linea di richiamo (/CL) anche senza `callout`: qualche lettore la disegna.
    page.parent.xref_set_key(annot.xref, "CL", "null")
    if face is not None:
        _write_font_appearance(page, annot, box, face, font_xref or fonts.full_font(page.parent, face))
    return annot


def _write_font_appearance(page, annot, box: TextBox, face, font_xref: int) -> None:
    """Sostituisce il testo dell'aspetto generato da MuPDF (Helvetica) con quello nel font scelto.

    Del suo aspetto restano BBox, Matrix, ritaglio e rotazione; cambiano solo il blocco BT…ET
    e le risorse. /DA nomina la risorsa del font, /DS la famiglia per la riapertura.
    """
    doc = page.parent
    from pymupdf import mupdf

    # Prima l'aspetto delle modifiche in sospeso (titolo, flag): dopo, MuPDF non deve
    # rigenerarlo da /DA, dove il font non lo conosce.
    mupdf.pdf_update_annot(annot.this)
    ap = int(doc.xref_get_key(annot.xref, "AP/N")[1].split()[0])
    stream = doc.xref_stream(ap).decode("latin-1")
    start, end = stream.find("BT"), stream.rfind("ET")
    x0, y0, x1, y1 = box.rect
    block = fonts.appearance_text(face, box.text, box.size, x1 - x0, y1 - y0, _rgb(box.color), box.align)
    doc.update_stream(ap, (stream[:start] + block + stream[end + 2:]).encode("latin-1"))
    doc.xref_set_key(ap, "Resources", f"<</Font<</{fonts.RESOURCE} {font_xref} 0 R>>>>")
    r, g, b = _rgb(box.color)
    doc.xref_set_key(annot.xref, "DA", pymupdf.get_pdf_str(f"{r:g} {g:g} {b:g} rg /{fonts.RESOURCE} {box.size:g} Tf"))
    doc.xref_set_key(annot.xref, "DS", pymupdf.get_pdf_str(fonts.style_string(box.font, box.size, box.color)))


def embed_fonts(doc, boxes) -> dict[int, int]:
    """Font da incorporare per le caselle da scrivere nel file: id(casella) → xref del /Font.

    Una casella riusa il font già incorporato se ha tutti i caratteri; le altre di una stessa
    famiglia condividono un solo subset con i caratteri di tutte.
    """
    result, texts = {}, {}
    for box in boxes:
        if box.font in FONTS:
            continue
        face = fonts.resolve(box.font, box.embedded, box.text)
        if face is None:
            continue
        if face.embedded is not None and fonts.reusable(doc, face.embedded):
            result[id(box)] = face.embedded.xref
        else:
            texts.setdefault(face, []).append(box)
    for face, group in texts.items():
        xref = fonts.subset_font(doc, face, "".join(sorted(set("".join(b.text for b in group)))))
        for box in group:
            result[id(box)] = xref
    return result


def add_ink(page, ink: Ink):
    """Scrive il tratto come Ink di PieDF sulla pagina (punti come si vede)."""
    m = page.derotation_matrix
    strokes = []
    for stroke in ink.strokes:
        # Liste di coppie di float: con oggetti Point PyMuPDF solleva ValueError.
        points = [(q.x, q.y) for q in (pymupdf.Point(x, y) * m for x, y in stroke)]
        if len(points) == 1:
            points.append(points[0])  # un puntino: MuPDF lo disegna come segmento nullo con estremi tondi
        strokes.append(points)
    annot = page.add_ink_annot(strokes)
    annot.set_border(width=ink.width)
    annot.set_colors(stroke=_rgb(ink.color))
    if ink.opacity < 1:
        annot.set_opacity(ink.opacity)
    annot.set_info(title=PIEDF_TITLE)
    annot.set_flags(pymupdf.PDF_ANNOT_IS_PRINT)
    annot.update()
    return annot


def text_box_lines(text: str, font: str, size: float, width: float, embedded: EmbeddedFont | None = None) -> int:
    """Righe del testo a capo automatico nella larghezza data, contate come le impagina MuPDF."""
    face = fonts.resolve(font, embedded, text)
    if face is not None:
        return max(1, len(fonts.break_lines(text, face, size, max(width, 1.0))))
    font = font if font in FONTS else FONTS[0]
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=max(width, 1.0), height=14400)
        annot = page.add_freetext_annot(page.rect, text, fontsize=size, fontname=font)
        ap = doc.xref_get_key(annot.xref, "AP/N")[1]
        stream = doc.xref_stream(int(ap.split()[0]))
        # Un "Td" iniziale e poi uno per riga, anche per le righe vuote.
        return max(1, stream.count(b" Td\n") - 1)
    finally:
        doc.close()


def longest_word(text: str, font: str, size: float, embedded: EmbeddedFont | None = None) -> float:
    """Larghezza in punti della parola più lunga (+1 di margine): sotto, MuPDF la taglierebbe."""
    words = text.split()
    face = fonts.resolve(font, embedded, text)
    if face is not None:
        return max((face.width(w, size) for w in words), default=0.0) + 1.0
    font = font if font in FONTS else FONTS[0]
    return max((pymupdf.get_text_length(w, fontname=font, fontsize=size) for w in words), default=0.0) + 1.0


def text_box_height(text: str, font: str, size: float, width: float, embedded: EmbeddedFont | None = None) -> float:
    """Altezza in punti che contiene tutto il testo: l'altezza della casella segue il testo."""
    return text_box_lines(text, font, size, width, embedded) * LINE_HEIGHT * size
