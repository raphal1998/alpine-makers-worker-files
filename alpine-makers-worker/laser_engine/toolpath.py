# -*- coding: utf-8 -*-
"""Parcours et G-code laser/CNC : SVG par calques de couleur → plan d'usinage → GRBL 1.1.

Moteur d'Alpine Laser Studio, embarqué dans le paquet Worker (``worker_agent/laser_engine``).
Pure Python (bibliothèque standard ; Pillow seulement pour lire une image) : chaque calcul de
parcours d'un job tourne sur un Worker, dans le Python isolé du composant « laser-engine », via
``laser_engine.cli``. Le site l'importe encore à travers le shim ``laser_toolpath.py`` (aperçus).
Inspiré de LightBurn : chaque couleur de trait du SVG devient un calque avec ses réglages (mode
ligne, remplissage ou image, puissance, vitesse, passes), le plan ordonne les tracés (par calque,
intérieur d'abord ou plus proche voisin), le G-code cible GRBL 1.1 (M4 dynamique ou M3 constant,
S = puissance, S0 en déplacement, M3 + Z de sécurité et plongée en CNC) et ``simulate()`` relit le
G-code pour l'aperçu et l'estimation de durée.

Réglages de coupe façon LightBurn (tous facultatifs, valeurs par défaut = comportement historique) :
ligne (décalage de trait, perforation, ponts automatiques ou manuels, entrée/sortie tangentes,
surcoupe), remplissage (va-et-vient ou sens unique, hachures croisées, surbalayage, regroupement
des formes, rampe de puissance), remplissage décalé (contours concentriques), image (dix algorithmes
de trame, négatif, correction de largeur de point, relais, puissance min/max), sous-calques
exécutés après le calque principal, puissance constante (M3) par calque et vitesse bornée par la
machine. Tout est converti en tracés et puissances explicites dans le plan : le G-code ne fait que
les émettre (G1 S0 pour les passages laser éteint : surbalayage, perforation, ponts).

Repères : le document SVG est exprimé en mm, origine en haut à gauche, Y vers le bas (convention
SVG). ``plan()`` place le dessin sur le lit machine (``MachineProfile.origin`` et ``y_up``) et
refuse tout parcours qui en sort. Aucune commande matérielle n'est envoyée ici : le module produit
du texte ; le pilotage GRBL des machines passe par les équipements du Worker.
"""

from __future__ import annotations

import dataclasses
import io
import json
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:  # Pillow n'est requis que pour lire une image (fichier, octets ou objet Image).
    from PIL import Image as _PILImage
except ImportError:  # pragma: no cover - dépend de l'environnement
    _PILImage = None

Point = tuple[float, float]
Polyline = list[Point]
Matrix = tuple[float, float, float, float, float, float]

DEFAULT_TOLERANCE_MM = 0.05
LAYER_MODES = ("line", "fill", "offset_fill", "image")
MACHINE_KINDS = ("laser", "cnc")
MACHINE_ORIGINS = ("front-left", "rear-left", "front-right", "rear-right", "center")
PLAN_ORDERS = ("layer", "inside-out", "nearest")
IMAGE_MODES = ("grayscale", "dither")   # paramètre « mode » historique de raster_image()
# Algorithmes de trame (LayerSettings.image_mode) ; « dither » = Floyd-Steinberg (libellé « Tramage »).
IMAGE_ALGORITHMS = ("threshold", "ordered", "atkinson", "dither", "stucki", "jarvis", "newsprint",
                    "halftone", "sketch", "grayscale")
IMAGE_ALGORITHM_LABELS = {
    "threshold": {"label": "Seuil", "description": "Noir ou blanc : pixel brûlé au-delà de 50 % d'obscurité."},
    "ordered": {"label": "Ordonné", "description": "Tramage ordonné (matrice de Bayer 8 × 8) : motif régulier."},
    "atkinson": {"label": "Atkinson", "description": "Diffusion d'erreur partielle (6/8) : contrastes marqués, blancs propres."},
    "dither": {"label": "Tramage", "description": "Diffusion d'erreur Floyd-Steinberg : rendu photo polyvalent."},
    "stucki": {"label": "Stucki", "description": "Diffusion d'erreur large (Stucki) : grain fin et régulier."},
    "jarvis": {"label": "Jarvis", "description": "Diffusion d'erreur Jarvis-Judice-Ninke : dégradés doux."},
    "newsprint": {"label": "Papier journal", "description": "Trame de lignes inclinées, épaisseur selon l'obscurité."},
    "halftone": {"label": "Demi-ton", "description": "Points de taille variable sur une grille inclinée (trames par pouce)."},
    "sketch": {"label": "Esquisse", "description": "Contours détectés (filtre de Sobel) gravés en traits."},
    "grayscale": {"label": "Niveaux de gris", "description": "Puissance variable entre la puissance minimale et maximale."},
}
FILL_GROUPINGS = ("all", "groups", "individual")
TABS_MODES = ("auto", "manual")
TAB_SPACING_MODES = ("uniform", "per_shape")
LASER_TYPES = ("diode", "co2", "fiber", "infrared")
MITER_LIMIT = 4.0                  # onglet borné : biseau au-delà de 4 × le décalage (comme SVG)
MAX_OFFSET_POINTS = 2_000_000      # remplissage décalé : au-delà, les contours intérieurs sont omis
MIN_PERFORATION_MM = 0.01
RAMP_STEP_MM = 0.1                 # rampe de puissance : paliers de 0,1 mm (16 au plus par rampe)
MAX_RAMP_STEPS = 16
SKETCH_THRESHOLD = 1.0             # esquisse : gradient de Sobel (gris 0-1) ≈ saut de 25 %
NEAREST_MAX_SEGMENTS = 3000        # au-delà, l'ordre des tracés est conservé (coût quadratique)
MAX_FLATTEN_SEGMENTS = 4096
MAX_SVG_CHARS = 32_000_000          # même échelle que MAX_GCODE_CHARS et la requête (32 Mio) : un
# SVG en dessous du plafond de la requête ne doit pas échouer sur un plafond interne plus strict
# (photo convertie en plusieurs niveaux de puissance vectoriels : des dizaines de milliers de
# tracés, plusieurs Mio de texte SVG légitimes ; incident 29/09, 9,5 Mio refusés).
MAX_GCODE_CHARS = 32_000_000
MAX_IMAGE_BYTES = 16_000_000
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
EPSILON = 1e-6
_GEOM_EPS = 1e-9

_UNIT_MM = {"mm": 1.0, "cm": 10.0, "in": 25.4, "pt": 25.4 / 72.0, "pc": 25.4 / 6.0, "q": 0.25}
_NUMBER_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_TRANSFORM_RE = re.compile(r"([a-zA-Z]+)\s*\(([^)]*)\)")
_CSS_RULE_RE = re.compile(r"([^{}]+)\{([^}]*)\}")
_GCODE_WORD_RE = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)")
_HEADER_VALUE_RE = re.compile(r"^;\s*([a-z_]+)\s*:\s*(.+?)\s*$")
_SKIP_TAGS = frozenset({
    "defs", "clippath", "mask", "symbol", "marker", "pattern", "metadata", "title", "desc",
    "style", "namedview", "lineargradient", "radialgradient", "filter", "script", "foreignobject",
})
_GROUP_TAGS = frozenset({"g", "svg", "a", "switch"})
_INKSCAPE_GROUPMODE = "{http://www.inkscape.org/namespaces/inkscape}groupmode"
_SHAPE_TAGS = frozenset({"rect", "circle", "ellipse", "line", "polyline", "polygon", "path"})
_NAMED_COLORS = {
    "black": "#000000", "white": "#ffffff", "red": "#ff0000", "lime": "#00ff00", "blue": "#0000ff",
    "yellow": "#ffff00", "cyan": "#00ffff", "aqua": "#00ffff", "magenta": "#ff00ff",
    "fuchsia": "#ff00ff", "green": "#008000", "orange": "#ffa500", "gray": "#808080",
    "grey": "#808080", "silver": "#c0c0c0", "maroon": "#800000", "navy": "#000080",
    "purple": "#800080", "teal": "#008080", "olive": "#808000", "brown": "#a52a2a",
    "pink": "#ffc0cb", "gold": "#ffd700", "violet": "#ee82ee", "indigo": "#4b0082",
    "darkgreen": "#006400", "darkblue": "#00008b", "darkred": "#8b0000", "lightgray": "#d3d3d3",
    "lightgrey": "#d3d3d3", "darkgray": "#a9a9a9", "darkgrey": "#a9a9a9",
}


class ToolpathError(ValueError):
    """Erreur métier (SVG invalide, réglage hors plage, parcours hors du lit…)."""

    def __init__(self, message: str, status_code: int = 400, code: str = "TOOLPATH_ERROR"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------

@dataclass
class Shape:
    """Une forme SVG aplatie : polylignes en mm (repère SVG, Y vers le bas)."""

    id: str
    layer_key: str                       # couleur de trait normalisée « #rrggbb » ou « none »
    kind: str                            # rect, circle, ellipse, line, polyline, polygon, path
    paths: list[Polyline] = field(default_factory=list)
    closed: bool = False                 # toutes les sous-polylignes sont fermées (dernier = premier)
    fill: bool = False                   # la forme porte un remplissage (fill ≠ none)
    group: str = ""                      # objet/groupe SVG (<g> le plus extérieur hors calque Inkscape)


@dataclass
class Document:
    width_mm: float
    height_mm: float
    shapes: list[Shape] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class LayerSettings:
    """Réglages d'un calque (une couleur de trait) : mode, puissance, vitesse, passes."""

    name: str = ""
    color: str = "#000000"
    mode: str = "line"                   # line | fill | image
    power_pct: float = 50.0              # 0-100, S = power_pct / 100 × max_power
    speed_mm_min: float = 1000.0
    passes: int = 1
    z_offset_mm: float = 0.0             # Z de la première passe (laser : mise au point ; CNC : profondeur)
    z_step_mm: float = 0.0               # décalage Z ajouté à chaque passe supplémentaire
    air_assist: bool = False             # M8 / M9
    interval_mm: float = 0.1             # pas des lignes de remplissage ou d'image
    angle_deg: float = 0.0               # angle des hachures (remplissage)
    enabled: bool = True
    # --- Réglages LightBurn : facultatifs, la valeur par défaut garde le comportement historique ---
    min_power_pct: float = 0.0           # image niveaux de gris : puissance du plus clair ; départ des rampes
    constant_power: bool = False         # True : M3 (puissance constante) pour ce calque au lieu du M4 du profil
    sublayers: list[dict[str, Any]] = field(default_factory=list)   # réglages complets, exécutés après
    # Ligne
    kerf_offset_mm: float = 0.0          # > 0 : vers l'extérieur (pièce agrandie, trous réduits) ; formes fermées
    perforation: bool = False            # découpe en pointillés : allumé sur perf_cut_mm, éteint sur perf_skip_mm
    perf_cut_mm: float = 0.1
    perf_skip_mm: float = 0.1
    tabs_enabled: bool = False           # ponts (attaches) laissés sur le tracé
    tabs_mode: str = "auto"              # auto | manual (tabs_points en coordonnées document)
    tabs_points: list[Point] = field(default_factory=list)
    tab_size_mm: float = 0.5
    tab_spacing_mode: str = "uniform"    # uniform (tab_spacing_mm) | per_shape (tabs_per_shape)
    tab_spacing_mm: float = 50.0
    tabs_per_shape: int = 1
    tabs_max_enabled: bool = False
    tabs_max: int = 1
    tab_power_pct: float = 0.0           # % de la puissance du calque sur les ponts (0 = laser coupé)
    tabs_skip_inner: bool = False        # pas de pont automatique sur les formes intérieures
    lead_in_mm: float = 0.0              # entrée tangente, laser allumé (formes fermées)
    lead_out_mm: float = 0.0             # sortie tangente, laser allumé (formes fermées)
    overcut_mm: float = 0.0              # recouvrement au-delà du point de départ (formes fermées)
    # Remplissage, remplissage décalé, image
    bidirectional: bool = True           # va-et-vient ; False : même sens, retour laser éteint
    crosshatch: bool = False             # remplissage : seconde passe à angle + 90°
    overscan_pct: float = 0.0            # surbalayage : overscan_pct/100 × vitesse (mm/s), en G1 S0
    fill_grouping: str = "individual"    # all | groups | individual (historique : forme par forme)
    ramp_length_mm: float = 0.0          # rampe linéaire de puissance aux extrémités des segments brûlés
    # Image
    image_mode: str = ""                 # un des IMAGE_ALGORITHMS ; vide : mode de l'opération
    negative: bool = False               # inverse l'image (XOR avec l'inversion de l'opération)
    dot_width_correction: bool = False
    dot_width_mm: float = 0.08           # segment brûlé raccourci de dot_width_mm/2 à chaque bout
    pass_through: bool = False           # relais : pixels > 50 % brûlés à pleine puissance, grille native
    halftone_cells_per_inch: float = 50.0
    halftone_angle_deg: float = 22.5


@dataclass
class MachineProfile:
    """Profil machine GRBL : lit, puissance S maximale, vitesses, origine et G-code d'encadrement."""

    name: str = "Laser GRBL"
    kind: str = "laser"                  # laser | cnc
    width_mm: float = 400.0
    height_mm: float = 400.0
    max_power: float = 1000.0            # valeur S maximale ($30 sur GRBL)
    laser_mode_dynamic: bool = True      # M4 (dynamique) ou M3 (constant)
    travel_speed_mm_min: float = 3000.0
    origin: str = "front-left"           # coin du lit où se trouve (0, 0)
    y_up: bool = True                    # Y croît de l'avant vers l'arrière
    homing_command: str = "$H"
    start_gcode: str = ""
    end_gcode: str = ""
    safe_z_mm: float = 5.0               # CNC : hauteur de dégagement
    plunge_rate: float = 100.0           # CNC : vitesse de plongée (mm/min)
    spindle_rpm_max: float = 10000.0     # CNC : S maximal de la broche
    model: str = ""                      # modèle commercial (informatif)
    laser_type: str = "diode"            # diode | co2 | fiber | infrared (informatif)
    laser_power_w: float = 0.0           # puissance optique (informatif)
    max_speed_mm_min: float = 0.0        # > 0 : vitesses de calque bornées (avertissement dans le plan)


@dataclass
class Operation:
    """Une opération d'usinage : les segments (polylignes machine, mm) d'un calque et ses réglages."""

    layer: str
    mode: str
    segments: list[Polyline] = field(default_factory=list)
    passes: int = 1
    power_pct: float = 50.0
    speed_mm_min: float = 1000.0
    z_offset_mm: float = 0.0
    z_step_mm: float = 0.0
    air_assist: bool = False
    powers: list[float] | None = None    # puissance (%) de chaque segment (image, ponts, surbalayage…)
    name: str = ""
    constant_power: bool = False         # M3 pour cette opération (sinon mode du profil machine)
    overscan_pct: float = 0.0            # image : surbalayage appliqué par plan()
    ramp_length_mm: float = 0.0          # image : rampe appliquée par plan()
    min_power_pct: float = 0.0           # départ des rampes
    prepared: bool = False               # surbalayage et rampe déjà convertis en segments


@dataclass
class Plan:
    operations: list[Operation] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    machine: str = ""
    order: str = "layer"
    warnings: list[str] = field(default_factory=list)


_CLASSES = {cls.__name__: cls for cls in (Shape, Document, LayerSettings, MachineProfile, Operation, Plan)}
_NESTED = {Document: {"shapes": Shape}, Plan: {"operations": Operation}}
_POLYLINE_FIELDS = {"paths", "segments"}


# ---------------------------------------------------------------------------
# Géométrie de base
# ---------------------------------------------------------------------------

def _finite(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ToolpathError(f"Valeur numérique attendue pour {label}") from None
    if not math.isfinite(number):
        raise ToolpathError(f"Valeur non finie pour {label}")
    return number


def _fmt(value: float) -> str:
    """Coordonnée à 3 décimales, jamais NaN ni « -0.000 »."""
    if not math.isfinite(value):
        raise ToolpathError("Coordonnée non finie dans le parcours")
    text = f"{value:.3f}"
    return "0.000" if text == "-0.000" else text


def _fmt_short(value: float) -> str:
    """Vitesse ou puissance : 3 décimales maximum, sans zéros inutiles."""
    text = _fmt(value)
    text = text.rstrip("0").rstrip(".")
    return text or "0"


def _dist(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _multiply(m1: Matrix, m2: Matrix) -> Matrix:
    """Produit m1 · m2 (m2 est appliquée d'abord au point)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (a1 * a2 + c1 * b2, b1 * a2 + d1 * b2,
            a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
            a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1)


def _apply(m: Matrix, p: Point) -> Point:
    a, b, c, d, e, f = m
    return (a * p[0] + c * p[1] + e, b * p[0] + d * p[1] + f)


def _matrix_scale(m: Matrix) -> float:
    return math.sqrt(abs(m[0] * m[3] - m[1] * m[2])) or 1e-9


def _angle_step(radius: float, tol: float) -> float:
    if radius <= tol:
        return math.pi / 2
    return max(2.0 * math.acos(max(-1.0, 1.0 - tol / radius)), 2 * math.pi / MAX_FLATTEN_SEGMENTS)


def _ellipse_arc(cx: float, cy: float, rx: float, ry: float, phi: float,
                 theta1: float, dtheta: float, tol: float) -> Polyline:
    """Points d'un arc d'ellipse (sans le point de départ), angle paramétrique de theta1 à theta1+dtheta."""
    n = max(1, min(MAX_FLATTEN_SEGMENTS, math.ceil(abs(dtheta) / _angle_step(max(rx, ry), tol))))
    cos_phi, sin_phi = math.cos(phi), math.sin(phi)
    points: Polyline = []
    for i in range(1, n + 1):
        t = theta1 + dtheta * i / n
        x = rx * math.cos(t)
        y = ry * math.sin(t)
        points.append((cx + x * cos_phi - y * sin_phi, cy + x * sin_phi + y * cos_phi))
    return points


def _vector_angle(ux: float, uy: float, vx: float, vy: float) -> float:
    dot = ux * vx + uy * vy
    norm = math.hypot(ux, uy) * math.hypot(vx, vy)
    if norm == 0:
        return 0.0
    angle = math.acos(max(-1.0, min(1.0, dot / norm)))
    return -angle if ux * vy - uy * vx < 0 else angle


def _endpoint_arc(x1: float, y1: float, rx: float, ry: float, rotation_deg: float,
                  large: bool, sweep: bool, x2: float, y2: float, tol: float) -> Polyline:
    """Arc SVG (paramétrage par extrémités, annexe F.6.5) aplati en segments, sans le point de départ."""
    if (x1, y1) == (x2, y2):
        return []
    rx, ry = abs(rx), abs(ry)
    if rx == 0 or ry == 0:
        return [(x2, y2)]
    phi = math.radians(rotation_deg)
    cos_phi, sin_phi = math.cos(phi), math.sin(phi)
    dx2, dy2 = (x1 - x2) / 2.0, (y1 - y2) / 2.0
    x1p = cos_phi * dx2 + sin_phi * dy2
    y1p = -sin_phi * dx2 + cos_phi * dy2
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1:
        rx *= math.sqrt(lam)
        ry *= math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        coef = -coef
    cxp = coef * rx * y1p / ry
    cyp = -coef * ry * x1p / rx
    cx = cos_phi * cxp - sin_phi * cyp + (x1 + x2) / 2.0
    cy = sin_phi * cxp + cos_phi * cyp + (y1 + y2) / 2.0
    ux, uy = (x1p - cxp) / rx, (y1p - cyp) / ry
    vx, vy = (-x1p - cxp) / rx, (-y1p - cyp) / ry
    theta1 = _vector_angle(1.0, 0.0, ux, uy)
    dtheta = _vector_angle(ux, uy, vx, vy)
    if not sweep and dtheta > 0:
        dtheta -= 2 * math.pi
    elif sweep and dtheta < 0:
        dtheta += 2 * math.pi
    points = _ellipse_arc(cx, cy, rx, ry, phi, theta1, dtheta, tol)
    if points:
        points[-1] = (x2, y2)
    return points


def _flatten_cubic(p0: Point, p1: Point, p2: Point, p3: Point, tol: float) -> Polyline:
    dd = max(math.hypot(p0[0] - 2 * p1[0] + p2[0], p0[1] - 2 * p1[1] + p2[1]),
             math.hypot(p1[0] - 2 * p2[0] + p3[0], p1[1] - 2 * p2[1] + p3[1]))
    n = max(1, min(MAX_FLATTEN_SEGMENTS, math.ceil(math.sqrt(0.75 * dd / tol))))
    points: Polyline = []
    for i in range(1, n):
        t = i / n
        u = 1 - t
        points.append((u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
                       u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1]))
    points.append(p3)
    return points


def _flatten_quadratic(p0: Point, p1: Point, p2: Point, tol: float) -> Polyline:
    dd = math.hypot(p0[0] - 2 * p1[0] + p2[0], p0[1] - 2 * p1[1] + p2[1])
    n = max(1, min(MAX_FLATTEN_SEGMENTS, math.ceil(math.sqrt(0.5 * dd / tol))))
    points: Polyline = []
    for i in range(1, n):
        t = i / n
        u = 1 - t
        points.append((u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
                       u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]))
    points.append(p2)
    return points


# ---------------------------------------------------------------------------
# Lecture SVG
# ---------------------------------------------------------------------------

def _local(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _length_mm(value: str | None, dpi: float) -> float | None:
    if value is None:
        return None
    m = re.fullmatch(r"\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([a-zA-Z%]*)\s*", value)
    if not m:
        return None
    number = float(m.group(1))
    unit = m.group(2).lower()
    if unit in ("", "px"):
        return number * 25.4 / dpi
    factor = _UNIT_MM.get(unit)
    return number * factor if factor else None


def _float_attr(el: ET.Element, name: str, default: float = 0.0) -> float:
    raw = el.get(name)
    if raw is None:
        return default
    m = _NUMBER_RE.search(raw)
    return float(m.group(0)) if m else default


def normalize_color(value: str | None) -> str:
    """Couleur SVG/CSS → « #rrggbb » minuscule, ou « none »."""
    if value is None:
        return "none"
    text = value.strip().lower()
    if not text or text in ("none", "transparent"):
        return "none"
    if text.startswith("url("):
        return "#000000"          # dégradé ou motif : peinture présente, couleur indéterminée
    if text == "currentcolor":
        return "#000000"
    if text.startswith("#"):
        digits = text[1:]
        if len(digits) in (3, 4):
            digits = "".join(ch * 2 for ch in digits[:3])
        elif len(digits) == 8:
            digits = digits[:6]
        if len(digits) == 6 and all(ch in "0123456789abcdef" for ch in digits):
            return "#" + digits
        return "none"
    m = re.fullmatch(r"rgba?\(([^)]*)\)", text)
    if m:
        parts = [p.strip() for p in re.split(r"[,\s/]+", m.group(1)) if p.strip()][:3]
        if len(parts) == 3:
            channels = []
            for part in parts:
                if part.endswith("%"):
                    channels.append(int(float(part[:-1]) * 255 / 100 + 0.5))
                else:
                    channels.append(int(float(part)))
            return "#%02x%02x%02x" % tuple(max(0, min(255, c)) for c in channels)
        return "none"
    return _NAMED_COLORS.get(text, "none")


def _parse_transform(text: str | None) -> Matrix:
    m = IDENTITY
    for name, args in _TRANSFORM_RE.findall(text or ""):
        nums = [float(v) for v in _NUMBER_RE.findall(args)]
        name = name.lower()
        if name == "matrix" and len(nums) == 6:
            t: Matrix = (nums[0], nums[1], nums[2], nums[3], nums[4], nums[5])
        elif name == "translate" and nums:
            t = (1.0, 0.0, 0.0, 1.0, nums[0], nums[1] if len(nums) > 1 else 0.0)
        elif name == "scale" and nums:
            t = (nums[0], 0.0, 0.0, nums[1] if len(nums) > 1 else nums[0], 0.0, 0.0)
        elif name == "rotate" and nums:
            a = math.radians(nums[0])
            cos_a, sin_a = math.cos(a), math.sin(a)
            t = (cos_a, sin_a, -sin_a, cos_a, 0.0, 0.0)
            if len(nums) >= 3:
                cx, cy = nums[1], nums[2]
                t = _multiply(_multiply((1.0, 0.0, 0.0, 1.0, cx, cy), t), (1.0, 0.0, 0.0, 1.0, -cx, -cy))
        elif name == "skewx" and nums:
            t = (1.0, 0.0, math.tan(math.radians(nums[0])), 1.0, 0.0, 0.0)
        elif name == "skewy" and nums:
            t = (1.0, math.tan(math.radians(nums[0])), 0.0, 1.0, 0.0, 0.0)
        else:
            continue
        m = _multiply(m, t)
    return m


def _parse_declarations(text: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in (text or "").split(";"):
        if ":" in part:
            key, value = part.split(":", 1)
            result[key.strip().lower()] = value.strip()
    return result


def _parse_css(root: ET.Element) -> dict[str, dict[str, str]]:
    """Règles simples des blocs <style> : « tag », « .classe », « #id » (Illustrator, Inkscape)."""
    rules: dict[str, dict[str, str]] = {}
    for el in root.iter():
        if _local(el.tag) != "style" or not el.text:
            continue
        for selectors, body in _CSS_RULE_RE.findall(el.text):
            declarations = _parse_declarations(body)
            for selector in selectors.split(","):
                selector = selector.strip().lower()
                if not selector or re.search(r"[\s>:+~\[\]*]", selector):
                    continue
                rules.setdefault(selector, {}).update(declarations)
    return rules


def _element_style(el: ET.Element, tag: str, inherited: dict[str, str],
                   css: dict[str, dict[str, str]]) -> dict[str, str]:
    style = dict(inherited)
    for prop in ("fill", "stroke", "display", "visibility"):
        value = el.get(prop)
        if value is not None:
            style[prop] = value.strip()
    if css:
        for key in [tag] + [f".{c}" for c in (el.get("class") or "").split()] + [f"#{el.get('id')}"]:
            style.update(css.get(key.lower(), {}))
    style.update(_parse_declarations(el.get("style")))
    return style


class _PathScanner:
    """Lecture séquentielle d'un attribut « d » : commandes, nombres et drapeaux d'arc collés."""

    __slots__ = ("text", "pos")

    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    def _skip(self) -> None:
        text, n, i = self.text, len(self.text), self.pos
        while i < n and text[i] in " \t\r\n,":
            i += 1
        self.pos = i

    def command(self) -> str | None:
        self._skip()
        if self.pos < len(self.text) and self.text[self.pos].isalpha():
            self.pos += 1
            return self.text[self.pos - 1]
        return None

    def number(self) -> float | None:
        self._skip()
        m = _NUMBER_RE.match(self.text, self.pos)
        if not m:
            return None
        self.pos = m.end()
        return float(m.group(0))

    def numbers(self, count: int) -> list[float] | None:
        values = []
        for _ in range(count):
            v = self.number()
            if v is None:
                return None
            values.append(v)
        return values

    def flag(self) -> bool | None:
        self._skip()
        if self.pos < len(self.text) and self.text[self.pos] in "01":
            self.pos += 1
            return self.text[self.pos - 1] == "1"
        return None


def parse_path_data(d: str, tolerance: float = DEFAULT_TOLERANCE_MM) -> list[tuple[Polyline, bool]]:
    """Attribut « d » d'un <path> → liste de (polyligne, fermée). Courbes et arcs aplatis."""
    scanner = _PathScanner(d or "")
    subpaths: list[tuple[Polyline, bool]] = []
    current: Polyline = []
    closed = False
    cx = cy = 0.0
    last_cubic: Point | None = None
    last_quad: Point | None = None
    cmd: str | None = None

    def flush() -> None:
        nonlocal current, closed
        if len(current) >= 2:
            subpaths.append((current, closed))
        current, closed = [], False

    while True:
        letter = scanner.command()
        if letter is not None:
            if letter not in "MmLlHhVvCcSsQqTtAaZz":
                raise ToolpathError(f"Commande de tracé SVG inconnue « {letter} »")
            cmd = letter
        elif cmd is None:
            break
        elif cmd in "Mm":
            cmd = "L" if cmd == "M" else "l"
        if cmd in "Zz":
            if current:
                if current[0] != current[-1]:
                    current.append(current[0])
                closed = True
                first = current[0]
                flush()
                cx, cy = first
                current = [first]
            cmd, last_cubic, last_quad = None, None, None
            continue
        rel = cmd.islower()
        upper = cmd.upper()
        if not current:
            current = [(cx, cy)]
        if upper == "M":
            values = scanner.numbers(2)
            if values is None:
                break
            x, y = values
            if rel:
                x, y = x + cx, y + cy
            flush()
            current = [(x, y)]
            cx, cy = x, y
        elif upper == "L":
            values = scanner.numbers(2)
            if values is None:
                break
            x, y = values
            if rel:
                x, y = x + cx, y + cy
            current.append((x, y))
            cx, cy = x, y
        elif upper == "H":
            values = scanner.numbers(1)
            if values is None:
                break
            x = values[0] + cx if rel else values[0]
            current.append((x, cy))
            cx = x
        elif upper == "V":
            values = scanner.numbers(1)
            if values is None:
                break
            y = values[0] + cy if rel else values[0]
            current.append((cx, y))
            cy = y
        elif upper in "CS":
            count = 6 if upper == "C" else 4
            values = scanner.numbers(count)
            if values is None:
                break
            if rel:
                values = [v + (cx if i % 2 == 0 else cy) for i, v in enumerate(values)]
            if upper == "C":
                p1, p2, p3 = (values[0], values[1]), (values[2], values[3]), (values[4], values[5])
            else:
                p1 = (2 * cx - last_cubic[0], 2 * cy - last_cubic[1]) if last_cubic else (cx, cy)
                p2, p3 = (values[0], values[1]), (values[2], values[3])
            current.extend(_flatten_cubic((cx, cy), p1, p2, p3, tolerance))
            last_cubic = p2
            cx, cy = p3
        elif upper in "QT":
            count = 4 if upper == "Q" else 2
            values = scanner.numbers(count)
            if values is None:
                break
            if rel:
                values = [v + (cx if i % 2 == 0 else cy) for i, v in enumerate(values)]
            if upper == "Q":
                p1, p2 = (values[0], values[1]), (values[2], values[3])
            else:
                p1 = (2 * cx - last_quad[0], 2 * cy - last_quad[1]) if last_quad else (cx, cy)
                p2 = (values[0], values[1])
            current.extend(_flatten_quadratic((cx, cy), p1, p2, tolerance))
            last_quad = p1
            cx, cy = p2
        elif upper == "A":
            head = scanner.numbers(3)
            large = scanner.flag() if head is not None else None
            sweep = scanner.flag() if large is not None else None
            tail = scanner.numbers(2) if sweep is not None else None
            if tail is None:
                break
            x, y = tail
            if rel:
                x, y = x + cx, y + cy
            current.extend(_endpoint_arc(cx, cy, head[0], head[1], head[2], large, sweep, x, y, tolerance))
            cx, cy = x, y
        if upper not in "CS":
            last_cubic = None
        if upper not in "QT":
            last_quad = None
    flush()
    return subpaths


def _rect_polyline(el: ET.Element, tol: float) -> Polyline | None:
    x, y = _float_attr(el, "x"), _float_attr(el, "y")
    w, h = _float_attr(el, "width"), _float_attr(el, "height")
    if w <= 0 or h <= 0:
        return None
    rx_raw, ry_raw = el.get("rx"), el.get("ry")
    rx = _float_attr(el, "rx") if rx_raw is not None else None
    ry = _float_attr(el, "ry") if ry_raw is not None else None
    if rx is None and ry is None:
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]
    rx = ry if rx is None else rx
    ry = rx if ry is None else ry
    rx, ry = min(max(rx, 0.0), w / 2), min(max(ry, 0.0), h / 2)
    if rx <= 0 or ry <= 0:
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]
    quarter = math.pi / 2
    points: Polyline = [(x + rx, y), (x + w - rx, y)]
    points += _ellipse_arc(x + w - rx, y + ry, rx, ry, 0.0, -quarter, quarter, tol)
    points.append((x + w, y + h - ry))
    points += _ellipse_arc(x + w - rx, y + h - ry, rx, ry, 0.0, 0.0, quarter, tol)
    points.append((x + rx, y + h))
    points += _ellipse_arc(x + rx, y + h - ry, rx, ry, 0.0, quarter, quarter, tol)
    points.append((x, y + ry))
    points += _ellipse_arc(x + rx, y + ry, rx, ry, 0.0, 2 * quarter, quarter, tol)
    points.append(points[0])
    return points


def _ellipse_polyline(cx: float, cy: float, rx: float, ry: float, tol: float) -> Polyline | None:
    if rx <= 0 or ry <= 0:
        return None
    points = [(cx + rx, cy)] + _ellipse_arc(cx, cy, rx, ry, 0.0, 0.0, 2 * math.pi, tol)
    if len(points) < 9:  # au moins 8 segments pour un cercle, même minuscule
        points = [(cx + rx, cy)] + [(cx + rx * math.cos(2 * math.pi * i / 8), cy + ry * math.sin(2 * math.pi * i / 8))
                                   for i in range(1, 9)]
    points[-1] = points[0]
    return points


def _points_attr(el: ET.Element) -> Polyline:
    numbers = [float(v) for v in _NUMBER_RE.findall(el.get("points") or "")]
    return [(numbers[i], numbers[i + 1]) for i in range(0, len(numbers) - 1, 2)]


def _element_polylines(el: ET.Element, tag: str, tol: float) -> list[tuple[Polyline, bool]]:
    if tag == "rect":
        poly = _rect_polyline(el, tol)
        return [(poly, True)] if poly else []
    if tag == "circle":
        r = _float_attr(el, "r")
        poly = _ellipse_polyline(_float_attr(el, "cx"), _float_attr(el, "cy"), r, r, tol)
        return [(poly, True)] if poly else []
    if tag == "ellipse":
        poly = _ellipse_polyline(_float_attr(el, "cx"), _float_attr(el, "cy"),
                                 _float_attr(el, "rx"), _float_attr(el, "ry"), tol)
        return [(poly, True)] if poly else []
    if tag == "line":
        return [([(_float_attr(el, "x1"), _float_attr(el, "y1")), (_float_attr(el, "x2"), _float_attr(el, "y2"))], False)]
    if tag in ("polyline", "polygon"):
        points = _points_attr(el)
        if len(points) < 2:
            return []
        if tag == "polygon":
            if points[0] != points[-1]:
                points.append(points[0])
            return [(points, True)]
        return [(points, False)]
    if tag == "path":
        return parse_path_data(el.get("d") or "", tol)
    return []


def _root_matrix(root: ET.Element, dpi: float) -> tuple[Matrix, float | None, float | None]:
    px = 25.4 / dpi
    width_mm = _length_mm(root.get("width"), dpi)
    height_mm = _length_mm(root.get("height"), dpi)
    view_box = [float(v) for v in _NUMBER_RE.findall(root.get("viewBox") or "")]
    if len(view_box) == 4 and view_box[2] > 0 and view_box[3] > 0:
        vx, vy, vw, vh = view_box
        if width_mm is None or width_mm <= 0:
            width_mm = vw * px if height_mm is None or height_mm <= 0 else height_mm * vw / vh
        if height_mm is None or height_mm <= 0:
            height_mm = width_mm * vh / vw
        sx, sy = width_mm / vw, height_mm / vh
        tx, ty = -vx * sx, -vy * sy
        aspect = (root.get("preserveAspectRatio") or "xMidYMid meet").strip().lower()
        if not aspect.startswith("none") and abs(sx - sy) > 1e-12:
            s = max(sx, sy) if "slice" in aspect else min(sx, sy)
            dx, dy = width_mm - vw * s, height_mm - vh * s
            align = aspect.split()[0]
            fx = 0.0 if "xmin" in align else 1.0 if "xmax" in align else 0.5
            fy = 0.0 if "ymin" in align else 1.0 if "ymax" in align else 0.5
            sx = sy = s
            tx, ty = -vx * s + dx * fx, -vy * s + dy * fy
        return (sx, 0.0, 0.0, sy, tx, ty), width_mm, height_mm
    return (px, 0.0, 0.0, px, 0.0, 0.0), width_mm, height_mm


def parse_svg(text: str | bytes, default_dpi: float = 96.0,
              tolerance_mm: float = DEFAULT_TOLERANCE_MM) -> Document:
    """SVG (texte ou octets) → Document en mm : formes aplaties, calques par couleur de trait.

    Pris en charge : viewBox et unités (mm, cm, in, px, pt, pc), transformations composées,
    rect (rx/ry), circle, ellipse, line, polyline, polygon, path complet (arcs compris), styles
    en attribut, en « style="…" » ou en <style> simple. <text>, <image> et <use> sont ignorés et
    signalés dans ``document.warnings`` ; <defs> et les gradients sont ignorés.
    """
    if isinstance(text, bytes):
        raw: bytes | str = text.lstrip(b"\xef\xbb\xbf")
    else:
        raw = (text or "").lstrip("﻿")
    if len(raw) > MAX_SVG_CHARS:
        raise ToolpathError("SVG trop volumineux", 413, "SVG_TOO_LARGE")
    if not str(raw).strip():
        raise ToolpathError("SVG vide", 400, "SVG_EMPTY")
    dpi = _finite(default_dpi, "default_dpi")
    if dpi <= 0:
        raise ToolpathError("default_dpi doit être positif")
    tol = _finite(tolerance_mm, "tolerance_mm")
    if tol <= 0:
        raise ToolpathError("La tolérance d'aplatissement doit être positive")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ToolpathError(f"SVG illisible : {exc}", 400, "SVG_INVALID") from None
    if _local(root.tag) != "svg":
        raise ToolpathError("Le document n'est pas un SVG (racine <svg> absente)", 400, "SVG_INVALID")

    matrix, width_mm, height_mm = _root_matrix(root, dpi)
    css = _parse_css(root)
    document = Document(width_mm or 0.0, height_mm or 0.0)
    counter = [0]
    warned: set[str] = set()

    def warn(message: str) -> None:
        if message not in warned:
            warned.add(message)
            document.warnings.append(message)

    groups_seen = [0]

    def walk(el: ET.Element, ctm: Matrix, inherited: dict[str, str], group: str = "") -> None:
        tag = _local(el.tag)
        if not tag or tag in _SKIP_TAGS:
            return
        style = _element_style(el, tag, inherited, css)
        if style.get("display", "").lower() == "none":
            return
        ctm = _multiply(ctm, _parse_transform(el.get("transform")))
        if tag in _GROUP_TAGS:
            # Objet « groupe » façon LightBurn : le <g> le plus extérieur, hors calques Inkscape.
            if tag == "g" and not group and el.get(_INKSCAPE_GROUPMODE) != "layer":
                groups_seen[0] += 1
                group = str(el.get("id") or f"group-{groups_seen[0]}")
            for child in el:
                walk(child, ctm, style, group)
            return
        if tag == "text":
            warn("Texte ignoré : convertir le texte en tracés avant l'export")
            return
        if tag == "image":
            warn("Image intégrée ignorée : utiliser raster_image() pour graver une image")
            return
        if tag == "use":
            warn("Élément <use> ignoré : dégrouper ou convertir en tracés")
            return
        if tag not in _SHAPE_TAGS or style.get("visibility", "").lower() in ("hidden", "collapse"):
            return
        tol_user = tol / _matrix_scale(ctm)
        subpaths = _element_polylines(el, tag, tol_user)
        if not subpaths:
            return
        counter[0] += 1
        stroke = normalize_color(style.get("stroke"))
        fill = normalize_color(style.get("fill", "black"))
        layer_key = stroke if stroke != "none" else fill
        paths = [[_apply(ctm, p) for p in poly] for poly, _ in subpaths]
        for poly in paths:
            for x, y in poly:
                if not (math.isfinite(x) and math.isfinite(y)):
                    raise ToolpathError(f"Coordonnée non finie dans la forme {el.get('id') or counter[0]}")
        document.shapes.append(Shape(
            id=str(el.get("id") or f"shape-{counter[0]}"),
            layer_key=layer_key,
            kind=tag,
            paths=paths,
            closed=all(closed for _, closed in subpaths),
            fill=fill != "none",
            group=group,
        ))

    for child in root:
        walk(child, matrix, {})

    if not width_mm or not height_mm or width_mm <= 0 or height_mm <= 0:
        max_x = max((x for s in document.shapes for poly in s.paths for x, _ in poly), default=0.0)
        max_y = max((y for s in document.shapes for poly in s.paths for _, y in poly), default=0.0)
        document.width_mm = width_mm if width_mm and width_mm > 0 else max(max_x, 0.0)
        document.height_mm = height_mm if height_mm and height_mm > 0 else max(max_y, 0.0)
        warn("Taille du document déduite des formes (attributs width/height absents)")
    return document


def layers_from_document(document: Document, template: LayerSettings | None = None) -> dict[str, LayerSettings]:
    """Un LayerSettings par couleur rencontrée (ordre d'apparition), à ajuster par l'utilisateur."""
    layers: dict[str, LayerSettings] = {}
    base = dataclasses.asdict(template) if template else {}
    for shape in document.shapes:
        if shape.layer_key not in layers:
            settings = LayerSettings(**base)
            settings.color = shape.layer_key if shape.layer_key != "none" else "#000000"
            settings.name = settings.name or f"Calque {shape.layer_key}"
            layers[shape.layer_key] = settings
    return layers


# ---------------------------------------------------------------------------
# Hachurage, ordre des tracés
# ---------------------------------------------------------------------------

def hatch(polylines: list[Polyline], interval_mm: float, angle_deg: float = 0.0,
          bidirectional: bool = True) -> list[Polyline]:
    """Lignes parallèles (angle, pas) remplissant les polygones donnés, règle pair-impair, sens alterné
    (``bidirectional``) ou toutes dans le même sens.

    Les polylignes sont fermées implicitement ; plusieurs sous-tracés forment des trous (pair-impair).
    """
    interval = _finite(interval_mm, "interval_mm")
    if interval <= 0:
        raise ToolpathError("L'intervalle de remplissage doit être positif")
    a = math.radians(_finite(angle_deg, "angle_deg"))
    cos_a, sin_a = math.cos(a), math.sin(a)

    def rotate(p: Point) -> Point:
        return (p[0] * cos_a + p[1] * sin_a, -p[0] * sin_a + p[1] * cos_a)

    def unrotate(x: float, y: float) -> Point:
        return (x * cos_a - y * sin_a, x * sin_a + y * cos_a)

    edges: list[tuple[float, float, float, float]] = []
    y_min, y_max = math.inf, -math.inf
    for poly in polylines:
        pts = [rotate(p) for p in poly]
        if len(pts) < 3:
            continue
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            y_min, y_max = min(y_min, y0, y1), max(y_max, y0, y1)
            if y0 != y1:
                edges.append((x0, y0, x1, y1))
    if not edges or y_max - y_min < interval / 2:
        return []
    spans: list[Polyline] = []
    row = 0
    y = y_min + interval / 2
    while y < y_max:
        xs = []
        for x0, y0, x1, y1 in edges:
            if (y0 <= y < y1) or (y1 <= y < y0):
                xs.append(x0 + (y - y0) * (x1 - x0) / (y1 - y0))
        xs.sort()
        pairs = [(xs[i], xs[i + 1]) for i in range(0, len(xs) - 1, 2)]
        if bidirectional and row % 2:
            pairs = [(xb, xa) for xa, xb in reversed(pairs)]
        for xa, xb in pairs:
            if abs(xb - xa) > EPSILON:
                spans.append([unrotate(xa, y), unrotate(xb, y)])
        row += 1
        y = y_min + interval / 2 + row * interval
    return spans


def _point_in_polygon(pt: Point, poly: Polyline) -> bool:
    x, y = pt
    inside = False
    for (x0, y0), (x1, y1) in zip(poly, poly[1:]):
        if (y0 > y) != (y1 > y):
            if x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
                inside = not inside
    return inside


def _is_closed(poly: Polyline) -> bool:
    return len(poly) > 2 and poly[0] == poly[-1]


def _order_nearest(segments: list[Polyline], start: Point) -> list[Polyline]:
    """Plus proche voisin sur les départs : tracé ouvert inversé si sa fin est plus proche,
    tracé fermé démarré au sommet le plus proche."""
    if len(segments) > NEAREST_MAX_SEGMENTS:
        return list(segments)
    remaining = list(segments)
    ordered: list[Polyline] = []
    pos = start
    while remaining:
        best_index, best_dist, reverse = 0, math.inf, False
        for index, seg in enumerate(remaining):
            d0 = _dist(pos, seg[0])
            if d0 < best_dist:
                best_index, best_dist, reverse = index, d0, False
            if not _is_closed(seg):
                d1 = _dist(pos, seg[-1])
                if d1 < best_dist:
                    best_index, best_dist, reverse = index, d1, True
        seg = remaining.pop(best_index)
        if reverse:
            seg = seg[::-1]
        elif _is_closed(seg):
            ring = seg[:-1]
            nearest = min(range(len(ring)), key=lambda i: _dist(pos, ring[i]))
            if nearest:
                ring = ring[nearest:] + ring[:nearest]
                seg = ring + [ring[0]]
        ordered.append(seg)
        pos = seg[-1]
    return ordered


def _order_segments(segments: list[Polyline], order: str, start: Point) -> list[Polyline]:
    if order == "layer" or len(segments) < 2:
        return list(segments)
    if order == "nearest":
        return _order_nearest(segments, start)
    closed = [seg for seg in segments if _is_closed(seg)]
    depth: dict[int, int] = {}
    for index, seg in enumerate(segments):
        depth[index] = sum(1 for other in closed if other is not seg and _point_in_polygon(seg[0], other))
    ordered: list[Polyline] = []
    pos = start
    for level in sorted(set(depth.values()), reverse=True):
        group = _order_nearest([segments[i] for i in range(len(segments)) if depth[i] == level], pos)
        ordered.extend(group)
        if group:
            pos = group[-1][-1]
    return ordered


def _order_groups(groups: list[list[Polyline]], start: Point) -> list[list[Polyline]]:
    remaining = list(groups)
    ordered: list[list[Polyline]] = []
    pos = start
    while remaining:
        index = min(range(len(remaining)), key=lambda i: _dist(pos, remaining[i][0][0]))
        group = remaining.pop(index)
        ordered.append(group)
        pos = group[-1][-1]
    return ordered


# ---------------------------------------------------------------------------
# Décalages de polygones (décalage de trait, remplissage décalé)
# ---------------------------------------------------------------------------
# Méthode de Clipper : chaque anneau est décalé arête par arête (jointure en onglet borné ou arrondie
# côté ouvert, intersection ou retour au sommet côté recouvrement), puis on garde le contour de la
# zone de winding > 0. Les boucles inversées (forme trop petite, goulot pincé) disparaissent ainsi.

def _polyline_length(poly: Polyline) -> float:
    return sum(_dist(a, b) for a, b in zip(poly, poly[1:]))


def _unit(a: Point, b: Point) -> Point | None:
    length = _dist(a, b)
    if length <= _GEOM_EPS:
        return None
    return ((b[0] - a[0]) / length, (b[1] - a[1]) / length)


def _clean_ring(poly: Polyline) -> list[Point]:
    """Anneau en liste ouverte (sans point de fermeture), doublons consécutifs retirés."""
    out: list[Point] = []
    for p in poly:
        if not out or _dist(out[-1], p) > _GEOM_EPS:
            out.append(p)
    while len(out) > 1 and _dist(out[0], out[-1]) <= _GEOM_EPS:
        out.pop()
    return out


def _ring_area(ring: list[Point]) -> float:
    """Aire signée d'un anneau (liste ouverte) : > 0 dans le sens trigonométrique."""
    total = 0.0
    count = len(ring)
    for i in range(count):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % count]
        total += x0 * y1 - x1 * y0
    return total / 2.0


def _rotate_ring_to(ring: list[Point], target: Point) -> list[Point]:
    """Anneau (liste ouverte) démarré au sommet le plus proche de ``target``."""
    if not ring:
        return ring
    index = min(range(len(ring)), key=lambda i: _dist(ring[i], target))
    return ring[index:] + ring[:index]


def _offset_ring(ring: list[Point], d: float, join: str = "miter",
                 tol: float = DEFAULT_TOLERANCE_MM) -> list[Point]:
    """Décalage brut d'un anneau de ``d`` vers la droite de son sens de parcours (l'extérieur d'un
    anneau trigonométrique). Côté ouvert : onglet borné par MITER_LIMIT (biseau au-delà) ou arc
    (``join="round"``). Côté recouvrement : intersection des arêtes décalées quand aucune ne
    s'inverse, sinon retour par le sommet (boucle éliminée ensuite par _union_positive)."""
    count = len(ring)
    dirs: list[Point] = []
    lengths: list[float] = []
    for i in range(count):
        (x0, y0), (x1, y1) = ring[i], ring[(i + 1) % count]
        length = math.hypot(x1 - x0, y1 - y0) or 1e-300
        dirs.append(((x1 - x0) / length, (y1 - y0) / length))
        lengths.append(length)
    ad = abs(d)
    turns: list[tuple[float, float, bool]] = []
    retreat = [0.0] * count
    for i in range(count):
        u0x, u0y = dirs[i - 1]
        u1x, u1y = dirs[i]
        cross = u0x * u1y - u0y * u1x
        dot = u0x * u1x + u0y * u1y
        gap = cross * d > 0 or (abs(cross) <= 1e-12 and dot < 0)
        turns.append((cross, dot, gap))
        if not gap and abs(cross) > 1e-12:
            retreat[i] = ad * abs(cross) / max(1.0 + dot, 1e-12)   # |d|·tan(θ/2)
    step = _angle_step(ad, tol) if join == "round" else 0.0
    out: list[Point] = []
    for i in range(count):
        vx, vy = ring[i]
        u0x, u0y = dirs[i - 1]
        u1x, u1y = dirs[i]
        n0x, n0y = u0y, -u0x
        n1x, n1y = u1y, -u1x
        a = (vx + d * n0x, vy + d * n0y)
        b = (vx + d * n1x, vy + d * n1y)
        cross, dot, gap = turns[i]
        if abs(cross) <= 1e-12 and dot > 0:
            out.append(a)
            continue
        miter_ok = dot > -1.0 + 1e-12
        if gap:
            ratio = math.sqrt(2.0 / (1.0 + dot)) if miter_ok else math.inf
            steps = 1
            if join == "round" and step > 0:
                theta = math.atan2(cross, dot) if abs(cross) > 1e-12 else (math.pi if d > 0 else -math.pi)
                steps = int(math.ceil(abs(theta) / step))
                if steps > 1:
                    phi0 = math.atan2(n0y, n0x)
                    out.extend((vx + d * math.cos(phi0 + theta * k / steps), vy + d * math.sin(phi0 + theta * k / steps))
                               for k in range(steps + 1))
                    continue
            if ratio <= MITER_LIMIT:
                f = d / (1.0 + dot)
                out.append((vx + f * (n0x + n1x), vy + f * (n0y + n1y)))
            else:
                out.extend((a, b))
        else:
            safe = (miter_ok and lengths[i - 1] >= retreat[i - 1] + retreat[i] - 1e-12
                    and lengths[i] >= retreat[i] + retreat[(i + 1) % count] - 1e-12)
            if safe:
                f = d / (1.0 + dot)
                out.append((vx + f * (n0x + n1x), vy + f * (n0y + n1y)))
            else:
                out.extend((a, (vx, vy), b))
    return out


class _WindingIndex:
    """Nombre d'enroulement d'un point par rapport à des arêtes orientées, indexées par bandes de Y."""

    __slots__ = ("y0", "h", "buckets")

    def __init__(self, segments: list[tuple[float, float, float, float]]):
        segments = [s for s in segments if s[1] != s[3]]
        self.buckets: list[list[tuple[float, float, float, float]]] = []
        self.y0 = 0.0
        self.h = 1.0
        if not segments:
            return
        y_min = min(min(s[1], s[3]) for s in segments)
        y_max = max(max(s[1], s[3]) for s in segments)
        count = max(1, min(4096, int(math.sqrt(len(segments)) * 2)))
        self.y0 = y_min
        self.h = (y_max - y_min) / count or 1.0
        self.buckets = [[] for _ in range(count)]
        for s in segments:
            lo = int((min(s[1], s[3]) - y_min) / self.h)
            hi = int((max(s[1], s[3]) - y_min) / self.h)
            for index in range(max(0, lo), min(count - 1, hi) + 1):
                self.buckets[index].append(s)
        for bucket in self.buckets:      # X max décroissant : les arêtes à gauche du point ne comptent pas
            bucket.sort(key=lambda s: -max(s[0], s[2]))

    def winding(self, x: float, y: float) -> int:
        if not self.buckets:
            return 0
        index = int(math.floor((y - self.y0) / self.h))
        if index < 0 or index >= len(self.buckets):
            return 0
        w = 0
        for x0, y0, x1, y1 in self.buckets[index]:
            if x0 < x and x1 < x:
                break
            if y0 <= y:
                if y1 > y and (x1 - x0) * (y - y0) - (x - x0) * (y1 - y0) > 0:
                    w += 1
            elif y1 <= y and (x1 - x0) * (y - y0) - (x - x0) * (y1 - y0) < 0:
                w -= 1
        return w


def _side_windings(a: Point, b: Point, index: _WindingIndex) -> tuple[int, int]:
    """Enroulements (gauche, droite) juste de part et d'autre du milieu de l'arête a → b."""
    length = _dist(a, b)
    eps = min(1e-7, length * 1e-3)
    ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
    mx, my = (a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0
    return index.winding(mx - uy * eps, my + ux * eps), index.winding(mx + uy * eps, my - ux * eps)


def _edge_contact(edges: list[tuple[Point, Point, int]], k: int, j: int,
                  splits: list[list[tuple[float, Point]]], nodes: set[Point]) -> bool:
    """Contact entre deux arêtes (croisement, sommet posé sur une arête, recouvrement colinéaire) :
    enregistre les points de coupe et les nœuds (points de contact), renvoie True. Deux arêtes
    voisines d'un même anneau sont ignorées."""
    p, p2, ring_k = edges[k]
    q, q2, ring_j = edges[j]
    if ring_k == ring_j and (p2 == q or q2 == p):
        return False
    rx, ry = p2[0] - p[0], p2[1] - p[1]
    sx, sy = q2[0] - q[0], q2[1] - q[1]
    lr, ls = math.hypot(rx, ry), math.hypot(sx, sy)
    tol_t, tol_u = _GEOM_EPS / lr, _GEOM_EPS / ls
    qpx, qpy = q[0] - p[0], q[1] - p[1]
    denom = rx * sy - ry * sx
    if abs(denom) <= 1e-12 * lr * ls:
        if abs(qpx * ry - qpy * rx) > _GEOM_EPS * lr:
            return False
        touched = False
        for point in (q, q2):
            t = ((point[0] - p[0]) * rx + (point[1] - p[1]) * ry) / (lr * lr)
            if -tol_t <= t <= 1 + tol_t:
                touched = True
                nodes.add(point)
                nodes.add(p if t <= tol_t else p2 if t >= 1 - tol_t else point)
            if tol_t < t < 1 - tol_t:
                splits[k].append((t, point))
        for point in (p, p2):
            u = ((point[0] - q[0]) * sx + (point[1] - q[1]) * sy) / (ls * ls)
            if -tol_u <= u <= 1 + tol_u:
                touched = True
                nodes.add(point)
                nodes.add(q if u <= tol_u else q2 if u >= 1 - tol_u else point)
            if tol_u < u < 1 - tol_u:
                splits[j].append((u, point))
        return touched
    t = (qpx * sy - qpy * sx) / denom
    u = (qpx * ry - qpy * rx) / denom
    if t < -tol_t or t > 1 + tol_t or u < -tol_u or u > 1 + tol_u:
        return False
    if t <= tol_t:
        point = p
    elif t >= 1 - tol_t:
        point = p2
    elif u <= tol_u:
        point = q
    elif u >= 1 - tol_u:
        point = q2
    else:
        point = (p[0] + t * rx, p[1] + t * ry)
    nodes.add(point)
    # Sommets confondus à peu près (écart < 1e-9 mm) : les deux sont des nœuds.
    nodes.add(p if t <= tol_t else p2 if t >= 1 - tol_t else point)
    nodes.add(q if u <= tol_u else q2 if u >= 1 - tol_u else point)
    if tol_t < t < 1 - tol_t:
        splits[k].append((t, point))
    if tol_u < u < 1 - tol_u:
        splits[j].append((u, point))
    return True


def _candidate_pairs(edges: list[tuple[Point, Point, int]]):
    """Paires d'arêtes dont les boîtes (élargies de _GEOM_EPS) se touchent, chacune une seule fois :
    grille uniforme (paire traitée dans la cellule du coin bas-gauche de l'intersection des boîtes),
    les arêtes couvrant trop de cellules étant comparées à toutes les autres."""
    eps = _GEOM_EPS
    boxes = [(min(a[0], b[0]) - eps, max(a[0], b[0]) + eps, min(a[1], b[1]) - eps, max(a[1], b[1]) + eps)
             for a, b, _ in edges]
    sizes = sorted(max(box[1] - box[0], box[3] - box[2]) for box in boxes)
    h = max(sizes[len(sizes) // 2] * 2.0, 1e-6)
    grid: dict[tuple[int, int], list[int]] = {}
    long_edges: list[int] = []
    for k, (x0, x1, y0, y1) in enumerate(boxes):
        ix0, ix1, iy0, iy1 = math.floor(x0 / h), math.floor(x1 / h), math.floor(y0 / h), math.floor(y1 / h)
        if (ix1 - ix0 + 1) * (iy1 - iy0 + 1) > 256:
            long_edges.append(k)
            continue
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                grid.setdefault((ix, iy), []).append(k)

    def overlap(k: int, j: int) -> bool:
        a, b = boxes[k], boxes[j]
        return a[0] <= b[1] and b[0] <= a[1] and a[2] <= b[3] and b[2] <= a[3]

    for (ix, iy), members in grid.items():
        for m, k in enumerate(members):
            for j in members[m + 1:]:
                if overlap(k, j) and math.floor(max(boxes[k][0], boxes[j][0]) / h) == ix \
                        and math.floor(max(boxes[k][2], boxes[j][2]) / h) == iy:
                    yield k, j
    is_long = set(long_edges)
    for k in long_edges:
        for j in range(len(edges)):
            if j != k and (j not in is_long or j > k) and overlap(k, j):
                yield k, j


def _union_positive(rings: list[list[Point]]) -> list[list[Point]]:
    """Contours (listes ouvertes, orientés) de la zone de winding > 0 d'un ensemble d'anneaux."""
    edges: list[tuple[Point, Point, int]] = []
    ring_edges: list[list[int]] = []
    for ring_index, ring in enumerate(rings):
        ids: list[int] = []
        count = len(ring)
        if count >= 3:
            for i in range(count):
                a, b = ring[i], ring[(i + 1) % count]
                if a != b:
                    ids.append(len(edges))
                    edges.append((a, b, ring_index))
        ring_edges.append(ids)
    if not edges:
        return []
    splits: list[list[tuple[float, Point]]] = [[] for _ in edges]
    nodes: set[Point] = set()
    touched = [False] * len(rings)
    for k, j in _candidate_pairs(edges):
        if _edge_contact(edges, k, j, splits, nodes):
            touched[edges[k][2]] = True
            touched[edges[j][2]] = True
    index = _WindingIndex([(a[0], a[1], b[0], b[1]) for a, b, _ in edges])
    result: list[list[Point]] = []
    pieces: list[tuple[Point, Point]] = []

    def key(p: Point) -> tuple[int, int]:
        return (round(p[0] * 1e6), round(p[1] * 1e6))

    seen: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    for ring_index, ids in enumerate(ring_edges):
        if not ids:
            continue
        if not touched[ring_index]:
            # Aucun contact : les enroulements de part et d'autre sont les mêmes tout le long de l'anneau.
            longest = max(ids, key=lambda e: _dist(edges[e][0], edges[e][1]))
            left, right = _side_windings(edges[longest][0], edges[longest][1], index)
            if left > 0 >= right:
                result.append(_clean_ring(rings[ring_index]))
            continue
        # Les enroulements ne changent qu'aux nœuds (points de contact) : un seul calcul par chaîne.
        keep: bool | None = None
        for k in ids:
            a, b, _ = edges[k]
            points = [a] + [point for _, point in sorted(splits[k], key=lambda item: item[0])] + [b]
            for s0, s1 in zip(points, points[1:]):
                if _dist(s0, s1) <= _GEOM_EPS:
                    keep = None
                    continue
                if keep is None or s0 in nodes:
                    # Bord de la zone : intérieur (winding > 0) à gauche, extérieur à droite. Deux arêtes
                    # confondues de sens opposés s'annulent ; de même sens, une seule est gardée.
                    left, right = _side_windings(s0, s1, index)
                    keep = left > 0 >= right
                signature = (key(s0), key(s1))
                if keep and signature not in seen:
                    seen.add(signature)
                    pieces.append((s0, s1))

    outgoing: dict[tuple[int, int], list[int]] = {}
    for i, (s0, _) in enumerate(pieces):
        outgoing.setdefault(key(s0), []).append(i)
    used = [False] * len(pieces)
    for i, (s0, s1) in enumerate(pieces):
        if used[i]:
            continue
        used[i] = True
        start = key(s0)
        loop = [s0, s1]
        current = key(s1)
        while current != start:
            candidates = [j for j in outgoing.get(current, ()) if not used[j]]
            if not candidates:
                break
            dx, dy = loop[-1][0] - loop[-2][0], loop[-1][1] - loop[-2][1]

            def turn(j: int) -> float:
                ex, ey = pieces[j][1][0] - pieces[j][0][0], pieces[j][1][1] - pieces[j][0][1]
                return math.atan2(dx * ey - dy * ex, dx * ex + dy * ey)

            chosen = max(candidates, key=turn)       # virage le plus à gauche : boucles séparées
            used[chosen] = True
            loop.append(pieces[chosen][1])
            current = key(pieces[chosen][1])
        if current == start:
            result.append(_clean_ring(loop[:-1]))
    return [ring for ring in result if len(ring) >= 3 and abs(_ring_area(ring)) > 1e-12]


def _nesting_depths(polylines: list[Polyline]) -> list[int]:
    """Pour chaque tracé fermé (premier point = dernier), nombre d'autres tracés qui contiennent son
    premier point (boîtes englobantes testées d'abord : coût raisonnable avec beaucoup de formes)."""
    boxes = [(min(x for x, _ in poly), min(y for _, y in poly), max(x for x, _ in poly), max(y for _, y in poly))
             for poly in polylines]
    depths = []
    for i, poly in enumerate(polylines):
        px, py = poly[0]
        depths.append(sum(1 for j, other in enumerate(polylines)
                          if j != i and boxes[j][0] <= px <= boxes[j][2] and boxes[j][1] <= py <= boxes[j][3]
                          and _point_in_polygon(poly[0], other)))
    return depths


def _orient_even_odd(rings: list[list[Point]]) -> list[list[Point]]:
    """Anneaux orientés selon la règle pair-impair : extérieurs trigonométriques, trous horaires."""
    depths = _nesting_depths([ring + [ring[0]] for ring in rings])
    return [ring if (_ring_area(ring) > 0) == (depth % 2 == 0) else ring[::-1] for ring, depth in zip(rings, depths)]


def _region_offset(rings: list[list[Point]], d: float, join: str = "miter",
                   tol: float = DEFAULT_TOLERANCE_MM) -> list[list[Point]]:
    """Zone (anneaux orientés) décalée de ``d`` : > 0 agrandit, < 0 rétrécit."""
    return _union_positive([_offset_ring(ring, d, join, tol) for ring in rings if len(ring) >= 3])


def _kerf_polylines(polylines: list[Polyline], kerf: float) -> tuple[list[Polyline], list[int], list[int]]:
    """Décalage de trait : chaque tracé fermé est décalé de ``kerf`` (pièces agrandies, trous réduits
    pour kerf > 0), en gardant son sens de parcours et un départ proche de l'original. Renvoie les
    tracés, les indices des tracés ouverts (inchangés) et des tracés trop petits (conservés)."""
    closed_indices = [i for i, poly in enumerate(polylines) if _is_closed(poly)]
    depths = dict(zip(closed_indices, _nesting_depths([polylines[i] for i in closed_indices])))
    out: list[Polyline] = []
    open_indices: list[int] = []
    vanished: list[int] = []
    for i, poly in enumerate(polylines):
        if not _is_closed(poly):
            out.append(poly)
            open_indices.append(i)
            continue
        ring = _clean_ring(poly)
        area = _ring_area(ring) if len(ring) >= 3 else 0.0
        if abs(area) <= 1e-12:
            out.append(poly)
            vanished.append(i)
            continue
        signed = kerf if depths[i] % 2 == 0 else -kerf
        loops = _region_offset([ring if area > 0 else ring[::-1]], signed, "miter")
        if not loops:
            out.append(poly)
            vanished.append(i)
            continue
        for loop in loops:
            if area < 0:
                loop = loop[::-1]
            loop = _rotate_ring_to(loop, poly[0])
            out.append(loop + [loop[0]])
    return out, open_indices, vanished


def _offset_fill_levels(polylines: list[Polyline], interval: float) -> tuple[list[list[list[Point]]], bool]:
    """Remplissage décalé : contours concentriques vers l'intérieur (premier à interval/2 du bord,
    puis pas ``interval``) jusqu'à disparition de la forme. Renvoie les niveaux et un drapeau de
    troncature (MAX_OFFSET_POINTS atteint)."""
    rings = [ring for ring in (_clean_ring(poly) for poly in polylines)
             if len(ring) >= 3 and abs(_ring_area(ring)) > 1e-12]
    if not rings:
        return [], False
    xs = [x for ring in rings for x, _ in ring]
    ys = [y for ring in rings for _, y in ring]
    max_levels = int(min(max(xs) - min(xs), max(ys) - min(ys)) / interval) + 3
    tol = min(DEFAULT_TOLERANCE_MM, interval / 4.0)
    levels: list[list[list[Point]]] = []
    current = _region_offset(_orient_even_odd(rings), -interval / 2.0, "round", tol)
    points = 0
    while current:
        levels.append(current)
        points += sum(len(ring) for ring in current)
        if points > MAX_OFFSET_POINTS or len(levels) >= max_levels:
            return levels, True
        current = _region_offset(current, -interval, "round", tol)
    return levels, False


# ---------------------------------------------------------------------------
# Ligne : ponts, perforation, entrée/sortie, surcoupe ; balayage : surbalayage, rampe
# ---------------------------------------------------------------------------

def _quantize_power(power_pct: float, machine: MachineProfile) -> float:
    """Puissance (%) arrondie à un S entier de la machine (rampes, ponts)."""
    maximum = machine.max_power if machine.kind == "laser" else machine.spindle_rpm_max
    if maximum <= 0:
        return max(0.0, min(100.0, power_pct))
    return max(0.0, min(100.0, round(power_pct / 100.0 * maximum) / maximum * 100.0))


def _ray_room(p: Point, u: Point, bounds: tuple[float, float, float, float]) -> float:
    """Distance disponible depuis ``p`` dans la direction ``u`` avant de sortir du lit."""
    x0, y0, x1, y1 = bounds
    room = math.inf
    if u[0] > 1e-12:
        room = min(room, (x1 - p[0]) / u[0])
    elif u[0] < -1e-12:
        room = min(room, (x0 - p[0]) / u[0])
    if u[1] > 1e-12:
        room = min(room, (y1 - p[1]) / u[1])
    elif u[1] < -1e-12:
        room = min(room, (y0 - p[1]) / u[1])
    return max(0.0, room)


def _walk(poly: Polyline, length: float) -> Polyline:
    """Début du tracé ``poly`` sur ``length`` mm."""
    out = [poly[0]]
    remaining = length
    for a, b in zip(poly, poly[1:]):
        if remaining <= _GEOM_EPS:
            break
        step = _dist(a, b)
        if step <= remaining:
            out.append(b)
            remaining -= step
        else:
            t = remaining / step
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            break
    return out


def _start_direction(path: Polyline) -> Point | None:
    for a, b in zip(path, path[1:]):
        u = _unit(a, b)
        if u:
            return u
    return None


def _end_direction(path: Polyline) -> Point | None:
    for i in range(len(path) - 1, 0, -1):
        u = _unit(path[i - 1], path[i])
        if u:
            return u
    return None


def _project_on_polyline(p: Point, poly: Polyline) -> tuple[float, float]:
    """(distance, abscisse curviligne) du point de ``poly`` le plus proche de ``p``."""
    best = (math.inf, 0.0)
    travelled = 0.0
    for a, b in zip(poly, poly[1:]):
        length = _dist(a, b)
        if length <= 0:
            continue
        t = ((p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])) / (length * length)
        t = max(0.0, min(1.0, t))
        distance = math.hypot(p[0] - (a[0] + t * (b[0] - a[0])), p[1] - (a[1] + t * (b[1] - a[1])))
        if distance < best[0]:
            best = (distance, travelled + t * length)
        travelled += length
    return best


def _split_path(path: Polyline, cumulative: list[float], breaks: list[float]) -> list[tuple[Polyline, float, float]]:
    """Tracé coupé aux abscisses ``breaks`` (croissantes) : morceaux contigus (polyligne, début, fin)."""
    pieces: list[tuple[Polyline, float, float]] = []
    current: Polyline = [path[0]]
    start = 0.0
    index = 0
    for i in range(1, len(path)):
        a, b = path[i - 1], path[i]
        sa, sb = cumulative[i - 1], cumulative[i]
        while index < len(breaks) and breaks[index] < sb - _GEOM_EPS:
            mark = breaks[index]
            index += 1
            if mark <= sa + _GEOM_EPS:
                if len(current) >= 2:
                    pieces.append((current, start, sa))
                    current, start = [a], sa
                continue
            t = (mark - sa) / (sb - sa)
            point = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            current.append(point)
            pieces.append((current, start, mark))
            current, start = [point], mark
        current.append(b)
    if len(current) >= 2:
        pieces.append((current, start, cumulative[-1]))
    return pieces


def _split_by_power(path: Polyline, base: float, intervals: list[tuple[float, float, float]],
                    perforation: tuple[float, float] | None) -> list[tuple[Polyline, float]]:
    """Morceaux contigus du tracé avec leur puissance : ``base`` réduite sur les intervalles
    (ponts) et coupée sur les « skip » de la perforation (cut allumé, skip éteint, depuis le départ)."""
    cumulative = [0.0]
    for a, b in zip(path, path[1:]):
        cumulative.append(cumulative[-1] + _dist(a, b))
    total = cumulative[-1]
    if total <= _GEOM_EPS:
        return [(path, base)]
    marks = {a for a, _, _ in intervals} | {b for _, b, _ in intervals}
    period = 0.0
    if perforation:
        cut, skip = perforation
        period = cut + skip
        k = 0
        while k * period < total:
            marks.add(k * period + cut)
            marks.add((k + 1) * period)
            k += 1
    breaks = sorted(m for m in marks if _GEOM_EPS < m < total - _GEOM_EPS)
    out: list[tuple[Polyline, float]] = []
    for piece, s0, s1 in _split_path(path, cumulative, breaks):
        middle = (s0 + s1) / 2.0
        power = base
        if perforation and middle % period >= perforation[0]:
            power = 0.0
        for a, b, reduced in intervals:
            if a <= middle <= b:
                power = min(power, reduced)
        if out and out[-1][1] == power:
            out[-1][0].extend(piece[1:])
        else:
            out.append((list(piece), power))
    return out


def _tab_centers(segments: list[Polyline], closed: list[bool], settings: LayerSettings,
                 convert: Callable[[Point], Point], label: str, warnings: list[str]) -> dict[int, list[float]]:
    """Centres des ponts (abscisse sur chaque tracé) : répartis automatiquement sur les formes
    fermées, ou points manuels (coordonnées document) projetés sur le tracé le plus proche."""
    lengths = [_polyline_length(seg) for seg in segments]
    centers: dict[int, list[float]] = {}
    if settings.tabs_mode == "manual":
        for point in settings.tabs_points:
            target = convert((float(point[0]), float(point[1])))
            best: tuple[float, int, float] | None = None
            for index, seg in enumerate(segments):
                distance, position = _project_on_polyline(target, seg)
                if best is None or distance < best[0]:
                    best = (distance, index, position)
            if best is not None:
                centers.setdefault(best[1], []).append(best[2])
        if settings.tabs_max_enabled:
            centers = {index: values[:max(0, int(settings.tabs_max))] for index, values in centers.items()}
    else:
        inner: set[int] = set()
        if settings.tabs_skip_inner:
            ring_indices = [index for index, is_closed in enumerate(closed) if is_closed]
            depths = _nesting_depths([segments[index] for index in ring_indices])
            inner = {index for index, depth in zip(ring_indices, depths) if depth > 0}
        for index, seg in enumerate(segments):
            if not closed[index] or index in inner or lengths[index] <= 0:
                continue
            if settings.tab_spacing_mode == "per_shape":
                count = int(settings.tabs_per_shape)
            else:
                count = max(1, int(lengths[index] / settings.tab_spacing_mm + 0.5))
            if settings.tabs_max_enabled:
                count = min(count, int(settings.tabs_max))
            if count > 0:
                step = lengths[index] / count
                centers[index] = [(k + 0.5) * step for k in range(count)]   # le départ tombe entre deux ponts
    for index in [i for i, values in centers.items() if values]:
        if len(centers[index]) * settings.tab_size_mm >= lengths[index]:
            warnings.append(f"Calque {label} : ponts plus grands que la forme (tracé {index + 1}), ponts ignorés")
            del centers[index]
    return centers


def _decorate_lines(segments: list[Polyline], settings: LayerSettings, machine: MachineProfile,
                    bounds: tuple[float, float, float, float], convert: Callable[[Point], Point],
                    label: str, warnings: list[str]) -> tuple[list[Polyline], list[float] | None]:
    """Mode ligne : surcoupe, entrée/sortie tangentes (formes fermées), puis ponts et perforation
    (laser) en morceaux contigus de puissance explicite. Sans réglage actif : tracés inchangés."""
    lead_in, lead_out, overcut = float(settings.lead_in_mm), float(settings.lead_out_mm), float(settings.overcut_mm)
    geometric = lead_in > 0 or lead_out > 0 or overcut > 0
    modulate = bool(settings.perforation or settings.tabs_enabled)
    if modulate and machine.kind != "laser":
        warnings.append(f"Calque {label} : perforation et ponts ignorés sur une CNC")
        modulate = False
    if not geometric and not modulate:
        return segments, None
    closed = [_is_closed(seg) for seg in segments]
    if geometric and not all(closed):
        warnings.append(f"Calque {label} : entrée, sortie et surcoupe appliquées aux seules formes fermées")
    base = float(settings.power_pct)
    tabs = _tab_centers(segments, closed, settings, convert, label, warnings) if modulate and settings.tabs_enabled else {}
    tab_power = _quantize_power(base * float(settings.tab_power_pct) / 100.0, machine)
    perforation = (float(settings.perf_cut_mm), float(settings.perf_skip_mm)) if modulate and settings.perforation else None
    half_tab = float(settings.tab_size_mm) / 2.0
    out_segments: list[Polyline] = []
    out_powers: list[float] = []
    clipped = False
    for index, seg in enumerate(segments):
        path = list(seg)
        loop = _polyline_length(seg)
        lead = extra = 0.0
        if closed[index] and geometric:
            if overcut > 0 and loop > 0:
                extra = min(overcut, loop)
                path += _walk(seg, extra)[1:]
            u = _start_direction(path) if lead_in > 0 else None
            if u:
                lead = min(lead_in, _ray_room(path[0], (-u[0], -u[1]), bounds))
                clipped |= lead < lead_in - 1e-9
                if lead > _GEOM_EPS:
                    path.insert(0, (path[0][0] - u[0] * lead, path[0][1] - u[1] * lead))
                else:
                    lead = 0.0
            u = _end_direction(path) if lead_out > 0 else None
            if u:
                length = min(lead_out, _ray_room(path[-1], u, bounds))
                clipped |= length < lead_out - 1e-9
                if length > _GEOM_EPS:
                    path.append((path[-1][0] + u[0] * length, path[-1][1] + u[1] * length))
        if not modulate:
            out_segments.append(path)
            continue
        intervals: list[tuple[float, float, float]] = []
        for center in tabs.get(index, ()):
            if closed[index]:
                for shift in (-loop, 0.0, loop):          # pont à cheval sur le départ ou dans la surcoupe
                    a, b = max(center - half_tab + shift, 0.0), min(center + half_tab + shift, loop + extra)
                    if b > a:
                        intervals.append((a + lead, b + lead, tab_power))
            else:
                a, b = max(center - half_tab, 0.0), min(center + half_tab, loop)
                if b > a:
                    intervals.append((a, b, tab_power))
        for piece, power in _split_by_power(path, base, intervals, perforation):
            out_segments.append(piece)
            out_powers.append(power)
    if clipped:
        warnings.append(f"Calque {label} : entrée ou sortie raccourcie par les limites du lit")
    return out_segments, (out_powers if modulate else None)


def _ramp_pieces(stretch: list[tuple[Point, Point, float]], ramp: float, min_power: float,
                 machine: MachineProfile) -> list[tuple[Polyline, float]]:
    """Segments brûlés contigus (a, b, puissance) avec rampe linéaire de ``ramp`` mm à chaque bout."""
    if ramp <= 0:
        return [([a, b], power) for a, b, power in stretch]
    total = sum(_dist(a, b) for a, b, _ in stretch)
    steps = max(2, min(MAX_RAMP_STEPS, int(math.ceil(ramp / RAMP_STEP_MM))))
    h = ramp / steps
    marks = sorted({k * h for k in range(1, steps + 1)} | {total - k * h for k in range(1, steps + 1)})
    out: list[tuple[Polyline, float]] = []
    offset = 0.0
    for a, b, power in stretch:
        length = _dist(a, b)
        cuts = [0.0] + [m - offset for m in marks if offset + _GEOM_EPS < m < offset + length - _GEOM_EPS] + [length]
        low = min(min_power, power)
        previous = a
        for c0, c1 in zip(cuts, cuts[1:]):
            point = b if c1 == length else (a[0] + (b[0] - a[0]) * c1 / length, a[1] + (b[1] - a[1]) * c1 / length)
            middle = offset + (c0 + c1) / 2.0
            factor = max(0.0, min(1.0, middle / ramp, (total - middle) / ramp))
            level = _quantize_power(low + (power - low) * factor, machine)
            if out and out[-1][1] == level:
                out[-1][0].append(point)
            else:
                out.append(([previous, point], level))
            previous = point
        offset += length
    return out


def _decorate_scan(segments: list[Polyline], powers: list[float] | None, base: float, overscan: float,
                   ramp: float, min_power: float, machine: MachineProfile,
                   bounds: tuple[float, float, float, float]) -> tuple[list[Polyline], list[float], bool]:
    """Remplissage et image : les segments colinéaires consécutifs dans le même sens forment une
    rangée ; surbalayage laser éteint (G1 S0) de ``overscan`` mm avant et après chaque rangée, trous
    de moins de 2 × overscan franchis en G1 S0, rampe sur chaque suite de segments brûlés contigus.
    Renvoie les segments, leurs puissances et True si le lit a raccourci un surbalayage."""
    out_segments: list[Polyline] = []
    out_powers: list[float] = []
    clipped = False

    def add(poly: Polyline, power: float) -> None:
        out_segments.append(poly)
        out_powers.append(power)

    runs = [(seg, powers[i] if powers is not None else base) for i, seg in enumerate(segments)]
    i = 0
    while i < len(runs):
        seg, power = runs[i]
        u = _unit(seg[0], seg[-1]) if len(seg) == 2 else None
        if u is None:
            add(seg, power)
            i += 1
            continue
        row = [runs[i]]
        j = i + 1
        while j < len(runs):
            nxt = runs[j][0]
            v = _unit(nxt[0], nxt[-1]) if len(nxt) == 2 else None
            if v is None or abs(v[0] - u[0]) > 1e-9 or abs(v[1] - u[1]) > 1e-9:
                break
            gx, gy = nxt[0][0] - row[-1][0][1][0], nxt[0][1] - row[-1][0][1][1]
            if abs(gx * u[1] - gy * u[0]) > 1e-6 or gx * u[0] + gy * u[1] < -1e-9:
                break
            row.append(runs[j])
            j += 1
        stretches: list[list[tuple[Point, Point, float]]] = []
        for (a, b), level in ((r[0], r[1]) for r in row):
            if stretches and stretches[-1][-1][1] == a:
                stretches[-1].append((a, b, level))
            else:
                stretches.append([(a, b, level)])
        start = stretches[0][0][0]
        if overscan > 0:
            room = min(overscan, _ray_room(start, (-u[0], -u[1]), bounds))
            clipped |= room < overscan - 1e-9
            if room > _GEOM_EPS:
                add([(start[0] - u[0] * room, start[1] - u[1] * room), start], 0.0)
        for index, stretch in enumerate(stretches):
            for poly, level in _ramp_pieces(stretch, ramp, min_power, machine):
                add(poly, level)
            end = stretch[-1][1]
            if index + 1 < len(stretches):
                nxt = stretches[index + 1][0][0]
                gap = (nxt[0] - end[0]) * u[0] + (nxt[1] - end[1]) * u[1]
                if overscan > 0 and gap < 2 * overscan:
                    add([end, nxt], 0.0)
                elif overscan > 0:
                    add([end, (end[0] + u[0] * overscan, end[1] + u[1] * overscan)], 0.0)
                    add([(nxt[0] - u[0] * overscan, nxt[1] - u[1] * overscan), nxt], 0.0)
            elif overscan > 0:
                room = min(overscan, _ray_room(end, u, bounds))
                clipped |= room < overscan - 1e-9
                if room > _GEOM_EPS:
                    add([end, (end[0] + u[0] * room, end[1] + u[1] * room)], 0.0)
        i = j
    return out_segments, out_powers, clipped


# ---------------------------------------------------------------------------
# Image → opération raster
# ---------------------------------------------------------------------------

def _image_matrix(image: Any, cols: int | None, rows: int | None) -> list[list[int]]:
    """Niveaux de gris 0-255 (0 = noir) rééchantillonnés sur cols × rows (None : taille native)."""
    if isinstance(image, (list, tuple)):
        source = [list(row) for row in image]
        if not source or not source[0]:
            raise ToolpathError("Image vide")
        src_h, src_w = len(source), len(source[0])
        if cols is None or rows is None:
            if any(len(row) != src_w for row in source):
                raise ToolpathError("Image : toutes les lignes de la matrice doivent avoir la même longueur")
            cols, rows = src_w, src_h
        return [[max(0, min(255, int(source[min(src_h - 1, r * src_h // rows)][min(src_w - 1, c * src_w // cols)])))
                 for c in range(cols)] for r in range(rows)]
    if _PILImage is None:
        raise ToolpathError("Pillow n'est pas installé : impossible de lire cette image", 501, "PILLOW_MISSING")
    if isinstance(image, (str, Path)):
        path = Path(image)
        if not path.is_file():
            raise ToolpathError(f"Image introuvable : {path.name}", 404, "IMAGE_NOT_FOUND")
        with _PILImage.open(path) as opened:
            pil = opened.convert("L")
    elif isinstance(image, (bytes, bytearray)):
        if len(image) > MAX_IMAGE_BYTES:
            raise ToolpathError("Image trop volumineuse", 413, "IMAGE_TOO_LARGE")
        with _PILImage.open(io.BytesIO(bytes(image))) as opened:
            pil = opened.convert("L")
    elif hasattr(image, "convert") and hasattr(image, "resize"):
        pil = image.convert("L")
    else:
        raise ToolpathError("Image non reconnue (chemin, octets, Image Pillow ou matrice attendus)")
    if cols is None or rows is None:
        cols, rows = pil.size
        if cols * rows > 25_000_000:
            raise ToolpathError("Image trop grande pour le relais (plus de 25 millions de pixels)")
        data = list(pil.getdata())
        return [[int(v) for v in data[r * cols:(r + 1) * cols]] for r in range(rows)]
    resampling = getattr(getattr(_PILImage, "Resampling", _PILImage), "LANCZOS")
    resized = pil.resize((cols, rows), resampling)
    data = list(resized.getdata())
    return [[int(v) for v in data[r * cols:(r + 1) * cols]] for r in range(rows)]


def _bayer_matrix(size: int) -> list[list[int]]:
    matrix = [[0]]
    while len(matrix) < size:
        matrix = ([[4 * v for v in row] + [4 * v + 2 for v in row] for row in matrix]
                  + [[4 * v + 3 for v in row] + [4 * v + 1 for v in row] for row in matrix])
    return matrix


_BAYER8 = _bayer_matrix(8)
# Noyaux de diffusion d'erreur : (dx, dy, poids), diviseur. Atkinson ne diffuse que 6/8 de l'erreur.
_DIFFUSION_KERNELS: dict[str, tuple[tuple[tuple[int, int, int], ...], int]] = {
    "atkinson": (((1, 0, 1), (2, 0, 1), (-1, 1, 1), (0, 1, 1), (1, 1, 1), (0, 2, 1)), 8),
    "stucki": (((1, 0, 8), (2, 0, 4), (-2, 1, 2), (-1, 1, 4), (0, 1, 8), (1, 1, 4), (2, 1, 2),
                (-2, 2, 1), (-1, 2, 2), (0, 2, 4), (1, 2, 2), (2, 2, 1)), 42),
    "jarvis": (((1, 0, 7), (2, 0, 5), (-2, 1, 3), (-1, 1, 5), (0, 1, 7), (1, 1, 5), (2, 1, 3),
                (-2, 2, 1), (-1, 2, 3), (0, 2, 5), (1, 2, 3), (2, 2, 1)), 48),
}
_HALFTONE_CACHE: dict[int, float] = {}


def _error_diffusion(darkness: list[list[float]], kernel: tuple[tuple[int, int, int], ...], divisor: int) -> list[list[int]]:
    rows, cols = len(darkness), len(darkness[0])
    work = [list(row) for row in darkness]
    weights = [(dx, dy, w / divisor) for dx, dy, w in kernel]
    levels: list[list[int]] = []
    for r in range(rows):
        current = work[r]
        out_row = []
        for c in range(cols):
            old = current[c]
            new = 1.0 if old >= 0.5 else 0.0
            out_row.append(100 if new else 0)
            err = old - new
            if err:
                for dx, dy, w in weights:
                    cc, rr = c + dx, r + dy
                    if 0 <= cc < cols and rr < rows:
                        work[rr][cc] += err * w
        levels.append(out_row)
    return levels


def _halftone_radius2(darkness: float) -> float:
    """Rayon² (cellule ramenée à [-1, 1]²) du point dont la surface couvre ``darkness`` de la cellule."""
    if darkness <= 0:
        return -1.0
    if darkness >= 1:
        return 3.0
    key = int(round(darkness * 4096))
    if key in _HALFTONE_CACHE:
        return _HALFTONE_CACHE[key]
    target = key / 4096.0
    if target <= math.pi / 4:
        value = 4.0 * target / math.pi
    else:   # disque rogné par la cellule : bissection sur le rayon entre 1 et √2
        lo, hi = 1.0, math.sqrt(2.0)
        for _ in range(40):
            mid = (lo + hi) / 2.0
            area = math.pi * mid * mid - 4.0 * (mid * mid * math.acos(1.0 / mid) - math.sqrt(mid * mid - 1.0))
            lo, hi = (mid, hi) if area / 4.0 < target else (lo, mid)
        value = ((lo + hi) / 2.0) ** 2
    _HALFTONE_CACHE[key] = value
    return value


def _screen_levels(algorithm: str, darkness: list[list[float]], px_w: float, px_h: float,
                   settings: LayerSettings | None) -> list[list[int]]:
    """Trames binaires (100 = brûlé) calculées en Python pur sur la matrice d'obscurité (0-1)."""
    rows, cols = len(darkness), len(darkness[0])
    if algorithm == "threshold":
        return [[100 if v >= 0.5 else 0 for v in row] for row in darkness]
    if algorithm == "ordered":
        return [[100 if v > (_BAYER8[r % 8][c % 8] + 0.5) / 64.0 else 0 for c, v in enumerate(row)]
                for r, row in enumerate(darkness)]
    if algorithm in _DIFFUSION_KERNELS:
        kernel, divisor = _DIFFUSION_KERNELS[algorithm]
        return _error_diffusion(darkness, kernel, divisor)
    if algorithm in ("newsprint", "halftone"):
        cells = float(settings.halftone_cells_per_inch) if settings else 50.0
        angle = math.radians(float(settings.halftone_angle_deg) if settings else 22.5)
        pitch = 25.4 / cells
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        levels = []
        for r, row in enumerate(darkness):
            y = (r + 0.5) * px_h
            out_row = []
            for c, v in enumerate(row):
                x = (c + 0.5) * px_w
                if algorithm == "newsprint":      # lignes centrées sur la trame, largeur ∝ obscurité
                    t = ((-x * sin_a + y * cos_a) / pitch) % 1.0
                    out_row.append(100 if abs(t - 0.5) * 2.0 < v else 0)
                else:                             # points ronds au centre de chaque cellule
                    fu = ((x * cos_a + y * sin_a) / pitch) % 1.0 - 0.5
                    fv = ((-x * sin_a + y * cos_a) / pitch) % 1.0 - 0.5
                    out_row.append(100 if 4.0 * (fu * fu + fv * fv) < _halftone_radius2(v) else 0)
            levels.append(out_row)
        return levels
    if algorithm == "sketch":
        clamped = [[max(0.0, min(1.0, v)) for v in row] for row in darkness]
        padded = [[row[0]] + row + [row[-1]] for row in [clamped[0]] + clamped + [clamped[-1]]]
        levels = []
        for r in range(rows):
            up, mid, down = padded[r], padded[r + 1], padded[r + 2]
            out_row = []
            for c in range(cols):
                gx = (up[c + 2] + 2 * mid[c + 2] + down[c + 2]) - (up[c] + 2 * mid[c] + down[c])
                gy = (down[c] + 2 * down[c + 1] + down[c + 2]) - (up[c] + 2 * up[c + 1] + up[c + 2])
                out_row.append(100 if math.hypot(gx, gy) >= SKETCH_THRESHOLD else 0)
            levels.append(out_row)
        return levels
    raise ToolpathError(f"Algorithme d'image inconnu « {algorithm} »")


def _shrink_stretches(spans: list[tuple[float, float, int]], shrink: float) -> list[tuple[float, float, int]]:
    """Correction de largeur de point : chaque suite de pixels brûlés contigus perd ``shrink`` mm à
    chaque bout (au plus 45 % de sa longueur : le segment n'est jamais annulé)."""
    out: list[tuple[float, float, int]] = []
    i = 0
    while i < len(spans):
        j = i
        while j + 1 < len(spans) and spans[j + 1][0] == spans[j][1]:
            j += 1
        a, b = spans[i][0], spans[j][1]
        cut = min(shrink, (b - a) * 0.45)
        lo, hi = a + cut, b - cut
        for xa, xb, level in spans[i:j + 1]:
            xa, xb = max(xa, lo), min(xb, hi)
            if xb - xa > _GEOM_EPS:
                out.append((xa, xb, level))
        i = j + 1
    return out


def _validate_image_settings(settings: LayerSettings, key: str = "image") -> None:
    """Réglages propres à la trame (les autres restent contrôlés par le plan)."""
    if settings.image_mode and settings.image_mode not in IMAGE_ALGORITHMS:
        raise ToolpathError(f"Calque {key} : algorithme d'image inconnu « {settings.image_mode} »")
    minimum = _finite(settings.min_power_pct, f"min_power_pct du calque {key}")
    if not 0 <= minimum <= 100:
        raise ToolpathError(f"Calque {key} : la puissance minimale doit être entre 0 et 100 %")
    if minimum > _finite(settings.power_pct, f"power_pct du calque {key}"):
        raise ToolpathError(f"Calque {key} : la puissance minimale dépasse la puissance maximale")
    if _finite(settings.dot_width_mm, f"dot_width_mm du calque {key}") < 0:
        raise ToolpathError(f"Calque {key} : la largeur de point ne peut pas être négative")
    if settings.image_mode in ("halftone", "newsprint") and \
            _finite(settings.halftone_cells_per_inch, f"halftone_cells_per_inch du calque {key}") <= 0:
        raise ToolpathError(f"Calque {key} : le nombre de trames par pouce doit être positif")
    _finite(settings.halftone_angle_deg, f"halftone_angle_deg du calque {key}")
    if not 0 <= _finite(settings.overscan_pct, f"overscan_pct du calque {key}") <= 100:
        raise ToolpathError(f"Calque {key} : le surbalayage doit être entre 0 et 100 %")
    if _finite(settings.ramp_length_mm, f"ramp_length_mm du calque {key}") < 0:
        raise ToolpathError(f"Calque {key} : la longueur de rampe ne peut pas être négative")


def raster_image(image: Any, width_mm: float, height_mm: float, interval_mm: float = 0.1,
                 mode: str = "grayscale", invert: bool = False, settings: LayerSettings | None = None,
                 offset_mm: Point = (0.0, 0.0)) -> Operation:
    """Image → opération « image » : lignes horizontales en va-et-vient, puissance par pixel.

    ``image`` : chemin, octets, Image Pillow, ou matrice de niveaux de gris (0 = noir) sans Pillow.
    ``mode`` : « grayscale » (puissance proportionnelle à l'obscurité) ou « dither »
    (Floyd-Steinberg, pixels noirs à pleine puissance). Les pixels voisins de même puissance
    forment un segment ; les blancs sont sautés. Coordonnées dans le repère document (mm,
    Y vers le bas), coin haut-gauche de l'image en ``offset_mm``.

    ``settings`` (facultatif) : ``image_mode`` (un des IMAGE_ALGORITHMS) prime sur ``mode`` ;
    ``negative`` inverse l'image (XOR avec ``invert``) ; ``min_power_pct`` = puissance du gris le
    plus clair en niveaux de gris (le blanc pur reste sauté) ; ``bidirectional`` False : toutes les
    lignes de gauche à droite ; ``dot_width_correction`` raccourcit les segments brûlés ;
    ``pass_through`` grave l'image telle quelle (grille native, pixels > 50 % à pleine puissance).
    Surbalayage et rampe sont portés par l'opération et appliqués par plan() (limites du lit).
    """
    width = _finite(width_mm, "width_mm")
    height = _finite(height_mm, "height_mm")
    interval = _finite(interval_mm, "interval_mm")
    if width <= 0 or height <= 0 or interval <= 0:
        raise ToolpathError("Largeur, hauteur et intervalle de l'image doivent être positifs")
    overriding_algorithm = settings.image_mode if settings is not None and settings.image_mode else ""
    # « mode » n'est qu'un repli historique : une valeur qu'il ne connaît pas (un algorithme de
    # settings.image_mode, envoyé aussi ici par compatibilité) ne doit pas bloquer si ce dernier
    # la prend de toute façon en charge (incident : job laser-studio, 27/09, mode « stucki »).
    if not overriding_algorithm and mode not in IMAGE_MODES:
        raise ToolpathError(f"Mode image inconnu « {mode} » (grayscale ou dither)")
    if settings is not None:
        _validate_image_settings(settings, settings.name or "image")
    algorithm = overriding_algorithm or mode
    pass_through = bool(settings is not None and settings.pass_through)
    if pass_through:
        matrix = _image_matrix(image, None, None)
        rows, cols = len(matrix), len(matrix[0])
        if cols * rows > 25_000_000:
            raise ToolpathError("Image trop grande pour le relais (plus de 25 millions de pixels)")
    else:
        cols = max(1, int(round(width / interval)))
        rows = max(1, int(round(height / interval)))
        if cols * rows > 25_000_000:
            raise ToolpathError("Image trop fine pour cet intervalle (plus de 25 millions de pixels)")
        matrix = _image_matrix(image, cols, rows)
    ox, oy = _finite(offset_mm[0], "offset_mm"), _finite(offset_mm[1], "offset_mm")
    scale = (settings.power_pct if settings else 100.0) / 100.0
    flip = bool(invert) != bool(settings is not None and settings.negative)
    darkness = [[(v if flip else 255 - v) / 255.0 for v in row] for row in matrix]
    levels: list[list[int]]
    if pass_through:
        levels = [[100 if v > 0.5 else 0 for v in row] for row in darkness]
    elif algorithm not in ("dither", "grayscale"):
        levels = _screen_levels(algorithm, darkness, width / cols, height / rows, settings)
    elif algorithm == "dither":
        levels = []
        for r in range(rows):
            out_row = []
            for c in range(cols):
                old = darkness[r][c]
                new = 1.0 if old >= 0.5 else 0.0
                out_row.append(100 if new else 0)
                err = old - new
                if c + 1 < cols:
                    darkness[r][c + 1] += err * 7 / 16
                if r + 1 < rows:
                    if c > 0:
                        darkness[r + 1][c - 1] += err * 3 / 16
                    darkness[r + 1][c] += err * 5 / 16
                    if c + 1 < cols:
                        darkness[r + 1][c + 1] += err * 1 / 16
            levels.append(out_row)
    else:
        levels = [[int(round(max(0.0, min(1.0, v)) * 100)) for v in row] for row in darkness]

    # Niveaux de gris avec puissance minimale : 1 % d'obscurité → min_power_pct, noir → power_pct.
    min_power = float(settings.min_power_pct) if settings is not None and algorithm == "grayscale" \
        and not pass_through else 0.0
    peak = float(settings.power_pct) if settings else 100.0

    def level_power(level: int) -> float:
        if min_power > 0:
            return round(min_power + (peak - min_power) * level / 100.0, 3)
        return round(level * scale, 3)

    bidirectional = settings.bidirectional if settings is not None else True
    shrink = float(settings.dot_width_mm) / 2.0 if settings is not None and settings.dot_width_correction else 0.0
    px_w, px_h = width / cols, height / rows
    segments: list[Polyline] = []
    powers: list[float] = []
    for r, row in enumerate(levels):
        y = oy + (r + 0.5) * px_h
        runs: list[tuple[int, int, int]] = []
        c = 0
        while c < cols:
            level = row[c]
            end = c
            while end + 1 < cols and row[end + 1] == level:
                end += 1
            if level > 0:
                runs.append((c, end, level))
            c = end + 1
        spans = [(ox + start * px_w, ox + (end + 1) * px_w, level) for start, end, level in runs]
        if shrink > 0:
            spans = _shrink_stretches(spans, shrink)
        if bidirectional and r % 2:
            for xa, xb, level in reversed(spans):
                segments.append([(xb, y), (xa, y)])
                powers.append(level_power(level))
        else:
            for xa, xb, level in spans:
                segments.append([(xa, y), (xb, y)])
                powers.append(level_power(level))
    return Operation(
        layer="image", mode="image", segments=segments, powers=powers,
        passes=settings.passes if settings else 1,
        power_pct=settings.power_pct if settings else 100.0,
        speed_mm_min=settings.speed_mm_min if settings else 3000.0,
        z_offset_mm=settings.z_offset_mm if settings else 0.0,
        z_step_mm=settings.z_step_mm if settings else 0.0,
        air_assist=settings.air_assist if settings else False,
        name=(settings.name if settings and settings.name else "Image"),
        constant_power=bool(settings.constant_power) if settings else False,
        overscan_pct=float(settings.overscan_pct) if settings else 0.0,
        ramp_length_mm=float(settings.ramp_length_mm) if settings else 0.0,
        min_power_pct=float(settings.min_power_pct) if settings else 0.0,
    )


def _sublayer_settings(settings: LayerSettings, key: str) -> list[LayerSettings]:
    """Sous-calques (réglages complets) d'un calque ; un seul niveau (sous-sous-calques ignorés)."""
    raw = settings.sublayers or []
    if not isinstance(raw, (list, tuple)):
        raise ToolpathError(f"Calque {key} : sublayers doit être une liste de réglages")
    result = []
    for index, item in enumerate(raw, 1):
        if isinstance(item, LayerSettings):
            result.append(dataclasses.replace(item, sublayers=[]))
        elif isinstance(item, dict):
            result.append(from_dict(LayerSettings, {k: v for k, v in item.items() if k != "sublayers"}))
        else:
            raise ToolpathError(f"Calque {key} : sous-calque {index} invalide (objet de réglages attendu)")
    return result


def raster_operations(image: Any, width_mm: float, height_mm: float, interval_mm: float = 0.1,
                      mode: str = "grayscale", invert: bool = False, settings: LayerSettings | None = None,
                      offset_mm: Point = (0.0, 0.0), name: str = "") -> list[Operation]:
    """raster_image() + une opération par sous-calque actif (même image, réglages du sous-calque,
    son propre ``interval_mm``), dans l'ordre. ``name`` remplace le nom de l'opération principale."""
    main = raster_image(image, width_mm, height_mm, interval_mm, mode, invert, settings, offset_mm)
    if name:
        main.name = name
    operations = [main]
    if settings is None:
        return operations
    for index, sub in enumerate(_sublayer_settings(settings, main.name), 1):
        if not sub.enabled:
            continue
        interval = _finite(sub.interval_mm, "interval_mm")
        operation = raster_image(image, width_mm, height_mm, interval if interval > 0 else interval_mm,
                                 mode, invert, sub, offset_mm)
        operation.name = sub.name or f"{main.name} (sous-calque {index})"
        operations.append(operation)
    return operations


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def _validate_machine(machine: MachineProfile) -> None:
    if machine.kind not in MACHINE_KINDS:
        raise ToolpathError(f"Type de machine inconnu « {machine.kind} » (laser ou cnc)")
    if machine.origin not in MACHINE_ORIGINS:
        raise ToolpathError(f"Origine machine inconnue « {machine.origin} »")
    for label in ("width_mm", "height_mm", "max_power", "travel_speed_mm_min"):
        if _finite(getattr(machine, label), label) <= 0:
            raise ToolpathError(f"Le réglage machine {label} doit être positif")
    if machine.kind == "cnc":
        for label in ("plunge_rate", "spindle_rpm_max"):
            if _finite(getattr(machine, label), label) <= 0:
                raise ToolpathError(f"Le réglage CNC {label} doit être positif")
        _finite(machine.safe_z_mm, "safe_z_mm")
    if str(machine.laser_type or "diode").lower() not in LASER_TYPES:
        raise ToolpathError(f"Type de laser inconnu « {machine.laser_type} » (diode, co2, fiber ou infrared)")
    for label in ("laser_power_w", "max_speed_mm_min"):
        if _finite(getattr(machine, label), label) < 0:
            raise ToolpathError(f"Le réglage machine {label} ne peut pas être négatif")


def _validate_lightburn(settings: LayerSettings, key: str) -> None:
    """Réglages LightBurn d'un calque ; les valeurs par défaut passent toujours."""
    def number(label: str) -> float:
        return _finite(getattr(settings, label), f"{label} du calque {key}")

    for label in ("min_power_pct", "tab_power_pct", "overscan_pct"):
        if not 0 <= number(label) <= 100:
            raise ToolpathError(f"Calque {key} : {label} doit être entre 0 et 100 %")
    for label in ("lead_in_mm", "lead_out_mm", "overcut_mm", "ramp_length_mm", "dot_width_mm"):
        if number(label) < 0:
            raise ToolpathError(f"Calque {key} : {label} ne peut pas être négatif")
    if (settings.mode == "image" or settings.ramp_length_mm > 0) and settings.min_power_pct > settings.power_pct:
        raise ToolpathError(f"Calque {key} : la puissance minimale dépasse la puissance maximale")
    number("kerf_offset_mm")
    number("halftone_angle_deg")
    if settings.perforation:
        for label in ("perf_cut_mm", "perf_skip_mm"):
            if number(label) < MIN_PERFORATION_MM:
                raise ToolpathError(f"Calque {key} : {label} doit valoir au moins {MIN_PERFORATION_MM} mm")
    choices = (("tabs_mode", TABS_MODES), ("tab_spacing_mode", TAB_SPACING_MODES),
               ("fill_grouping", FILL_GROUPINGS), ("image_mode", IMAGE_ALGORITHMS))
    for label, allowed in choices:
        value = getattr(settings, label)
        if value and value not in allowed:
            raise ToolpathError(f"Calque {key} : {label} inconnu « {value} » ({', '.join(allowed)})")
    if settings.tabs_enabled:
        if number("tab_size_mm") <= 0:
            raise ToolpathError(f"Calque {key} : la taille des ponts doit être positive")
        if settings.tab_spacing_mode == "per_shape":
            if not isinstance(settings.tabs_per_shape, int) or settings.tabs_per_shape < 1:
                raise ToolpathError(f"Calque {key} : tabs_per_shape doit être un entier ≥ 1")
        elif number("tab_spacing_mm") <= 0:
            raise ToolpathError(f"Calque {key} : l'espacement des ponts doit être positif")
        if settings.tabs_max_enabled and (not isinstance(settings.tabs_max, int) or settings.tabs_max < 0):
            raise ToolpathError(f"Calque {key} : tabs_max doit être un entier ≥ 0")
        for point in settings.tabs_points or []:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ToolpathError(f"Calque {key} : tabs_points attend des couples [x, y]")
            _finite(point[0], "tabs_points")
            _finite(point[1], "tabs_points")
    if settings.image_mode in ("halftone", "newsprint") and number("halftone_cells_per_inch") <= 0:
        raise ToolpathError(f"Calque {key} : le nombre de trames par pouce doit être positif")
    for index, sub in enumerate(_sublayer_settings(settings, key), 1):
        if sub.enabled:
            _validate_layer(sub, f"{key} (sous-calque {index})")


def _validate_layer(settings: LayerSettings, key: str) -> None:
    if settings.mode not in LAYER_MODES:
        raise ToolpathError(f"Calque {key} : mode inconnu « {settings.mode} »")
    power = _finite(settings.power_pct, f"power_pct du calque {key}")
    if not 0 <= power <= 100:
        raise ToolpathError(f"Calque {key} : la puissance doit être entre 0 et 100 %")
    if _finite(settings.speed_mm_min, f"speed_mm_min du calque {key}") <= 0:
        raise ToolpathError(f"Calque {key} : la vitesse doit être positive")
    if not isinstance(settings.passes, int) or isinstance(settings.passes, bool) or settings.passes < 1:
        raise ToolpathError(f"Calque {key} : le nombre de passes doit être un entier ≥ 1")
    for label in ("z_offset_mm", "z_step_mm", "angle_deg"):
        _finite(getattr(settings, label), f"{label} du calque {key}")
    if settings.mode != "line" and _finite(settings.interval_mm, f"interval_mm du calque {key}") <= 0:
        raise ToolpathError(f"Calque {key} : l'intervalle doit être positif")
    _validate_lightburn(settings, key)


def _validate_operation(op: Operation, machine: MachineProfile) -> None:
    if op.mode not in LAYER_MODES:
        raise ToolpathError(f"Opération {op.name or op.layer} : mode inconnu « {op.mode} »")
    if not isinstance(op.passes, int) or isinstance(op.passes, bool) or op.passes < 1:
        raise ToolpathError(f"Opération {op.name or op.layer} : passes ≥ 1 attendu")
    if not 0 <= _finite(op.power_pct, "power_pct") <= 100:
        raise ToolpathError(f"Opération {op.name or op.layer} : puissance entre 0 et 100 % attendue")
    if _finite(op.speed_mm_min, "speed_mm_min") <= 0:
        raise ToolpathError(f"Opération {op.name or op.layer} : vitesse positive attendue")
    _finite(op.z_offset_mm, "z_offset_mm")
    _finite(op.z_step_mm, "z_step_mm")
    if not 0 <= _finite(op.overscan_pct, "overscan_pct") <= 100 or _finite(op.ramp_length_mm, "ramp_length_mm") < 0 \
            or not 0 <= _finite(op.min_power_pct, "min_power_pct") <= 100:
        raise ToolpathError(f"Opération {op.name or op.layer} : surbalayage, rampe ou puissance minimale hors plage")
    if op.powers is not None:
        if len(op.powers) != len(op.segments):
            raise ToolpathError(f"Opération {op.name or op.layer} : une puissance par segment attendue")
        for p in op.powers:
            if not 0 <= _finite(p, "powers") <= 100:
                raise ToolpathError(f"Opération {op.name or op.layer} : puissance de segment hors 0-100 %")
    for seg in op.segments:
        if len(seg) < 2:
            raise ToolpathError(f"Opération {op.name or op.layer} : segment de moins de deux points")
        for x, y in seg:
            _finite(x, "x")
            _finite(y, "y")
    if machine.kind == "cnc" and op.z_offset_mm >= machine.safe_z_mm:
        raise ToolpathError(f"Opération {op.name or op.layer} : la profondeur Z doit rester sous la hauteur de sécurité")


def _bed_bounds(machine: MachineProfile) -> tuple[float, float, float, float]:
    if machine.origin == "center":
        return (-machine.width_mm / 2, -machine.height_mm / 2, machine.width_mm / 2, machine.height_mm / 2)
    return (0.0, 0.0, machine.width_mm, machine.height_mm)


def _document_to_machine(document: Document, machine: MachineProfile, offset: Point) -> Callable[[Point], Point]:
    width, height = document.width_mm, document.height_mm
    ox, oy = _finite(offset[0], "offset_mm"), _finite(offset[1], "offset_mm")
    mirror_x = "right" in machine.origin
    center = machine.origin == "center"
    y_up = machine.y_up

    def convert(p: Point) -> Point:
        x, y = p
        if center:
            mx = x - width / 2
            my = (height / 2 - y) if y_up else (y - height / 2)
        else:
            mx = (width - x) if mirror_x else x
            my = (height - y) if y_up else y
        return (mx + ox, my + oy)

    return convert


def _check_bed(operations: list[Operation], machine: MachineProfile) -> None:
    points = [p for op in operations for seg in op.segments for p in seg]
    if not points:
        return
    min_x = min(x for x, _ in points)
    max_x = max(x for x, _ in points)
    min_y = min(y for _, y in points)
    max_y = max(y for _, y in points)
    bx0, by0, bx1, by1 = _bed_bounds(machine)
    if min_x < bx0 - EPSILON or max_x > bx1 + EPSILON or min_y < by0 - EPSILON or max_y > by1 + EPSILON:
        raise ToolpathError(
            f"Le parcours sort du lit de la machine « {machine.name} » "
            f"({_fmt_short(machine.width_mm)} × {_fmt_short(machine.height_mm)} mm) : "
            f"X de {_fmt(min_x)} à {_fmt(max_x)} mm, Y de {_fmt(min_y)} à {_fmt(max_y)} mm "
            f"(lit : X {_fmt(bx0)}..{_fmt(bx1)}, Y {_fmt(by0)}..{_fmt(by1)})",
            400, "OUT_OF_BED")


@dataclass
class _Move:
    x0: float
    y0: float
    z0: float
    x1: float
    y1: float
    z1: float
    feed: float
    power_pct: float
    rapid: bool


def _build_moves(operations: list[Operation], machine: MachineProfile) -> list[Any]:
    """Séquence de déplacements (et repères « op », « pass », « end ») que le G-code émet et que
    les statistiques mesurent : une seule source pour rester cohérent avec simulate()."""
    items: list[Any] = []
    x = y = z = 0.0
    cnc = machine.kind == "cnc"
    travel = machine.travel_speed_mm_min

    def rapid(nx: float, ny: float, nz: float) -> None:
        nonlocal x, y, z
        if (nx, ny, nz) != (x, y, z):
            items.append(_Move(x, y, z, nx, ny, nz, travel, 0.0, True))
            x, y, z = nx, ny, nz

    def cut(nx: float, ny: float, nz: float, feed: float, power: float) -> None:
        nonlocal x, y, z
        if (nx, ny, nz) != (x, y, z):
            items.append(_Move(x, y, z, nx, ny, nz, feed, power, False))
            x, y, z = nx, ny, nz

    if cnc:
        rapid(x, y, machine.safe_z_mm)
    for op in operations:
        items.append(("op", op))
        for pass_index in range(op.passes):
            items.append(("pass", pass_index + 1))
            level = op.z_offset_mm + op.z_step_mm * pass_index
            if not cnc and level != z:
                rapid(x, y, level)
            for index, seg in enumerate(op.segments):
                power = op.powers[index] if op.powers is not None else op.power_pct
                sx, sy = seg[0]
                if cnc:
                    rapid(x, y, machine.safe_z_mm)
                    rapid(sx, sy, machine.safe_z_mm)
                    cut(sx, sy, level, machine.plunge_rate, power)
                else:
                    rapid(sx, sy, z)
                for px, py in seg[1:]:
                    cut(px, py, z, op.speed_mm_min, power)
            if cnc:
                rapid(x, y, machine.safe_z_mm)
    items.append(("end", None))
    rapid(0.0, 0.0, z)
    return items


def _stats_from_moves(items: list[Any]) -> dict[str, Any]:
    cut_length = travel_length = burn_length = seconds = 0.0
    xs: list[float] = []
    ys: list[float] = []
    for item in items:
        if not isinstance(item, _Move):
            continue
        xy = math.hypot(item.x1 - item.x0, item.y1 - item.y0)
        xyz = math.sqrt((item.x1 - item.x0) ** 2 + (item.y1 - item.y0) ** 2 + (item.z1 - item.z0) ** 2)
        if item.rapid:
            travel_length += xy
        else:
            cut_length += xy                    # tout G1, y compris laser éteint (surbalayage, ponts)
            if item.power_pct > 0:
                burn_length += xy
            xs.extend((item.x0, item.x1))
            ys.extend((item.y0, item.y1))
        seconds += xyz / item.feed * 60.0 if item.feed > 0 else 0.0
    bounds = {"min_x": min(xs), "min_y": min(ys), "max_x": max(xs), "max_y": max(ys)} if xs else \
        {"min_x": 0.0, "min_y": 0.0, "max_x": 0.0, "max_y": 0.0}
    return {
        "cut_length_mm": round(cut_length, 3),
        "travel_length_mm": round(travel_length, 3),
        "burn_length_mm": round(burn_length, 3),
        "estimated_seconds": round(seconds, 3),
        "bounds": {k: round(v, 3) for k, v in bounds.items()},
    }


def _bounded_speed(speed: float, machine: MachineProfile, label: str, warnings: list[str]) -> float:
    """Vitesse de calque bornée à ``machine.max_speed_mm_min`` (si > 0), avec avertissement."""
    speed = float(speed)
    limit = float(machine.max_speed_mm_min or 0.0)
    if limit > 0 and speed > limit:
        warnings.append(f"{label} : vitesse {_fmt_short(speed)} mm/min bornée à {_fmt_short(limit)} mm/min "
                        f"(maximum de la machine « {machine.name} »)")
        return limit
    return speed


def _fill_groups(shapes: list[Shape], grouping: str) -> list[list[Shape]]:
    """Champs de balayage : toutes les formes ensemble, par groupe SVG, ou forme par forme."""
    if grouping == "all":
        return [list(shapes)]
    if grouping == "groups":
        buckets: dict[Any, list[Shape]] = {}
        ordered: list[list[Shape]] = []
        for index, shape in enumerate(shapes):
            key = ("group", shape.group) if shape.group else ("shape", index)
            if key not in buckets:
                buckets[key] = []
                ordered.append(buckets[key])
            buckets[key].append(shape)
        return ordered
    return [[shape] for shape in shapes]


def _offset_fill_contours(polylines: list[Polyline], settings: LayerSettings, label: str,
                          warnings: list[str]) -> list[Polyline]:
    """Contours concentriques fermés (extérieur d'abord), chacun démarré près de la fin du précédent ;
    en va-et-vient, un niveau sur deux est parcouru en sens inverse."""
    levels, truncated = _offset_fill_levels(polylines, float(settings.interval_mm))
    if truncated:
        warnings.append(f"Calque {label} : remplissage décalé tronqué (forme trop complexe pour ce pas)")
    contours: list[Polyline] = []
    for level_index, rings in enumerate(levels):
        for ring in rings:
            if settings.bidirectional and level_index % 2:
                ring = ring[::-1]
            if contours:
                ring = _rotate_ring_to(ring, contours[-1][-1])
            contours.append(ring + [ring[0]])
    return contours


def _vector_operation(key: str, label: str, name: str, settings: LayerSettings, shapes: list[Shape],
                      convert: Callable[[Point], Point], machine: MachineProfile, order: str, pos: Point,
                      bounds: tuple[float, float, float, float], warnings: list[str]) -> Operation | None:
    """Une opération (calque ou sous-calque) sur les formes d'une couleur ; None si rien à usiner."""
    speed = _bounded_speed(settings.speed_mm_min, machine, f"Calque {label}", warnings)
    powers: list[float] | None = None
    prepared = False
    if settings.mode == "line":
        owners = [shape for shape in shapes for _ in shape.paths]
        polylines = [[convert(p) for p in poly] for shape in shapes for poly in shape.paths]
        if settings.kerf_offset_mm:
            polylines, open_indices, vanished = _kerf_polylines(polylines, float(settings.kerf_offset_mm))
            for shape_id in dict.fromkeys(owners[i].id for i in open_indices):
                warnings.append(f"Calque {label} : décalage de trait impossible sur la forme ouverte {shape_id} "
                                "(tracé conservé)")
            for shape_id in dict.fromkeys(owners[i].id for i in vanished):
                warnings.append(f"Calque {label} : décalage de trait trop grand pour la forme {shape_id} "
                                "(tracé d'origine conservé)")
        segments = _order_segments(polylines, order, pos)
        segments, powers = _decorate_lines(segments, settings, machine, bounds, convert, label, warnings)
    elif settings.mode in ("fill", "offset_fill"):
        span_groups: list[list[Polyline]] = []
        for group in _fill_groups(shapes, settings.fill_grouping):
            for shape in group:
                if not shape.closed:
                    warnings.append(f"Calque {label} : la forme {shape.id} n'est pas fermée, remplissage approximatif")
            paths = [poly for shape in group for poly in shape.paths]
            if settings.mode == "fill":
                spans = hatch(paths, settings.interval_mm, settings.angle_deg, settings.bidirectional)
                if settings.crosshatch:
                    spans += hatch(paths, settings.interval_mm, settings.angle_deg + 90.0, settings.bidirectional)
                if spans:
                    span_groups.append([[convert(p) for p in span] for span in spans])
            else:
                contours = _offset_fill_contours([[convert(p) for p in poly] for poly in paths], settings, label,
                                                 warnings)
                if contours:
                    span_groups.append(contours)
        if order != "layer":
            span_groups = _order_groups(span_groups, pos)
        segments = [span for group in span_groups for span in group]
        if settings.mode == "fill" and segments and (settings.overscan_pct > 0 or settings.ramp_length_mm > 0):
            if machine.kind == "laser":
                overscan = float(settings.overscan_pct) / 100.0 * speed / 60.0
                segments, powers, clipped = _decorate_scan(segments, None, float(settings.power_pct), overscan,
                                                           float(settings.ramp_length_mm),
                                                           float(settings.min_power_pct), machine, bounds)
                prepared = True
                if clipped:
                    warnings.append(f"Calque {label} : surbalayage réduit par les limites du lit")
            else:
                warnings.append(f"Calque {label} : surbalayage et rampe ignorés sur une CNC")
    else:
        raise ToolpathError(f"Calque {key} : le mode image se règle avec raster_image(), pas sur un calque vectoriel")
    if not segments:
        return None
    fill = settings.mode == "fill"
    return Operation(
        layer=key, mode=settings.mode, segments=segments, passes=settings.passes,
        power_pct=float(settings.power_pct), speed_mm_min=speed,
        z_offset_mm=float(settings.z_offset_mm), z_step_mm=float(settings.z_step_mm),
        air_assist=bool(settings.air_assist), name=name, powers=powers,
        constant_power=bool(settings.constant_power),
        overscan_pct=float(settings.overscan_pct) if fill else 0.0,
        ramp_length_mm=float(settings.ramp_length_mm) if fill else 0.0,
        min_power_pct=float(settings.min_power_pct) if fill else 0.0,
        prepared=prepared,
    )


def plan(document: Document | None, layers: dict[str, LayerSettings], machine: MachineProfile,
         order: str = "layer", images: list[Operation] | None = None,
         offset_mm: Point = (0.0, 0.0)) -> Plan:
    """Document + réglages par calque + machine → Plan (opérations en coordonnées machine, stats).

    ``order`` : « layer » (ordre des calques puis des formes du document), « nearest » (plus proche
    voisin dans chaque calque), « inside-out » (formes intérieures avant les contours qui les
    entourent, puis plus proche voisin). Les opérations ``images`` (voir raster_image) passent en
    premier : on grave avant de découper. Les calques vectoriels suivent l'ordre du dict ``layers``
    (l'ordre choisi dans le studio), chacun suivi de ses sous-calques actifs. Refuse un parcours
    qui sort du lit.
    """
    if order not in PLAN_ORDERS:
        raise ToolpathError(f"Ordre d'usinage inconnu « {order} »")
    _validate_machine(machine)
    if document is None:
        document = Document(machine.width_mm, machine.height_mm)
    convert = _document_to_machine(document, machine, offset_mm)
    warnings = list(document.warnings)
    operations: list[Operation] = []
    pos: Point = (0.0, 0.0)
    bounds = _bed_bounds(machine)

    for op in images or []:
        if op.mode != "image":
            raise ToolpathError("Seules des opérations « image » (raster_image) peuvent être ajoutées au plan")
        mapped = dataclasses.replace(op, segments=[[convert(p) for p in seg] for seg in op.segments],
                                     powers=list(op.powers) if op.powers is not None else None)
        label = f"Image {mapped.name or mapped.layer}"
        mapped.speed_mm_min = _bounded_speed(mapped.speed_mm_min, machine, label, warnings)
        if not mapped.prepared and (mapped.overscan_pct > 0 or mapped.ramp_length_mm > 0) and mapped.segments:
            if machine.kind == "laser":
                overscan = mapped.overscan_pct / 100.0 * mapped.speed_mm_min / 60.0
                mapped.segments, mapped.powers, clipped = _decorate_scan(
                    mapped.segments, mapped.powers, mapped.power_pct, overscan, mapped.ramp_length_mm,
                    mapped.min_power_pct, machine, bounds)
                mapped.prepared = True
                if clipped:
                    warnings.append(f"{label} : surbalayage réduit par les limites du lit")
            else:
                warnings.append(f"{label} : surbalayage et rampe ignorés sur une CNC")
        _validate_operation(mapped, machine)
        if mapped.segments:
            operations.append(mapped)
            pos = mapped.segments[-1][-1]

    groups: dict[str, list[Shape]] = {}
    for shape in document.shapes:
        groups.setdefault(shape.layer_key, []).append(shape)
    for key, settings in layers.items():
        _validate_layer(settings, key)
        if not settings.enabled:
            continue
        shapes = groups.get(key) or []
        if not shapes:
            warnings.append(f"Calque {key} : aucune forme dans le document")
            continue
        variants = [(settings, key, settings.name or key)]
        for index, sub in enumerate(_sublayer_settings(settings, key), 1):
            if sub.enabled:
                variants.append((sub, f"{key} (sous-calque {index})",
                                 sub.name or f"{settings.name or key} (sous-calque {index})"))
        for variant, label, name in variants:
            operation = _vector_operation(key, label, name, variant, shapes, convert, machine, order, pos,
                                          bounds, warnings)
            if operation is None:
                continue
            _validate_operation(operation, machine)
            operations.append(operation)
            pos = operation.segments[-1][-1]

    _check_bed(operations, machine)
    stats = _stats_from_moves(_build_moves(operations, machine))
    stats["operations"] = len(operations)
    stats["segments"] = sum(len(op.segments) for op in operations)
    return Plan(operations=operations, stats=stats, machine=machine.name, order=order, warnings=warnings)


# ---------------------------------------------------------------------------
# G-code
# ---------------------------------------------------------------------------

def _ascii(text: str) -> str:
    return " ".join(str(text or "").split()).encode("ascii", "replace").decode("ascii")


def _extra_lines(block: str) -> list[str]:
    return [line.strip() for line in (block or "").splitlines() if line.strip()]


def _header(machine: MachineProfile, job: str, stats: dict[str, Any], now: datetime | None) -> list[str]:
    stamp = (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()
    b = stats.get("bounds") or {}
    max_power = machine.spindle_rpm_max if machine.kind == "cnc" else machine.max_power
    return [
        "; Alpine Makers - laser_toolpath (GRBL 1.1)",
        f"; job: {job}",
        f"; machine: {_ascii(machine.name)} ({machine.kind})",
        f"; date: {stamp}",
        f"; max_power: {_fmt_short(max_power)}",
        f"; travel_speed: {_fmt_short(machine.travel_speed_mm_min)}",
        f"; cut_length_mm: {_fmt(stats.get('cut_length_mm', 0.0))}",
        f"; travel_length_mm: {_fmt(stats.get('travel_length_mm', 0.0))}",
        f"; estimated_seconds: {_fmt(stats.get('estimated_seconds', 0.0))}",
        f"; bounds: X {_fmt(b.get('min_x', 0.0))}..{_fmt(b.get('max_x', 0.0))} "
        f"Y {_fmt(b.get('min_y', 0.0))}..{_fmt(b.get('max_y', 0.0))}",
    ]


class _Emitter:
    """Émet G0/G1 en n'écrivant que les axes, F et S qui changent (GRBL est modal).

    ``compact`` (G-code laser du job, comme LightBurn) : G0/G1 omis tant que le mode de mouvement ne
    change pas, mots collés et zéros inutiles retirés (« X150.32S0 » au lieu de « G1 X150.320 S0 »).
    Une image tramée compte une ligne par point : 17 octets deviennent 11 à 13, et le tampon de 127
    octets de la carte reçoit une dizaine de lignes d'avance au lieu de 7. Coordonnées toujours
    absolues (G90, mm) : le Worker déduit de chaque ligne la position d'arrivée pour reprendre après
    un « ok » perdu (``equipment_grbl.Endpoints``).
    """

    def __init__(self, machine: MachineProfile, compact: bool = False):
        self.machine = machine
        self.laser = machine.kind == "laser"
        self.compact = compact
        self.lines: list[str] = []
        self.x = self.y = self.z = 0.0
        self.feed: float | None = None
        self.s = 0.0
        self.motion: str | None = None  # dernier G0/G1 écrit ; None : le prochain mouvement l'écrit

    def _axes(self, move: _Move) -> list[str]:
        fmt = _fmt_short if self.compact else _fmt
        words = []
        if move.x1 != self.x:
            words.append(f"X{fmt(move.x1)}")
        if move.y1 != self.y:
            words.append(f"Y{fmt(move.y1)}")
        if move.z1 != self.z:
            words.append(f"Z{fmt(move.z1)}")
        self.x, self.y, self.z = move.x1, move.y1, move.z1
        return words

    def move(self, move: _Move) -> None:
        axes = self._axes(move)
        if not axes:
            return
        code = "G0" if move.rapid else "G1"
        words = axes if self.compact and self.motion == code else [code] + axes
        self.motion = code
        if move.rapid:
            if self.laser and self.s != 0:
                words.append("S0")
                self.s = 0.0
        else:
            if move.feed != self.feed:
                words.append(f"F{_fmt_short(move.feed)}")
                self.feed = move.feed
            if self.laser:
                s = move.power_pct / 100.0 * self.machine.max_power
                if s != self.s:
                    words.append(f"S{_fmt_short(s)}")
                    self.s = s
        self.lines.append(("" if self.compact else " ").join(words))


def generate_gcode(plan_obj: Plan, machine: MachineProfile, now: datetime | None = None) -> str:
    """Plan → G-code GRBL 1.1 (texte). Laser : M4/M3 (M3 S0 / M4 S0 entre deux opérations dont le
    mode diffère, ``Operation.constant_power``), S = puissance, S0 en G0, G1 à S0 pour les passages
    laser éteint, passes répétées, M5 et retour origine. CNC : Z de sécurité, plongée à
    ``plunge_rate``, M3 S<tr/min>, jamais M4."""
    _validate_machine(machine)
    for op in plan_obj.operations:
        _validate_operation(op, machine)
    _check_bed(plan_obj.operations, machine)
    items = _build_moves(plan_obj.operations, machine)
    stats = _stats_from_moves(items)
    laser = machine.kind == "laser"
    lines = _header(machine, "parcours", stats, now)
    lines += ["G21", "G90"]
    if machine.homing_command and machine.homing_command.strip():
        lines.append(machine.homing_command.strip())
    lines += _extra_lines(machine.start_gcode)
    spindle_mode = "M4" if machine.laser_mode_dynamic else "M3"
    if laser:
        lines.append(f"{spindle_mode} S0")
    emitter = _Emitter(machine, compact=laser)
    emitter.lines = lines
    air = False
    rpm: float | None = None
    for item in items:
        if isinstance(item, _Move):
            emitter.move(item)
            continue
        emitter.motion = None  # début de calque, de passe ou fin : le mouvement suivant réécrit son G0/G1
        kind, payload = item
        if kind == "op":
            op: Operation = payload
            lines.append(f"; calque {_ascii(op.name or op.layer)} ({op.mode}), {op.passes} passe(s), "
                         f"{_fmt_short(op.power_pct)} %, F{_fmt_short(op.speed_mm_min)}")
            if op.air_assist != air:
                air = op.air_assist
                lines.append("M8" if air else "M9")
            if laser:
                # Puissance constante par calque : bascule M3/M4, toujours avec S0 (tête à l'arrêt).
                wanted = "M3" if op.constant_power or not machine.laser_mode_dynamic else "M4"
                if wanted != spindle_mode:
                    spindle_mode = wanted
                    lines.append(f"{wanted} S0")
                    emitter.s = 0.0
            if not laser:
                wanted = op.power_pct / 100.0 * machine.spindle_rpm_max
                if wanted != rpm:
                    rpm = wanted
                    lines.append(f"M3 S{_fmt_short(rpm)}")
        elif kind == "pass":
            lines.append(f"; passe {payload}")
        elif kind == "end":
            lines.append("M5")
            emitter.s = 0.0          # laser coupé : le retour origine n'a plus besoin de S0
    if air:
        lines.append("M9")
    lines += _extra_lines(machine.end_gcode)
    lines.append("M2")
    return "\n".join(lines) + "\n"


_COMPACT_MOVE_RE = re.compile(r"^(G[01])?((?:[XYZ]-?\d+(?:\.\d+)?)+)(F\d+(?:\.\d+)?)?(S\d+(?:\.\d+)?)?$")
_COMPACT_AXIS_RE = re.compile(r"([XYZ])(-?\d+(?:\.\d+)?)")


def explicit_gcode(gcode: str) -> str:
    """G-code compact de ``generate_gcode`` → forme explicite : G0/G1 sur chaque mouvement, mots séparés,
    coordonnées à 3 décimales. Rend exactement le G-code écrit avant le format compact (agent 1.31.8),
    pour le comparer ou le relire ; les lignes déjà explicites et les autres commandes sont inchangées."""
    out = []
    motion = None
    for line in gcode.split("\n"):
        match = _COMPACT_MOVE_RE.match(line)
        code = match and (match.group(1) or motion)
        if not code:
            out.append(line)
            continue
        motion = code
        words = [code] + [f"{axis}{_fmt(float(value))}" for axis, value in _COMPACT_AXIS_RE.findall(match.group(2))]
        out.append(" ".join(words + [word for word in match.group(3, 4) if word]))
    return "\n".join(out)


def frame_gcode(plan_obj: Plan, machine: MachineProfile, speed_mm_min: float = 3000.0,
                power_pct: float = 0.0, now: datetime | None = None) -> str:
    """Parcours du rectangle englobant du plan (cadrage), laser éteint (S0) ou à ``power_pct``."""
    _validate_machine(machine)
    speed = _finite(speed_mm_min, "speed_mm_min")
    power = _finite(power_pct, "power_pct")
    if speed <= 0 or not 0 <= power <= 100:
        raise ToolpathError("Cadrage : vitesse positive et puissance entre 0 et 100 % attendues")
    if not any(op.segments for op in plan_obj.operations):
        raise ToolpathError("Cadrage impossible : le plan ne contient aucun tracé", 400, "PLAN_EMPTY")
    b = plan_obj.stats.get("bounds") if plan_obj.stats else None
    if not b:
        b = _stats_from_moves(_build_moves(plan_obj.operations, machine))["bounds"]
    x0, y0, x1, y1 = b["min_x"], b["min_y"], b["max_x"], b["max_y"]
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    laser = machine.kind == "laser"
    z = machine.safe_z_mm if not laser else 0.0
    frame = Operation(layer="frame", mode="line", segments=[corners], power_pct=power, speed_mm_min=speed,
                      z_offset_mm=z, name="cadrage")
    items = _build_moves([frame], machine) if laser else _build_moves([], machine)
    if not laser:  # CNC : on parcourt le cadre à la hauteur de sécurité, broche arrêtée
        items = [_Move(0.0, 0.0, 0.0, 0.0, 0.0, machine.safe_z_mm, machine.travel_speed_mm_min, 0.0, True),
                 _Move(0.0, 0.0, machine.safe_z_mm, x0, y0, machine.safe_z_mm, machine.travel_speed_mm_min, 0.0, True)]
        px, py = x0, y0
        for cx, cy in corners[1:]:
            items.append(_Move(px, py, machine.safe_z_mm, cx, cy, machine.safe_z_mm, speed, 0.0, False))
            px, py = cx, cy
        items.append(("end", None))
        items.append(_Move(px, py, machine.safe_z_mm, 0.0, 0.0, machine.safe_z_mm, machine.travel_speed_mm_min, 0.0, True))
    stats = _stats_from_moves(items)
    lines = _header(machine, "cadrage", stats, now)
    lines += ["G21", "G90"]
    lines += _extra_lines(machine.start_gcode)
    if laser:
        lines.append(f"{'M4' if machine.laser_mode_dynamic else 'M3'} S0")
    else:
        lines.append("M5")
    emitter = _Emitter(machine)
    emitter.lines = lines
    for item in items:
        if isinstance(item, _Move):
            emitter.move(item)
        elif item[0] == "end":
            lines.append("M5")
    lines += _extra_lines(machine.end_gcode)
    lines.append("M2")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Simulation (aperçu)
# ---------------------------------------------------------------------------

def _strip_comments(line: str) -> str:
    line = re.sub(r"\([^)]*\)", " ", line)
    return line.split(";", 1)[0].strip()


def simulate(gcode: str, max_power: float | None = None,
             travel_speed_mm_min: float | None = None) -> dict[str, Any]:
    """Relit un G-code GRBL : segments (x1, y1, x2, y2, puissance %), limites des tracés,
    longueurs et durée estimée. ``max_power`` et la vitesse de déplacement sont lus dans l'en-tête
    (« ; max_power: », « ; travel_speed: ») s'ils ne sont pas fournis."""
    if not isinstance(gcode, str):
        raise ToolpathError("G-code texte attendu")
    if len(gcode) > MAX_GCODE_CHARS:
        raise ToolpathError("G-code trop volumineux", 413, "GCODE_TOO_LARGE")
    for line in gcode.splitlines():
        m = _HEADER_VALUE_RE.match(line.strip())
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        try:
            if key == "max_power" and max_power is None:
                max_power = float(value)
            elif key == "travel_speed" and travel_speed_mm_min is None:
                travel_speed_mm_min = float(value)
        except ValueError:
            continue
    travel = travel_speed_mm_min if travel_speed_mm_min and travel_speed_mm_min > 0 else 3000.0
    scale = 100.0 / max_power if max_power and max_power > 0 else 1.0

    x = y = z = 0.0
    feed = 1000.0
    s = 0.0
    motion: int | None = None
    absolute = True
    unit = 1.0
    laser_on = False
    segments: list[tuple[float, float, float, float, float]] = []
    cut_length = travel_length = seconds = 0.0
    xs: list[float] = []
    ys: list[float] = []
    moves = 0
    for raw in gcode.splitlines():
        line = _strip_comments(raw)
        if not line or line.startswith("$") or line.startswith("%"):
            continue
        target = {"X": None, "Y": None, "Z": None}
        has_axis = False
        line_motion = None
        for letter, value in _GCODE_WORD_RE.findall(line):
            letter = letter.upper()
            number = float(value)
            if not math.isfinite(number):
                raise ToolpathError("Valeur non finie dans le G-code")
            if letter == "G":
                code = int(number)
                if code in (0, 1):
                    line_motion = code
                elif code == 90:
                    absolute = True
                elif code == 91:
                    absolute = False
                elif code == 20:
                    unit = 25.4
                elif code == 21:
                    unit = 1.0
            elif letter == "M":
                code = int(number)
                if code in (3, 4):
                    laser_on = True
                elif code == 5:
                    laser_on = False
            elif letter == "F":
                feed = number * unit
            elif letter == "S":
                s = number
            elif letter in target:
                target[letter] = number * unit
                has_axis = True
        if line_motion is not None:
            motion = line_motion
        if not has_axis or motion is None:
            continue
        nx = target["X"] if target["X"] is not None else (x if absolute else 0.0)
        ny = target["Y"] if target["Y"] is not None else (y if absolute else 0.0)
        nz = target["Z"] if target["Z"] is not None else (z if absolute else 0.0)
        if not absolute:
            nx, ny, nz = x + nx, y + ny, z + nz
        xy = math.hypot(nx - x, ny - y)
        xyz = math.sqrt((nx - x) ** 2 + (ny - y) ** 2 + (nz - z) ** 2)
        moves += 1
        if motion == 0:
            travel_length += xy
            seconds += xyz / travel * 60.0
            power = 0.0
        else:
            cut_length += xy
            seconds += xyz / feed * 60.0 if feed > 0 else 0.0
            power = s * scale if laser_on else 0.0
            xs.extend((x, nx))
            ys.extend((y, ny))
        if xy > 0:
            segments.append((round(x, 3), round(y, 3), round(nx, 3), round(ny, 3), round(power, 3)))
        x, y, z = nx, ny, nz
    bounds = {"min_x": min(xs), "min_y": min(ys), "max_x": max(xs), "max_y": max(ys)} if xs else \
        {"min_x": 0.0, "min_y": 0.0, "max_x": 0.0, "max_y": 0.0}
    return {
        "segments": segments,
        "bounds": {k: round(v, 3) for k, v in bounds.items()},
        "estimated_seconds": round(seconds, 3),
        "cut_length_mm": round(cut_length, 3),
        "travel_length_mm": round(travel_length, 3),
        "moves": moves,
        "max_power": max_power,
    }


# ---------------------------------------------------------------------------
# Sérialisation
# ---------------------------------------------------------------------------

def to_dict(obj: Any) -> dict[str, Any]:
    if not dataclasses.is_dataclass(obj) or isinstance(obj, type):
        raise ToolpathError("Objet du module attendu (Document, Shape, LayerSettings, MachineProfile, Operation, Plan)")
    return dataclasses.asdict(obj)


def from_dict(cls: type | str, data: dict[str, Any]) -> Any:
    """Dict (JSON) → dataclass du module ; clés inconnues ignorées, points convertis en tuples."""
    if isinstance(cls, str):
        if cls not in _CLASSES:
            raise ToolpathError(f"Type inconnu « {cls} »")
        cls = _CLASSES[cls]
    if cls not in _CLASSES.values():
        raise ToolpathError("Type de dataclass non pris en charge")
    if not isinstance(data, dict):
        raise ToolpathError(f"Objet JSON attendu pour {cls.__name__}")
    kwargs: dict[str, Any] = {}
    nested = _NESTED.get(cls, {})
    for f in dataclasses.fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        try:
            if f.name in nested:
                value = [from_dict(nested[f.name], item) for item in (value or [])]
            elif f.name in _POLYLINE_FIELDS:
                value = [[(_finite(p[0], f.name), _finite(p[1], f.name)) for p in poly] for poly in (value or [])]
            elif f.name == "powers" and value is not None:
                value = [_finite(p, "powers") for p in value]
            elif f.name == "passes":
                value = int(value)
            elif f.name == "tabs_points":
                value = [(_finite(p[0], f.name), _finite(p[1], f.name)) for p in (value or [])]
            elif f.name == "sublayers":
                if value is None:
                    value = []
                if not isinstance(value, list) or not all(isinstance(item, (dict, LayerSettings)) for item in value):
                    raise ValueError(f.name)
                value = [dict(item) if isinstance(item, dict) else item for item in value]
            elif f.type in ("int", int) and value is not None:
                number = _finite(value, f.name)
                if number != int(number):
                    raise ValueError(f.name)
                value = int(number)
            elif f.type in ("float", float) and value is not None:
                value = _finite(value, f.name)
            elif f.type in ("bool", bool):
                value = bool(value)
            elif f.type in ("str", str):
                value = str(value if value is not None else "")
        except (TypeError, LookupError, ValueError):
            raise ToolpathError(f"Champ {f.name} invalide pour {cls.__name__}") from None
        kwargs[f.name] = value
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ToolpathError(f"{cls.__name__} : {exc}") from None


def to_json(obj: Any, indent: int | None = None) -> str:
    return json.dumps(to_dict(obj), ensure_ascii=False, indent=indent)


def from_json(text: str, cls: type | str) -> Any:
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        raise ToolpathError("JSON invalide") from None
    return from_dict(cls, data)


__all__ = [
    "ToolpathError", "Shape", "Document", "LayerSettings", "MachineProfile", "Operation", "Plan",
    "parse_svg", "parse_path_data", "normalize_color", "layers_from_document", "hatch", "raster_image",
    "raster_operations", "plan", "generate_gcode", "explicit_gcode", "frame_gcode", "simulate", "to_dict", "from_dict",
    "to_json", "from_json",
    "LAYER_MODES", "MACHINE_KINDS", "MACHINE_ORIGINS", "PLAN_ORDERS", "IMAGE_MODES", "DEFAULT_TOLERANCE_MM",
    "IMAGE_ALGORITHMS", "IMAGE_ALGORITHM_LABELS", "FILL_GROUPINGS", "TABS_MODES", "TAB_SPACING_MODES",
    "LASER_TYPES", "MAX_SVG_CHARS", "MAX_GCODE_CHARS", "MAX_IMAGE_BYTES",
]
