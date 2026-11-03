"""Émetteur G-code d'Alpine Model Engine (docs/MODEL_ENGINE_ARCHITECTURE.md §6.1).

Entrées : des ``LayerMoves`` (parcours ``Move`` en micromètres entiers, §5.7), un ``settings`` plat (clés et défauts
dans ``SETTINGS_DEFAULTS``), des fiches de filament par extrudeur, une ``Machine`` (machine/) et un ``PrintContext``
(AMS, nivellement, timelapse, plaque, objets). Sortie : texte G-code complet (HEADER_BLOCK, CONFIG_BLOCK avec nos clés,
EXECUTABLE_BLOCK, pied) et statistiques (temps, première couche, filament par extrudeur, couches, hauteur).

Règles : E relatif (M83), débit E = largeur · hauteur · longueur · flow / section du filament (section du cordon
(w − h)·h + π·h²/4), rétraction / dé-rétraction / essuyage / z-hop, M204 par feature, M106 0–255, températures
bornées par la machine, M73 P/R à chaque pour cent et à chaque couche, marqueurs de couche, de feature et d'objet.
Aucune coordonnée hors plateau ou dans la zone exclue n'est émise (erreur OUT_OF_BED). Sortie déterministe pour une
même entrée (la date de génération est un paramètre). Tout le texte est écrit par Alpine Makers.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import math
import re
import time
from dataclasses import dataclass, field

import numpy as np

from . import VERSION
from . import estimate
from .machine.base import FAN_PWM_MAX, GcodeSequence, Machine, PrintContext

SCALE = 1000  # micromètres par millimètre

FEATURES = ("OuterWall", "InnerWall", "TopSurface", "BottomSurface", "InternalSolidInfill", "SparseInfill", "Bridge",
            "Support", "SupportInterface", "Brim", "Skirt", "GapFill", "Custom")

# Clés lues dans ``settings`` (et, pour les clés de filament, dans chaque fiche de ``filaments``) avec leurs défauts.
# Unités : mm, mm/s, mm/s², °C, %, g/cm³, coût par kg. 0 = « sans limite » ou « valeur par défaut » selon la clé.
SETTINGS_DEFAULTS = {
    # Rétraction et déplacements (par filament : retraction_*, z_hop, wipe*).
    "retraction_length": 0.8,            # mm rétractés avant un déplacement long
    "retraction_speed": 30.0,            # mm/s
    "deretraction_speed": 30.0,          # mm/s (0 = même vitesse que la rétraction)
    "retraction_minimum_travel": 2.0,    # mm : déplacement plus court = pas de rétraction (sauf Move.retract)
    "retract_before_wipe": 100.0,        # % de la rétraction faite avant l'essuyage, le reste pendant
    "retract_when_changing_layer": True, # rétraction au changement de couche
    "wipe": True,                        # essuyage en revenant sur le dernier cordon
    "wipe_distance": 2.0,                # mm
    "z_hop": 0.4,                        # mm de levée pendant un déplacement rétracté (0 = aucune)
    "z_hop_type": "normal",              # seul type en V1 (« spiral » plus tard)
    # Ventilation (% de 0 à 100 ; convertie en M106 S0–255).
    "fan_min_speed": 100.0,              # ventilation d'un parcours sans consigne
    "fan_max_speed": 100.0,              # plafond
    "overhang_fan_speed": 100.0,         # ventilation imposée dès la classe de surplomb seuil
    "overhang_fan_threshold": 2,         # classe de surplomb (0–3) à partir de laquelle overhang_fan_speed s'applique
    "bridge_fan_speed": 100.0,           # ventilation des ponts
    "close_fan_the_first_x_layers": 1,   # couches sans ventilation
    "aux_fan_speed": 0.0,                # ventilateur auxiliaire (M106 P2) après les premières couches
    "chamber_fan_speed": 0.0,            # ventilateur de chambre / extraction (M106 P3) pendant l'impression
    # Températures (°C ; buse par filament).
    "nozzle_temperature": 220,
    "nozzle_temperature_initial_layer": 220,
    "nozzle_standby_temperature": 140,   # attente pendant le référencement
    "bed_temperature": 55,
    "bed_temperature_initial_layer": 55,
    # Filament (par fiche).
    "filament_type": "PLA",
    "filament_colour": "#FFFFFF",
    "filament_settings_id": "Alpine PLA",
    "filament_vendor": "Alpine Makers",
    "filament_diameter": 1.75,           # mm
    "filament_flow_ratio": 1.0,
    "filament_density": 1.24,            # g/cm³
    "filament_cost": 20.0,               # par kg
    "filament_max_volumetric_speed": 0.0,  # mm³/s, 0 = sans limite
    "tray_info_idx": "",
    # Vitesses et accélérations (mm/s, mm/s² ; 0 = accélération par défaut).
    "travel_speed": 250.0,
    "travel_speed_z": 12.0,
    "default_print_speed": 60.0,         # parcours d'extrusion sans vitesse
    "default_acceleration": 10000.0,
    "initial_layer_acceleration": 500.0,
    "outer_wall_acceleration": 5000.0,
    "inner_wall_acceleration": 0.0,
    "top_surface_acceleration": 2000.0,
    "sparse_infill_acceleration": 0.0,
    "bridge_acceleration": 0.0,
    "travel_acceleration": 0.0,
    "default_jerk": 9.0,                 # mm/s, vitesse de jonction (estimation)
    # Identification du fichier (CONFIG_BLOCK).
    "printer_model": "",
    "printer_settings_id": "",
    "print_settings_id": "0.20mm Standard",
    "nozzle_diameter": 0.4,
    "layer_height": 0.2,
    "initial_layer_print_height": 0.2,
    "sparse_infill_density": 15,
    "sparse_infill_pattern": "grid",
    "wall_loops": 2,
    "enable_support": False,
    "support_type": "normal",
    "brim_type": "auto_brim",
    "enable_prime_tower": False,
    "print_sequence": "by layer",
    # Pauses demandées (indices de couche, 1 = première) : séquence machine.pause_gcode avant la couche.
    "pause_print_layers": (),
}

# Clés lues par fiche de filament (valeur de ``settings`` si la fiche ne la donne pas).
FILAMENT_KEYS = ("filament_type", "filament_colour", "filament_settings_id", "filament_vendor", "filament_diameter",
                 "filament_flow_ratio", "filament_density", "filament_cost", "filament_max_volumetric_speed",
                 "tray_info_idx", "nozzle_temperature", "nozzle_temperature_initial_layer", "nozzle_standby_temperature",
                 "retraction_length", "retraction_speed", "deretraction_speed", "retraction_minimum_travel",
                 "retract_before_wipe", "wipe", "wipe_distance", "z_hop")

# Clés lues comme nombres (validées et converties à la construction : valeur non numérique = SETTINGS_INVALID).
_NUMERIC_KEYS = ("retraction_length", "retraction_speed", "deretraction_speed", "retraction_minimum_travel",
                 "retract_before_wipe", "wipe_distance", "z_hop", "fan_min_speed", "fan_max_speed", "overhang_fan_speed",
                 "bridge_fan_speed", "aux_fan_speed", "chamber_fan_speed", "nozzle_temperature",
                 "nozzle_temperature_initial_layer", "nozzle_standby_temperature", "bed_temperature",
                 "bed_temperature_initial_layer", "filament_diameter", "filament_flow_ratio", "filament_density",
                 "filament_cost", "filament_max_volumetric_speed", "travel_speed", "travel_speed_z", "default_print_speed",
                 "default_acceleration", "initial_layer_acceleration", "outer_wall_acceleration", "inner_wall_acceleration",
                 "top_surface_acceleration", "sparse_infill_acceleration", "bridge_acceleration", "travel_acceleration",
                 "default_jerk", "layer_height", "initial_layer_print_height")
_INT_KEYS = ("overhang_fan_threshold", "close_fan_the_first_x_layers")
_BOOL_KEYS = ("retract_when_changing_layer", "wipe")
_NEGATIVE_FORBIDDEN = ("retraction_length", "retraction_speed", "deretraction_speed", "wipe_distance", "z_hop", "travel_speed",
                       "travel_speed_z", "default_acceleration", "fan_min_speed", "fan_max_speed", "filament_density",
                       "filament_cost", "filament_max_volumetric_speed", "layer_height", "initial_layer_print_height")

# Accélération par feature (clé de réglage ; 0 ou absente = default_acceleration).
_ACCEL_KEYS = {"OuterWall": "outer_wall_acceleration", "InnerWall": "inner_wall_acceleration",
               "TopSurface": "top_surface_acceleration", "SparseInfill": "sparse_infill_acceleration",
               "Bridge": "bridge_acceleration"}

# Clé du CONFIG_BLOCK : texte imprimable sans retour ligne ni séparateur (« : », « = »), 2 à 80 caractères.
_CONFIG_KEY_RE = re.compile(r"^[^\s:=;][^\r\n\t:=]{0,78}[^\s:=]$")

HEADER_START = "; HEADER_BLOCK_START"
HEADER_END = "; HEADER_BLOCK_END"
CONFIG_START = "; CONFIG_BLOCK_START"
CONFIG_END = "; CONFIG_BLOCK_END"
EXECUTABLE_START = "; EXECUTABLE_BLOCK_START"
EXECUTABLE_END = "; EXECUTABLE_BLOCK_END"


class GcodeError(ValueError):
    """Erreur d'émission avec code stable pour la CLI (OUT_OF_BED, EMPTY_SLICE, SETTINGS_INVALID)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# --- structures d'entrée et de sortie ------------------------------------------------------------------------------

@dataclass
class Move:
    """Un parcours planifié (§5.7). ``points`` : int64 (k, 2) en µm. ``kind`` : « travel » ou « extrude ».
    Extensions Alpine : ``retract`` (None = décision de l'émetteur par longueur), ``object_id`` (marqueurs d'objet),
    ``flow_ratio`` (facteur de débit du parcours, ponts par exemple, multiplié par filament_flow_ratio)."""

    kind: str
    feature: str
    points: np.ndarray
    line_width_um: int = 0
    height_um: int = 0
    speed_mm_s: float = 0.0
    fan_percent: float | None = None
    extruder: int = 1
    overhang_class: int = 0
    retract: bool | None = None
    object_id: int | None = None
    flow_ratio: float = 1.0


@dataclass
class LayerMoves:
    """Parcours d'une couche : ``index`` (1 = première), ``z_um`` (haut de la couche), ``height_um``."""

    index: int
    z_um: int
    height_um: int
    moves: list = field(default_factory=list)


@dataclass
class FilamentStats:
    extruder: int
    mm: float
    cm3: float
    g: float
    cost: float
    filament_type: str = "PLA"
    colour: str = "#FFFFFF"

    def to_dict(self) -> dict:
        return {"extruder": self.extruder, "mm": round(self.mm, 2), "cm3": round(self.cm3, 3), "g": round(self.g, 2),
                "cost": round(self.cost, 2), "type": self.filament_type, "color": self.colour}


@dataclass
class GcodeStats:
    prediction_s: float
    model_time_s: float
    first_layer_time_s: float
    layers: int
    max_z_mm: float
    filaments: list
    total_filament_mm: float
    total_filament_cm3: float
    total_filament_g: float
    total_filament_cost: float
    md5: str
    lines: int
    seconds_by_category: dict
    warnings: list

    def to_dict(self) -> dict:
        return {"prediction_s": round(self.prediction_s, 1), "model_time_s": round(self.model_time_s, 1),
                "first_layer_time_s": round(self.first_layer_time_s, 1), "layers": self.layers,
                "max_z_mm": round(self.max_z_mm, 3), "filaments": [f.to_dict() for f in self.filaments],
                "total_filament_mm": round(self.total_filament_mm, 2), "total_filament_cm3": round(self.total_filament_cm3, 3),
                "total_filament_g": round(self.total_filament_g, 2), "total_filament_cost": round(self.total_filament_cost, 2),
                "md5": self.md5, "lines": self.lines, "seconds_by_category": {k: round(v, 1) for k, v in self.seconds_by_category.items()},
                "warnings": list(self.warnings)}


@dataclass
class GcodeResult:
    text: str
    stats: GcodeStats


# --- aides de formatage (déterministes) ----------------------------------------------------------------------------

def _mm(um: int) -> str:
    """Micromètres entiers → millimètres, 3 décimales exactes sans zéros inutiles (« 12.3 », « 0.005 », « -1.2 »)."""
    text = "%.3f" % (int(um) / 1000.0)
    return text.rstrip("0").rstrip(".") or "0"


def _num(value: float, decimals: int = 3) -> str:
    text = f"{float(value):.{decimals}f}"
    return (text.rstrip("0").rstrip(".") if "." in text else text) or "0"


def _feed(speed_mm_s: float) -> int:
    return max(1, int(round(float(speed_mm_s) * 60.0)))


def _pwm(percent: float) -> int:
    """Pourcentage 0–100 → M106 S0–255 (arrondi au plus proche, demi vers le haut : 70 % → 179)."""
    return int(math.floor(FAN_PWM_MAX * max(0.0, min(100.0, float(percent))) / 100.0 + 0.5))


def _bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "oui", "on")
    return bool(value)


def extrusion_area_mm2(width_mm: float, height_mm: float) -> float:
    """Section d'un cordon à bouts arrondis : (w − h)·h + π·h²/4 (§6.1)."""
    w, h = float(width_mm), float(height_mm)
    return max(w - h, 0.0) * h + math.pi * h * h / 4.0


def _config_value(value) -> str | None:
    """Valeur d'une ligne « ; clé = valeur » du CONFIG_BLOCK (None = clé omise)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float, np.integer, np.floating)):
        return _num(value, 4) if isinstance(value, (float, np.floating)) else str(int(value))
    if isinstance(value, (list, tuple)):
        parts = [_config_value(v) for v in value]
        text = ",".join("" if p is None else p for p in parts)  # une case vide garde sa position (« GFL99,,GFL98 »)
        return text if text.strip(",") else None
    text = " ".join(str(value).split())
    return text[:300] if text else None


def _segments_cross_rect(pts: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> bool:
    """Vrai si un segment de la polyligne (k, 2) touche le rectangle [x0, x1] × [y0, y1] (bords inclus).
    Découpe paramétrique vectorisée (Liang–Barsky) : un segment dont les deux extrémités sont dehors peut le traverser."""
    if pts.shape[0] < 2:
        return False
    p = pts[:-1].astype(np.float64)
    d = pts[1:].astype(np.float64) - p
    t_in = np.zeros(p.shape[0], dtype=np.float64)
    t_out = np.ones(p.shape[0], dtype=np.float64)
    rejected = np.zeros(p.shape[0], dtype=bool)
    for pk, qk in ((-d[:, 0], p[:, 0] - x0), (d[:, 0], x1 - p[:, 0]), (-d[:, 1], p[:, 1] - y0), (d[:, 1], y1 - p[:, 1])):
        parallel = pk == 0
        rejected |= parallel & (qk < 0)
        with np.errstate(divide="ignore", invalid="ignore"):
            t = np.where(parallel, 0.0, qk / np.where(parallel, 1.0, pk))
        t_in = np.where(pk < 0, np.maximum(t_in, t), t_in)
        t_out = np.where(pk > 0, np.minimum(t_out, t), t_out)
    return bool(np.any(~rejected & (t_in <= t_out)))


class _M73:
    """Marqueur de progression résolu une fois le temps total connu."""

    __slots__ = ("seconds", "forced")

    def __init__(self, seconds: float, forced: bool):
        self.seconds = seconds
        self.forced = forced


class _StartBlock:
    """Emplacement de la séquence de démarrage (regénérée avec les minutes estimées)."""

    __slots__ = ()


# --- émetteur -------------------------------------------------------------------------------------------------------

class Emitter:
    """Produit le G-code complet d'une impression à partir des parcours planifiés.

    ``filaments`` : liste de fiches (dict) par extrudeur (index 0 = extrudeur 1) ; chaque clé absente d'une fiche est
    lue dans ``settings`` puis dans ``SETTINGS_DEFAULTS``. ``context`` : ``PrintContext`` (options de la demande),
    jamais modifié : ``emit()`` travaille sur une copie, exposée après coup dans ``context_used`` (températures,
    filament, boîte de première couche, noms d'objets remplis). ``generated_at`` : date écrite dans l'en-tête (fixe
    pour un résultat reproductible ; défaut : heure UTC courante). Un même ``Emitter`` peut émettre plusieurs fois."""

    def __init__(self, machine: Machine, filaments: list | None = None, settings: dict | None = None,
                 context: PrintContext | None = None, generated_at: str | None = None):
        self.machine = machine
        self.settings = dict(SETTINGS_DEFAULTS)
        for key, value in (settings or {}).items():
            if value is not None:
                self.settings[key] = value
        fiches = list(filaments or [{}])
        self.filaments = []
        for fiche in fiches:
            merged = {key: self.settings[key] for key in FILAMENT_KEYS}
            for key, value in (fiche or {}).items():
                if value is not None:
                    merged[key] = value
            self.filaments.append(merged)
        self.context = context if context is not None else PrintContext()  # modèle, jamais modifié par emit()
        self.context_used: PrintContext | None = None  # copie de travail de la dernière émission (pour l'intégrateur)
        self.generated_at = generated_at or time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        self.warnings: list[str] = []
        self._validate_settings()

    # --- réglages ------------------------------------------------------------------------------------------------
    @staticmethod
    def _number(key: str, value, where: str = "") -> float:
        """Valeur numérique finie d'un réglage, sinon SETTINGS_INVALID (jamais un ValueError brut)."""
        try:
            if isinstance(value, bool):
                raise TypeError("booléen")
            number = float(value)
        except (TypeError, ValueError):
            raise GcodeError("SETTINGS_INVALID", f"Réglage {key}{where} non numérique : {value!r}.") from None
        if not math.isfinite(number):
            raise GcodeError("SETTINGS_INVALID", f"Réglage {key}{where} non fini : {value!r}.")
        return number

    @staticmethod
    def _pause_layers(raw) -> tuple:
        """Indices de couche des pauses : liste, tuple, ensemble, nombre ou texte « 3, 12 » → tuple trié d'entiers ≥ 1."""
        if raw is None or raw == "":
            return ()
        if isinstance(raw, str):
            parts = [p for p in re.split(r"[,;\s]+", raw.strip()) if p]
        elif isinstance(raw, (int, float)):
            parts = [raw]
        else:
            try:
                parts = list(raw)
            except TypeError:
                raise GcodeError("SETTINGS_INVALID", f"pause_print_layers invalide : {raw!r}.") from None
        result = set()
        for part in parts:
            try:
                index = int(float(part))
            except (TypeError, ValueError):
                raise GcodeError("SETTINGS_INVALID", f"pause_print_layers : indice de couche invalide {part!r}.") from None
            if index < 1:
                raise GcodeError("SETTINGS_INVALID", f"pause_print_layers : indice de couche {index} (1 = première couche).")
            result.add(index)
        return tuple(sorted(result))

    def _validate_settings(self) -> None:
        s = self.settings
        for key in _NUMERIC_KEYS:
            s[key] = self._number(key, s[key])
        for key in _INT_KEYS:
            s[key] = int(self._number(key, s[key]))
        for key in _BOOL_KEYS:
            s[key] = _bool(s[key])
        for key in _NEGATIVE_FORBIDDEN:
            if s[key] < 0:
                raise GcodeError("SETTINGS_INVALID", f"Réglage {key} négatif : {s[key]!r}.")
        for number, fiche in enumerate(self.filaments, start=1):
            where = f" (filament {number})"
            for key in FILAMENT_KEYS:
                if key in _NUMERIC_KEYS:
                    fiche[key] = self._number(key, fiche[key], where)
                elif key in _BOOL_KEYS:
                    fiche[key] = _bool(fiche[key])
            for key in _NEGATIVE_FORBIDDEN:
                if key in fiche and fiche[key] < 0:
                    raise GcodeError("SETTINGS_INVALID", f"Réglage {key}{where} négatif : {fiche[key]!r}.")
            if not (1.0 <= fiche["filament_diameter"] <= 3.5):
                raise GcodeError("SETTINGS_INVALID", f"Diamètre de filament invalide{where} : {fiche['filament_diameter']!r}.")
            if fiche["filament_flow_ratio"] <= 0:
                raise GcodeError("SETTINGS_INVALID", f"Facteur de débit invalide{where} : {fiche['filament_flow_ratio']!r}.")
        s["pause_print_layers"] = self._pause_layers(s["pause_print_layers"])
        if s["travel_speed"] <= 0:
            s["travel_speed"] = SETTINGS_DEFAULTS["travel_speed"]
        if s["travel_speed_z"] <= 0:
            s["travel_speed_z"] = SETTINGS_DEFAULTS["travel_speed_z"]

    def _f(self, key: str, extruder: int | None = None):
        """Valeur d'une clé de filament pour l'extrudeur courant (fiche 1 si l'extrudeur n'a pas de fiche)."""
        ext = int(extruder or self.extruder)
        fiche = self.filaments[ext - 1] if 0 < ext <= len(self.filaments) else self.filaments[0]
        return fiche[key]

    def _accel_for(self, move: Move | None, layer_index: int) -> float:
        s = self.settings
        default = float(s["default_acceleration"]) or self.machine.max_accel.x
        if layer_index == 1 and float(s["initial_layer_acceleration"]) > 0:
            value = float(s["initial_layer_acceleration"])
        elif move is None or move.kind == "travel":
            value = float(s["travel_acceleration"]) or default
        else:
            value = float(s.get(_ACCEL_KEYS.get(move.feature, ""), 0) or 0) or default
        return max(100.0, min(value, self.machine.max_accel.x))

    def _fan_for(self, move: Move, layer_index: int) -> float:
        s = self.settings
        if layer_index <= int(s["close_fan_the_first_x_layers"]):
            return 0.0
        percent = float(s["fan_min_speed"]) if move.fan_percent is None else float(move.fan_percent)
        if move.feature == "Bridge":
            percent = max(percent, float(s["bridge_fan_speed"]))
        elif int(move.overhang_class) >= int(s["overhang_fan_threshold"]) and int(move.overhang_class) > 0:
            percent = max(percent, float(s["overhang_fan_speed"]))
        return max(0.0, min(percent, float(s["fan_max_speed"]), 100.0))

    def _nozzle_temp(self, extruder: int, first_layer: bool) -> int:
        key = "nozzle_temperature_initial_layer" if first_layer else "nozzle_temperature"
        return self.machine.clamp_nozzle_temp(float(self._f(key, extruder)))

    def _bed_temp(self, first_layer: bool) -> int:
        key = "bed_temperature_initial_layer" if first_layer else "bed_temperature"
        return self.machine.clamp_bed_temp(float(self.settings[key]))

    # --- validation des parcours ------------------------------------------------------------------------------------
    def _check_layers(self, layers: list) -> None:
        if not layers or not any(m.kind == "extrude" for layer in layers for m in layer.moves):
            raise GcodeError("EMPTY_SLICE", "Aucun parcours d'extrusion : rien à imprimer.")
        bed = self.machine.bed
        x_max = int(round(bed.width_mm * SCALE))
        y_max = int(round(bed.depth_mm * SCALE))
        z_max = int(round(bed.height_mm * SCALE))
        excl = None
        if bed.exclude_mm:
            excl = tuple(int(round(float(v) * SCALE)) for v in bed.exclude_mm)
        indices = [int(layer.index) for layer in layers]
        if indices != list(range(1, len(layers) + 1)):
            raise GcodeError("SETTINGS_INVALID", f"Indices de couche attendus de 1 à {len(layers)} sans trou ni doublon, reçus : "
                                                 f"{indices[:8]}{'…' if len(indices) > 8 else ''}.")
        previous_z = 0
        for layer in layers:
            z_um = int(layer.z_um)
            if z_um <= previous_z or z_um > z_max:
                raise GcodeError("OUT_OF_BED", f"Couche {layer.index} : hauteur {_mm(z_um)} mm hors plage (0–{_num(bed.height_mm)} mm, croissante).")
            if int(layer.height_um) <= 0:
                raise GcodeError("SETTINGS_INVALID", f"Couche {layer.index} : hauteur de couche nulle.")
            if int(layer.height_um) > z_um:
                raise GcodeError("SETTINGS_INVALID", f"Couche {layer.index} : hauteur de couche {_mm(int(layer.height_um))} mm "
                                                     f"supérieure à sa hauteur {_mm(z_um)} mm (extrusion sous le plateau).")
            previous_z = z_um
            for move in layer.moves:
                pts = np.asarray(move.points)
                if pts.ndim != 2 or pts.shape[1] != 2:
                    raise GcodeError("SETTINGS_INVALID", f"Couche {layer.index} : points de forme {pts.shape} (attendu (k, 2)).")
                if pts.shape[0] == 0:
                    continue
                if move.kind not in ("travel", "extrude"):
                    raise GcodeError("SETTINGS_INVALID", f"Type de parcours inconnu : {move.kind!r}.")
                xs, ys = pts[:, 0], pts[:, 1]
                if xs.min() < 0 or ys.min() < 0 or xs.max() > x_max or ys.max() > y_max:
                    raise GcodeError("OUT_OF_BED", f"Couche {layer.index} : parcours {move.feature} hors du plateau "
                                                   f"({_mm(int(xs.min()))}–{_mm(int(xs.max()))} × {_mm(int(ys.min()))}–{_mm(int(ys.max()))} mm).")
                if excl is not None and move.kind == "extrude":
                    x0, y0, x1, y1 = excl
                    # Rejet rapide par boîte englobante, puis test segment par segment (traversée sans sommet dedans).
                    if xs.min() <= x1 and xs.max() >= x0 and ys.min() <= y1 and ys.max() >= y0 and _segments_cross_rect(pts, x0, y0, x1, y1):
                        raise GcodeError("OUT_OF_BED", f"Couche {layer.index} : parcours {move.feature} dans la zone exclue du plateau.")
                if move.kind == "extrude" and (int(move.line_width_um) <= 0 or int(move.height_um) <= 0):
                    raise GcodeError("SETTINGS_INVALID", f"Couche {layer.index} : largeur ou hauteur de cordon nulle ({move.feature}).")
                if int(move.extruder) < 1 or int(move.extruder) > 16:
                    raise GcodeError("SETTINGS_INVALID", f"Extrudeur {move.extruder} hors plage 1–16.")

    # --- contexte machine ----------------------------------------------------------------------------------------------
    def _prepare_context(self, layers: list) -> PrintContext:
        """Copie de travail du contexte (le modèle reste intact : un même PrintContext peut servir à plusieurs plateaux)."""
        ctx = copy.copy(self.context)
        ctx.ams_slots = dict(self.context.ams_slots or {})
        ctx.object_ids = dict(self.context.object_ids or {})
        self._ctx = ctx
        self.context_used = ctx
        s = self.settings
        first = layers[0]
        first_extruder = next((int(m.extruder) for m in first.moves if m.kind == "extrude"), 1)
        ctx.extruder = ctx.previous_extruder = ctx.next_extruder = first_extruder
        ctx.nozzle_temperature_initial_layer = self._nozzle_temp(first_extruder, True)
        ctx.nozzle_temperature = self._nozzle_temp(first_extruder, False)
        ctx.bed_temperature_initial_layer = self._bed_temp(True)
        ctx.bed_temperature = self._bed_temp(False)
        ctx.nozzle_standby_temperature = self.machine.clamp_nozzle_temp(float(self._f("nozzle_standby_temperature", first_extruder)))
        ctx.filament_type = str(self._f("filament_type", first_extruder))
        ctx.filament_diameter_mm = float(self._f("filament_diameter", first_extruder))
        ctx.flow_ratio = float(self._f("filament_flow_ratio", first_extruder))
        ctx.retraction_length_mm = float(self._f("retraction_length", first_extruder))
        ctx.retraction_speed_mm_s = float(self._f("retraction_speed", first_extruder))
        ctx.travel_speed_mm_s = min(float(s["travel_speed"]), self.machine.max_speed.x)
        ctx.z_speed_mm_s = min(float(s["travel_speed_z"]), self.machine.max_speed.z)
        ctx.default_acceleration = max(100.0, min(float(s["default_acceleration"]) or self.machine.max_accel.x, self.machine.max_accel.x))
        ctx.total_layers = len(layers)
        ctx.layer_index = 1
        ctx.z_mm = int(first.z_um) / SCALE
        ctx.first_layer_height_mm = int(first.height_um) / SCALE
        ctx.layer_height_mm = float(s["layer_height"]) or int(layers[-1].height_um) / SCALE
        ctx.retracted = False
        if ctx.first_layer_box_mm is None:
            boxes = [np.asarray(m.points) for m in first.moves if m.kind == "extrude" and len(m.points)]
            if boxes:
                allpts = np.concatenate(boxes, axis=0)
                ctx.first_layer_box_mm = (float(allpts[:, 0].min()) / SCALE, float(allpts[:, 1].min()) / SCALE,
                                          float(allpts[:, 0].max()) / SCALE, float(allpts[:, 1].max()) / SCALE)
        # Noms d'objets : ceux de la demande (clés entières normalisées), complétés pour chaque identifiant des parcours.
        names: dict = {}
        for key, value in ctx.object_ids.items():
            try:
                names[int(key)] = str(value)
            except (TypeError, ValueError):
                names[key] = str(value)
        ids = sorted({int(m.object_id) for layer in layers for m in layer.moves if m.object_id is not None})
        for i in ids:
            names.setdefault(i, f"Objet {i}")
        ctx.object_ids = names if names else {1: "Objet 1"}
        return ctx

    # --- émission -------------------------------------------------------------------------------------------------------
    def emit(self, layers: list) -> GcodeResult:
        """G-code complet et statistiques pour les couches données (triées par index croissant)."""
        self.machine.ensure_sliceable()
        layers = sorted(layers, key=lambda layer: int(layer.index))
        self._check_layers(layers)
        self.warnings = []
        ctx = self._prepare_context(layers)
        self.est = estimate.Estimator()
        self.out: list = []
        self._pauses = set(self.settings["pause_print_layers"])
        # État modal.
        self.extruder = ctx.extruder
        self.pos: tuple | None = None       # (x_um, y_um) connue
        self.z_um = 0                       # Z de la couche courante
        self.hop_um = 0                     # levée au-dessus de z_um (Z physique = z_um + hop_um quand z_known)
        self.z_known = False                # Z physique connue ; sinon « G1 Z » réémis avant la prochaine extrusion
        self.retracted = False
        self.retracted_mm = 0.0
        self.feed = -1
        self.fan_pwm = -1
        self.accel = -1.0
        self.feature = None
        self.line_width_um = -1
        self.object_id = None
        self.e_exact = 0.0
        self.e_emitted = 0.0
        self.last_extrusion: np.ndarray | None = None
        self.last_speed = float(self.settings["default_print_speed"])
        self.layer_index = 1
        self.max_z_um = 0

        # Démarrage : état connu à la fin de la séquence (le texte est regénéré avec les minutes estimées).
        ctx.estimated_minutes = 0
        start = self.machine.start_gcode(ctx)
        self._apply_sequence_state(start, ctx.extruder)
        self.est.start_s = start.seconds
        self.out.append(_StartBlock())

        chamber = float(self.settings["chamber_fan_speed"])
        if chamber > 0 and self.machine.fans.chamber:
            self.out.append(f"M106 P3 S{_pwm(chamber)} ; ventilateur de chambre")

        for layer in layers:
            self._emit_layer(layer, ctx, len(layers))

        self._close_object()
        ctx.layer_index = self.layer_index
        ctx.z_mm = (self.z_um + self.hop_um) / SCALE
        ctx.retracted = self.retracted
        ctx.extruder = self.extruder
        end = self.machine.end_gcode(ctx)
        self.out.extend(end.lines)
        self.est.end_s = end.seconds
        if end.extruded_mm:
            self.est.add_filament(self.extruder, end.extruded_mm)

        # Temps total connu : séquence de démarrage définitive et marqueurs M73.
        total_s = self.est.total_s
        ctx.estimated_minutes = int(math.ceil(total_s / 60.0))
        ctx.layer_index, ctx.z_mm, ctx.retracted, ctx.extruder = 1, int(layers[0].z_um) / SCALE, False, layers_first_extruder(layers)
        final_start = self.machine.start_gcode(ctx)
        if (final_start.position_mm, final_start.retracted, round(final_start.extruded_mm, 6)) != \
                (start.position_mm, start.retracted, round(start.extruded_mm, 6)):
            raise GcodeError("SETTINGS_INVALID", "Séquence de démarrage non déterministe (état final différent).")
        body = self._resolve(final_start.lines, total_s)
        usages = self.est.usages(self.filaments)
        stats_filaments = [FilamentStats(u.extruder, u.mm, u.cm3, u.g, u.cost,
                                         str(self._f("filament_type", u.extruder)), str(self._f("filament_colour", u.extruder)))
                           for u in usages]
        header = self._header(len(layers), total_s, stats_filaments, ctx)
        config = self._config(total_s, stats_filaments, ctx)
        footer = self._footer(len(layers), total_s, stats_filaments)
        text = "\n".join(header + config + [EXECUTABLE_START] + body + [EXECUTABLE_END] + footer) + "\n"
        stats = GcodeStats(
            prediction_s=total_s, model_time_s=self.est.model_s, first_layer_time_s=self.est.first_layer_s(),
            layers=len(layers), max_z_mm=self.max_z_um / SCALE, filaments=stats_filaments,
            total_filament_mm=sum(f.mm for f in stats_filaments), total_filament_cm3=sum(f.cm3 for f in stats_filaments),
            total_filament_g=sum(f.g for f in stats_filaments), total_filament_cost=sum(f.cost for f in stats_filaments),
            md5=hashlib.md5(text.encode("utf-8")).hexdigest().upper(), lines=text.count("\n"),
            seconds_by_category=dict(self.est.seconds, start=self.est.start_s, end=self.est.end_s), warnings=list(self.warnings))
        return GcodeResult(text=text, stats=stats)

    def _apply_sequence_state(self, seq: GcodeSequence, extruder: int) -> None:
        """État modal après une séquence machine : position XY (ou inconnue), Z physique (ou inconnue : réémise avant
        la prochaine extrusion), rétraction, filament consommé ; F, ventilation et accélération à réémettre."""
        z_physical = None
        if seq.position_mm is not None:
            x, y, z = seq.position_mm
            self.pos = (int(round(x * SCALE)), int(round(y * SCALE)))
            z_physical = z
        else:
            self.pos = None
            z_physical = seq.z_mm
        if z_physical is not None:
            physical_um = int(round(float(z_physical) * SCALE))
            if physical_um >= self.z_um:
                self.hop_um = physical_um - self.z_um
                self.z_known = True
            else:  # séquence restée sous la couche : descente/montée réémise avant la prochaine extrusion
                self.hop_um = 0
                self.z_known = False
        else:
            self.hop_um = 0
            self.z_known = False
        self.retracted = bool(seq.retracted)
        self.retracted_mm = float(self._f("retraction_length", extruder)) if seq.retracted else 0.0
        self.feed = -1
        self.fan_pwm = -1
        self.accel = -1.0
        self.last_extrusion = None
        if seq.extruded_mm:
            self.est.add_filament(extruder, seq.extruded_mm)

    # --- couche --------------------------------------------------------------------------------------------------------
    def _emit_layer(self, layer: LayerMoves, ctx: PrintContext, total_layers: int) -> None:
        s = self.settings
        index = int(layer.index)
        z_um = int(layer.z_um)
        # Rétraction de fin de couche (le dernier parcours est en général une extrusion) : émise avant de fermer le bloc
        # d'objet, pour qu'un objet sauté par l'écran (M624/M625) emporte sa rétraction et laisse la buse cohérente.
        if index > 1 and s["retract_when_changing_layer"] and not self.retracted:
            self._retract(self.layer_index)
        if index in self._pauses and not self.retracted:
            self._retract(self.layer_index)  # pas de suintement pendant une pause
        self._close_object()
        self.layer_index = index
        self.feature = None          # chaque couche commence par ses marqueurs FEATURE / LINE_WIDTH
        self.line_width_um = -1
        ctx.layer_index = index
        ctx.z_mm = z_um / SCALE
        ctx.layer_height_mm = int(layer.height_um) / SCALE
        ctx.retracted = self.retracted
        ctx.extruder = self.extruder
        out = self.out
        if index in self._pauses:
            pause = self.machine.pause_gcode(ctx)
            out.extend(pause.lines)
            self.est.add("overhead", pause.seconds, index)
            # Après la pause, le firmware remet la tête en place : Z réémise avant la prochaine extrusion, F/M106/M204 aussi.
            self.retracted = bool(pause.retracted)
            if not self.retracted:
                self.retracted_mm = 0.0
            self.z_known = False
            self.feed = -1
            self.fan_pwm = -1
            self.accel = -1.0
        out.append("; CHANGE_LAYER")
        out.append(f"; Z_HEIGHT: {_mm(z_um)}")
        out.append(f"; LAYER_HEIGHT: {_mm(int(layer.height_um))}")
        out.append(f"; layer num/total_layer_count: {index}/{total_layers}")
        change = self.machine.layer_change_gcode(ctx)
        out.extend(change.lines)
        self.est.add("layer_change", change.seconds, index)
        out.append(_M73(self.est.model_s, True))
        # Déplacement Z vers la couche. Montée : tout de suite (annule une levée en cours). Descente (première couche
        # depuis le dégagement de la séquence de démarrage, levée de fin de couche) ou Z inconnue : différée juste avant
        # la première extrusion, pour que le déplacement vers l'objet se fasse en hauteur et non en traînant la buse.
        physical = self.z_um + self.hop_um
        if not self.z_known:
            self.z_um = z_um
            self.hop_um = 0
        elif z_um >= physical:
            z_speed = min(float(s["travel_speed_z"]), self.machine.max_speed.z)
            z_feed = _feed(z_speed)
            out.append(f"G1 Z{_mm(z_um)} F{z_feed}")
            self.feed = z_feed
            self.est.add("z", estimate.axis_move_time_s((z_um - physical) / SCALE, z_speed, self.machine.max_accel.z), index)
            self.z_um = z_um
            self.hop_um = 0
        else:
            self.z_um = z_um
            self.hop_um = physical - z_um
        self.max_z_um = max(self.max_z_um, z_um)
        # Températures des couches suivantes et ventilateur auxiliaire.
        if index == 2:
            nozzle_t = self._nozzle_temp(self.extruder, False)
            if nozzle_t != self._nozzle_temp(self.extruder, True):
                out.append(f"M104 S{nozzle_t} ; température de buse des couches suivantes")
            bed_t = self._bed_temp(False)
            if bed_t != self._bed_temp(True):
                out.append(f"M140 S{bed_t} ; température du lit des couches suivantes")
        aux = float(s["aux_fan_speed"])
        if aux > 0 and self.machine.fans.aux and index == int(s["close_fan_the_first_x_layers"]) + 1:
            out.append(f"M106 P2 S{_pwm(aux)} ; ventilateur auxiliaire")
        # Objet visé par chaque déplacement explicite : celui de la prochaine extrusion (None = aucune / sans objet),
        # pour fermer le bloc M624/M625 courant AVANT le déplacement qui quitte l'objet.
        moves = list(layer.moves)
        next_object = [None] * len(moves)
        pending = None
        for i in range(len(moves) - 1, -1, -1):
            if moves[i].kind == "extrude":
                pending = None if moves[i].object_id is None else int(moves[i].object_id)
            next_object[i] = pending
        for i, move in enumerate(moves):
            pts = np.asarray(move.points, dtype=np.int64)
            if pts.shape[0] == 0:
                continue
            if move.kind == "extrude" and int(move.extruder) != self.extruder:
                self._change_filament(int(move.extruder), ctx)
            if move.kind == "travel":
                leaving = self.object_id is not None and next_object[i] != self.object_id
                self._travel(pts, move.retract, index, close_object=leaving)
            else:
                self._extrude(move, pts, index)
            out.append(_M73(self.est.model_s, False))

    # --- changement de filament ----------------------------------------------------------------------------------------
    def _change_filament(self, new_extruder: int, ctx: PrintContext) -> None:
        # Le filament courant est rétracté avec son propre essuyage avant de fermer le bloc d'objet (voir _emit_layer).
        if not self.retracted:
            self._retract(self.layer_index)
        self._close_object()
        ctx.previous_extruder = self.extruder
        ctx.next_extruder = new_extruder
        ctx.z_mm = (self.z_um + self.hop_um) / SCALE  # Z physique (levée comprise)
        ctx.retracted = self.retracted
        ctx.retraction_length_mm = float(self._f("retraction_length"))  # filament sortant
        ctx.retraction_speed_mm_s = float(self._f("retraction_speed"))
        ctx.nozzle_temperature = self._nozzle_temp(new_extruder, self.layer_index == 1)
        seq = self.machine.filament_change_gcode(ctx)
        self.out.extend(seq.lines)
        self.est.add("overhead", seq.seconds, self.layer_index)
        self.extruder = new_extruder
        ctx.extruder = new_extruder
        ctx.filament_type = str(self._f("filament_type", new_extruder))
        ctx.filament_diameter_mm = float(self._f("filament_diameter", new_extruder))
        ctx.flow_ratio = float(self._f("filament_flow_ratio", new_extruder))
        ctx.retraction_length_mm = float(self._f("retraction_length", new_extruder))
        ctx.retraction_speed_mm_s = float(self._f("retraction_speed", new_extruder))
        # État après la séquence : XY inconnue ou connue, Z connue (dégagement) ou réémise, rétracté selon la séquence.
        self._apply_sequence_state(seq, new_extruder)
        self.feature = None
        self.line_width_um = -1

    # --- objets --------------------------------------------------------------------------------------------------------
    def _open_object(self, object_id: int) -> None:
        if not self._ctx.label_objects:
            return
        if self.object_id == object_id:
            return
        self._close_object()
        name = str(self._ctx.object_ids.get(object_id, self._ctx.object_ids.get(str(object_id), f"Objet {object_id}")))
        payload = base64.b64encode(name.encode("utf-8")).decode("ascii")
        self.out.append(f"; start printing object, unique label id: {object_id}")
        self.out.append(f"M624 {payload}")
        self.object_id = object_id

    def _close_object(self) -> None:
        if self.object_id is None:
            return
        self.out.append(f"; stop printing object, unique label id: {self.object_id}")
        self.out.append("M625")
        self.object_id = None

    # --- rétraction, essuyage, levée -----------------------------------------------------------------------------------
    def _retract(self, layer_index: int, wipe_allowed: bool = True) -> None:
        """Rétraction (avec essuyage si réglé et possible) puis levée Z. Sans effet si déjà rétracté."""
        if self.retracted:
            return
        length = max(0.0, float(self._f("retraction_length")))
        if length <= 0:
            return
        speed = max(1.0, float(self._f("retraction_speed")))
        feed = _feed(speed)
        before = length
        wipe = wipe_allowed and _bool(self._f("wipe")) and self.last_extrusion is not None and self.pos is not None
        wipe_path = None
        if wipe:
            wipe_path = self._wipe_path(float(self._f("wipe_distance")))
            if wipe_path is None:
                wipe = False
        out = self.out
        if wipe:
            before = length * max(0.0, min(100.0, float(self._f("retract_before_wipe")))) / 100.0
        if before > 0:
            out.append(f"G1 E-{before:.5f} F{feed}")
            self.feed = feed
            self.est.add("retract", estimate.retraction_time_s(before, speed, self.machine.max_accel.e), layer_index)
        if wipe:
            points, lengths = wipe_path
            remaining = length - before
            total = float(lengths.sum())
            wipe_speed = max(1.0, min(float(self.last_speed), self.machine.max_speed.x))
            wipe_feed = _feed(wipe_speed)
            out.append("; WIPE_START")
            first = True
            for point, seg in zip(points, lengths):
                e_part = remaining * float(seg) / total if total > 0 and remaining > 0 else 0.0
                e_text = f" E-{e_part:.5f}" if e_part > 0 else ""
                f_text = f" F{wipe_feed}" if first and wipe_feed != self.feed else ""
                out.append(f"G1 X{_mm(int(point[0]))} Y{_mm(int(point[1]))}{e_text}{f_text}")
                first = False
            self.feed = wipe_feed
            out.append("; WIPE_END")
            self.pos = (int(points[-1][0]), int(points[-1][1]))
            self.est.add("retract", total / SCALE / wipe_speed, layer_index)
        self.retracted = True
        self.retracted_mm = length
        hop = max(0.0, float(self._f("z_hop")))
        if hop > 0:
            physical = self.z_um + self.hop_um
            z_target = min(self.z_um + int(round(hop * SCALE)), int(round(self.machine.bed.height_mm * SCALE)))
            if z_target > physical or not self.z_known:
                z_speed = min(float(self.settings["travel_speed_z"]), self.machine.max_speed.z)
                z_feed = _feed(z_speed)
                out.append(f"G1 Z{_mm(z_target)} F{z_feed}")
                self.feed = z_feed
                self.est.add("z", estimate.axis_move_time_s(max(0, z_target - physical) / SCALE, z_speed, self.machine.max_accel.z), layer_index)
                self.hop_um = z_target - self.z_um
                self.z_known = True

    def _wipe_path(self, distance_mm: float):
        """Points de l'essuyage : retour sur le dernier cordon depuis la position courante, jusqu'à ``distance_mm``."""
        if distance_mm <= 0 or self.last_extrusion is None or self.last_extrusion.shape[0] < 2:
            return None
        pts = self.last_extrusion
        if (int(pts[-1][0]), int(pts[-1][1])) != self.pos:
            return None
        budget = distance_mm * SCALE
        points, lengths = [], []
        current = pts[-1].astype(np.float64)
        for i in range(pts.shape[0] - 2, -1, -1):
            target = pts[i].astype(np.float64)
            seg = float(np.hypot(*(target - current)))
            if seg <= 0:
                continue
            if seg >= budget:
                target = current + (target - current) * (budget / seg)
                seg = budget
            points.append((int(round(target[0])), int(round(target[1]))))
            lengths.append(seg)
            budget -= seg
            current = target
            if budget <= 0:
                break
        if not points:
            return None
        return points, np.asarray(lengths, dtype=np.float64)

    def _unhop_and_deretract(self, layer_index: int) -> None:
        """Juste avant une extrusion : retour à la hauteur de la couche (levée, descente différée ou Z inconnue) puis
        dé-rétraction. Toujours à l'intérieur du bloc d'objet courant."""
        out = self.out
        if self.hop_um or not self.z_known:
            z_speed = min(float(self.settings["travel_speed_z"]), self.machine.max_speed.z)
            z_feed = _feed(z_speed)
            out.append(f"G1 Z{_mm(self.z_um)} F{z_feed}")
            self.feed = z_feed
            self.est.add("z", estimate.axis_move_time_s(self.hop_um / SCALE, z_speed, self.machine.max_accel.z), layer_index)
            self.hop_um = 0
            self.z_known = True
        if self.retracted:
            length = self.retracted_mm if self.retracted_mm > 0 else float(self._f("retraction_length"))
            if length > 0:
                speed = float(self._f("deretraction_speed")) or float(self._f("retraction_speed"))
                speed = max(1.0, speed)
                feed = _feed(speed)
                out.append(f"G1 E{length:.5f} F{feed}")
                self.feed = feed
                self.est.add("retract", estimate.retraction_time_s(length, speed, self.machine.max_accel.e), layer_index)
            self.retracted = False
            self.retracted_mm = 0.0

    # --- déplacement ---------------------------------------------------------------------------------------------------
    def _travel(self, pts: np.ndarray, retract_flag, layer_index: int, close_object: bool = False) -> None:
        """Déplacement sans extrusion. ``close_object`` : le déplacement quitte l'objet courant → rétraction (toujours,
        pour qu'un objet sauté laisse la buse rétractée et levée), fermeture du bloc M624/M625, puis déplacement."""
        targets = pts
        if self.pos is not None and int(pts[0][0]) == self.pos[0] and int(pts[0][1]) == self.pos[1]:
            targets = pts[1:]
        if targets.shape[0] == 0:
            if close_object:
                self._retract(layer_index)
                self._close_object()
            return
        if self.pos is None:
            path = targets
            length_mm = float(np.sum(np.hypot(*np.diff(path, axis=0).T))) / SCALE if path.shape[0] >= 2 else 0.0
        elif targets.shape[0] == 1:  # cas courant : un seul point visé, calcul scalaire
            path = None
            length_mm = math.hypot(int(targets[0][0]) - self.pos[0], int(targets[0][1]) - self.pos[1]) / SCALE
        else:
            path = np.concatenate((np.asarray([self.pos], dtype=np.int64), targets), axis=0)
            length_mm = float(np.sum(np.hypot(*np.diff(path, axis=0).T))) / SCALE
        if close_object:
            do_retract = True
        elif retract_flag is None:
            do_retract = length_mm >= float(self._f("retraction_minimum_travel")) and float(self._f("retraction_length")) > 0
        else:
            do_retract = bool(retract_flag)
        if do_retract:
            self._retract(layer_index)
        if close_object:
            self._close_object()
        speed = min(float(self.settings["travel_speed"]), self.machine.max_speed.x)
        feed = _feed(speed)
        accel = self._accel_for(None, layer_index)
        if accel != self.accel:
            self.out.append(f"M204 S{_num(accel, 0)}")
            self.accel = accel
        out = self.out
        for i in range(targets.shape[0]):
            x, y = int(targets[i][0]), int(targets[i][1])
            if feed != self.feed:
                out.append(f"G1 X{_mm(x)} Y{_mm(y)} F{feed}")
                self.feed = feed
            else:
                out.append(f"G1 X{_mm(x)} Y{_mm(y)}")
        self.pos = (int(targets[-1][0]), int(targets[-1][1]))
        if path is None:
            self.est.add("travel", estimate.axis_move_time_s(length_mm, speed, accel), layer_index)
        elif path.shape[0] >= 2:
            self.est.add("travel", estimate.plan_polyline(path.astype(np.float64) / SCALE, speed, accel,
                                                          min(float(self.settings["default_jerk"]), self.machine.max_jerk.x)), layer_index)
        self.last_extrusion = None

    # --- extrusion -----------------------------------------------------------------------------------------------------
    def _extrude(self, move: Move, pts: np.ndarray, layer_index: int) -> None:
        # Points consécutifs identiques retirés.
        if pts.shape[0] == 2:
            if int(pts[0][0]) == int(pts[1][0]) and int(pts[0][1]) == int(pts[1][1]):
                pts = pts[:1]
        elif pts.shape[0] > 2:
            keep = np.ones(pts.shape[0], dtype=bool)
            keep[1:] = np.any(pts[1:] != pts[:-1], axis=1)
            pts = pts[keep]
        if pts.shape[0] < 2:
            self.warnings.append(f"Couche {layer_index} : parcours {move.feature} réduit à un point, ignoré.")
            return
        start = (int(pts[0][0]), int(pts[0][1]))
        target_object = None if move.object_id is None else int(move.object_id)
        leaving = self.object_id is not None and target_object != self.object_id
        if self.pos != start:
            self._travel(pts[:1], None, layer_index, close_object=leaving)
        elif leaving:
            self._retract(layer_index)
            self._close_object()
        if target_object is not None:
            self._open_object(target_object)
        self._unhop_and_deretract(layer_index)
        out = self.out
        if move.feature != self.feature:
            out.append(f"; FEATURE: {move.feature}")
            self.feature = move.feature
        if int(move.line_width_um) != self.line_width_um:
            out.append(f"; LINE_WIDTH: {_mm(int(move.line_width_um))}")
            self.line_width_um = int(move.line_width_um)
        fan = _pwm(self._fan_for(move, layer_index))
        if fan != self.fan_pwm:
            out.append(f"M106 S{fan}")
            self.fan_pwm = fan
        accel = self._accel_for(move, layer_index)
        if accel != self.accel:
            out.append(f"M204 S{_num(accel, 0)}")
            self.accel = accel
        # Débit et vitesse.
        width_mm = int(move.line_width_um) / SCALE
        height_mm = int(move.height_um) / SCALE
        section = extrusion_area_mm2(width_mm, height_mm)
        flow = float(self._f("filament_flow_ratio")) * float(move.flow_ratio or 1.0)
        e_per_mm = section * flow / estimate.filament_area_mm2(float(self._f("filament_diameter")))
        speed = float(move.speed_mm_s) or float(self.settings["default_print_speed"])
        speed = max(0.5, min(speed, self.machine.max_speed.x))
        max_vol = float(self._f("filament_max_volumetric_speed"))
        if max_vol > 0 and section > 0:
            speed = min(speed, max_vol / section)
        feed = _feed(speed)
        self.last_speed = speed
        # E par segment avec contrôle de dérive (cumul exact arrondi à 5 décimales, différences émises).
        if pts.shape[0] == 2:
            # Segment unique : calcul scalaire, même arithmétique que la voie vectorisée (round(x·1e5)/1e5 ≡ np.round).
            x1, y1 = int(pts[1][0]), int(pts[1][1])
            length_mm = math.hypot(x1 - int(pts[0][0]), y1 - int(pts[0][1])) / SCALE
            e_exact = self.e_exact + length_mm * e_per_mm
            e_round = round(e_exact * 100000.0) / 100000.0
            out.append(f"G1 X{_mm(x1)} Y{_mm(y1)} E{e_round - self.e_emitted:.5f}" + (f" F{feed}" if feed != self.feed else ""))
            self.feed = feed
            self.e_exact = e_exact
            self.e_emitted = e_round
            total_length = length_mm
            seconds = estimate.axis_move_time_s(length_mm, speed, accel)
        else:
            deltas = np.diff(pts, axis=0).astype(np.float64)
            lengths = np.hypot(deltas[:, 0], deltas[:, 1]) / SCALE
            e_cum = self.e_exact + np.cumsum(lengths * e_per_mm)
            e_round = np.round(e_cum, 5)
            e_rel = np.diff(np.concatenate(([self.e_emitted], e_round)))
            xs = pts[1:, 0].tolist()
            ys = pts[1:, 1].tolist()
            es = e_rel.tolist()
            first = f"G1 X{_mm(xs[0])} Y{_mm(ys[0])} E{es[0]:.5f}"
            out.append(first + (f" F{feed}" if feed != self.feed else ""))
            self.feed = feed
            out.extend([f"G1 X{_mm(x)} Y{_mm(y)} E{e:.5f}" for x, y, e in zip(xs[1:], ys[1:], es[1:])])
            self.e_exact = float(e_cum[-1])
            self.e_emitted = float(e_round[-1])
            total_length = float(np.sum(lengths))
            seconds = estimate.plan_polyline(pts.astype(np.float64) / SCALE, speed, accel,
                                             min(float(self.settings["default_jerk"]), self.machine.max_jerk.x))
        self.est.add_filament(self.extruder, total_length * e_per_mm)
        self.est.add("extrude", seconds, layer_index, move.feature)
        self.pos = (int(pts[-1][0]), int(pts[-1][1]))
        self.last_extrusion = pts

    # --- finalisation ----------------------------------------------------------------------------------------------------
    def _resolve(self, start_lines: list, total_s: float) -> list:
        """Remplace les marqueurs par la séquence de démarrage et les lignes M73 (P monotone, R décroissant)."""
        body: list = []
        last_percent = -1
        start_s = self.est.start_s
        for item in self.out:
            if isinstance(item, str):
                body.append(item)
            elif isinstance(item, _StartBlock):
                body.extend(start_lines)
            else:
                elapsed = start_s + item.seconds
                percent = int(math.floor(100.0 * elapsed / total_s)) if total_s > 0 else 100
                percent = max(0, min(100, percent))
                if item.forced or percent > last_percent:
                    remaining = max(0, int(math.ceil((total_s - elapsed) / 60.0)))
                    body.append(f"M73 P{percent} R{remaining}")
                    last_percent = max(last_percent, percent)
        return body

    def _header(self, layers: int, total_s: float, filaments: list, ctx: PrintContext) -> list:
        ids = ",".join(str(i) for i in ctx.object_ids) if ctx.object_ids else "1"
        lines = [HEADER_START,
                 f"; generated by Alpine Model Engine {VERSION} on {self.generated_at}",
                 f"; model printing time: {estimate.format_duration(self.est.model_s)}; total estimated time: {estimate.format_duration(total_s)}",
                 f"; total layer number: {layers}",
                 f"; model label id: {ids}",
                 f"; filament_density: {','.join(_num(float(self._f('filament_density', f.extruder)), 3) for f in filaments)}",
                 f"; filament_diameter: {','.join(_num(float(self._f('filament_diameter', f.extruder)), 3) for f in filaments)}",
                 f"; max_z_height: {_mm(self.max_z_um)}",
                 f"; filament: {','.join(str(f.extruder) for f in filaments)}",
                 HEADER_END]
        return lines

    def _config(self, total_s: float, filaments: list, ctx: PrintContext) -> list:
        s = self.settings
        m = self.machine
        first = filaments[0].extruder if filaments else 1
        values = {
            "printer_model": s["printer_model"] or m.model,
            "printer_model_id": m.model_id,
            "printer_settings_id": s["printer_settings_id"] or m.name,
            "printer_variant": _num(m.nozzle_mm, 2),
            "print_settings_id": s["print_settings_id"],
            "filament_settings_id": [self._f("filament_settings_id", f.extruder) for f in filaments],
            "filament_type": [self._f("filament_type", f.extruder) for f in filaments],
            "filament_colour": [self._f("filament_colour", f.extruder) for f in filaments],
            "filament_vendor": [self._f("filament_vendor", f.extruder) for f in filaments],
            "filament_ids": [self._f("tray_info_idx", f.extruder) or "" for f in filaments],
            "filament_diameter": [float(self._f("filament_diameter", f.extruder)) for f in filaments],
            "filament_density": [float(self._f("filament_density", f.extruder)) for f in filaments],
            "filament_cost": [float(self._f("filament_cost", f.extruder)) for f in filaments],
            "filament_flow_ratio": [float(self._f("filament_flow_ratio", f.extruder)) for f in filaments],
            "nozzle_diameter": float(m.nozzle_mm),
            "nozzle_type": m.nozzle_type,
            "layer_height": float(s["layer_height"]),
            "initial_layer_print_height": float(s["initial_layer_print_height"] or ctx.first_layer_height_mm),
            "sparse_infill_density": s["sparse_infill_density"],
            "nozzle_temperature": [self._nozzle_temp(f.extruder, False) for f in filaments],
            "nozzle_temperature_initial_layer": [self._nozzle_temp(f.extruder, True) for f in filaments],
            "bed_temperature": self._bed_temp(False),
            "bed_temperature_initial_layer": self._bed_temp(True),
            "hot_plate_temp_initial_layer": self._bed_temp(True),
            "curr_bed_type": ctx.bed_type,
            "printable_area": [f"{_num(x, 1)}x{_num(y, 1)}" for x, y in m.bed.printable_area()],
            "printable_height": float(m.bed.height_mm),
            "bed_exclude_area": [f"{_num(x, 1)}x{_num(y, 1)}" for x, y in m.bed.exclude_area()],
            "gcode_flavor": m.gcode_flavor,
            "use_ams": bool(ctx.use_ams),
            "bed_levelling": bool(ctx.bed_levelling),
            "timelapse": bool(ctx.timelapse),
            "first_extruder": first,
            "model_printing_time": estimate.format_duration(self.est.model_s),
            "total_estimated_time": estimate.format_duration(total_s),
            "model_printing_time_s": int(round(self.est.model_s)),
            "total_estimated_time_s": int(round(total_s)),
            "first_layer_time_s": int(round(self.est.first_layer_s())),
            "total_filament_used_g": round(sum(f.g for f in filaments), 2),
            "filament_used_mm": round(sum(f.mm for f in filaments), 2),
            "filament_used_cm3": round(sum(f.cm3 for f in filaments), 3),
            "total_filament_cost": round(sum(f.cost for f in filaments), 2),
            "engine_version": VERSION,
            "generated_by": "Alpine Model Engine",
        }
        for key in FILAMENT_KEYS:
            if key not in values:
                values[key] = [self._f(key, f.extruder) for f in filaments]
        reserved = set(values) | {"pause_print_layers"}
        for key in sorted(s):
            if key not in reserved:
                values[key] = s[key]
        lines = [CONFIG_START]
        for key, value in values.items():
            text = _config_value(value)
            key = str(key)
            if text is not None and _CONFIG_KEY_RE.match(key) and key.isprintable():
                lines.append(f"; {key} = {text}")
        lines.append(CONFIG_END)
        return lines

    def _footer(self, layers: int, total_s: float, filaments: list) -> list:
        return [
            f"; filament used [mm] = {', '.join(_num(f.mm, 2) for f in filaments)}",
            f"; filament used [cm3] = {', '.join(_num(f.cm3, 2) for f in filaments)}",
            f"; filament used [g] = {', '.join(_num(f.g, 2) for f in filaments)}",
            f"; filament cost = {', '.join(_num(f.cost, 2) for f in filaments)}",
            f"; total filament used [g] = {_num(sum(f.g for f in filaments), 2)}",
            f"; total filament cost = {_num(sum(f.cost for f in filaments), 2)}",
            f"; total layers count = {layers}",
            f"; estimated printing time (normal mode) = {estimate.format_duration(total_s)}",
        ]


def layers_first_extruder(layers: list) -> int:
    for layer in layers:
        for move in layer.moves:
            if move.kind == "extrude":
                return int(move.extruder)
    return 1


def emit_gcode(layers: list, machine: Machine, settings: dict | None = None, filaments: list | None = None,
               context: PrintContext | None = None, generated_at: str | None = None) -> GcodeResult:
    """Raccourci : ``Emitter(machine, filaments, settings, context, generated_at).emit(layers)``."""
    return Emitter(machine, filaments, settings, context, generated_at).emit(layers)
