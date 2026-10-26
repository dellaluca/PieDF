"""Font delle caselle di testo (#59), senza Qt: quelli installati sul PC e quelli già incorporati.

Helvetica, Times e Courier (`STANDARD`) sono i font standard del PDF: l'aspetto lo scrive
MuPDF e non c'è niente da incorporare. Con gli altri font l'aspetto della FreeText lo scrive
PieDF (`document.add_text_box`): testo impaginato come MuPDF (`break_lines`), font
incorporato in subset (`subset_font`), nome della famiglia in /DS per riconoscerlo alla
riapertura. `richtext` di MuPDF non va bene: ignora i font da file (e il colore).

Elenco dei font: i file .ttf/.otf delle cartelle dei font, letti una volta sola con `struct`
(tabelle name, OS/2, head; cache in memoria). Restano fuori i font che non permettono di essere
incorporati (OS/2 fsType 0x0002 o 0x0200), quelli senza contorni (bitmap, emoji a colori)
e le raccolte .ttc.
"""

import os
import re
import struct
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf
from pymupdf import mupdf

STANDARD = {"Helv": "Helvetica", "TiRo": "Times", "Cour": "Courier"}
EXTENSIONS = (".ttf", ".otf")
RESTRICTED = 0x0002  # fsType: incorporamento vietato (restricted license embedding)
BITMAP_ONLY = 0x0200  # fsType: si possono incorporare solo le bitmap
RESOURCE = "PieDF"  # nome del font nelle risorse dell'aspetto e in /DA
FACE_CACHE = 16  # font aperti tenuti in memoria


@dataclass(frozen=True)
class FontInfo:
    """Quello che serve di un file di font per l'elenco."""

    families: tuple[tuple[str, str], ...]  # (famiglia, stile): name 1/2 e 16/17, tutte le lingue
    italic: bool
    bold: bool
    weight: int
    embeddable: bool
    outlines: bool


def font_info(path) -> FontInfo | None:
    """Legge famiglia, stile e permessi di un .ttf/.otf; None se non è un font leggibile."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if len(head) < 12 or head[:4] not in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
                return None  # anche le raccolte (ttcf)
            count = struct.unpack(">H", head[4:6])[0]
            records = f.read(16 * count)
            tables = {}
            for i in range(len(records) // 16):
                tag, _, offset, length = struct.unpack(">4sIII", records[16 * i:16 * i + 16])
                tables[tag] = (offset, length)

            def table(tag, limit=None):
                if tag not in tables:
                    return b""
                offset, length = tables[tag]
                f.seek(offset)
                return f.read(min(length, limit) if limit else length)

            os2 = table(b"OS/2", 64)
            name = table(b"name", 1 << 20)
            mac_style = table(b"head", 46)[44:46]
    except OSError:
        return None
    fs_type = struct.unpack(">H", os2[8:10])[0] if len(os2) >= 10 else 0
    weight = struct.unpack(">H", os2[4:6])[0] if len(os2) >= 6 else 400
    if len(os2) >= 64:
        selection = struct.unpack(">H", os2[62:64])[0]
        italic, bold = bool(selection & 0x01), bool(selection & 0x20)
    else:
        style = struct.unpack(">H", mac_style)[0] if len(mac_style) == 2 else 0
        italic, bold = bool(style & 0x02), bool(style & 0x01)
    return FontInfo(
        families=_families(name),
        italic=italic,
        bold=bold,
        weight=weight,
        embeddable=not fs_type & (RESTRICTED | BITMAP_ONLY),
        outlines=any(tag in tables for tag in (b"glyf", b"CFF ", b"CFF2")),
    )


def _families(data: bytes) -> tuple[tuple[str, str], ...]:
    """Coppie (famiglia, stile) della tabella name: 1/2 (GDI) e 16/17 (tipografiche)."""
    if len(data) < 6:
        return ()
    count, base = struct.unpack(">HH", data[2:6])
    names: dict[tuple[int, int], str] = {}  # (lingua, nameID) → testo
    for i in range(count):
        record = data[6 + 12 * i:18 + 12 * i]
        if len(record) < 12:
            break
        platform, encoding, language, name_id, length, offset = struct.unpack(">6H", record)
        if name_id not in (1, 2, 16, 17):
            continue
        raw = data[base + offset:base + offset + length]
        if platform == 3 and encoding in (0, 1, 10):
            text = raw.decode("utf-16-be", "replace")
        elif platform == 1 and encoding == 0:
            text, language = raw.decode("mac_roman", "replace"), -1 - language
        else:
            continue
        names.setdefault((language, name_id), text.strip())
    pairs = []
    for (language, name_id), family in names.items():
        if name_id in (1, 16) and family:
            style = names.get((language, name_id + 1)) or names.get((0x409, name_id + 1)) or ""
            if (family, style) not in pairs:
                pairs.append((family, style))
    return tuple(pairs)


def font_dirs() -> list[Path]:
    """Cartelle dei font installati: per tutti e (Windows 10 1809+) solo per l'utente."""
    if sys.platform == "win32":
        dirs = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
        return dirs
    home = Path.home()
    return [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), home / ".local/share/fonts", home / ".fonts"]


_info_cache: dict[str, tuple[tuple[int, int], FontInfo | None]] = {}  # percorso → ((mtime, size), info)
_installed: dict[str, str] | None = None
_lock = threading.Lock()
_preload: threading.Thread | None = None


def scan(dirs) -> dict[str, str]:
    """Famiglia → file del suo stile normale, solo per i font che si possono incorporare.

    Di ogni famiglia vince il file meno corsivo, meno grassetto e col peso più vicino a 400;
    se quel file non si può incorporare, la famiglia resta fuori.
    """
    best: dict[str, tuple[tuple, str, FontInfo]] = {}
    for folder in dirs:
        for root, _, files in os.walk(folder):
            for name in sorted(files):
                if not name.lower().endswith(EXTENSIONS):
                    continue
                path = os.path.join(root, name)
                info = _cached_info(path)
                if info is None:
                    continue
                for family, style in info.families:
                    score = (info.italic, info.bold, abs(info.weight - 400), not _regular_name(style))
                    if family not in best or score < best[family][0]:
                        best[family] = (score, path, info)
    return {family: path for family, (_, path, info) in best.items() if info.embeddable and info.outlines}


def _regular_name(style: str) -> bool:
    return style.casefold() in ("regular", "normal", "book", "roman", "standard", "")


def _cached_info(path: str) -> FontInfo | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    cached = _info_cache.get(path)
    if cached is None or cached[0] != key:
        cached = (key, font_info(path))
        _info_cache[path] = cached
    return cached[1]


def installed() -> dict[str, str]:
    """Font installati (famiglia → file), letti la prima volta che servono (o da `preload`)."""
    global _installed
    with _lock:
        if _installed is None:
            _installed = scan(font_dirs())
        return _installed


def ready() -> bool:
    """L'elenco dei font è già pronto: `installed()` non blocca."""
    return _installed is not None


def preload() -> None:
    """Legge i font in un thread, così l'elenco è pronto quando serve senza fermare la GUI."""
    global _preload
    if _preload is None and _installed is None:
        _preload = threading.Thread(target=installed, name="piedf-fonts", daemon=True)
        _preload.start()


# --- Font da usare per impaginare e scrivere ----------------------------------------------


@dataclass(eq=False)
class EmbeddedFont:
    """Font già incorporato da PieDF in una casella del file aperto (anche se non installato qui)."""

    family: str
    xref: int  # oggetto /Font (Type0) nel file
    base_font: str  # /BaseFont con il prefisso del subset: riconosce l'oggetto nel documento
    data: bytes = field(repr=False)  # programma del font (subset: i glifi non usati sono vuoti)
    cmap: dict[str, int] = field(repr=False)  # carattere → glifo, dalla /ToUnicode
    chars: set[str] = field(default_factory=set, repr=False)  # caratteri con il glifo: quelli delle caselle che lo usano


class Face:
    """Font aperto con MuPDF: glifi e larghezze dei caratteri (in em)."""

    def __init__(self, font, cmap: dict[str, int] | None = None, embedded: EmbeddedFont | None = None) -> None:
        self.font = font  # mupdf.FzFont
        self.cmap = cmap
        self.embedded = embedded
        self._gids: dict[str, int] = {}
        self._advances: dict[str, float] = {}

    def gid(self, ch: str) -> int:
        gid = self._gids.get(ch)
        if gid is None:
            if self.cmap is not None:
                gid = self.cmap.get(ch, 0)
            else:
                gid = mupdf.fz_encode_character(self.font, ord(ch))
            self._gids[ch] = gid
        return gid

    def advance(self, ch: str) -> float:
        adv = self._advances.get(ch)
        if adv is None:
            adv = self._advances[ch] = mupdf.fz_advance_glyph(self.font, self.gid(ch), 0)
        return adv

    def width(self, text: str, size: float) -> float:
        return sum(self.advance(ch) for ch in text) * size


_faces: dict[object, Face] = {}


def _face(key, load) -> Face:
    face = _faces.get(key)
    if face is None:
        if len(_faces) >= FACE_CACHE:
            _faces.clear()
        face = _faces[key] = load()
    return face


def file_face(path: str) -> Face:
    return _face(path, lambda: Face(mupdf.fz_new_font_from_file(None, path, 0, 0)))


def embedded_face(embedded: EmbeddedFont) -> Face:
    return _face(id(embedded), lambda: Face(
        mupdf.fz_new_font_from_buffer(None, mupdf.fz_new_buffer_from_copied_data(embedded.data), 0, 0),
        embedded.cmap, embedded,
    ))


def covers(embedded: EmbeddedFont | None, family: str, text: str) -> bool:
    """Il font incorporato è di quella famiglia e ha tutti i caratteri del testo."""
    return embedded is not None and embedded.family == family and set(text) - {"\n"} <= embedded.chars


def resolve(family: str, embedded: EmbeddedFont | None, text: str) -> Face | None:
    """Font con cui impaginare e scrivere il testo; None = font standard (Helvetica se non si può).

    Prima quello già incorporato, se ha tutti i caratteri (il file non cresce e l'aspetto non
    cambia), poi il file installato, infine quello incorporato anche senza tutti i caratteri.
    """
    if family in STANDARD:
        return None
    if covers(embedded, family, text):
        return embedded_face(embedded)
    path = installed().get(family)
    if path is not None:
        try:
            return file_face(path)
        except Exception:
            pass
    if embedded is not None and embedded.family == family:
        return embedded_face(embedded)
    return None


def available(family: str, embedded: EmbeddedFont | None, text: str) -> bool:
    """Il testo si può scrivere in quel font (standard, installato o incorporato con tutti i caratteri)."""
    return family in STANDARD or family in installed() or covers(embedded, family, text)


# --- Impaginazione -----------------------------------------------------------------------


def break_lines(text: str, face: Face, size: float, width: float) -> list[tuple[str, float]]:
    """Righe (testo, larghezza) come le impagina MuPDF nelle FreeText.

    A capo solo dopo uno spazio, quando la riga supera la larghezza; la larghezza della riga
    spezzata non conta lo spazio finale. Una parola più larga della casella resta intera
    (la taglia il bordo). Verificato carattere per carattere sull'aspetto di MuPDF con Helvetica.
    """
    lines = []
    for para in text.split("\n"):
        while True:
            w, wrap, wrap_w = 0.0, None, 0.0
            for i, ch in enumerate(para):
                if ch == " ":
                    wrap, wrap_w = i + 1, w
                w += face.advance(ch) * size
                if w > width and wrap is not None:
                    break
            else:
                lines.append((para, w))
                break
            lines.append((para[:wrap], wrap_w))
            para = para[wrap:]
            if not para:
                break
    return lines


# --- Scrittura nel PDF -------------------------------------------------------------------


def subset_font(doc, face: Face, text: str) -> int:
    """Incorpora in `doc` il font con i soli glifi del testo e restituisce l'xref del /Font.

    Il subset lo fa MuPDF (`subset_fonts`) in un documento a parte, da cui si copia il font:
    così i font del documento non si toccano.
    """
    scratch = pymupdf.open()
    try:
        page = scratch.new_page()
        font = mupdf.pdf_add_cid_font(pymupdf._as_pdf_document(scratch), face.font)
        scratch.xref_set_key(page.xref, "Resources", f"<</Font<</F {mupdf.pdf_to_num(font)} 0 R>>>>")
        contents = scratch.get_new_xref()
        scratch.update_object(contents, "<<>>")
        scratch.update_stream(contents, f"BT /F 1 Tf <{encode(face, text)}> Tj ET".encode())
        scratch.xref_set_key(page.xref, "Contents", f"{contents} 0 R")
        try:
            scratch.subset_fonts()
        except Exception:
            pass  # senza subset il font resta intero: il testo si vede comunque giusto
        ref = mupdf.pdf_dict_getp(mupdf.pdf_load_object(pymupdf._as_pdf_document(scratch), page.xref),
                                  "Resources/Font/F")
        copied = mupdf.pdf_graft_object(pymupdf._as_pdf_document(doc), ref)
        return mupdf.pdf_to_num(copied)
    finally:
        scratch.close()


def full_font(doc, face: Face) -> int:
    """Incorpora il font intero (per l'immagine della casella nella vista, mai nel file salvato)."""
    return mupdf.pdf_to_num(mupdf.pdf_add_cid_font(pymupdf._as_pdf_document(doc), face.font))


def reusable(doc, embedded: EmbeddedFont | None) -> bool:
    """L'oggetto del font incorporato è ancora quello, in questo documento (il file sorgente)."""
    if embedded is None or not 0 < embedded.xref < doc.xref_length():
        return False
    try:
        return doc.xref_get_key(embedded.xref, "BaseFont") == ("name", "/" + embedded.base_font)
    except Exception:
        return False


def encode(face: Face, text: str) -> str:
    """Testo come glifi in esadecimale (font Type0 con Identity-H: codice = glifo)."""
    return "".join(f"{face.gid(ch):04x}" for ch in text if ch != "\n")


def appearance_text(face: Face, text: str, size: float, width: float, height: float,
                    color: tuple[float, float, float], align: int) -> str:
    """Blocco BT…ET dell'aspetto, con le stesse posizioni che usa MuPDF per i font standard.

    Prima linea di base a `altezza + 0,4 × dimensione − 1,2 × dimensione` dal fondo, poi una
    riga ogni 1,2 × dimensione; centro e destra con la larghezza della riga.
    """
    out = ["BT", "%g %g %g rg" % color, f"/{RESOURCE} {size:g} Tf", f"0 {height + 0.4 * size:g} Td"]
    x = 0.0
    for line, w in break_lines(text, face, size, width):
        dx = (width - w) / 2 if align == 1 else width - w if align == 2 else 0.0
        out.append(f"{dx - x:g} {-1.2 * size:g} Td")
        x = dx
        if line:
            out.append(f"<{encode(face, line)}> Tj")
    out.append("ET")
    return "\n".join(out)


# --- Lettura -----------------------------------------------------------------------------


def style_string(family: str, size: float, color: str) -> str:
    """Valore di /DS: il nome della famiglia per riconoscere il font alla riapertura."""
    family = family.replace('"', "")
    return f'font-family: "{family}"; font-size: {size:g}pt; color: {color}'


def style_family(ds: str) -> str | None:
    match = re.search(r'font-family:\s*"([^"]+)"', ds or "")
    return match.group(1) if match else None


def used_glyphs(stream: bytes) -> set[int]:
    """Glifi scritti da `appearance_text` in uno stream dell'aspetto: quelli presenti nel subset."""
    return {int(h[i:i + 4], 16) for h in re.findall(rb"<([0-9a-fA-F]+)> Tj", stream) for i in range(0, len(h), 4)}


def read_embedded(doc, xref: int, family: str, used: set[int]) -> EmbeddedFont | None:
    """Il font Type0 `xref` scritto da PieDF: programma del font e mappa carattere → glifo.

    `used` sono i glifi scritti dalle caselle che lo usano: solo quelli ci sono davvero nel
    subset (la /ToUnicode copre ancora tutto il font), e solo i loro caratteri si possono riusare.
    """
    try:
        kind, base = doc.xref_get_key(xref, "BaseFont")
        kind_d, descendants = doc.xref_get_key(xref, "DescendantFonts")
        if kind != "name" or kind_d != "array":
            return None
        cid = int(descendants.strip("[] ").split()[0])
        descriptor = int(doc.xref_get_key(cid, "FontDescriptor")[1].split()[0])
        data = None
        for key in ("FontFile2", "FontFile3", "FontFile"):
            kind, value = doc.xref_get_key(descriptor, key)
            if kind == "xref":
                data = doc.xref_stream(int(value.split()[0]))
                break
        kind, value = doc.xref_get_key(xref, "ToUnicode")
        if not data or kind != "xref":
            return None
        cmap = parse_to_unicode(doc.xref_stream(int(value.split()[0])), used)
        cmap = {ch: gid for ch, gid in cmap.items() if gid in used}
        return EmbeddedFont(family, xref, base[1:], data, cmap, set(cmap))
    except Exception:
        return None


def parse_to_unicode(data: bytes, prefer: set[int] = frozenset()) -> dict[str, int]:
    """Carattere → codice (= glifo con Identity-H) da una CMap /ToUnicode (bfchar e bfrange).

    Con più glifi per lo stesso carattere vince il primo, o uno di quelli in `prefer`.
    """
    text = data.decode("latin-1")
    cmap: dict[str, int] = {}

    def put(code: int, uni: str) -> None:
        try:
            ch = bytes.fromhex(uni).decode("utf-16-be")
        except ValueError:
            return
        if len(ch) == 1 and (ch not in cmap or code in prefer and cmap[ch] not in prefer):
            cmap[ch] = code

    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for code, uni in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            put(int(code, 16), uni)
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for lo, hi, dest in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]+>|\[[^\]]*\])", block):
            lo, hi = int(lo, 16), int(hi, 16)
            if hi - lo > 0xFFFF:
                continue
            if dest.startswith("["):
                for i, uni in enumerate(re.findall(r"<([0-9A-Fa-f]+)>", dest)[:hi - lo + 1]):
                    put(lo + i, uni)
            else:
                start = int(dest[1:-1], 16)
                digits = len(dest) - 2
                for i in range(hi - lo + 1):
                    put(lo + i, f"{start + i:0{digits}x}")
    return cmap
