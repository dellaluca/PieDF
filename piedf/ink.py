"""Geometria dei tratti a mano libera (#22), senza Qt: levigatura, semplificazione, distanze.

Un tratto è una sequenza di punti (x, y) in punti PDF, pagina come si vede.
"""

import math

Point = tuple[float, float]

SMOOTH_STEPS = 4  # punti per ogni curva tra due punti medi
SIMPLIFY_TOLERANCE = 0.3  # punti PDF: scarto massimo dal tratto levigato


def smooth_points(points: list[Point], steps: int = SMOOTH_STEPS) -> list[Point]:
    """Curva morbida che passa per i punti medi (toglie gli spigoli del mouse), come punti.

    È la curva di `signature_dialog.smooth_path`: dal punto medio di ogni segmento al
    successivo, una quadratica con il punto in comune come controllo.
    """
    if len(points) < 3:
        return list(points)
    out = [points[0]]
    start = points[0]
    for prev, cur in zip(points, points[1:]):
        end = ((prev[0] + cur[0]) / 2, (prev[1] + cur[1]) / 2)
        for i in range(1, steps + 1):
            t = i / steps
            a, b, c = (1 - t) ** 2, 2 * (1 - t) * t, t * t
            out.append((a * start[0] + b * prev[0] + c * end[0], a * start[1] + b * prev[1] + c * end[1]))
        start = end
    out.append(points[-1])
    return out


def simplify(points: list[Point], tolerance: float = SIMPLIFY_TOLERANCE) -> list[Point]:
    """Ramer–Douglas–Peucker: meno punti, mai più lontani di `tolerance` dal tratto di partenza."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        worst, index = 0.0, -1
        for i in range(first + 1, last):
            d = segment_distance(points[i], points[first], points[last])
            if d > worst:
                worst, index = d, i
        if worst > tolerance:
            keep[index] = True
            stack.append((first, index))
            stack.append((index, last))
    return [p for p, k in zip(points, keep) if k]


def segment_distance(p: Point, a: Point, b: Point) -> float:
    """Distanza del punto `p` dal segmento a-b."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    if length == 0:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def stroke_distance(p: Point, stroke: tuple[Point, ...]) -> float:
    """Distanza del punto dal tratto (un punto solo vale come un puntino)."""
    if len(stroke) == 1:
        return math.hypot(p[0] - stroke[0][0], p[1] - stroke[0][1])
    return min(segment_distance(p, a, b) for a, b in zip(stroke, stroke[1:]))


def bounds(strokes) -> tuple[float, float, float, float]:
    """(x0, y0, x1, y1) dei punti di tutti i tratti."""
    xs = [x for stroke in strokes for x, _ in stroke]
    ys = [y for stroke in strokes for _, y in stroke]
    return (min(xs), min(ys), max(xs), max(ys))


def translated(strokes, dx: float, dy: float) -> tuple[tuple[Point, ...], ...]:
    return tuple(tuple((x + dx, y + dy) for x, y in stroke) for stroke in strokes)
