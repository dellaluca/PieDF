"""Font delle caselle di testo (#59), senza Qt: quelli installati sul PC e quelli già incorporati.

Helvetica, Times e Courier (`STANDARD`) sono i font standard del PDF: l'aspetto lo scrive
MuPDF e non c'è niente da incorporare. Con gli altri font l'aspetto della FreeText lo scrive
PieDF (`document.add_text_box`): testo impaginato come MuPDF (`break_lines`), font
incorporato in subset (`subset_font`), nome della famiglia in /DS per riconoscerlo alla
riapertura. `richtext` di MuPDF non va bene: ignora i font da file (e il colore).

Elenco dei font: i file .ttf/.otf delle cartelle dei font, letti con `struct` (tabelle name,
OS/2, head). Restano fuori i font che non permettono di essere incorporati (OS/2 fsType 0x0002
o 0x0200), quelli senza contorni (bitmap, emoji a colori) e le raccolte .ttc. Mai all'avvio:
l'elenco si chiede quando serve (`request`) e lo legge un processo "spawn" (`_scan_process`),
perché leggere centinaia di file in Python tiene il GIL e fermava la GUI. Cache su disco in
`<dati>/fonts.json` (percorso → mtime_ns, size, informazioni): dal secondo avvio si rileggono
solo i file nuovi o cambiati.
"""

import json
import multiprocessing
import os
import re
import struct
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pymupdf
from pymupdf import mupdf

STANDARD = {"Helv": "Helvetica", "TiRo": "Times", "Cour": "Courier"}
EXTENSIONS = (".ttf", ".otf")
RESTRICTED = 0x0002  # fsType: incorporamento vietato (restricted license embedding)
BITMAP_ONLY = 0x0200  # fsType: si possono incorporare solo le bitmap
RESOURCE = "PieDF"  # nome del font nelle risorse dell'aspetto e in /DA
FACE_CACHE = 16  # font aperti tenuti in memoria
CACHE_FILE = "fonts.json"
CACHE_VERSION = 1
POLL_S = 0.05  # attesa dell'elenco quando serve subito
PARENT_POLL_S = 1.0  # ogni quanto il processo che legge i font controlla che la GUI ci sia ancora


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


Cache = dict[str, tuple[int, int, "FontInfo | None"]]  # percorso → (mtime_ns, size, informazioni)

_installed: dict[str, str] | None = None
_job = None  # (processo, lato in lettura della pipe) mentre il processo legge i font
_lock = threading.Lock()


def scan(dirs, cache: Cache | None = None, check=None) -> dict[str, str]:
    """Famiglia → file del suo stile normale, solo per i font che si possono incorporare.

    Di ogni famiglia vince il file meno corsivo, meno grassetto e col peso più vicino a 400;
    se quel file non si può incorporare, la famiglia resta fuori. Con `cache` si rileggono solo
    i file nuovi o cambiati, e alla fine la cache contiene solo i file trovati. `check` si
    chiama a ogni file (il processo lo usa per uscire se la GUI non c'è più).
    """
    old = cache or {}
    seen: Cache = {}
    best: dict[str, tuple[tuple, str, FontInfo]] = {}
    for folder in dirs:
        for root, _, files in os.walk(folder):
            for name in sorted(files):
                if not name.lower().endswith(EXTENSIONS):
                    continue
                if check is not None:
                    check()
                path = os.path.join(root, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                entry = old.get(path)
                if entry is None or entry[:2] != (st.st_mtime_ns, st.st_size):
                    entry = (st.st_mtime_ns, st.st_size, font_info(path))
                seen[path] = entry
                info = entry[2]
                if info is None:
                    continue
                for family, style in info.families:
                    score = (info.italic, info.bold, abs(info.weight - 400), not _regular_name(style))
                    if family not in best or score < best[family][0]:
                        best[family] = (score, path, info)
    if cache is not None:
        cache.clear()
        cache.update(seen)
    return {family: path for family, (_, path, info) in best.items() if info.embeddable and info.outlines}


def _regular_name(style: str) -> bool:
    return style.casefold() in ("regular", "normal", "book", "roman", "standard", "")


def load_cache(file) -> Cache:
    """Cache dei font da `fonts.json`; illeggibile o di un'altra versione = vuota."""
    try:
        data = json.loads(Path(file).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("versione") != CACHE_VERSION:
            return {}
        cache: Cache = {}
        for path, (mtime, size, info) in data["font"].items():
            if info is not None:
                info = FontInfo(
                    families=tuple((str(f), str(s)) for f, s in info["families"]),
                    italic=bool(info["italic"]), bold=bool(info["bold"]), weight=int(info["weight"]),
                    embeddable=bool(info["embeddable"]), outlines=bool(info["outlines"]),
                )
            cache[str(path)] = (int(mtime), int(size), info)
        return cache
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {}


def save_cache(file, cache: Cache) -> None:
    """Scrittura atomica: un file a metà non c'è mai (al massimo resta il temporaneo)."""
    file = Path(file)
    data = {"versione": CACHE_VERSION,
            "font": {p: [m, s, asdict(i) if i is not None else None] for p, (m, s, i) in cache.items()}}
    tmp = file.with_name(f"{file.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, file)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def scan_cached(dirs, cache_file, check=None) -> dict[str, str]:
    """`scan` con la cache su disco, riscritta solo se qualcosa è cambiato."""
    cache = load_cache(cache_file)
    before = dict(cache)
    result = scan(dirs, cache, check)
    if cache != before:
        save_cache(cache_file, cache)
    return result


def _scan_process(dirs, cache_file, conn) -> None:
    """Processo che legge i font: manda famiglia → file sulla pipe ed esce. Niente Qt.

    Se la GUI non c'è più (uccisa, in crash) esce senza finire: niente processi orfani.
    """
    parent = multiprocessing.parent_process()
    last = [time.monotonic()]

    def check():
        now = time.monotonic()
        if now - last[0] > PARENT_POLL_S:
            last[0] = now
            if parent is not None and not parent.is_alive():
                os._exit(0)

    try:
        result = scan_cached(dirs, cache_file, check)
    except Exception:
        result = {}
    try:
        conn.send(result)
    except OSError:
        pass
    conn.close()


def _cache_file() -> Path:
    from piedf import paths  # importa Qt: solo nella GUI, mai nel processo che legge i font

    return paths.data_dir() / CACHE_FILE


def request() -> None:
    """L'elenco serve: lo legge un processo "spawn", senza fermare la GUI (`ready` dice quando c'è).

    Non fa niente se l'elenco c'è già o lo si sta leggendo.
    """
    global _job
    if _installed is not None or _job is not None:
        return
    if multiprocessing.parent_process() is not None:
        return  # in un processo figlio `installed` legge da sé
    ctx = multiprocessing.get_context("spawn")
    reader, writer = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_scan_process, args=([str(d) for d in font_dirs()], str(_cache_file()), writer),
                          name="piedf-fonts", daemon=True)
    process.start()
    writer.close()  # scrive solo il processo
    _job = (process, reader)


def _collect(wait: bool) -> None:
    """Prende l'elenco dal processo, se è arrivato (o aspettandolo). Processo morto = solo gli standard."""
    global _installed, _job
    if _installed is not None or _job is None:
        return
    process, conn = _job
    while True:
        try:
            if conn.poll(POLL_S if wait else 0):
                result = conn.recv()
                break
            # Morto senza rispondere. Non basta l'EOF: su Windows il lato in scrittura passa
            # al figlio solo quando parte (#54).
            if process.exitcode is not None and not conn.poll(0):
                result = None
                break
        except (EOFError, OSError):
            result = None
            break
        if not wait:
            return
    _installed = result if isinstance(result, dict) else {}
    _job = None
    conn.close()
    process.join(1)


def ready() -> bool:
    """L'elenco dei font è già pronto: `installed()` non blocca."""
    _collect(False)
    return _installed is not None


def loading() -> bool:
    """Il processo sta leggendo i font (dopo `request`)."""
    _collect(False)
    return _job is not None


def installed() -> dict[str, str]:
    """Font installati (famiglia → file). Se l'elenco non c'è ancora lo chiede e lo aspetta."""
    global _installed
    with _lock:
        if _installed is None:
            if multiprocessing.parent_process() is not None:
                _installed = scan(font_dirs())  # in un processo figlio: niente Qt, quindi niente data_dir
            else:
                request()
                _collect(True)
        return _installed


def shutdown() -> None:
    """Chiude il processo che legge i font, se c'è ancora (uscita)."""
    global _job
    if _job is None:
        return
    process, conn = _job
    _job = None
    if process.is_alive():
        process.terminate()
    process.join(5)
    conn.close()


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
    così i font del documento non si toccano. La /ToUnicode di MuPDF si sostituisce con quella
    dei caratteri del testo (`to_unicode`).
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
        xref = mupdf.pdf_to_num(mupdf.pdf_graft_object(pymupdf._as_pdf_document(doc), ref))
        cmap = doc.get_new_xref()
        doc.update_object(cmap, "<<>>")
        doc.update_stream(cmap, to_unicode(face, text))
        doc.xref_set_key(xref, "ToUnicode", f"{cmap} 0 R")
        return xref
    finally:
        scratch.close()


def to_unicode(face: Face, text: str) -> bytes:
    """CMap /ToUnicode con i soli glifi del testo, ognuno col carattere che lo ha scritto.

    Quella di MuPDF copre tutto il font, ma con più caratteri sullo stesso glifo ne sceglie
    uno a caso: Georgia ha lo stesso glifo per lo spazio e per U+00A0, e MuPDF sceglie
    U+00A0 (get_text non trovava più il testo e alla riapertura lo spazio mancava).
    """
    chars: dict[int, str] = {}
    for ch in text:
        gid = face.gid(ch)
        if ch != "\n" and gid:
            chars.setdefault(gid, ch)
    entries = [f"<{gid:04x}> <{ch.encode('utf-16-be').hex()}>" for gid, ch in sorted(chars.items())]
    blocks = [f"{len(entries[i:i + 100])} beginbfchar\n" + "\n".join(entries[i:i + 100]) + "\nendbfchar"
              for i in range(0, len(entries), 100)]
    return "\n".join([
        "/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /Adobe-Identity-UCS def", "/CMapType 2 def",
        "1 begincodespacerange", "<0000> <ffff>", "endcodespacerange",
        *blocks,
        "endcmap", "CMapName currentdict /CIDInit /ProcSet findresource /defineresource pop", "end", "end",
    ]).encode()


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
    subset (la /ToUnicode può avere anche altri caratteri del subset), e solo i loro caratteri
    si possono riusare.
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
