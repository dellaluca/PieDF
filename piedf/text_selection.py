"""Selezione del testo (#15), senza Qt: righe come si vedono, punto → posizione, testo da copiare.

Le parole di una pagina arrivano dal processo di render (vedi `render_worker`):
(x0, y0, x1, y1, parola, blocco, riga, n) in punti nello spazio della pagina come si vede,
nell'ordine di lettura. Una posizione è il punto *tra* due parole: 0 = prima della prima,
len(parole) = dopo l'ultima. La selezione va da (pagina, posizione) a (pagina, posizione)
e prende le parole in mezzo.
"""

# Posizione "fine pagina", valida anche per le pagine le cui parole non sono ancora arrivate:
# si riporta a len(parole) quando si usa.
END = 1 << 30
LINE_TOLERANCE = 3.0  # punti: due parole sono sulla stessa riga, come in `get_text(sort=True)`
WORD_PAD = 1.0  # punti intorno alla parola che contano ancora come "sopra la parola"


def reading_order(words: list[tuple]) -> list[tuple]:
    """Lo stesso ordinamento di `page.get_text("words", sort=True)` di PyMuPDF, ma su rettangoli
    già portati nello spazio come si vede: righe dall'alto, parole da sinistra."""
    if not words:
        return []
    words = sorted(words, key=lambda w: (w[3], w[0]))
    result, line = [], [words[0]]
    y0, y1 = words[0][1], words[0][3]
    for w in words[1:]:
        if abs(w[1] - y0) <= LINE_TOLERANCE or abs(w[3] - y1) <= LINE_TOLERANCE:
            line.append(w)
            y0, y1 = min(y0, w[1]), max(y1, w[3])
        else:
            result.extend(sorted(line, key=lambda w: w[0]))
            line, y0, y1 = [w], w[1], w[3]
    result.extend(sorted(line, key=lambda w: w[0]))
    return result


class PageText:
    """Parole di una pagina e righe come si vedono: (inizio, fine, y0, y1), fine esclusa."""

    def __init__(self, words: list[tuple]) -> None:
        self.words = words
        self.lines = _visual_lines(words)

    def __len__(self) -> int:
        return len(self.words)

    def word_at(self, x: float, y: float) -> int | None:
        """Parola sotto il punto (punti PDF), se c'è."""
        for i, w in enumerate(self.words):
            if w[0] - WORD_PAD <= x <= w[2] + WORD_PAD and w[1] - WORD_PAD <= y <= w[3] + WORD_PAD:
                return i
        return None

    def caret_at(self, x: float, y: float) -> int:
        """Posizione più vicina al punto: sulla riga sotto il punto (o la più vicina), prima della
        prima parola la cui metà sta a destra del punto."""
        if not self.lines:
            return 0
        start, end, _, _ = min(self.lines, key=lambda line: _distance(line, y))
        for i in range(start, end):
            w = self.words[i]
            if x < (w[0] + w[2]) / 2:
                return i
        return end

    def line_of(self, index: int) -> tuple[int, int]:
        """Inizio e fine (esclusa) della riga della parola `index`."""
        for start, end, _, _ in self.lines:
            if start <= index < end:
                return start, end
        return index, index + 1

    def highlight(self, start: int, end: int) -> list[tuple[float, float, float, float]]:
        """Rettangoli (x0, y0, x1, y1) da evidenziare per le parole [start, end): uno per riga,
        spazi tra le parole compresi."""
        rects = []
        for line_start, line_end, y0, y1 in self.lines:
            a, b = max(start, line_start), min(end, line_end)
            if a < b:
                rects.append((self.words[a][0], y0, self.words[b - 1][2], y1))
        return rects


def ordered(a: tuple[int, int], b: tuple[int, int]) -> tuple[tuple[int, int], tuple[int, int]]:
    return (a, b) if a <= b else (b, a)


def page_span(selection, page: int, count: int) -> tuple[int, int]:
    """Parole [inizio, fine) della pagina `page` (con `count` parole) dentro la selezione ordinata."""
    (p0, c0), (p1, c1) = selection
    if page < p0 or page > p1:
        return 0, 0
    start = min(c0, count) if page == p0 else 0
    end = min(c1, count) if page == p1 else count
    return start, max(start, end)


def selected_text(selection, pages: dict[int, PageText]) -> str:
    """Testo della selezione ordinata: parole unite da uno spazio, a capo quando cambiano
    blocco, riga o pagina. Le pagine della selezione devono esserci tutte in `pages`."""
    (p0, _), (p1, _) = selection
    parts = []
    last = None
    for page in range(p0, p1 + 1):
        text = pages[page]
        start, end = page_span(selection, page, len(text))
        for w in text.words[start:end]:
            key = (page, w[5], w[6])
            if last is not None:
                parts.append(" " if key == last else "\n")
            parts.append(w[4])
            last = key
    return "".join(parts)


def _visual_lines(words: list[tuple]) -> list[tuple[int, int, float, float]]:
    # Nell'ordine di lettura una riga è una sequenza di parole alla stessa altezza che vanno
    # verso destra: una parola più a sinistra o più in basso inizia la riga dopo.
    lines = []
    start = 0
    y0 = y1 = 0.0
    for i, w in enumerate(words):
        if i > start:
            same = abs(w[1] - y0) <= LINE_TOLERANCE or abs(w[3] - y1) <= LINE_TOLERANCE
            if same and w[0] >= words[i - 1][0]:
                y0, y1 = min(y0, w[1]), max(y1, w[3])
                continue
            lines.append((start, i, y0, y1))
            start = i
        y0, y1 = w[1], w[3]
    if words:
        lines.append((start, len(words), y0, y1))
    return lines


def _distance(line: tuple[int, int, float, float], y: float) -> float:
    _, _, y0, y1 = line
    return 0.0 if y0 <= y <= y1 else min(abs(y - y0), abs(y - y1))
