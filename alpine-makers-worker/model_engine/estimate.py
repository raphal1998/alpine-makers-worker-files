"""Estimation du temps d'impression et du filament (docs/MODEL_ENGINE_ARCHITECTURE.md §6.2).

Modèle cinématique écrit par Alpine Makers : profil trapézoïdal par segment (accélération constante, palier à la
vitesse demandée, décélération), vitesse de jonction limitée par l'angle entre segments (variation de vitesse
instantanée ≤ jerk : |Δv| = 2·v·sin(φ/2)), planification avant/arrière vectorisée sur toute la polyligne, temps de
chauffe, rétractions et déplacements Z. Unités : millimètres, secondes, mm/s, mm/s². Tout est en flottants ici,
les micromètres entiers restent dans gcode.py.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

SCALE = 1000  # micromètres par millimètre
_EPS = 1e-9


# --- profils de mouvement --------------------------------------------------------------------------------------

def trapezoid_time_s(length_mm: float, v_entry: float, v_cruise: float, v_exit: float, accel: float) -> float:
    """Durée d'un segment : accélération de v_entry vers v_cruise, palier, décélération vers v_exit.
    Si le segment est trop court pour atteindre v_cruise, profil triangulaire. accel ≤ 0 = vitesse constante."""
    length = max(0.0, float(length_mm))
    if length <= _EPS:
        return 0.0
    v_cruise = max(float(v_cruise), 1e-6)
    if accel is None or float(accel) <= 0.0:
        return length / v_cruise
    accel = float(accel)
    v0 = min(max(float(v_entry), 0.0), v_cruise)
    v1 = min(max(float(v_exit), 0.0), v_cruise)
    d_acc = (v_cruise * v_cruise - v0 * v0) / (2.0 * accel)
    d_dec = (v_cruise * v_cruise - v1 * v1) / (2.0 * accel)
    if d_acc + d_dec <= length:
        return (v_cruise - v0) / accel + (length - d_acc - d_dec) / v_cruise + (v_cruise - v1) / accel
    peak_sq = (2.0 * accel * length + v0 * v0 + v1 * v1) / 2.0
    peak = max(math.sqrt(max(peak_sq, 0.0)), v0, v1)
    return (peak - v0) / accel + (peak - v1) / accel


def junction_speed(dir_in, dir_out, jerk_mm_s: float, v_limit: float) -> float:
    """Vitesse admissible à la jonction de deux directions unitaires : |Δv| = 2·v·sin(φ/2) ≤ jerk,
    φ = angle de déviation (0 = tout droit, π = demi-tour)."""
    cos_phi = float(dir_in[0] * dir_out[0] + dir_in[1] * dir_out[1])
    sin_half = math.sqrt(max(0.0, min(1.0, (1.0 - cos_phi) / 2.0)))
    if sin_half < 1e-6:
        return float(v_limit)
    return min(float(v_limit), float(jerk_mm_s) / (2.0 * sin_half))


def plan_polyline(points_mm, speed_mm_s: float, accel: float, jerk_mm_s: float = 9.0,
                  v_entry: float = 0.0, v_exit: float = 0.0) -> float:
    """Durée d'une polyligne (n, 2) ou (n, 3) en mm parcourue à ``speed_mm_s`` avec accélération ``accel``.

    Planification avant/arrière vectorisée : à accélération constante, la vitesse maximale atteignable au sommet i
    vaut v_f[i]² = 2·a·C[i] + min_{j ≤ i}(v_j[j]² − 2·a·C[j]) (C = abscisse curviligne), symétriquement en arrière ;
    la vitesse retenue est le minimum des deux. Les segments de longueur nulle sont ignorés."""
    pts = np.asarray(points_mm, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[0] < 2:
        return 0.0
    if pts.shape[0] == 2:
        # Segment unique (cas le plus fréquent : lignes de remplissage, déplacements) : même résultat que la version
        # vectorisée (vitesses d'entrée/sortie bornées l'une par l'autre), sans la surcharge numpy.
        length = math.dist(pts[0].tolist(), pts[1].tolist())
        if length <= _EPS:
            return 0.0
        speed = max(float(speed_mm_s), 1e-6)
        if accel is None or float(accel) <= 0.0:
            return length / speed
        accel = float(accel)
        sq0 = min(max(float(v_entry), 0.0), speed) ** 2
        sq1 = min(max(float(v_exit), 0.0), speed) ** 2
        v0 = math.sqrt(min(sq0, sq1 + 2.0 * accel * length))
        v1 = math.sqrt(min(sq1, sq0 + 2.0 * accel * length))
        return trapezoid_time_s(length, v0, speed, v1, accel)
    deltas = np.diff(pts, axis=0)
    lengths = np.sqrt(np.sum(deltas * deltas, axis=1))
    keep = lengths > _EPS
    if not np.any(keep):
        return 0.0
    deltas = deltas[keep]
    lengths = lengths[keep]
    speed = max(float(speed_mm_s), 1e-6)
    if accel is None or float(accel) <= 0.0:
        return float(np.sum(lengths) / speed)
    accel = float(accel)
    n = lengths.shape[0]
    # Vitesses de jonction limitées par l'angle (vectorisé sur les n − 1 jonctions internes).
    bounds = np.full(n + 1, speed, dtype=np.float64)
    if n > 1:
        units = deltas / lengths[:, None]
        cos_phi = np.sum(units[:-1] * units[1:], axis=1)
        sin_half = np.sqrt(np.clip((1.0 - cos_phi) / 2.0, 0.0, 1.0))
        limited = np.where(sin_half < 1e-6, speed, float(jerk_mm_s) / (2.0 * np.maximum(sin_half, 1e-6)))
        bounds[1:n] = np.minimum(speed, limited)
    bounds[0] = min(max(float(v_entry), 0.0), speed)
    bounds[n] = min(max(float(v_exit), 0.0), speed)
    cum = np.concatenate(([0.0], np.cumsum(lengths)))  # abscisse curviligne de chaque sommet
    sq = bounds * bounds
    forward = 2.0 * accel * cum + np.minimum.accumulate(sq - 2.0 * accel * cum)
    backward = -2.0 * accel * cum + np.minimum.accumulate((sq + 2.0 * accel * cum)[::-1])[::-1]
    v_sq = np.minimum(np.minimum(forward, backward), sq)
    v = np.sqrt(np.maximum(v_sq, 0.0))
    v0, v1 = v[:-1], v[1:]
    d_acc = (speed * speed - v0 * v0) / (2.0 * accel)
    d_dec = (speed * speed - v1 * v1) / (2.0 * accel)
    full = d_acc + d_dec <= lengths
    t_full = (speed - v0) / accel + np.maximum(lengths - d_acc - d_dec, 0.0) / speed + (speed - v1) / accel
    peak = np.sqrt(np.maximum((2.0 * accel * lengths + v0 * v0 + v1 * v1) / 2.0, 0.0))
    peak = np.maximum(peak, np.maximum(v0, v1))
    t_tri = (peak - v0) / accel + (peak - v1) / accel
    return float(np.sum(np.where(full, t_full, t_tri)))


def axis_move_time_s(distance_mm: float, speed_mm_s: float, accel: float) -> float:
    """Déplacement d'un seul axe (Z, E) de l'arrêt à l'arrêt."""
    return trapezoid_time_s(abs(float(distance_mm)), 0.0, speed_mm_s, 0.0, accel)


def retraction_time_s(length_mm: float, speed_mm_s: float, accel: float = 5000.0) -> float:
    """Durée d'une rétraction ou dé-rétraction (axe E seul)."""
    return axis_move_time_s(length_mm, speed_mm_s, accel)


def heating_time_s(from_c: float, to_c: float, rate_c_s: float) -> float:
    """Durée d'une chauffe (montée seulement ; un refroidissement n'attend pas)."""
    delta = float(to_c) - float(from_c)
    if delta <= 0.0 or float(rate_c_s) <= 0.0:
        return 0.0
    return delta / float(rate_c_s)


def format_duration(seconds: float) -> str:
    """« 1h 12m 3s », « 12m 3s » ou « 45s » pour les en-têtes du G-code."""
    total = int(round(max(0.0, float(seconds))))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


# --- filament ------------------------------------------------------------------------------------------------------

def filament_area_mm2(diameter_mm: float) -> float:
    return math.pi * (float(diameter_mm) / 2.0) ** 2


@dataclass
class FilamentUsage:
    """Consommation d'un extrudeur : longueur (mm), volume (cm³), masse (g), coût (unité monétaire du réglage)."""

    extruder: int = 1
    mm: float = 0.0
    cm3: float = 0.0
    g: float = 0.0
    cost: float = 0.0

    def to_dict(self) -> dict:
        return {"extruder": self.extruder, "mm": round(self.mm, 2), "cm3": round(self.cm3, 3),
                "g": round(self.g, 2), "cost": round(self.cost, 2)}


def filament_usage(extruder: int, length_mm: float, diameter_mm: float, density_g_cm3: float,
                   cost_per_kg: float) -> FilamentUsage:
    """Longueur de filament → volume, masse et coût (densité en g/cm³, coût par kg)."""
    length = max(0.0, float(length_mm))
    cm3 = length * filament_area_mm2(diameter_mm) / 1000.0
    grams = cm3 * max(0.0, float(density_g_cm3))
    return FilamentUsage(extruder=int(extruder), mm=length, cm3=cm3, g=grams, cost=grams / 1000.0 * max(0.0, float(cost_per_kg)))


# --- accumulateur ---------------------------------------------------------------------------------------------------

CATEGORIES = ("extrude", "travel", "retract", "z", "layer_change", "heating", "overhead")


@dataclass
class Estimator:
    """Accumule les durées par catégorie, par couche et par feature, plus la longueur de filament par extrudeur."""

    seconds: dict = field(default_factory=lambda: {name: 0.0 for name in CATEGORIES})
    by_layer: dict = field(default_factory=dict)      # index de couche -> secondes (corps seulement)
    by_feature: dict = field(default_factory=dict)    # feature -> secondes d'extrusion
    filament_mm: dict = field(default_factory=dict)   # extrudeur -> mm
    start_s: float = 0.0                              # séquence de démarrage (chauffe + préparatifs)
    end_s: float = 0.0                                # séquence de fin

    def add(self, category: str, seconds: float, layer_index: int | None = None, feature: str | None = None) -> float:
        value = max(0.0, float(seconds))
        self.seconds[category] = self.seconds.get(category, 0.0) + value
        if layer_index is not None:
            self.by_layer[layer_index] = self.by_layer.get(layer_index, 0.0) + value
        if feature:
            self.by_feature[feature] = self.by_feature.get(feature, 0.0) + value
        return value

    def add_filament(self, extruder: int, length_mm: float) -> None:
        self.filament_mm[int(extruder)] = self.filament_mm.get(int(extruder), 0.0) + max(0.0, float(length_mm))

    @property
    def model_s(self) -> float:
        """Temps du corps de l'impression (couches), hors démarrage et fin."""
        return float(sum(self.seconds.values()))

    @property
    def total_s(self) -> float:
        return self.model_s + self.start_s + self.end_s

    def first_layer_s(self) -> float:
        if not self.by_layer:
            return 0.0
        return float(self.by_layer[min(self.by_layer)])

    def usages(self, filaments: list[dict]) -> list[FilamentUsage]:
        """Consommation par extrudeur à partir des fiches (diamètre, densité, coût/kg), extrudeurs utilisés seulement."""
        result = []
        for extruder in sorted(self.filament_mm):
            fiche = filaments[extruder - 1] if 0 < extruder <= len(filaments) else (filaments[0] if filaments else {})
            result.append(filament_usage(extruder, self.filament_mm[extruder], fiche.get("filament_diameter", 1.75),
                                         fiche.get("filament_density", 1.24), fiche.get("filament_cost", 20.0)))
        return result
