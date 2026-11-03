"""Planification des parcours d'Alpine Model Engine (docs/MODEL_ENGINE_ARCHITECTURE.md §5.7).

Entrée : une ``Layer`` (layers.py) remplie par les étapes précédentes (``adhesion``, ``support``, ``walls``,
``infill_lines``), le ``settings`` résolu (clés de profiles/process.py), la fiche filament et la machine. Sortie :
``gcode.LayerMoves`` dont les ``gcode.Move`` sont ordonnés comme l'imprimante doit les exécuter :

  jupe / brim → supports (base puis interface) → pour chaque île, de la plus proche à la plus lointaine :
  murs (``wall_sequence``) → interstices → surfaces pleines (dessous, dessus, plein interne) → ponts → remplissage
  (ou remplissage avant les murs si ``is_infill_first``).

Entre deux parcours, un ``Move`` « travel » porte la décision de rétraction : ``True`` quand le déplacement quitte
l'île courante (ou croise son mur extérieur si ``avoid_crossing_perimeters`` / ``only_retract_when_crossing_perimeters``),
``False`` quand il reste dans l'île et mesure moins que ``retraction_minimum_travel``, ``None`` sinon (l'émetteur décide
par longueur). Vitesses par feature, première couche, petits périmètres, classes de surplomb (un mur est découpé en
sous-parcours quand la classe change), plafond volumique du filament, limite machine, puis ralentissement uniforme de
la couche (``slow_down_layer_time`` / ``slow_down_min_speed``). Unités : micromètres int64 ; mm/s pour les vitesses.
Déterministe : même entrée → même sortie (ordres stables, aucun aléa).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .gcode import FEATURES, LayerMoves, Move
from .layers import OVERHANG_NONE

INT = np.int64
SCALE = 1000

# Vitesse de chaque feature (clé de réglage hors première couche).
_SPEED_KEYS = {
    "OuterWall": "outer_wall_speed", "InnerWall": "inner_wall_speed", "SparseInfill": "sparse_infill_speed",
    "InternalSolidInfill": "internal_solid_infill_speed", "TopSurface": "top_surface_speed",
    "BottomSurface": "bottom_surface_speed", "Bridge": "bridge_speed", "Support": "support_speed",
    "SupportInterface": "support_interface_speed", "GapFill": "gap_infill_speed", "Brim": "initial_layer_speed",
    "Skirt": "initial_layer_speed", "Custom": "default_print_speed",
}
# Largeur de ligne de chaque feature (clé de réglage, mm) quand le parcours n'en porte pas.
_WIDTH_KEYS = {
    "OuterWall": "outer_wall_line_width", "InnerWall": "inner_wall_line_width", "SparseInfill": "sparse_infill_line_width",
    "InternalSolidInfill": "internal_solid_infill_line_width", "BottomSurface": "internal_solid_infill_line_width",
    "TopSurface": "top_surface_line_width", "Bridge": "line_width", "Support": "support_line_width",
    "SupportInterface": "support_line_width", "GapFill": "inner_wall_line_width", "Brim": "initial_layer_line_width",
    "Skirt": "initial_layer_line_width", "Custom": "line_width",
}
# Features de remplissage : vitesse ``initial_layer_infill_speed`` sur la première couche, ordre d'impression dans l'île.
_INFILL_ORDER = ("BottomSurface", "TopSurface", "InternalSolidInfill", "Bridge", "SparseInfill")
_INFILL_FEATURES = frozenset(_INFILL_ORDER)
# Anciens noms acceptés en plus des clés du schéma Alpine (profiles/process.py).
_ALIASES = {
    "wall_infill_order": "wall_sequence", "first_layer_speed": "initial_layer_speed",
    "overhang_speed_1": "overhang_1_4_speed", "overhang_speed_2": "overhang_2_4_speed",
    "overhang_speed_3": "overhang_3_4_speed", "overhang_speed_4": "overhang_4_4_speed",
    "reduce_crossing_wall": "avoid_crossing_perimeters", "slow_down_layer_time_threshold": "slow_down_layer_time",
}
# Défauts quand ``settings`` n'a pas la clé (mêmes valeurs que profiles/process.py FIELDS ; lus à l'import si possible).
_FALLBACK_DEFAULTS = {
    "outer_wall_speed": 200.0, "inner_wall_speed": 300.0, "small_perimeter_speed": 100.0, "small_perimeter_threshold": 6.0,
    "sparse_infill_speed": 270.0, "internal_solid_infill_speed": 250.0, "top_surface_speed": 200.0, "gap_infill_speed": 250.0,
    "bridge_speed": 50.0, "initial_layer_speed": 50.0, "initial_layer_infill_speed": 105.0, "support_speed": 150.0,
    "support_interface_speed": 80.0, "enable_overhang_speed": True, "overhang_1_4_speed": 0.0, "overhang_2_4_speed": 50.0,
    "overhang_3_4_speed": 30.0, "overhang_4_4_speed": 10.0, "travel_speed": 500.0, "default_print_speed": 60.0,
    "line_width": 0.42, "outer_wall_line_width": 0.42, "inner_wall_line_width": 0.45, "top_surface_line_width": 0.42,
    "sparse_infill_line_width": 0.45, "internal_solid_infill_line_width": 0.42, "initial_layer_line_width": 0.50,
    "support_line_width": 0.42, "wall_sequence": "inner_outer", "is_infill_first": False, "bridge_flow": 1.0,
    "top_solid_infill_flow_ratio": 1.0, "bottom_solid_infill_flow_ratio": 1.0, "slow_down_for_layer_cooling": True,
    "slow_down_layer_time": 6.0, "slow_down_min_speed": 20.0, "retraction_minimum_travel": 2.0,
    "avoid_crossing_perimeters": False, "only_retract_when_crossing_perimeters": False, "overhang_fan_speed": 100,
    "overhang_fan_threshold": 25, "bridge_fan_speed": 100, "filament_max_volumetric_speed": 0.0,
}


def _schema_defaults() -> dict:
    """Défauts du schéma Alpine (profiles/process.py) ; repli local si le module est indisponible."""
    defaults = dict(_FALLBACK_DEFAULTS)
    try:
        from .profiles.process import FIELDS
        for f in FIELDS:
            defaults[f.key] = f.default
    except Exception:  # module en relecture : le repli suffit
        pass
    return defaults


_DEFAULTS = _schema_defaults()


@dataclass
class PlannerState:
    """État porté d'une couche à l'autre : position courante (µm), dernier objet, extrudeur, graine (réservée aux
    choix aléatoires futurs, aucun en V1) et dernière zone (île) visitée."""

    position: tuple | None = None       # (x_um, y_um) après le dernier parcours
    last_object_id: int | None = None
    extruder: int = 1
    seed: int = 0
    zone: tuple | None = None           # clé de la zone courante : ("island", object_id, island) | ("support",) | ("adhesion",)


# --- lecture des réglages ---------------------------------------------------------------------------------------------

def _get(settings: dict, key: str, default=None):
    """Valeur d'un réglage (nom Alpine ou alias), sinon le défaut du schéma, sinon ``default``."""
    canonical = _ALIASES.get(key, key)
    for k in (canonical, key):
        if k in settings and settings[k] is not None:
            return settings[k]
        for alias, target in _ALIASES.items():
            if target == k and alias in settings and settings[alias] is not None:
                return settings[alias]
    if canonical in _DEFAULTS:
        return _DEFAULTS[canonical]
    return default


def _raw(settings: dict, key: str):
    """Valeur donnée explicitement dans ``settings`` (nom Alpine ou alias), sinon None (aucun défaut)."""
    canonical = _ALIASES.get(key, key)
    for k in (canonical, key):
        if k in settings and settings[k] is not None:
            return settings[k]
    for alias, target in _ALIASES.items():
        if target == canonical and alias in settings and settings[alias] is not None:
            return settings[alias]
    return None


def _setting_or_fiche(settings: dict, fiche: dict, key: str, default):
    """Réglage explicite → fiche filament → défaut du schéma → ``default``."""
    value = _raw(settings, key)
    if value is None:
        value = fiche.get(key)
    if value is None:
        value = _DEFAULTS.get(key, default)
    return value


def _num(value, default: float = 0.0) -> float:
    """Nombre fini (accepte « 12 », « 12,5 ») ; booléen ou texte invalide → ``default``."""
    if value is None or isinstance(value, bool):
        return float(default)
    try:
        number = float(str(value).strip().replace(",", ".").rstrip("%")) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def _bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "oui", "on", "vrai")
    return bool(value)


def _speed_or_percent(value, base: float) -> float:
    """« 50% » → 50 % de ``base`` ; nombre → mm/s ; ≤ 0 ou invalide → ``base``."""
    if isinstance(value, str) and value.strip().endswith("%"):
        pct = _num(value, 0.0)
        return base * pct / 100.0 if pct > 0 else base
    speed = _num(value, 0.0)
    return speed if speed > 0 else base


def _fan_class_threshold(raw) -> int:
    """Seuil de ventilation des surplombs → classe (1–4). Valeur > 4 = pour cent de porte-à-faux (25 → classe 2),
    sinon classe directe (convention de gcode.SETTINGS_DEFAULTS)."""
    value = _num(raw, 25.0)
    if value > 4:
        return int(min(4, max(1, value // 25 + 1)))
    return int(min(4, max(1, round(value))))


# --- géométrie locale -------------------------------------------------------------------------------------------------

def _as_points(points) -> np.ndarray:
    pts = points if isinstance(points, np.ndarray) and points.dtype == INT else np.asarray(points, dtype=INT)
    return pts.reshape(-1, 2)


def _length_um(pts: np.ndarray) -> float:
    """Longueur d'une polyligne (k, 2) ; cas à deux points en scalaire (le plus fréquent)."""
    k = pts.shape[0]
    if k < 2:
        return 0.0
    if k == 2:
        return math.hypot(int(pts[1, 0]) - int(pts[0, 0]), int(pts[1, 1]) - int(pts[0, 1]))
    d = np.diff(pts, axis=0).astype(np.float64)
    return float(np.hypot(d[:, 0], d[:, 1]).sum())


def _segment_crosses_ring(ax: int, ay: int, bx: int, by: int, ring: np.ndarray) -> bool:
    """Vrai si le segment AB coupe une arête du ring (test d'orientations, vectorisé, entiers exacts)."""
    p = ring
    q = np.roll(ring, -1, axis=0)
    # orientation de P et Q par rapport à AB, et de A et B par rapport à PQ (object → pas de débordement int64 ici : µm²).
    dx, dy = bx - ax, by - ay
    o_p = dx * (p[:, 1] - ay) - dy * (p[:, 0] - ax)
    o_q = dx * (q[:, 1] - ay) - dy * (q[:, 0] - ax)
    ex, ey = q[:, 0] - p[:, 0], q[:, 1] - p[:, 1]
    o_a = ex * (ay - p[:, 1]) - ey * (ax - p[:, 0])
    o_b = ex * (by - p[:, 1]) - ey * (bx - p[:, 0])
    if np.any((np.sign(o_p) * np.sign(o_q) < 0) & (np.sign(o_a) * np.sign(o_b) < 0)):
        return True
    # Passage exact par un sommet du ring strictement entre A et B (coin rentrant d'une île en C, par exemple) :
    # compté comme croisement. Un sommet égal à A (départ sur la couture) ou à B (arrivée sur le mur) ne compte pas.
    on_line = o_p == 0
    if not np.any(on_line):
        return False
    px, py = p[:, 0], p[:, 1]
    inside = (on_line & (px >= min(ax, bx)) & (px <= max(ax, bx)) & (py >= min(ay, by)) & (py <= max(ay, by))
              & ~((px == ax) & (py == ay)) & ~((px == bx) & (py == by)))
    return bool(np.any(inside))


def _points_in_ring(ring: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Indice d'enroulement ≠ 0 pour chaque point (n, 2) (vectorisé points × arêtes, même règle que poly.point_in_ring)."""
    x1, y1 = ring[:, 0][None, :], ring[:, 1][None, :]
    x2, y2 = np.roll(ring[:, 0], -1)[None, :], np.roll(ring[:, 1], -1)[None, :]
    x, y = pts[:, 0][:, None], pts[:, 1][:, None]
    up = (y1 <= y) & (y2 > y)
    down = (y2 <= y) & (y1 > y)
    cross = (x2 - x1) * (y - y1) - (x - x1) * (y2 - y1)
    w = np.count_nonzero(up & (cross > 0), axis=1) - np.count_nonzero(down & (cross < 0), axis=1)
    return w != 0


def _representative(pl: np.ndarray) -> tuple:
    """Point sûrement intérieur d'une polyligne : milieu de son segment médian."""
    i = (pl.shape[0] - 1) // 2
    j = min(i + 1, pl.shape[0] - 1)
    return (int(pl[i, 0]) + int(pl[j, 0])) // 2, (int(pl[i, 1]) + int(pl[j, 1])) // 2


# --- configuration d'une couche ---------------------------------------------------------------------------------------

class _LayerConfig:
    """Vitesses, largeurs, ventilation et limites résolues une fois par couche."""

    __slots__ = ("first", "height_um", "speeds", "widths", "first_width_um", "wall_sequence", "infill_first",
                 "small_perimeter_len_um", "small_perimeter", "overhang_speeds", "fan_overhang", "fan_class_threshold",
                 "fan_bridge", "vol_max", "machine_max", "min_travel_um", "avoid_crossing", "only_retract_crossing",
                 "slow_time", "slow_min", "flow_bridge", "flow_top", "flow_bottom", "travel_speed")

    def __init__(self, layer, settings: dict, filament: dict | None, machine):
        s = settings or {}
        fiche = filament or {}
        self.first = int(layer.index) <= 1
        self.height_um = int(layer.height_um)
        initial = _num(_get(s, "initial_layer_speed"), 50.0)
        initial_infill = _num(_get(s, "initial_layer_infill_speed"), initial)
        solid = _num(_get(s, "internal_solid_infill_speed"), 250.0)
        speeds = {}
        for feature in FEATURES:
            key = _SPEED_KEYS[feature]
            value = _get(s, key, None)
            if feature == "BottomSurface" and value is None:
                value = solid                                   # défaut : vitesse du plein interne
            base = _num(value, 60.0)
            if base <= 0:
                base = solid if feature == "BottomSurface" else 60.0
            if self.first:
                if feature in _INFILL_FEATURES:
                    base = initial_infill
                elif feature not in ("Custom",):
                    base = initial
            speeds[feature] = base
        self.speeds = speeds
        self.widths = {feature: int(round(_num(_get(s, key), 0.42) * SCALE)) for feature, key in _WIDTH_KEYS.items()}
        self.first_width_um = int(round(_num(_get(s, "initial_layer_line_width"), 0.5) * SCALE))
        self.wall_sequence = str(_get(s, "wall_sequence", "inner_outer") or "inner_outer").strip().lower()
        self.infill_first = _bool(_get(s, "is_infill_first", False))
        threshold_mm = _num(_get(s, "small_perimeter_threshold"), 0.0)
        self.small_perimeter_len_um = 2.0 * math.pi * threshold_mm * SCALE if threshold_mm > 0 else 0.0
        self.small_perimeter = _get(s, "small_perimeter_speed", 0)
        enable_overhang = _bool(_get(s, "enable_overhang_speed", True))
        self.overhang_speeds = {}
        for cls, key in ((1, "overhang_1_4_speed"), (2, "overhang_2_4_speed"), (3, "overhang_3_4_speed"), (4, "overhang_4_4_speed")):
            value = _num(_get(s, key), 0.0)
            self.overhang_speeds[cls] = value if (enable_overhang and value > 0) else 0.0
        self.fan_overhang = max(0.0, min(100.0, _num(_setting_or_fiche(s, fiche, "overhang_fan_speed", 100), 100.0)))
        self.fan_class_threshold = _fan_class_threshold(_setting_or_fiche(s, fiche, "overhang_fan_threshold", 25))
        fan_bridge = s.get("bridge_fan_speed")
        self.fan_bridge = max(self.fan_overhang, _num(fan_bridge, 0.0)) if fan_bridge is not None else self.fan_overhang
        limits = [v for v in (_num(s.get("filament_max_volumetric_speed"), 0.0),
                              _num(fiche.get("filament_max_volumetric_speed"), 0.0)) if v > 0]
        self.vol_max = min(limits) if limits else 0.0
        machine_max = 0.0
        if machine is not None:
            try:
                machine_max = float(machine.max_speed.x)
            except (AttributeError, TypeError, ValueError):
                machine_max = 0.0
        self.machine_max = machine_max if machine_max > 0 else 0.0
        self.min_travel_um = max(0.0, _num(_get(s, "retraction_minimum_travel"), 2.0)) * SCALE
        self.avoid_crossing = _bool(_get(s, "avoid_crossing_perimeters", False))
        self.only_retract_crossing = _bool(_get(s, "only_retract_when_crossing_perimeters", False))
        slow_on = _bool(_get(s, "slow_down_for_layer_cooling", True))
        slow_time = _setting_or_fiche(s, fiche, "slow_down_layer_time", 0.0)
        slow_min = _setting_or_fiche(s, fiche, "slow_down_min_speed", 20.0)
        self.slow_time = _num(slow_time, 0.0) if slow_on else 0.0
        self.slow_min = max(0.0, _num(slow_min, 20.0))
        self.flow_bridge = _num(_get(s, "bridge_flow"), 1.0) or 1.0
        self.flow_top = _num(_get(s, "top_solid_infill_flow_ratio"), 1.0) or 1.0
        self.flow_bottom = _num(_get(s, "bottom_solid_infill_flow_ratio"), 1.0) or 1.0
        self.travel_speed = _num(_get(s, "travel_speed"), 500.0)

    def width_for(self, feature: str) -> int:
        if self.first and feature not in ("OuterWall", "InnerWall"):
            return self.first_width_um
        return self.widths.get(feature, self.widths["Custom"])

    def cap(self, speed: float, width_um: int, height_um: int) -> float:
        """Plafonds : machine, puis débit volumique du filament (speed ≤ max / (w·h))."""
        if self.machine_max > 0:
            speed = min(speed, self.machine_max)
        if self.vol_max > 0 and width_um > 0 and height_um > 0:
            section = (width_um / SCALE) * (height_um / SCALE)
            speed = min(speed, self.vol_max / section)
        return max(0.5, speed)

    def wall_base_speed(self, feature: str, loop_length_um: float) -> float:
        base = self.speeds[feature]
        if self.small_perimeter_len_um > 0 and 0 < loop_length_um < self.small_perimeter_len_um and not self.first:
            return _speed_or_percent(self.small_perimeter, base)
        return base

    def flow_for(self, feature: str) -> float:
        if feature == "Bridge":
            return self.flow_bridge
        if feature == "TopSurface":
            return self.flow_top
        if feature == "BottomSurface":
            return self.flow_bottom
        return 1.0


# --- planificateur d'une couche ---------------------------------------------------------------------------------------

class _LayerPlanner:
    __slots__ = ("cfg", "state", "out", "pos", "zone", "boundary", "extruder", "lengths")

    def __init__(self, cfg: _LayerConfig, state: PlannerState):
        self.cfg = cfg
        self.state = state
        self.out: list = []
        self.pos = tuple(int(v) for v in state.position) if state.position is not None else None
        self.zone = state.zone
        self.boundary: list = []      # rings (murs extérieurs) de l'île courante, pour le test de croisement
        self.extruder = int(state.extruder or 1)
        self.lengths: list = []       # (indice du Move d'extrusion, longueur µm) pour le ralentissement de couche

    # --- déplacements -----------------------------------------------------------------------------------------------
    def travel_to(self, x: int, y: int, zone: tuple, feature: str, object_id) -> None:
        pos = self.pos
        if pos is None:
            self.out.append(Move("travel", feature, np.array([[x, y]], dtype=INT), extruder=self.extruder,
                                 retract=True, object_id=object_id))
            return
        if pos[0] == x and pos[1] == y:
            return
        dist = math.hypot(x - pos[0], y - pos[1])
        cfg = self.cfg
        if zone != self.zone or zone is None:
            retract = True
        elif cfg.avoid_crossing or cfg.only_retract_crossing:
            crosses = any(_segment_crosses_ring(pos[0], pos[1], x, y, ring) for ring in self.boundary)
            if crosses:
                retract = True
            elif dist < cfg.min_travel_um or cfg.only_retract_crossing:
                retract = False
            else:
                retract = None
        elif dist < cfg.min_travel_um:
            retract = False
        else:
            retract = None
        self.out.append(Move("travel", feature, np.array([[pos[0], pos[1]], [x, y]], dtype=INT), extruder=self.extruder,
                             retract=retract, object_id=object_id))

    # --- extrusions ---------------------------------------------------------------------------------------------------
    def extrude(self, pts: np.ndarray, feature: str, width_um: int, speed: float, zone: tuple, object_id,
                fan: float | None = None, overhang_class: int = 0, flow: float = 1.0, length_um: float | None = None) -> None:
        if pts.shape[0] < 2:
            return
        self.travel_to(int(pts[0, 0]), int(pts[0, 1]), zone, feature, object_id)
        cfg = self.cfg
        self.out.append(Move("extrude", feature, pts, line_width_um=int(width_um), height_um=cfg.height_um,
                             speed_mm_s=cfg.cap(speed, width_um, cfg.height_um), fan_percent=fan, extruder=self.extruder,
                             overhang_class=int(overhang_class), object_id=object_id, flow_ratio=flow))
        if cfg.slow_time > 0:
            self.lengths.append((len(self.out) - 1, _length_um(pts) if length_um is None else length_um))
        self.pos = (int(pts[-1, 0]), int(pts[-1, 1]))
        self.zone = zone

    def polylines(self, items: list, feature: str, width_um: int, speed: float, zone: tuple, object_id,
                  fan: float | None = None, flow: float = 1.0) -> None:
        """Polylignes ouvertes dans l'ordre donné ; chacune est parcourue dans le sens dont le départ est le plus proche."""
        for item in items:
            pl = _as_points(item)
            if pl.shape[0] < 2:
                continue
            pos = self.pos
            if pos is not None:
                d_start = (int(pl[0, 0]) - pos[0]) ** 2 + (int(pl[0, 1]) - pos[1]) ** 2
                d_end = (int(pl[-1, 0]) - pos[0]) ** 2 + (int(pl[-1, 1]) - pos[1]) ** 2
                if d_end < d_start:
                    pl = pl[::-1]
            self.extrude(pl, feature, width_um, speed, zone, object_id, fan=fan, flow=flow)

    def wall(self, loop, zone: tuple, object_id: int) -> None:
        """Un mur : fermé (premier point répété, sauf s'il l'est déjà), découpé en sous-parcours par classe de surplomb.
        ``object_id`` = clé de ``layer.walls`` (WallLoop.object_id vaut 1 par défaut : la clé fait foi)."""
        pts = _as_points(loop.points)
        k = pts.shape[0]
        if k < 2:
            return
        closed = bool(getattr(loop, "closed", True))
        if closed and k >= 3 and not (pts[0, 0] == pts[-1, 0] and pts[0, 1] == pts[-1, 1]):
            pts = np.concatenate((pts, pts[:1]), axis=0)
        feature = "OuterWall" if loop.is_outer else "InnerWall"
        width = int(loop.line_width_um) if int(loop.line_width_um) > 0 else self.cfg.width_for(feature)
        length = _length_um(pts)
        base = self.cfg.wall_base_speed(feature, length if closed else 0.0)
        classes = getattr(loop, "overhang_class", None)
        if classes is None:
            self.extrude(pts, feature, width, base, zone, object_id, length_um=length)
            return
        classes = np.asarray(classes).reshape(-1)
        n_seg = pts.shape[0] - 1
        if classes.shape[0] < n_seg:
            classes = np.concatenate((classes, np.zeros(n_seg - classes.shape[0], dtype=classes.dtype)))
        classes = np.clip(classes[:n_seg].astype(np.int64), 0, 4)      # bornée avant la découpe (9 → 4, −3 → 0)
        if not np.any(classes != OVERHANG_NONE):
            self.extrude(pts, feature, width, base, zone, object_id, length_um=length)
            return
        starts = np.concatenate(([0], np.flatnonzero(classes[1:] != classes[:-1]) + 1, [n_seg]))
        for a, b in zip(starts[:-1], starts[1:]):
            cls = int(classes[a])
            speed = self.cfg.overhang_speeds.get(cls, 0.0) or base
            fan = self.cfg.fan_overhang if (cls > 0 and cls >= self.cfg.fan_class_threshold) else None
            self.extrude(pts[a:b + 1], feature, width, speed, zone, object_id, fan=fan, overhang_class=cls)

    # --- ralentissement de couche -----------------------------------------------------------------------------------
    def slow_down(self) -> None:
        cfg = self.cfg
        if cfg.slow_time <= 0 or not self.lengths:
            return
        out = self.out
        total = 0.0
        for index, length in self.lengths:
            speed = out[index].speed_mm_s
            if speed > 0:
                total += length / SCALE / speed
        if total <= 0 or total >= cfg.slow_time:
            return
        factor = total / cfg.slow_time
        for index, _length in self.lengths:
            move = out[index]
            speed = move.speed_mm_s
            move.speed_mm_s = max(speed * factor, min(speed, cfg.slow_min))


# --- ordre des îles ---------------------------------------------------------------------------------------------------

def _walls_of(layer, object_id: int):
    """``layer.walls[object_id]`` (clé entière ou texte « 1 »), sinon None."""
    walls = getattr(layer, "walls", None) or {}
    if not hasattr(walls, "get"):
        return None
    found = walls.get(object_id)
    return found if found is not None else walls.get(str(object_id))


def _islands(layer) -> list:
    """Îles de la couche : (object_id, island, loops, boundary, seam) ; ``seam`` = premier point du premier mur extérieur
    non vide (None si l'île n'a aucun mur exploitable)."""
    result = []
    for object_id in sorted(layer.walls, key=lambda k: int(k)):
        walls = layer.walls[object_id]
        groups: dict = {}
        for loop in walls.loops:
            groups.setdefault(int(loop.island), []).append(loop)
        for island in sorted(groups):
            loops = groups[island]
            outer = [l for l in loops if l.is_outer] or loops
            rings = [_as_points(l.points) for l in outer]
            boundary = [r for r in rings if r.shape[0] >= 3]
            seam = None
            for r in rings:
                if r.shape[0] >= 2:
                    seam = (int(r[0, 0]), int(r[0, 1]))
                    break
            result.append((int(object_id), island, loops, boundary, seam))
    return result


def _valid_polylines(items) -> list:
    """Polylignes d'au moins deux points (les dégénérées sont ignorées)."""
    return [it for it in (items or []) if _as_points(it).shape[0] >= 2]


def _assign_to_islands(items: list, islands: list) -> list:
    """Répartit des polylignes (≥ 2 points) entre les îles d'un objet (point représentatif dans le mur extérieur) ;
    ``islands`` = îles de cet objet. Une polyligne hors de toute île rejoint la plus proche (par boîte englobante)."""
    buckets = [[] for _ in islands]
    if not items:
        return buckets
    if len(islands) == 1:
        buckets[0] = list(items)
        return buckets
    reps = np.array([_representative(_as_points(it)) for it in items], dtype=INT)
    boxes = []
    for (_oid, _isl, _loops, boundary, _seam) in islands:
        if boundary:
            allpts = np.concatenate(boundary, axis=0)
            boxes.append((allpts[:, 0].min(), allpts[:, 1].min(), allpts[:, 0].max(), allpts[:, 1].max()))
        else:
            boxes.append((0, 0, -1, -1))       # île sans mur : boîte vide, ne capte rien
    boxes = np.array(boxes, dtype=INT)
    inside_box = ((reps[:, 0][:, None] >= boxes[:, 0][None, :]) & (reps[:, 0][:, None] <= boxes[:, 2][None, :]) &
                  (reps[:, 1][:, None] >= boxes[:, 1][None, :]) & (reps[:, 1][:, None] <= boxes[:, 3][None, :]))
    counts = inside_box.sum(axis=1)
    assigned = np.full(len(items), -1, dtype=np.int64)
    single = counts == 1
    assigned[single] = np.argmax(inside_box[single], axis=1)
    ambiguous = np.flatnonzero(counts > 1)
    if ambiguous.size:
        for j, (_oid, _isl, _loops, boundary, _seam) in enumerate(islands):
            candidates = ambiguous[inside_box[ambiguous, j] & (assigned[ambiguous] < 0)]
            if candidates.size == 0:
                continue
            inside = np.zeros(candidates.size, dtype=bool)
            for ring in boundary:
                inside |= _points_in_ring(ring, reps[candidates])
            assigned[candidates[inside]] = j
    missing = np.flatnonzero(assigned < 0)
    if missing.size:
        centres = (boxes[:, :2] + boxes[:, 2:]) // 2
        d = ((reps[missing][:, None, :] - centres[None, :, :]).astype(np.float64) ** 2).sum(axis=2)
        assigned[missing] = np.argmin(d, axis=1)
    for item, j in zip(items, assigned):
        buckets[int(j)].append(item)
    return buckets


def _order_walls(loops: list, sequence: str) -> list:
    inner = [l for l in loops if not l.is_outer]
    outer = [l for l in loops if l.is_outer]
    if sequence == "outer_inner":
        return outer + inner
    if sequence == "inner_outer_inner" and len(inner) >= 2:
        return inner[1:] + outer + inner[:1]     # approximation V1 : le premier mur intérieur de la liste après l'extérieur
    return inner + outer


# --- API --------------------------------------------------------------------------------------------------------------

def plan_layer(layer, settings: dict, state: PlannerState, filament: dict | None = None, machine=None) -> LayerMoves:
    """Ordonne tous les parcours d'une couche (voir l'en-tête du module) et met ``state`` à jour."""
    cfg = _LayerConfig(layer, settings, filament, machine)
    p = _LayerPlanner(cfg, state)
    # 1. Jupe puis brim (ordre donné à l'intérieur de chaque feature).
    adhesion = list(getattr(layer, "adhesion", None) or [])
    for wanted in ("Skirt", "Brim"):
        for item in adhesion:
            feature, polyline, width = item[0], item[1], (item[2] if len(item) > 2 else 0)
            if feature != wanted:
                continue
            pl = _as_points(polyline)
            width_um = int(width) if int(width or 0) > 0 else cfg.width_for(feature)
            p.extrude(pl, feature, width_um, cfg.speeds[feature], ("adhesion",), None)
    for item in adhesion:
        if item[0] not in ("Skirt", "Brim"):
            feature = item[0] if item[0] in FEATURES else "Custom"
            width_um = int(item[2]) if len(item) > 2 and int(item[2] or 0) > 0 else cfg.width_for(feature)
            p.extrude(_as_points(item[1]), feature, width_um, cfg.speeds[feature], ("adhesion",), None)
    # 2. Supports : base puis interface.
    support = getattr(layer, "support", None)
    if support is not None:
        p.polylines(list(support.base_lines or []), "Support", cfg.width_for("Support"), cfg.speeds["Support"], ("support",), None)
        p.polylines(list(support.interface_lines or []), "SupportInterface", cfg.width_for("SupportInterface"),
                    cfg.speeds["SupportInterface"], ("support",), None)
    # 3. Îles, de la plus proche à la plus lointaine depuis la position courante.
    islands = _islands(layer)
    infill_all = dict(getattr(layer, "infill_lines", None) or {})
    for object_id in sorted(infill_all, key=lambda k: int(k)):
        if not any(isl[0] == int(object_id) for isl in islands):
            islands.append((int(object_id), 0, [], [], None))     # objet sans mur : son remplissage forme une île
    per_island_infill: dict = {}
    per_island_gap: dict = {}
    for object_id in sorted({isl[0] for isl in islands}):
        mine = [isl for isl in islands if isl[0] == object_id]
        features = infill_all.get(object_id) or infill_all.get(str(object_id)) or {}
        for feature in list(_INFILL_ORDER) + sorted(k for k in features if k not in _INFILL_FEATURES):
            items = _valid_polylines(features.get(feature))
            if not items:
                continue
            emitted = feature if feature in FEATURES else "Custom"        # feature inconnue (« Ironing »…) → Custom
            for isl, bucket in zip(mine, _assign_to_islands(items, mine)):
                per_island_infill.setdefault((isl[0], isl[1]), []).append((emitted, bucket))
        walls = _walls_of(layer, object_id)
        if walls is not None:
            gaps = _valid_polylines(walls.gap_fill)
            thin = [(pl, w) for pl, w in (walls.thin_walls or []) if _as_points(pl).shape[0] >= 2]
            if gaps:
                for isl, bucket in zip(mine, _assign_to_islands(gaps, mine)):
                    per_island_gap.setdefault((isl[0], isl[1]), {})["gap"] = bucket
            if thin:
                for isl, bucket in zip(mine, _assign_to_islands([pl for pl, _w in thin], mine)):
                    widths = {id(pl): w for pl, w in thin}
                    per_island_gap.setdefault((isl[0], isl[1]), {})["thin"] = [(pl, widths.get(id(pl), 0)) for pl in bucket]
    # Plus proche voisin vectorisé : distance de la position courante à toutes les coutures restantes (les îles sans
    # couture passent après les autres, dans l'ordre objet / île).
    n_islands = len(islands)
    seams = np.array([isl[4] if isl[4] is not None else (0, 0) for isl in islands], dtype=np.float64).reshape(-1, 2)
    no_seam = np.array([isl[4] is None for isl in islands], dtype=bool)
    remaining = np.ones(n_islands, dtype=bool)
    for _step in range(n_islands):
        pos = p.pos
        j = int(np.flatnonzero(remaining)[0])
        if pos is not None and remaining.sum() > 1:
            d = (seams[:, 0] - pos[0]) ** 2 + (seams[:, 1] - pos[1]) ** 2
            d[no_seam | ~remaining] = np.inf
            best = int(np.argmin(d))
            if np.isfinite(d[best]):
                j = best
        remaining[j] = False
        object_id, island, loops, boundary, _seam = islands[j]
        zone = ("island", object_id, island)
        p.boundary = boundary
        key = (object_id, island)
        extras = per_island_gap.get(key, {})

        def do_walls():
            for loop in _order_walls(loops, cfg.wall_sequence):
                p.wall(loop, zone, object_id)
            for pl, w in extras.get("thin", []):
                width_um = int(w) if int(w or 0) > 0 else cfg.width_for("OuterWall")
                p.polylines([pl], "OuterWall", width_um, cfg.speeds["OuterWall"], zone, object_id)
            if extras.get("gap"):
                p.polylines(extras["gap"], "GapFill", cfg.width_for("GapFill"), cfg.speeds["GapFill"], zone, object_id)

        def do_infill():
            for feature, items in per_island_infill.get(key, []):
                fan = cfg.fan_bridge if feature == "Bridge" else None
                p.polylines(items, feature, cfg.width_for(feature), cfg.speeds[feature], zone, object_id,
                            fan=fan, flow=cfg.flow_for(feature))

        if cfg.infill_first:
            do_infill()
            do_walls()
        else:
            do_walls()
            do_infill()
        state.last_object_id = object_id
    # 4. Ralentissement uniforme de la couche.
    p.slow_down()
    state.position = p.pos
    state.zone = p.zone
    return LayerMoves(int(layer.index), int(layer.z_um), int(layer.height_um), p.out)


def plan(layers: list, settings: dict, filaments: list | None = None, machine=None, *, progress=None) -> list:
    """Planifie toutes les couches avec un état continu ; ``progress(i, n)`` après chaque couche."""
    state = PlannerState()
    fiches = list(filaments or [])
    result = []
    total = len(layers)
    for i, layer in enumerate(layers):
        fiche = fiches[state.extruder - 1] if 0 < state.extruder <= len(fiches) else (fiches[0] if fiches else None)
        result.append(plan_layer(layer, settings, state, fiche, machine))
        if progress is not None:
            progress(i + 1, total)
    return result


__all__ = ["PlannerState", "plan", "plan_layer"]
