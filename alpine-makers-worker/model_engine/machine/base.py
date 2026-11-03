"""Socle des machines du trancheur Alpine Model Engine (docs/MODEL_ENGINE_ARCHITECTURE.md §6.3, §7).

Fiche machine, plateau, limites, contexte d'impression et séquences G-code abstraites. Tout est écrit par
Alpine Makers : aucune valeur ni texte ne provient d'un profil ou d'un template d'un autre trancheur.
Unités : millimètres, secondes, degrés Celsius (les micromètres entiers restent dans gcode.py).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

# Bornes de sécurité absolues (spécification §7) : aucune séquence ne les dépasse, quelle que soit la machine.
NOZZLE_TEMP_MAX_C = 300
BED_TEMP_MAX_C = 110
FAN_PWM_MAX = 255


class PrinterUnsupportedError(ValueError):
    """Imprimante connue mais non tranchable en V1 (ou inconnue). Code d'erreur stable pour la CLI."""

    code = "PRINTER_UNSUPPORTED"


@dataclass(frozen=True)
class Bed:
    """Plateau : même signature que ``scene.Bed`` (largeur X, profondeur Y, hauteur Z, zone exclue, origine)."""

    width_mm: float
    depth_mm: float
    height_mm: float
    exclude_mm: tuple | None = None  # (x0, y0, x1, y1) en mm, None = aucune zone exclue
    origin: str = "corner"

    def contains_xy(self, x_mm: float, y_mm: float, margin_mm: float = 0.0) -> bool:
        """Point dans le rectangle imprimable (marge positive = rétrécit la zone autorisée)."""
        return (margin_mm <= x_mm <= self.width_mm - margin_mm) and (margin_mm <= y_mm <= self.depth_mm - margin_mm)

    def in_exclusion(self, x_mm: float, y_mm: float) -> bool:
        """Point dans la zone exclue (bords inclus)."""
        if not self.exclude_mm:
            return False
        x0, y0, x1, y1 = self.exclude_mm
        return x0 <= x_mm <= x1 and y0 <= y_mm <= y1

    def printable_area(self) -> list[tuple[float, float]]:
        """Quatre coins du plateau, sens trigonométrique, pour ``printable_area`` du fichier tranché."""
        return [(0.0, 0.0), (self.width_mm, 0.0), (self.width_mm, self.depth_mm), (0.0, self.depth_mm)]

    def exclude_area(self) -> list[tuple[float, float]]:
        """Quatre coins de la zone exclue (liste vide si aucune)."""
        if not self.exclude_mm:
            return []
        x0, y0, x1, y1 = self.exclude_mm
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


@dataclass(frozen=True)
class AxisLimits:
    """Limite par axe (mm/s pour les vitesses, mm/s² pour les accélérations, mm/s pour le jerk)."""

    x: float
    y: float
    z: float
    e: float


@dataclass(frozen=True)
class Retraction:
    """Rétraction par défaut de la machine (le réglage utilisateur du filament prime dans gcode.py)."""

    length_mm: float = 0.8
    speed_mm_s: float = 30.0
    deretraction_speed_mm_s: float = 30.0
    min_travel_mm: float = 2.0
    z_hop_mm: float = 0.4
    wipe: bool = True
    wipe_distance_mm: float = 2.0


@dataclass(frozen=True)
class Fans:
    """Ventilateurs présents : pièce (M106 P1), auxiliaire (M106 P2), chambre/extraction (M106 P3)."""

    part: bool = True
    aux: bool = False
    chamber: bool = False


@dataclass
class PrintContext:
    """Contexte d'une impression transmis aux séquences machine. Rempli par gcode.Emitter à partir des réglages ;
    les champs d'options (AMS, nivellement, timelapse, plaque, objets) viennent de la demande du client."""

    # Options de la demande d'impression.
    use_ams: bool = False
    ams_slots: dict = field(default_factory=dict)  # extrudeur (1..16) -> emplacement AMS (0..15)
    bed_levelling: bool = True
    timelapse: bool = False
    vibration_cali: bool = True
    flow_cali: bool = False
    bed_type: str = "textured_plate"  # cool_plate | hot_plate | textured_plate | eng_plate
    first_layer_box_mm: tuple | None = None  # (x0, y0, x1, y1) emprise de la première couche
    object_ids: dict = field(default_factory=dict)  # identifiant d'objet -> nom affiché
    label_objects: bool = True
    # Températures et filament (déjà bornées par la machine à l'émission).
    nozzle_temperature_initial_layer: int = 220
    nozzle_temperature: int = 220
    bed_temperature_initial_layer: int = 55
    bed_temperature: int = 55
    nozzle_standby_temperature: int = 140
    filament_type: str = "PLA"
    filament_diameter_mm: float = 1.75
    flow_ratio: float = 1.0
    retraction_length_mm: float = 0.8
    retraction_speed_mm_s: float = 30.0
    travel_speed_mm_s: float = 250.0
    z_speed_mm_s: float = 12.0
    default_acceleration: float = 10000.0
    # État courant (mis à jour par l'émetteur avant chaque séquence).
    extruder: int = 1
    previous_extruder: int = 1
    next_extruder: int = 1
    estimated_minutes: int = 0
    total_layers: int = 0
    layer_index: int = 0  # 1 = première couche
    z_mm: float = 0.0
    layer_height_mm: float = 0.2
    first_layer_height_mm: float = 0.2
    retracted: bool = False
    purge_line: bool = True

    def ams_slot(self, extruder: int | None = None) -> int:
        """Emplacement AMS de l'extrudeur (défaut : extrudeur n -> emplacement n − 1)."""
        ext = int(extruder or self.extruder)
        slot = self.ams_slots.get(ext, self.ams_slots.get(str(ext), ext - 1))
        return max(0, min(15, int(slot)))


@dataclass
class GcodeSequence:
    """Résultat d'une séquence machine : lignes G-code, état connu à la fin, filament consommé, durée estimée."""

    lines: list[str] = field(default_factory=list)
    position_mm: tuple | None = None  # (x, y, z) connue à la fin, None = inconnue (après G28 par exemple)
    z_mm: float | None = None  # hauteur connue à la fin quand XY est inconnue (dégagement d'un changement de filament)
    retracted: bool = False
    extruded_mm: float = 0.0
    seconds: float = 0.0

    def text(self) -> str:
        return "\n".join(self.lines)


def layer_height_bounds(nozzle_mm: float) -> tuple[float, float]:
    """Plage de hauteur de couche admise pour une buse : de 20 % (≥ 0,06 mm) à 75 % du diamètre."""
    nozzle = float(nozzle_mm)
    return (round(max(0.06, 0.2 * nozzle), 3), round(0.75 * nozzle, 3))


@dataclass
class Machine:
    """Fiche machine commune. Les sous-classes (machine/bambu.py) écrivent les séquences G-code."""

    id: str
    model_id: str
    name: str  # nom du préréglage, valeur du champ ``printer_profile`` des clients
    vendor: str
    model: str  # nom affiché du modèle, ex. « Bambu Lab P1S »
    bed: Bed
    nozzle_mm: float
    nozzle_type: str = "stainless_steel"
    max_speed: AxisLimits = AxisLimits(500.0, 500.0, 20.0, 30.0)
    max_accel: AxisLimits = AxisLimits(20000.0, 20000.0, 500.0, 5000.0)
    max_jerk: AxisLimits = AxisLimits(9.0, 9.0, 3.0, 2.5)
    retraction: Retraction = Retraction()
    fans: Fans = Fans()
    max_nozzle_temp_c: int = NOZZLE_TEMP_MAX_C
    max_bed_temp_c: int = 100
    supports_ams: bool = True
    gcode_flavor: str = "marlin"
    bed_heat_rate_c_s: float = 0.8  # estimation de chauffe (°C/s), valeur Alpine
    nozzle_heat_rate_c_s: float = 5.0
    sliceable: bool = True
    unsupported_reason: str = ""

    # --- bornes -------------------------------------------------------------------------------------
    @property
    def min_layer_height_mm(self) -> float:
        return layer_height_bounds(self.nozzle_mm)[0]

    @property
    def max_layer_height_mm(self) -> float:
        return layer_height_bounds(self.nozzle_mm)[1]

    def clamp_nozzle_temp(self, celsius: float) -> int:
        """Température de buse bornée à la machine et au plafond absolu (jamais négative)."""
        return int(max(0, min(float(celsius), self.max_nozzle_temp_c, NOZZLE_TEMP_MAX_C)))

    def clamp_bed_temp(self, celsius: float) -> int:
        return int(max(0, min(float(celsius), self.max_bed_temp_c, BED_TEMP_MAX_C)))

    def clamp_z(self, z_mm: float) -> float:
        """Hauteur bornée entre 0 et la hauteur imprimable."""
        return max(0.0, min(float(z_mm), self.bed.height_mm))

    def validate_layer_height(self, height_mm: float) -> None:
        lo, hi = layer_height_bounds(self.nozzle_mm)
        if not (lo - 1e-9 <= float(height_mm) <= hi + 1e-9):
            raise ValueError(f"Hauteur de couche {height_mm} mm hors plage {lo}–{hi} mm pour une buse de {self.nozzle_mm} mm.")

    def ensure_sliceable(self) -> None:
        if not self.sliceable:
            raise PrinterUnsupportedError(self.unsupported_reason or f"{self.model} : tranchage non pris en charge.")

    # --- séquences (à écrire par la sous-classe) -----------------------------------------------------
    def start_gcode(self, ctx: PrintContext) -> GcodeSequence:
        raise NotImplementedError("séquence de démarrage non définie pour cette machine")

    def end_gcode(self, ctx: PrintContext) -> GcodeSequence:
        raise NotImplementedError("séquence de fin non définie pour cette machine")

    def layer_change_gcode(self, ctx: PrintContext) -> GcodeSequence:
        raise NotImplementedError("séquence de changement de couche non définie pour cette machine")

    def filament_change_gcode(self, ctx: PrintContext) -> GcodeSequence:
        raise NotImplementedError("séquence de changement de filament non définie pour cette machine")

    def pause_gcode(self, ctx: PrintContext | None = None) -> GcodeSequence:
        raise NotImplementedError("séquence de pause non définie pour cette machine")

    def to_dict(self) -> dict:
        """Fiche sérialisable (catalogue, fichier tranché)."""
        data = dataclasses.asdict(self)
        data["min_layer_height_mm"] = self.min_layer_height_mm
        data["max_layer_height_mm"] = self.max_layer_height_mm
        return data
