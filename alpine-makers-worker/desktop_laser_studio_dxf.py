"""Lecture d'un DXF ASCII pour Alpine Laser Studio dans Alpine Makers Desktop : DXF → SVG en millimètres.

Le moteur laser du Worker ne lit que le SVG ; ce module convertit les entités 2D courantes d'un DXF (R12 à 2018,
format texte) en un SVG que le moteur analyse ensuite comme n'importe quel import :

- LINE, LWPOLYLINE (renflements « bulge » compris), POLYLINE/VERTEX/SEQEND, CIRCLE, ARC, ELLIPSE, SPLINE (évaluation
  de la B-spline par l'algorithme de de Boor, poids NURBS compris), POINT ignoré ;
- INSERT : blocs insérés (translation, échelle, rotation), imbrication bornée ;
- unités : en-tête ``$INSUNITS`` (pouce, cm, m…) ; sans unité déclarée, millimètres ;
- calques : couleur ACI de l'entité, sinon celle de son calque DXF → couleur de trait (un calque Laser Studio par
  couleur, comme un SVG). Le Y du DXF (vers le haut) est retourné pour le repère du document (Y vers le bas).

Rien n'est exécuté ni écrit : texte en entrée, texte en sortie. Les DXF binaires, les textes (TEXT/MTEXT), les
hachures et les cotes sont ignorés et signalés.
"""
import math

MAX_BYTES = 32 * 1024 * 1024
MAX_POINTS = 2_000_000
MAX_INSERT_DEPTH = 8
TOLERANCE_MM = 0.05
UNITS_MM = {0: 1.0, 1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0, 8: 0.0000254, 9: 0.0254, 10: 914.4, 13: 0.001, 14: 100.0}
# Couleurs ACI 1-9 → trait ; 7 (blanc / noir selon le fond) devient noir, les autres indices une couleur de la palette.
ACI = {1: "#ff0000", 2: "#c0c000", 3: "#00c000", 4: "#00c0c0", 5: "#0000ff", 6: "#ff00ff", 7: "#000000", 8: "#404040",
       9: "#a0a0a0"}
PALETTE_REST = ("#ff8000", "#000080", "#800000", "#008000", "#808000", "#c06000", "#0080ff", "#800080", "#8060ff",
                "#ff6080", "#40a0ff", "#c03060", "#60c080", "#e0a060", "#20b040")
IGNORED = {"TEXT", "MTEXT", "HATCH", "DIMENSION", "SOLID", "3DFACE", "IMAGE", "WIPEOUT", "LEADER", "MLEADER", "ATTRIB",
           "ATTDEF", "REGION", "3DSOLID", "BODY", "MESH", "TRACE"}


class DxfError(ValueError):
    """DXF illisible : message pour l'interface."""


def _pairs(text):
    lines = text.splitlines()
    if len(lines) % 2:
        lines.append("")
    pairs = []
    for index in range(0, len(lines) - 1, 2):
        code = lines[index].strip()
        try:
            pairs.append((int(code), lines[index + 1].strip()))
        except ValueError:
            raise DxfError("DXF illisible : seul le format texte (ASCII) est lu, pas le DXF binaire.") from None
    return pairs


def _float(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _sections(pairs):
    sections, current, name = {}, None, None
    index = 0
    while index < len(pairs):
        code, value = pairs[index]
        if code == 0 and value == "SECTION":
            name = pairs[index + 1][1] if index + 1 < len(pairs) and pairs[index + 1][0] == 2 else ""
            current = []
            index += 2
            continue
        if code == 0 and value == "ENDSEC":
            if name is not None:
                sections[name] = current
            current, name = None, None
            index += 1
            continue
        if current is not None:
            current.append((code, value))
        index += 1
    return sections


def _entities(pairs):
    """Liste de (type, [(code, valeur)…]) : une entrée par entité, dans l'ordre."""
    items, current = [], None
    for code, value in pairs:
        if code == 0:
            current = (value, [])
            items.append(current)
        elif current is not None:
            current[1].append((code, value))
    return items


def _first(data, code, default=None):
    for item_code, value in data:
        if item_code == code:
            return value
    return default


def _all(data, code):
    return [value for item_code, value in data if item_code == code]


def _arc_points(cx, cy, radius, start, end, tol=TOLERANCE_MM):
    """Arc (degrés, sens trigonométrique) aplati à ``tol`` (unités du dessin)."""
    while end <= start:
        end += 360.0
    sweep = math.radians(end - start)
    step = 2 * math.acos(max(-1.0, 1 - tol / radius)) if radius > tol else math.pi / 8
    count = max(2, min(2048, int(math.ceil(abs(sweep) / max(step, 1e-3)))))
    a0 = math.radians(start)
    return [(cx + radius * math.cos(a0 + sweep * i / count), cy + radius * math.sin(a0 + sweep * i / count)) for i in range(count + 1)]


def _bulge_points(p1, p2, bulge, tol=TOLERANCE_MM):
    """Segment de polyligne à renflement (bulge = tan(angle/4)) → points de l'arc, p1 exclu."""
    if abs(bulge) < 1e-9:
        return [p2]
    chord = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
    if chord < 1e-12:
        return [p2]
    angle = 4 * math.atan(bulge)
    radius = chord / (2 * math.sin(abs(angle) / 2))
    mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
    sagitta_to_center = radius * math.cos(abs(angle) / 2)
    nx, ny = -(p2[1] - p1[1]) / chord, (p2[0] - p1[0]) / chord
    direction = 1 if bulge > 0 else -1
    # Plus d'un demi-tour : cos(|angle|/2) < 0 place de lui-même le centre de l'autre côté de la corde.
    cx, cy = mx + direction * nx * sagitta_to_center, my + direction * ny * sagitta_to_center
    a1 = math.atan2(p1[1] - cy, p1[0] - cx)
    step = 2 * math.acos(max(-1.0, 1 - tol / radius)) if radius > tol else math.pi / 8
    count = max(2, min(2048, int(math.ceil(abs(angle) / max(step, 1e-3)))))
    points = [(cx + radius * math.cos(a1 + angle * i / count), cy + radius * math.sin(a1 + angle * i / count)) for i in range(1, count)]
    return points + [p2]


def _polyline(vertices, bulges, closed, tol=TOLERANCE_MM):
    if not vertices:
        return []
    points = [vertices[0]]
    count = len(vertices)
    for index in range(count - 1 + (1 if closed else 0)):
        a, b = vertices[index], vertices[(index + 1) % count]
        points.extend(_bulge_points(a, b, bulges[index] if index < len(bulges) else 0.0, tol))
    return points


def _ellipse(data, tol=TOLERANCE_MM):
    cx, cy = _float(_first(data, 10)), _float(_first(data, 20))
    mx, my = _float(_first(data, 11)), _float(_first(data, 21))
    ratio = _float(_first(data, 40), 1.0)
    start, end = _float(_first(data, 41), 0.0), _float(_first(data, 42), 2 * math.pi)
    major = math.hypot(mx, my)
    if major <= 0:
        return []
    rotation = math.atan2(my, mx)
    minor = major * ratio
    while end <= start:
        end += 2 * math.pi
    count = max(16, min(2048, int(math.ceil((end - start) / (2 * math.acos(max(-1.0, 1 - tol / major))))))) if major > tol else 16
    cos, sin = math.cos(rotation), math.sin(rotation)
    points = []
    for i in range(count + 1):
        t = start + (end - start) * i / count
        x, y = major * math.cos(t), minor * math.sin(t)
        points.append((cx + x * cos - y * sin, cy + x * sin + y * cos))
    return points


def _spline(data, tol=TOLERANCE_MM):
    degree = int(_float(_first(data, 71), 3))
    knots = [_float(value) for value in _all(data, 40)]
    xs, ys = [_float(value) for value in _all(data, 10)], [_float(value) for value in _all(data, 20)]
    weights = [_float(value, 1.0) for value in _all(data, 41)]
    control = list(zip(xs, ys))
    if len(control) < 2:
        fit = list(zip([_float(value) for value in _all(data, 11)], [_float(value) for value in _all(data, 21)]))
        return fit if len(fit) >= 2 else []
    if len(weights) != len(control):
        weights = [1.0] * len(control)
    if degree < 1 or len(knots) != len(control) + degree + 1:
        return control  # nœuds incohérents : polygone de contrôle (approximation honnête plutôt qu'un refus)

    def de_boor(t):
        k = degree
        span = k
        while span < len(control) - 1 and t >= knots[span + 1]:
            span += 1
        d = [(control[j][0] * weights[j], control[j][1] * weights[j], weights[j]) for j in range(span - k, span + 1)]
        for r in range(1, k + 1):
            for j in range(k, r - 1, -1):
                i = j + span - k
                denominator = knots[i + k - r + 1] - knots[i]
                alpha = 0.0 if denominator == 0 else (t - knots[i]) / denominator
                d[j] = tuple((1 - alpha) * d[j - 1][n] + alpha * d[j][n] for n in range(3))
        x, y, w = d[k]
        return (x / w, y / w) if w else (x, y)

    low, high = knots[degree], knots[len(control)]
    if high <= low:
        return control
    length = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(control, control[1:]))
    count = max(16, min(4096, int(length / (10 * tol)) + len(control) * 4))
    return [de_boor(low + (high - low) * i / count) for i in range(count)] + [de_boor(high - 1e-12 * (high - low))]


class _Reader:
    def __init__(self, text):
        pairs = _pairs(text)
        sections = _sections(pairs)
        header = sections.get("HEADER", [])
        self.scale = 1.0
        for index, (code, value) in enumerate(header):
            if code == 9 and value == "$INSUNITS" and index + 1 < len(header):
                self.scale = UNITS_MM.get(int(_float(header[index + 1][1], 0)), 1.0)
        self.tol = TOLERANCE_MM / self.scale     # tolérance d'aplatissement en unités du dessin
        self.layer_colors = {}
        tables = _entities(sections.get("TABLES", []))
        for kind, data in tables:
            if kind == "LAYER":
                name = _first(data, 2, "0")
                color = int(_float(_first(data, 62), 7))
                self.layer_colors[name] = abs(color)
        self.blocks = {}
        current = None
        for kind, data in _entities(sections.get("BLOCKS", [])):
            if kind == "BLOCK":
                current = {"name": _first(data, 2, ""), "base": (_float(_first(data, 10)), _float(_first(data, 20))), "items": []}
                self.blocks[current["name"]] = current
            elif kind == "ENDBLK":
                current = None
            elif current is not None:
                current["items"].append((kind, data))
        self.entities = _entities(sections.get("ENTITIES", []))
        self.paths = []          # (couleur, points, fermé)
        self.ignored = {}
        self.points = 0
        self.extra = {}

    def color(self, data, inherited):
        value = _first(data, 62)
        layer = _first(data, 8, "0")
        index = int(_float(value, 256)) if value is not None else 256
        if index == 0:  # DUBLOC : couleur du bloc inséré
            index = inherited
        if index == 256:  # DUCALQUE
            index = self.layer_colors.get(layer, 7)
        if index in ACI:
            return ACI[index]
        if index not in self.extra:
            self.extra[index] = PALETTE_REST[len(self.extra) % len(PALETTE_REST)]
        return self.extra[index]

    def add(self, color, points, closed, transform):
        if len(points) < 2:
            return
        self.points += len(points)
        if self.points > MAX_POINTS:
            raise DxfError("DXF trop lourd : plus de 2 millions de points.")
        self.paths.append((color, [transform(p) for p in points], closed))

    def walk(self, items, transform, inherited, depth):
        index = 0
        while index < len(items):
            kind, data = items[index]
            index += 1
            color = self.color(data, inherited)
            if kind == "LINE":
                self.add(color, [(_float(_first(data, 10)), _float(_first(data, 20))), (_float(_first(data, 11)), _float(_first(data, 21)))], False, transform)
            elif kind == "LWPOLYLINE":
                vertices, bulges = [], []
                xs = [(code, value) for code, value in data if code in (10, 20, 42)]
                for code, value in xs:
                    if code == 10:
                        vertices.append([_float(value), 0.0])
                        bulges.append(0.0)
                    elif code == 20 and vertices:
                        vertices[-1][1] = _float(value)
                    elif code == 42 and bulges:
                        bulges[-1] = _float(value)
                closed = int(_float(_first(data, 70), 0)) & 1 == 1
                points = _polyline([tuple(v) for v in vertices], bulges, closed, self.tol)
                self.add(color, points, closed, transform)
            elif kind == "POLYLINE":
                closed = int(_float(_first(data, 70), 0)) & 1 == 1
                vertices, bulges = [], []
                while index < len(items) and items[index][0] == "VERTEX":
                    vertex = items[index][1]
                    if not int(_float(_first(vertex, 70), 0)) & 16:  # point de contrôle de lissage : ignoré
                        vertices.append((_float(_first(vertex, 10)), _float(_first(vertex, 20))))
                        bulges.append(_float(_first(vertex, 42), 0.0))
                    index += 1
                if index < len(items) and items[index][0] == "SEQEND":
                    index += 1
                self.add(color, _polyline(vertices, bulges, closed, self.tol), closed, transform)
            elif kind == "CIRCLE":
                radius = _float(_first(data, 40))
                if radius > 0:
                    self.add(color, _arc_points(_float(_first(data, 10)), _float(_first(data, 20)), radius, 0.0, 360.0, self.tol), True, transform)
            elif kind == "ARC":
                radius = _float(_first(data, 40))
                if radius > 0:
                    self.add(color, _arc_points(_float(_first(data, 10)), _float(_first(data, 20)), radius,
                                                _float(_first(data, 50)), _float(_first(data, 51)), self.tol), False, transform)
            elif kind == "ELLIPSE":
                points = _ellipse(data, self.tol)
                full = abs(_float(_first(data, 42), 2 * math.pi) - _float(_first(data, 41), 0.0) - 2 * math.pi) < 1e-6
                self.add(color, points, full, transform)
            elif kind == "SPLINE":
                closed = int(_float(_first(data, 70), 0)) & 1 == 1
                self.add(color, _spline(data, self.tol), closed, transform)
            elif kind == "INSERT":
                self.insert(data, transform, color, depth)
            elif kind in ("POINT", "VERTEX", "SEQEND", "ENDBLK", "BLOCK", "EOF"):
                continue
            else:
                self.ignored[kind] = self.ignored.get(kind, 0) + 1

    def insert(self, data, transform, color, depth):
        block = self.blocks.get(_first(data, 2, ""))
        if block is None:
            return
        if depth >= MAX_INSERT_DEPTH:
            raise DxfError("DXF : blocs imbriqués trop profondément.")
        x, y = _float(_first(data, 10)), _float(_first(data, 20))
        sx, sy = _float(_first(data, 41), 1.0) or 1.0, _float(_first(data, 42), 1.0) or 1.0
        angle = math.radians(_float(_first(data, 50), 0.0))
        bx, by = block["base"]
        cos, sin = math.cos(angle), math.sin(angle)
        inherited = next((index for index, value in ACI.items() if value == color), 7)

        def local(point, outer=transform):
            px, py = (point[0] - bx) * sx, (point[1] - by) * sy
            return outer((x + px * cos - py * sin, y + px * sin + py * cos))
        self.walk(block["items"], local, inherited, depth + 1)


def dxf_to_svg(text):
    """Texte DXF → (svg, avertissements, nombre de tracés). Lève DxfError si rien n'est exploitable."""
    if not isinstance(text, str):
        raise DxfError("DXF illisible.")
    if len(text) > MAX_BYTES:
        raise DxfError("DXF trop volumineux (32 Mio au plus).")
    if text.startswith("AutoCAD Binary DXF"):
        raise DxfError("DXF binaire non pris en charge : enregistre-le en DXF texte (ASCII).")
    reader = _Reader(text)
    reader.walk(reader.entities, lambda point: point, 7, 0)
    if not reader.paths:
        raise DxfError("Aucun tracé exploitable dans ce DXF (lignes, polylignes, cercles, arcs, ellipses, splines).")
    scale = reader.scale
    xs = [p[0] for _, points, _ in reader.paths for p in points]
    ys = [p[1] for _, points, _ in reader.paths for p in points]
    min_x, max_y = min(xs), max(ys)
    width, height = max(1e-3, (max(xs) - min_x) * scale), max(1e-3, (max_y - min(ys)) * scale)
    parts = []
    for color, points, closed in reader.paths:
        d = " ".join(("L" if index else "M") + f"{(p[0] - min_x) * scale:.4f} {(max_y - p[1]) * scale:.4f}"
                     for index, p in enumerate(points)) + (" Z" if closed else "")
        parts.append(f'<path d="{d}" stroke="{color}" fill="none" stroke-width="0.1"/>')
    warnings = []
    if reader.ignored:
        names = ", ".join(f"{kind} ×{count}" for kind, count in sorted(reader.ignored.items()))
        warnings.append(f"DXF : entités ignorées ({names}) — textes, hachures et cotes ne sont pas lus.")
    if reader.scale != 1.0:
        warnings.append(f"DXF : unités converties en millimètres (× {reader.scale:g}).")
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.4f}mm" height="{height:.4f}mm" '
           f'viewBox="0 0 {width:.4f} {height:.4f}">' + "".join(parts) + "</svg>")
    return svg, warnings, len(reader.paths)
