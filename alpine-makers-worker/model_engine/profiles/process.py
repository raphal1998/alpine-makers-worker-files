"""Schéma Alpine des réglages de tranchage et préréglages par buse × hauteur de couche (spécification §6.3).

``FIELDS`` décrit chaque réglage (clé, libellé et aide en français, type, bornes, pas, défaut, catégorie, groupe).
Les 25 clés du contrat client de ``POST /api/workspace/slice`` existent sous le même nom et avec les mêmes
énumérations ; les autres clés sont nommées par Alpine Makers. ``resolve`` valide, borne et complète une demande
(réglages + ``process_settings`` + buse + matériau + plaque) en un dictionnaire plat typé ; ``schema_for_api``
sert ``/api/model-studio/process-schema``.

Pure Python (pas de numpy) : le site importe ce module pour afficher le schéma. Aucune valeur ne provient d'un
profil OrcaSlicer / Bambu Studio : les défauts sont des choix Alpine (volume public de la P1S, pratique courante).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .filament import (PLATES, PLATE_SHORT, PLATE_TEMP_KEYS, MaterialUnknownError, normalise_plate,
                       plate_temperatures, resolve_filament)

CATEGORIES = ("Qualité", "Résistance", "Vitesse", "Supports", "Adhérence", "Multi-matériau", "Avancé")
TYPES = ("float", "int", "bool", "enum", "percent", "string")
NOZZLES = ("0.2", "0.4", "0.6", "0.8")
NOZZLE_DEFAULT = "0.4"
BED_TYPES = PLATES
PRINTER_PROFILE_DEFAULT = "Bambu Lab P1S 0.4 nozzle"
LINE_WIDTH_FACTOR = 1.05           # largeur de ligne ≈ 1,05 × buse
LINE_WIDTH_RANGE = (0.6, 2.0)      # largeur admise, en multiples du diamètre de buse

# Les 25 clés du contrat client (app.py ORCA_SLICE_SETTING_KEYS) : mêmes noms, mêmes énumérations.
CONTRACT_KEYS = (
    "layer_height", "outer_wall_line_width", "wall_loops", "top_shell_layers", "bottom_shell_layers",
    "sparse_infill_density", "sparse_infill_pattern", "enable_support", "support_threshold_angle", "support_type",
    "brim_type", "brim_width", "outer_wall_speed", "sparse_infill_speed", "travel_speed", "orient", "arrange",
    "ironing", "material", "nozzle", "seam_position", "fuzzy_skin", "prime_tower", "bed_type", "printer_profile",
)
SUPPORT_TYPES = ("normal(auto)", "tree(auto)", "normal(manual)", "tree(manual)")
BRIM_TYPES = ("no_brim", "outer_only", "inner_only", "outer_and_inner", "auto_brim", "brim_ears")
SEAM_POSITIONS = ("aligned", "nearest", "random", "back", "rear")
INFILL_PATTERNS = ("gyroid", "grid", "cubic", "honeycomb", "rectilinear", "triangles", "lightning", "adaptive cubic",
                   "aligned_rectilinear")
SOLID_PATTERNS = ("monotonic", "rectilinear", "concentric", "aligned_rectilinear")

# Clés d'autres trancheurs ou anciennes clés du client acceptées en entrée, traduites vers la clé Alpine.
ALIASES = {
    "enable_prime_tower": "prime_tower",
    "reduce_crossing_wall": "avoid_crossing_perimeters",
    "elephant_foot_compensation": "elephant_foot_compensation_mm",
    "elefant_foot_compensation": "elephant_foot_compensation_mm",
    "support_material": "enable_support",
    "support_material_threshold": "support_threshold_angle",
    "fill_density": "sparse_infill_density",
    "fill_pattern": "sparse_infill_pattern",
    "perimeters": "wall_loops",
    "top_solid_layers": "top_shell_layers",
    "bottom_solid_layers": "bottom_shell_layers",
    "first_layer_height": "initial_layer_print_height",
    "first_layer_speed": "initial_layer_speed",
    "gcode_label_objects": "label_objects",
    "exclude_object": "label_objects",
    "wall_infill_order": "wall_sequence",
    "temperature": "nozzle_temperature",
    "first_layer_temperature": "nozzle_temperature_initial_layer",
    "curr_bed_type": "bed_type",
    "filament_type": "material",
    "nozzle_diameter": "nozzle",
    "printer_settings_id": "printer_profile",
    "slow_down_layer_time_threshold": "slow_down_layer_time",
    "additional_cooling_fan": "additional_cooling_fan_speed",
}


class SettingsError(ValueError):
    """Réglage invalide (code d'erreur stable ``SETTINGS_INVALID``) ; ``key`` = clé fautive."""

    code = "SETTINGS_INVALID"

    def __init__(self, key, detail):
        self.key = str(key)
        self.detail = str(detail)
        super().__init__(f"Réglage « {self.key} » : {self.detail}")


@dataclass(frozen=True)
class Field:
    """Un réglage du schéma Alpine."""

    key: str
    label: str
    type: str
    default: object
    category: str
    group: str = ""
    min: float | None = None
    max: float | None = None
    step: float | None = None
    options: tuple = ()
    unit: str = ""
    help: str = ""
    filament: str = ""     # clé de la fiche filament qui fournit le défaut (vide = défaut du schéma)
    reserved: bool = False  # accepté et conservé, sans effet en V1

    def is_number(self) -> bool:
        return self.type in ("float", "int", "percent")


def _f(key, label, type_, default, category, group="", *, min=None, max=None, step=None, options=(), unit="",
       help="", filament="", reserved=False):
    if type_ not in TYPES:
        raise ValueError(f"type inconnu pour {key}")
    if category not in CATEGORIES:
        raise ValueError(f"catégorie inconnue pour {key}")
    if type_ == "enum" and default not in options:
        raise ValueError(f"défaut hors énumération pour {key}")
    if type_ in ("float", "int", "percent"):
        if min is None or max is None:
            raise ValueError(f"bornes manquantes pour {key}")
        if step is None:
            step = 1 if type_ != "float" else 0.01
        if not (min <= default <= max):
            raise ValueError(f"défaut hors bornes pour {key}")
    if type_ == "percent":
        unit = unit or "%"
    return Field(key, label, type_, default, category, group, min, max, step, tuple(options), unit, help, filament,
                 reserved)


Q, R, V, S, A, M, X = CATEGORIES

FIELDS: tuple[Field, ...] = (
    # ------------------------------------------------------------------------------------------ Qualité
    _f("layer_height", "Hauteur de couche", "float", 0.20, Q, "Couches", min=0.04, max=0.64, step=0.01, unit="mm",
       help="Épaisseur de chaque couche : plus fine = plus de détails, plus longue. Bornée par la buse (20–75 % du diamètre)."),
    _f("initial_layer_print_height", "Hauteur de la première couche", "float", 0.20, Q, "Couches", min=0.04, max=0.64,
       step=0.01, unit="mm", help="Première couche souvent un peu plus épaisse pour compenser les défauts du plateau."),
    _f("resolution", "Résolution", "float", 0.012, Q, "Couches", min=0.001, max=0.1, step=0.001, unit="mm",
       help="Tolérance de simplification des contours (Douglas–Peucker). 0,012 mm par défaut."),
    _f("slice_gap_closing_radius", "Fermeture des trous de coupe", "float", 0.10, Q, "Couches", min=0.0, max=1.0, step=0.01,
       unit="mm", help="Distance jusqu'à laquelle une boucle de coupe ouverte est refermée (maillages imparfaits)."),
    _f("adaptive_layer_height", "Hauteur de couche adaptative", "bool", False, Q, "Couches", reserved=True,
       help="Couches plus fines sur les pentes douces. Réservé : sans effet en V1."),
    _f("line_width", "Largeur de ligne par défaut", "float", 0.42, Q, "Largeurs de ligne", min=0.1, max=2.0, step=0.01,
       unit="mm", help="Largeur utilisée quand une largeur spécifique vaut 0. Environ 1,05 × le diamètre de buse."),
    _f("outer_wall_line_width", "Largeur du mur extérieur", "float", 0.42, Q, "Largeurs de ligne", min=0.1, max=2.0,
       step=0.01, unit="mm", help="Largeur extrudée du mur visible."),
    _f("inner_wall_line_width", "Largeur des murs intérieurs", "float", 0.45, Q, "Largeurs de ligne", min=0.1, max=2.0,
       step=0.01, unit="mm", help="Un peu plus large que le mur extérieur pour la solidité."),
    _f("top_surface_line_width", "Largeur des surfaces supérieures", "float", 0.42, Q, "Largeurs de ligne", min=0.1,
       max=2.0, step=0.01, unit="mm", help="Largeur des lignes du dessus (fini de surface)."),
    _f("sparse_infill_line_width", "Largeur du remplissage", "float", 0.45, Q, "Largeurs de ligne", min=0.1, max=2.0,
       step=0.01, unit="mm", help="Largeur des lignes de remplissage creux."),
    _f("internal_solid_infill_line_width", "Largeur du remplissage plein interne", "float", 0.42, Q, "Largeurs de ligne",
       min=0.1, max=2.0, step=0.01, unit="mm", help="Largeur des zones pleines internes."),
    _f("initial_layer_line_width", "Largeur de la première couche", "float", 0.50, Q, "Largeurs de ligne", min=0.1,
       max=2.0, step=0.01, unit="mm", help="Lignes plus larges en première couche : meilleure adhérence."),
    _f("support_line_width", "Largeur des supports", "float", 0.42, Q, "Largeurs de ligne", min=0.1, max=2.0, step=0.01,
       unit="mm", help="Largeur des lignes de support."),
    _f("seam_position", "Position de la couture", "enum", "aligned", Q, "Couture", options=SEAM_POSITIONS,
       help="Où commencent les boucles de mur : alignée verticalement, au plus près, aléatoire ou à l'arrière (back/rear)."),
    _f("seam_gap", "Écart de couture", "percent", 10, Q, "Couture", min=0, max=100, step=1,
       help="Raccourcissement de la fin de boucle, en % de la largeur de ligne, pour éviter la bosse de couture."),
    _f("wipe_on_loops", "Essuyage sur les boucles", "bool", False, Q, "Couture",
       help="Petit mouvement d'essuyage vers l'intérieur à la fin de chaque boucle de mur."),
    _f("staggered_inner_seams", "Coutures intérieures décalées", "bool", False, Q, "Couture",
       help="Décale la couture des murs intérieurs par rapport au mur extérieur."),
    _f("elephant_foot_compensation_mm", "Compensation du pied d'éléphant", "float", 0.15, Q, "Précision", min=0.0,
       max=1.0, step=0.01, unit="mm", help="Rétrécit la première couche pour compenser l'écrasement sur le plateau."),
    _f("xy_hole_compensation", "Compensation des trous (XY)", "float", 0.0, Q, "Précision", min=-1.0, max=1.0, step=0.01,
       unit="mm", help="Agrandit (positif) ou réduit les trous pour corriger les cotes."),
    _f("xy_contour_compensation", "Compensation des contours (XY)", "float", 0.0, Q, "Précision", min=-1.0, max=1.0,
       step=0.01, unit="mm", help="Dilate (positif) ou rétrécit le contour extérieur."),
    _f("precise_outer_wall", "Mur extérieur précis", "bool", False, Q, "Précision",
       help="Réduit le recouvrement entre mur extérieur et intérieur pour des cotes plus justes."),
    _f("only_one_wall_top", "Un seul mur sur le dessus", "bool", False, Q, "Précision",
       help="Un seul mur sur les couches supérieures pour laisser plus de place au repassage de surface."),
    _f("only_one_wall_first_layer", "Un seul mur en première couche", "bool", False, Q, "Précision",
       help="Un seul mur en première couche (surfaces larges, pied d'éléphant)."),
    _f("detect_thin_wall", "Détecter les parois fines", "bool", True, Q, "Précision",
       help="Les régions plus étroites que deux largeurs de ligne deviennent un trait médian."),
    _f("detect_overhang_wall", "Détecter les murs en surplomb", "bool", True, Q, "Précision",
       help="Classe les murs non soutenus (0–25, 25–50, 50–75, 75–100 %) pour ralentir et ventiler."),
    _f("spiral_mode", "Mode vase (spirale)", "bool", False, Q, "Précision", reserved=True,
       help="Un seul mur en spirale continue, sans remplissage ni dessus. Réservé : sans effet en V1."),
    _f("arc_fitting", "Arcs (G2/G3)", "bool", False, Q, "Précision", reserved=True,
       help="Remplace les segments par des arcs. Réservé : G1 seulement en V1."),
    _f("ironing", "Repassage", "bool", False, Q, "Repassage",
       help="Lisse les surfaces supérieures en repassant la buse chaude avec un débit réduit."),
    _f("ironing_type", "Surfaces repassées", "enum", "top", Q, "Repassage", options=("top", "topmost", "all"),
       help="Toutes les surfaces supérieures, seulement la plus haute, ou toutes les surfaces pleines."),
    _f("ironing_pattern", "Motif de repassage", "enum", "rectilinear", Q, "Repassage", options=("rectilinear", "concentric"),
       help="Lignes droites ou concentriques."),
    _f("ironing_flow", "Débit de repassage", "percent", 10, Q, "Repassage", min=0, max=100, step=1,
       help="Débit du repassage en % d'une ligne normale."),
    _f("ironing_spacing", "Espacement de repassage", "float", 0.15, Q, "Repassage", min=0.05, max=1.0, step=0.01, unit="mm",
       help="Distance entre deux passes de repassage."),
    _f("ironing_speed", "Vitesse de repassage", "float", 30.0, Q, "Repassage", min=1, max=200, step=1, unit="mm/s",
       help="Vitesse des passes de repassage."),
    _f("fuzzy_skin", "Peau floue", "bool", False, Q, "Peau floue",
       help="Déplace aléatoirement les points du mur extérieur pour une texture rugueuse."),
    _f("fuzzy_skin_scope", "Murs concernés", "enum", "external", Q, "Peau floue", options=("external", "all"),
       help="Mur extérieur seulement, ou tous les murs."),
    _f("fuzzy_skin_thickness", "Épaisseur de la peau floue", "float", 0.30, Q, "Peau floue", min=0.05, max=1.0, step=0.01,
       unit="mm", help="Amplitude maximale du déplacement des points."),
    _f("fuzzy_skin_point_distance", "Distance des points", "float", 0.80, Q, "Peau floue", min=0.1, max=2.0, step=0.05,
       unit="mm", help="Distance moyenne entre deux points déplacés."),
    # ------------------------------------------------------------------------------------------ Résistance
    _f("wall_loops", "Nombre de murs", "int", 2, R, "Murs", min=1, max=20, step=1,
       help="Boucles de périmètre par couche. 2 suffisent en général, 3–4 pour une pièce résistante."),
    _f("wall_sequence", "Ordre des murs", "enum", "inner_outer", R, "Murs",
       options=("inner_outer", "outer_inner", "inner_outer_inner"),
       help="Intérieur puis extérieur (meilleur fini des surplombs), extérieur d'abord (cotes), ou intérieur/extérieur/intérieur."),
    _f("is_infill_first", "Remplissage avant les murs", "bool", False, R, "Murs",
       help="Imprime le remplissage avant les murs de la couche."),
    _f("wall_generator", "Générateur de murs", "enum", "classic", R, "Murs", options=("classic", "arachne"), reserved=True,
       help="Classique (largeur fixe) ou Arachne (largeur variable). Réservé : classique seulement en V1."),
    _f("ensure_vertical_shell_thickness", "Coque verticale garantie", "enum", "enabled", R, "Murs",
       options=("none", "partial", "enabled"),
       help="Ajoute des zones pleines pour que la coque garde son épaisseur sur les pentes."),
    _f("gap_fill", "Remplissage des interstices", "bool", True, R, "Murs",
       help="Trait médian dans les bandes trop étroites entre murs et remplissage."),
    _f("filter_out_gap_fill", "Ignorer les interstices courts", "float", 0.0, R, "Murs", min=0.0, max=10.0, step=0.1,
       unit="mm", help="Les interstices plus courts que cette longueur ne sont pas remplis (0 = tous)."),
    _f("top_shell_layers", "Couches supérieures", "int", 4, R, "Surfaces", min=0, max=30, step=1,
       help="Nombre de couches pleines sur le dessus."),
    _f("top_shell_thickness", "Épaisseur minimale du dessus", "float", 0.8, R, "Surfaces", min=0.0, max=10.0, step=0.1,
       unit="mm", help="Le nombre de couches est augmenté au besoin pour atteindre cette épaisseur (0 = ignoré)."),
    _f("bottom_shell_layers", "Couches inférieures", "int", 3, R, "Surfaces", min=0, max=30, step=1,
       help="Nombre de couches pleines sous la pièce."),
    _f("bottom_shell_thickness", "Épaisseur minimale du dessous", "float", 0.0, R, "Surfaces", min=0.0, max=10.0, step=0.1,
       unit="mm", help="Comme pour le dessus (0 = ignoré)."),
    _f("top_surface_pattern", "Motif du dessus", "enum", "monotonic", R, "Surfaces", options=SOLID_PATTERNS,
       help="Monotone = traits dans un ordre régulier, surface plus uniforme."),
    _f("bottom_surface_pattern", "Motif du dessous", "enum", "monotonic", R, "Surfaces", options=SOLID_PATTERNS,
       help="Motif de la première couche et des couches inférieures."),
    _f("internal_solid_infill_pattern", "Motif plein interne", "enum", "rectilinear", R, "Surfaces", options=SOLID_PATTERNS,
       help="Motif des zones pleines internes."),
    _f("top_solid_infill_flow_ratio", "Débit du dessus", "float", 1.0, R, "Surfaces", min=0.5, max=2.0, step=0.01,
       help="Facteur de débit des surfaces supérieures."),
    _f("bottom_solid_infill_flow_ratio", "Débit du dessous", "float", 1.0, R, "Surfaces", min=0.5, max=2.0, step=0.01,
       help="Facteur de débit de la première couche et du dessous."),
    _f("solid_infill_direction", "Direction des surfaces pleines", "float", 45.0, R, "Surfaces", min=0, max=360, step=1,
       unit="°", help="Angle des lignes pleines, alterné de 90° à chaque couche."),
    _f("sparse_infill_density", "Densité du remplissage", "percent", 15, R, "Remplissage", min=0, max=100, step=1,
       help="0 % = creux, 100 % = plein. 15 % convient à la plupart des pièces."),
    _f("sparse_infill_pattern", "Motif du remplissage", "enum", "gyroid", R, "Remplissage", options=INFILL_PATTERNS,
       help="Gyroïde (isotrope), grille, cubique, nid d'abeille, rectiligne, triangles, éclair, cubique adaptatif, rectiligne aligné."),
    _f("infill_direction", "Direction du remplissage", "float", 45.0, R, "Remplissage", min=0, max=360, step=1, unit="°",
       help="Angle de base des lignes de remplissage."),
    _f("infill_wall_overlap", "Chevauchement remplissage / murs", "percent", 15, R, "Remplissage", min=0, max=100, step=1,
       help="Recouvrement du remplissage sur le dernier mur, en % de la largeur de ligne."),
    _f("infill_combination", "Combiner les couches de remplissage", "bool", False, R, "Remplissage", reserved=True,
       help="Remplissage une couche sur deux, deux fois plus épais. Réservé : sans effet en V1."),
    _f("infill_anchor", "Ancrage du remplissage", "float", 0.4, R, "Remplissage", min=0.0, max=20.0, step=0.1, unit="mm",
       help="Longueur du raccord au mur au début d'une ligne de remplissage."),
    _f("infill_anchor_max", "Ancrage maximal", "float", 20.0, R, "Remplissage", min=0.0, max=100.0, step=0.5, unit="mm",
       help="Longueur maximale d'un raccord d'ancrage."),
    _f("minimum_sparse_infill_area", "Aire minimale du remplissage", "float", 15.0, R, "Remplissage", min=0.0, max=100.0,
       step=0.5, unit="mm²", help="Les zones creuses plus petites sont remplies en plein."),
    _f("bridge_flow", "Débit des ponts", "float", 1.0, R, "Ponts", min=0.5, max=2.0, step=0.01,
       help="Facteur de débit sur les ponts (lignes non soutenues)."),
    _f("bridge_angle", "Angle des ponts", "float", 0.0, R, "Ponts", min=0, max=360, step=1, unit="°",
       help="0 = angle automatique (longueur non soutenue minimale)."),
    _f("bridge_density", "Densité des ponts", "percent", 100, R, "Ponts", min=10, max=100, step=1,
       help="Densité des lignes de pont."),
    _f("thick_bridges", "Ponts épais", "bool", False, R, "Ponts",
       help="Ponts imprimés avec une section ronde plus épaisse."),
    _f("thick_internal_bridges", "Ponts internes épais", "bool", True, R, "Ponts",
       help="Ponts internes (sur remplissage creux) plus épais."),
    _f("max_bridge_length", "Longueur maximale de pont", "float", 10.0, R, "Ponts", min=0.0, max=1000.0, step=1, unit="mm",
       help="Au-delà, un support est généré sous le pont (si les supports sont actifs)."),
    # ------------------------------------------------------------------------------------------ Vitesse
    _f("outer_wall_speed", "Vitesse du mur extérieur", "float", 200.0, V, "Impression", min=1, max=500, step=1, unit="mm/s",
       help="Mur visible : plus lent = meilleur fini."),
    _f("inner_wall_speed", "Vitesse des murs intérieurs", "float", 300.0, V, "Impression", min=1, max=500, step=1, unit="mm/s",
       help="Murs cachés."),
    _f("small_perimeter_speed", "Vitesse des petits périmètres", "float", 100.0, V, "Impression", min=1, max=500, step=1,
       unit="mm/s", help="Boucles courtes (petits trous, piliers) ralenties pour refroidir."),
    _f("small_perimeter_threshold", "Seuil des petits périmètres", "float", 6.0, V, "Impression", min=0, max=100, step=0.5,
       unit="mm", help="Rayon en dessous duquel une boucle est « petite »."),
    _f("sparse_infill_speed", "Vitesse du remplissage", "float", 270.0, V, "Impression", min=1, max=500, step=1, unit="mm/s",
       help="Remplissage creux."),
    _f("internal_solid_infill_speed", "Vitesse du plein interne", "float", 250.0, V, "Impression", min=1, max=500, step=1,
       unit="mm/s", help="Zones pleines internes."),
    _f("top_surface_speed", "Vitesse des surfaces supérieures", "float", 200.0, V, "Impression", min=1, max=500, step=1,
       unit="mm/s", help="Dessus visible."),
    _f("gap_infill_speed", "Vitesse des interstices", "float", 250.0, V, "Impression", min=1, max=500, step=1, unit="mm/s",
       help="Traits médians des interstices."),
    _f("bridge_speed", "Vitesse des ponts", "float", 50.0, V, "Impression", min=1, max=500, step=1, unit="mm/s",
       help="Lignes non soutenues : lentement, ventilation au maximum."),
    _f("initial_layer_speed", "Vitesse de la première couche", "float", 50.0, V, "Impression", min=1, max=500, step=1,
       unit="mm/s", help="Murs et surfaces de la première couche."),
    _f("initial_layer_infill_speed", "Vitesse du remplissage en première couche", "float", 105.0, V, "Impression", min=1,
       max=500, step=1, unit="mm/s", help="Remplissage plein de la première couche."),
    _f("support_speed", "Vitesse des supports", "float", 150.0, V, "Impression", min=1, max=500, step=1, unit="mm/s",
       help="Colonnes de support."),
    _f("support_interface_speed", "Vitesse des interfaces de support", "float", 80.0, V, "Impression", min=1, max=500,
       step=1, unit="mm/s", help="Couches d'interface entre support et pièce."),
    _f("enable_overhang_speed", "Ralentir les surplombs", "bool", True, V, "Surplombs",
       help="Applique les vitesses par classe de surplomb ci-dessous."),
    _f("overhang_1_4_speed", "Surplomb 0–25 %", "float", 0.0, V, "Surplombs", min=0, max=500, step=1, unit="mm/s",
       help="0 = vitesse normale."),
    _f("overhang_2_4_speed", "Surplomb 25–50 %", "float", 50.0, V, "Surplombs", min=0, max=500, step=1, unit="mm/s",
       help="0 = vitesse normale."),
    _f("overhang_3_4_speed", "Surplomb 50–75 %", "float", 30.0, V, "Surplombs", min=0, max=500, step=1, unit="mm/s",
       help="0 = vitesse normale."),
    _f("overhang_4_4_speed", "Surplomb 75–100 %", "float", 10.0, V, "Surplombs", min=0, max=500, step=1, unit="mm/s",
       help="0 = vitesse normale."),
    _f("travel_speed", "Vitesse de déplacement", "float", 500.0, V, "Déplacements", min=1, max=500, step=1, unit="mm/s",
       help="Déplacements sans extrusion (500 mm/s maximum sur la P1S)."),
    _f("travel_speed_z", "Vitesse de déplacement en Z", "float", 0.0, V, "Déplacements", min=0, max=100, step=1,
       unit="mm/s", help="0 = limite de la machine."),
    _f("default_acceleration", "Accélération par défaut", "float", 10000.0, V, "Accélérations", min=100, max=20000, step=100,
       unit="mm/s²", help="Accélération des mouvements d'impression sans réglage dédié."),
    _f("outer_wall_acceleration", "Accélération du mur extérieur", "float", 5000.0, V, "Accélérations", min=100, max=20000,
       step=100, unit="mm/s²", help="Plus faible = moins d'ondulations sur le mur visible."),
    _f("inner_wall_acceleration", "Accélération des murs intérieurs", "float", 10000.0, V, "Accélérations", min=100,
       max=20000, step=100, unit="mm/s²", help="Murs cachés."),
    _f("sparse_infill_acceleration", "Accélération du remplissage", "float", 10000.0, V, "Accélérations", min=100,
       max=20000, step=100, unit="mm/s²", help="Remplissage creux."),
    _f("internal_solid_infill_acceleration", "Accélération du plein interne", "float", 10000.0, V, "Accélérations",
       min=100, max=20000, step=100, unit="mm/s²", help="Zones pleines internes."),
    _f("top_surface_acceleration", "Accélération des surfaces supérieures", "float", 2000.0, V, "Accélérations", min=100,
       max=20000, step=100, unit="mm/s²", help="Dessus visible."),
    _f("initial_layer_acceleration", "Accélération de la première couche", "float", 500.0, V, "Accélérations", min=100,
       max=20000, step=100, unit="mm/s²", help="Première couche : douce pour l'adhérence."),
    _f("travel_acceleration", "Accélération des déplacements", "float", 10000.0, V, "Accélérations", min=100, max=20000,
       step=100, unit="mm/s²", help="Déplacements sans extrusion."),
    _f("bridge_acceleration", "Accélération des ponts", "float", 5000.0, V, "Accélérations", min=100, max=20000, step=100,
       unit="mm/s²", help="Ponts."),
    _f("default_jerk", "Jerk par défaut", "float", 9.0, V, "Jerk", min=0, max=20, step=0.5, unit="mm/s",
       help="Changement de vitesse instantané toléré aux angles."),
    _f("outer_wall_jerk", "Jerk du mur extérieur", "float", 9.0, V, "Jerk", min=0, max=20, step=0.5, unit="mm/s",
       help="Jerk appliqué au mur visible."),
    _f("inner_wall_jerk", "Jerk des murs intérieurs", "float", 9.0, V, "Jerk", min=0, max=20, step=0.5, unit="mm/s",
       help="Jerk des murs cachés."),
    _f("infill_jerk", "Jerk du remplissage", "float", 9.0, V, "Jerk", min=0, max=20, step=0.5, unit="mm/s",
       help="Jerk du remplissage creux et plein."),
    _f("top_surface_jerk", "Jerk des surfaces supérieures", "float", 9.0, V, "Jerk", min=0, max=20, step=0.5, unit="mm/s",
       help="Jerk du dessus visible."),
    _f("initial_layer_jerk", "Jerk de la première couche", "float", 9.0, V, "Jerk", min=0, max=20, step=0.5, unit="mm/s",
       help="Jerk de la première couche."),
    _f("travel_jerk", "Jerk des déplacements", "float", 12.0, V, "Jerk", min=0, max=20, step=0.5, unit="mm/s",
       help="Jerk des déplacements sans extrusion."),
    _f("slow_down_for_layer_cooling", "Ralentir pour refroidir", "bool", True, V, "Refroidissement par couche",
       help="Ralentit les couches trop courtes pour qu'elles durent au moins le temps minimal."),
    _f("slow_down_layer_time", "Temps minimal par couche", "float", 6.0, V, "Refroidissement par couche", min=0, max=60,
       step=0.5, unit="s", filament="slow_down_layer_time",
       help="Durée minimale d'une couche (défaut de la fiche filament)."),
    _f("slow_down_min_speed", "Vitesse minimale de ralentissement", "float", 20.0, V, "Refroidissement par couche", min=1,
       max=100, step=1, unit="mm/s", filament="slow_down_min_speed", help="Plancher du ralentissement."),
    _f("fan_cooling_layer_time", "Temps de couche pour ventilation maximale", "float", 60.0, V, "Refroidissement par couche",
       min=0, max=200, step=1, unit="s", filament="fan_cooling_layer_time",
       help="En dessous de cette durée de couche, la ventilation monte progressivement vers le maximum."),
    # ------------------------------------------------------------------------------------------ Supports
    _f("enable_support", "Activer les supports", "bool", False, S, "Supports",
       help="Génère des supports sous les surplombs."),
    _f("support_type", "Type de supports", "enum", "normal(auto)", S, "Supports", options=SUPPORT_TYPES,
       help="Normaux ou arborescents ; auto = partout, manual = seulement sur les zones peintes."),
    _f("support_style", "Style des supports", "enum", "default", S, "Supports", options=("default", "grid", "snug"),
       help="Par défaut, grille (colonnes régulières) ou ajusté (au plus près de la pièce)."),
    _f("support_threshold_angle", "Seuil des surplombs", "int", 30, S, "Supports", min=0, max=90, step=1, unit="°",
       help="Angle à partir duquel une surface est supportée (0 = tout)."),
    _f("support_on_build_plate_only", "Supports depuis le plateau seulement", "bool", False, S, "Supports",
       help="Aucun support posé sur la pièce."),
    _f("support_critical_regions_only", "Zones critiques seulement", "bool", False, S, "Supports",
       help="Ne supporte que les zones qui ne tiendraient pas du tout (ponts longs, îlots)."),
    _f("support_remove_small_overhang", "Ignorer les petits surplombs", "bool", True, S, "Supports",
       help="Pas de support pour les surplombs plus petits qu'une largeur de ligne."),
    _f("support_base_pattern", "Motif des colonnes", "enum", "rectilinear", S, "Supports", options=("rectilinear", "grid"),
       help="Lignes ou grille."),
    _f("support_base_pattern_spacing", "Espacement des colonnes", "float", 2.5, S, "Supports", min=0.0, max=100.0, step=0.1,
       unit="mm", help="Distance entre deux lignes de support (le client envoie encore sa « densité » 0–100 ici)."),
    _f("support_interface_pattern", "Motif de l'interface", "enum", "rectilinear", S, "Supports",
       options=("rectilinear", "concentric"), help="Motif des couches d'interface."),
    _f("support_interface_top_layers", "Couches d'interface (dessus)", "int", 2, S, "Supports", min=0, max=10, step=1,
       help="Couches denses entre le support et la pièce."),
    _f("support_interface_bottom_layers", "Couches d'interface (dessous)", "int", 2, S, "Supports", min=0, max=10, step=1,
       help="Couches denses entre la pièce et un support posé dessus."),
    _f("support_interface_spacing", "Espacement de l'interface", "float", 0.5, S, "Supports", min=0.0, max=5.0, step=0.1,
       unit="mm", help="0 = interface pleine."),
    _f("support_top_z_distance", "Distance Z (dessus)", "float", 0.2, S, "Supports", min=0.0, max=1.0, step=0.05, unit="mm",
       help="Jeu entre le support et la pièce, arrondi à la couche."),
    _f("support_bottom_z_distance", "Distance Z (dessous)", "float", 0.2, S, "Supports", min=0.0, max=1.0, step=0.05,
       unit="mm", help="Jeu sous un support posé sur la pièce."),
    _f("support_object_xy_distance", "Distance XY à la pièce", "float", 0.35, S, "Supports", min=0.0, max=10.0, step=0.05,
       unit="mm", help="Écart horizontal entre support et pièce."),
    _f("support_angle", "Angle des lignes de support", "float", 0.0, S, "Supports", min=0, max=360, step=1, unit="°",
       help="Orientation des colonnes."),
    _f("support_expansion", "Expansion des supports", "float", 0.0, S, "Supports", min=-5.0, max=5.0, step=0.1, unit="mm",
       help="Élargit (positif) ou réduit l'emprise des supports."),
    _f("support_filament", "Filament des supports", "int", 0, S, "Supports", min=0, max=16, step=1,
       help="0 = filament de l'objet."),
    _f("support_interface_filament", "Filament des interfaces", "int", 0, S, "Supports", min=0, max=16, step=1,
       help="0 = filament de l'objet (PVA : filament soluble)."),
    _f("support_interface_not_for_body", "Pas d'interface sur la pièce", "bool", True, S, "Supports",
       help="Les supports posés sur la pièce n'ont pas d'interface dessous."),
    _f("bridge_no_support", "Pas de support sous les ponts", "bool", False, S, "Supports",
       help="Laisse les ponts se faire sans support."),
    _f("tree_support_branch_angle", "Angle des branches", "float", 40.0, S, "Arborescents", min=0, max=60, step=1, unit="°",
       help="Inclinaison maximale des branches des supports arborescents."),
    _f("tree_support_branch_distance", "Distance des branches", "float", 5.0, S, "Arborescents", min=1.0, max=20.0, step=0.5,
       unit="mm", help="Distance entre les pointes des branches."),
    _f("tree_support_branch_diameter", "Diamètre des branches", "float", 5.0, S, "Arborescents", min=1.0, max=20.0, step=0.5,
       unit="mm", help="Diamètre du tronc."),
    _f("tree_support_tip_diameter", "Diamètre des pointes", "float", 0.8, S, "Arborescents", min=0.5, max=5.0, step=0.1,
       unit="mm", help="Diamètre de l'extrémité qui touche la pièce."),
    _f("tree_support_wall_count", "Murs des branches", "int", 1, S, "Arborescents", min=0, max=10, step=1,
       help="Nombre de murs par branche (0 = automatique)."),
    # ------------------------------------------------------------------------------------------ Adhérence
    _f("brim_type", "Brim", "enum", "auto_brim", A, "Brim", options=BRIM_TYPES,
       help="Bord d'adhérence : aucun, extérieur, intérieur, les deux, automatique selon la pièce, ou oreilles."),
    _f("brim_width", "Largeur du brim", "float", 5.0, A, "Brim", min=0.0, max=100.0, step=0.5, unit="mm",
       help="Largeur du bord d'adhérence."),
    _f("brim_object_gap", "Écart brim / pièce", "float", 0.1, A, "Brim", min=0.0, max=2.0, step=0.05, unit="mm",
       help="Jeu entre le brim et la pièce pour le détacher facilement."),
    _f("brim_ears_max_angle", "Angle maximal des oreilles", "float", 125.0, A, "Brim", min=0, max=180, step=1, unit="°",
       help="Seuls les coins plus aigus reçoivent une oreille."),
    _f("brim_ears_detection_length", "Longueur de détection des oreilles", "float", 1.0, A, "Brim", min=0.0, max=100.0,
       step=0.5, unit="mm", help="Longueur de contour examinée pour détecter un coin."),
    _f("skirt_loops", "Boucles de jupe", "int", 0, A, "Jupe", min=0, max=10, step=1,
       help="Boucles autour des pièces pour amorcer la buse (0 = aucune : la P1S purge déjà)."),
    _f("skirt_distance", "Distance de la jupe", "float", 2.0, A, "Jupe", min=0.0, max=50.0, step=0.5, unit="mm",
       help="Écart entre la jupe et les pièces."),
    _f("skirt_height", "Hauteur de la jupe", "int", 1, A, "Jupe", min=1, max=100, step=1, unit="couches",
       help="Nombre de couches de la jupe."),
    _f("draft_shield", "Pare-courant d'air", "enum", "disabled", A, "Jupe", options=("disabled", "enabled"),
       help="Jupe montée jusqu'au sommet pour protéger des courants d'air (ABS)."),
    _f("raft_layers", "Couches de radeau", "int", 0, A, "Radeau", min=0, max=10, step=1, reserved=True,
       help="Socle sous la pièce. Réservé : sans effet en V1."),
    _f("raft_contact_distance", "Jeu radeau / pièce", "float", 0.1, A, "Radeau", min=0.0, max=1.0, step=0.05, unit="mm",
       reserved=True, help="Réservé : sans effet en V1."),
    _f("raft_first_layer_density", "Densité de la première couche du radeau", "percent", 90, A, "Radeau", min=10, max=100,
       step=1, reserved=True, help="Réservé : sans effet en V1."),
    _f("raft_first_layer_expansion", "Expansion du radeau", "float", 2.0, A, "Radeau", min=0.0, max=10.0, step=0.5,
       unit="mm", reserved=True, help="Réservé : sans effet en V1."),
    # ------------------------------------------------------------------------------------------ Multi-matériau
    _f("prime_tower", "Tour de purge", "bool", False, M, "Tour de purge",
       help="Tour où la buse purge à chaque changement de filament."),
    _f("prime_tower_width", "Largeur de la tour", "float", 35.0, M, "Tour de purge", min=2.0, max=100.0, step=1, unit="mm",
       help="Côté de la tour de purge."),
    _f("prime_tower_brim_width", "Brim de la tour", "float", 3.0, M, "Tour de purge", min=0.0, max=20.0, step=0.5, unit="mm",
       help="Bord d'adhérence de la tour."),
    _f("prime_volume", "Volume de purge", "float", 45.0, M, "Tour de purge", min=1.0, max=500.0, step=1, unit="mm³",
       help="Volume purgé à chaque changement de filament."),
    _f("wipe_tower_x", "Position X de la tour", "float", 165.0, M, "Tour de purge", min=0.0, max=256.0, step=1, unit="mm",
       help="Coin avant-gauche de la tour."),
    _f("wipe_tower_y", "Position Y de la tour", "float", 220.0, M, "Tour de purge", min=0.0, max=256.0, step=1, unit="mm",
       help="Coin avant-gauche de la tour."),
    _f("wipe_tower_rotation_angle", "Rotation de la tour", "float", 0.0, M, "Tour de purge", min=0, max=360, step=1, unit="°",
       help="Orientation de la tour."),
    _f("flush_multiplier", "Multiplicateur de purge", "float", 1.0, M, "Purge", min=0.0, max=5.0, step=0.05,
       help="Facteur appliqué aux volumes de purge."),
    _f("flush_into_infill", "Purger dans le remplissage", "bool", False, M, "Purge",
       help="Utilise le remplissage creux pour purger."),
    _f("flush_into_objects", "Purger dans les objets", "bool", False, M, "Purge",
       help="Purge dans les objets (mélange des couleurs à l'intérieur)."),
    _f("flush_into_support", "Purger dans les supports", "bool", True, M, "Purge",
       help="Utilise les supports pour purger."),
    _f("wall_filament", "Filament des murs", "int", 0, M, "Filaments par zone", min=0, max=16, step=1,
       help="0 = filament de l'objet."),
    _f("sparse_infill_filament", "Filament du remplissage", "int", 0, M, "Filaments par zone", min=0, max=16, step=1,
       help="0 = filament de l'objet."),
    _f("solid_infill_filament", "Filament des surfaces pleines", "int", 0, M, "Filaments par zone", min=0, max=16, step=1,
       help="0 = filament de l'objet."),
    _f("ooze_prevention", "Prévention du suintement", "bool", False, M, "Filaments par zone",
       help="Abaisse la température des filaments inactifs."),
    _f("standby_temperature_delta", "Baisse de température en attente", "int", -50, M, "Filaments par zone", min=-100,
       max=0, step=5, unit="°C", help="Écart appliqué aux filaments inactifs si la prévention du suintement est active."),
    # ------------------------------------------------------------------------------------------ Avancé
    _f("retraction_length", "Longueur de rétraction", "float", 0.8, X, "Rétraction", min=0.0, max=10.0, step=0.1, unit="mm",
       help="Filament retiré avant un déplacement (la fiche filament peut l'imposer : TPU)."),
    _f("retraction_speed", "Vitesse de rétraction", "float", 30.0, X, "Rétraction", min=1.0, max=100.0, step=1, unit="mm/s",
       help="Vitesse du retrait."),
    _f("deretraction_speed", "Vitesse de réamorçage", "float", 30.0, X, "Rétraction", min=0.0, max=100.0, step=1,
       unit="mm/s", help="0 = même vitesse que la rétraction."),
    _f("retraction_minimum_travel", "Déplacement minimal pour rétracter", "float", 2.0, X, "Rétraction", min=0.0, max=20.0,
       step=0.1, unit="mm", help="Pas de rétraction pour un déplacement plus court."),
    _f("retract_when_changing_layer", "Rétracter au changement de couche", "bool", True, X, "Rétraction",
       help="Rétraction avant la montée en Z."),
    _f("retract_before_wipe", "Rétraction avant essuyage", "percent", 70, X, "Rétraction", min=0, max=100, step=5,
       help="Part de la rétraction faite avant l'essuyage, le reste pendant."),
    _f("wipe", "Essuyage", "bool", True, X, "Rétraction",
       help="Recule la buse sur la ligne imprimée pendant la rétraction."),
    _f("wipe_distance", "Distance d'essuyage", "float", 2.0, X, "Rétraction", min=0.0, max=10.0, step=0.1, unit="mm",
       help="Longueur du mouvement d'essuyage."),
    _f("z_hop", "Saut en Z", "float", 0.4, X, "Rétraction", min=0.0, max=5.0, step=0.05, unit="mm",
       help="Montée de la buse pendant les déplacements (0 = aucune)."),
    _f("z_hop_types", "Type de saut en Z", "enum", "normal", X, "Rétraction", options=("normal", "spiral"),
       help="Montée droite ou en spirale."),
    _f("retract_lift_above", "Saut en Z au-dessus de", "float", 0.0, X, "Rétraction", min=0.0, max=256.0, step=1, unit="mm",
       help="Pas de saut sous cette hauteur (0 = sans limite)."),
    _f("retract_lift_below", "Saut en Z en dessous de", "float", 0.0, X, "Rétraction", min=0.0, max=256.0, step=1, unit="mm",
       help="Pas de saut au-dessus de cette hauteur (0 = sans limite)."),
    _f("only_retract_when_crossing_perimeters", "Rétracter seulement en croisant un mur", "bool", False, X, "Rétraction",
       help="Pas de rétraction pour un déplacement qui reste dans la pièce."),
    _f("reduce_infill_retraction", "Moins de rétractions dans le remplissage", "bool", True, X, "Rétraction",
       help="Pas de rétraction entre deux lignes de remplissage voisines."),
    _f("avoid_crossing_perimeters", "Éviter de croiser les murs", "bool", False, X, "Déplacements",
       help="Déplacements qui contournent les murs (moins de fils)."),
    _f("max_travel_detour_distance", "Détour maximal", "float", 0.0, X, "Déplacements", min=0.0, max=100.0, step=1,
       unit="mm", help="Longueur de détour maximale pour éviter un mur (0 = sans limite)."),
    _f("fan_min_speed", "Ventilation minimale", "int", 60, X, "Ventilation", min=0, max=100, step=5, unit="%",
       filament="fan_min_speed", help="Ventilation de la pièce hors surplombs (défaut de la fiche filament)."),
    _f("fan_max_speed", "Ventilation maximale", "int", 100, X, "Ventilation", min=0, max=100, step=5, unit="%",
       filament="fan_max_speed", help="Ventilation sur les couches courtes."),
    _f("overhang_fan_speed", "Ventilation des surplombs", "int", 100, X, "Ventilation", min=0, max=100, step=5, unit="%",
       filament="overhang_fan_speed", help="Ventilation sur les surplombs et les ponts."),
    _f("overhang_fan_threshold", "Seuil de surplomb ventilé", "percent", 25, X, "Ventilation", min=0, max=100, step=5,
       filament="overhang_fan_threshold", help="Part de porte-à-faux à partir de laquelle la ventilation de surplomb s'applique."),
    _f("close_fan_the_first_x_layers", "Couches sans ventilation", "int", 1, X, "Ventilation", min=0, max=50, step=1,
       filament="close_fan_the_first_x_layers", help="Premières couches imprimées ventilateur éteint."),
    _f("full_fan_speed_layer", "Couche de ventilation complète", "int", 3, X, "Ventilation", min=0, max=50, step=1,
       filament="full_fan_speed_layer", help="À partir de cette couche, la ventilation atteint sa valeur normale (0 = tout de suite)."),
    _f("additional_cooling_fan_speed", "Ventilateur auxiliaire", "int", 70, X, "Ventilation", min=0, max=100, step=5, unit="%",
       filament="additional_cooling_fan_speed", help="Ventilateur latéral de la P1S (M106 P2)."),
    _f("exhaust_fan_speed", "Extraction de chambre", "int", 0, X, "Ventilation", min=0, max=100, step=5, unit="%",
       filament="exhaust_fan_speed", help="Ventilateur de chambre pendant l'impression (M106 P3)."),
    _f("enable_overhang_bridge_fan", "Ventiler les ponts", "bool", True, X, "Ventilation",
       help="Applique la ventilation de surplomb aux ponts."),
    _f("nozzle_temperature_initial_layer", "Température de buse (première couche)", "int", 215, X, "Températures", min=150,
       max=300, step=5, unit="°C", filament="nozzle_temperature_initial_layer", help="Défaut de la fiche filament."),
    _f("nozzle_temperature", "Température de buse", "int", 210, X, "Températures", min=150, max=300, step=5, unit="°C",
       filament="nozzle_temperature", help="Couches suivantes (défaut de la fiche filament)."),
    _f("bed_temperature_initial_layer", "Température du lit (première couche)", "int", 60, X, "Températures", min=0, max=110,
       step=5, unit="°C", help="Défaut : fiche filament pour la plaque choisie."),
    _f("bed_temperature", "Température du lit", "int", 55, X, "Températures", min=0, max=110, step=5, unit="°C",
       help="Couches suivantes (défaut : fiche filament pour la plaque choisie)."),
    _f("chamber_temperature", "Température de chambre", "int", 0, X, "Températures", min=0, max=60, step=5, unit="°C",
       filament="chamber_temperature", help="0 = chambre non régulée (P1S)."),
    _f("nozzle_standby_temperature", "Température d'attente de la buse", "int", 140, X, "Températures", min=0, max=300,
       step=5, unit="°C", help="Préchauffe pendant le référencement et le nivellement."),
    _f("flow_ratio", "Débit", "float", 0.98, X, "Débit", min=0.5, max=1.5, step=0.01, filament="flow_ratio",
       help="Facteur global d'extrusion (défaut de la fiche filament)."),
    _f("filament_max_volumetric_speed", "Débit volumique maximal", "float", 15.0, X, "Débit", min=0.5, max=50.0, step=0.5,
       unit="mm³/s", filament="filament_max_volumetric_speed", help="Plafonne toutes les vitesses d'extrusion."),
    _f("filament_density", "Densité du filament", "float", 1.24, X, "Débit", min=0.5, max=3.0, step=0.01, unit="g/cm³",
       filament="filament_density", help="Sert au poids estimé."),
    _f("filament_diameter", "Diamètre du filament", "float", 1.75, X, "Débit", min=1.6, max=3.0, step=0.01, unit="mm",
       filament="filament_diameter", help="1,75 mm sur la P1S."),
    _f("filament_cost", "Coût du filament", "float", 20.0, X, "Débit", min=0.0, max=1000.0, step=0.5, unit="€/kg",
       filament="filament_cost", help="Sert au coût estimé."),
    _f("print_sequence", "Séquence d'impression", "enum", "by_layer", X, "Séquence et machine", options=("by_layer", "by_object"),
       help="Couche par couche (tous les objets) ou objet par objet (réservé en V1)."),
    _f("extruder_clearance_radius", "Rayon de dégagement de l'extrudeur", "float", 65.0, X, "Séquence et machine", min=1,
       max=200, step=1, unit="mm", help="Pour l'impression objet par objet."),
    _f("extruder_clearance_height_to_rod", "Hauteur de dégagement (tige)", "float", 34.0, X, "Séquence et machine", min=1,
       max=200, step=1, unit="mm", help="Pour l'impression objet par objet."),
    _f("extruder_clearance_height_to_lid", "Hauteur de dégagement (capot)", "float", 140.0, X, "Séquence et machine", min=1,
       max=300, step=1, unit="mm", help="Pour l'impression objet par objet."),
    _f("timelapse_type", "Timelapse", "enum", "none", X, "Séquence et machine", options=("none", "traditional", "smooth"),
       help="Aucun, traditionnel (photo au changement de couche) ou lissé (déplacement hors pièce)."),
    _f("label_objects", "Objets identifiés", "bool", True, X, "Séquence et machine",
       help="Marque chaque objet dans le G-code pour l'exclusion depuis l'écran de l'imprimante."),
    _f("bed_levelling", "Nivellement automatique", "bool", True, X, "Séquence et machine",
       help="Nivellement du lit sur l'emprise de la première couche avant l'impression."),
    _f("vibration_calibration", "Compensation des vibrations", "bool", True, X, "Séquence et machine",
       help="Calibration des résonances au démarrage."),
    _f("flow_calibration", "Calibration du débit", "bool", False, X, "Séquence et machine",
       help="Calibration dynamique du débit au démarrage."),
    _f("use_ams", "Filament depuis l'AMS", "bool", False, X, "Séquence et machine",
       help="Chargement par l'AMS dans la séquence de démarrage (sinon bobine externe)."),
    _f("threads", "Processus de calcul", "int", 0, X, "Séquence et machine", min=0, max=64, step=1,
       help="0 = automatique (cœurs − 1, au plus 8)."),
    _f("gcode_comments", "Commentaires détaillés", "bool", False, X, "Séquence et machine",
       help="Ajoute des commentaires explicatifs dans le G-code (fichier plus gros)."),
    _f("nozzle", "Buse", "enum", NOZZLE_DEFAULT, X, "Machine", options=NOZZLES, unit="mm",
       help="Diamètre de la buse montée."),
    _f("printer_profile", "Préréglage machine", "string", PRINTER_PROFILE_DEFAULT, X, "Machine",
       help="Nom du préréglage machine (« Bambu Lab P1S 0.4 nozzle »)."),
    _f("bed_type", "Plaque", "enum", "Textured PEI Plate", X, "Machine", options=BED_TYPES,
       help="Plaque posée sur l'imprimante : fixe la température du lit."),
    _f("material", "Matériau", "string", "PLA", X, "Machine",
       help="Type de filament (fiche Alpine : PLA, PETG, ABS, TPU…)."),
    _f("orient", "Orientation automatique", "bool", False, X, "Mise en place",
       help="Cherche l'orientation qui minimise surplombs et hauteur avant de trancher."),
    _f("arrange", "Placement automatique", "bool", True, X, "Mise en place",
       help="Place les objets sur le plateau hors zone exclue."),
)

FIELD_BY_KEY: dict[str, Field] = {f.key: f for f in FIELDS}
if len(FIELD_BY_KEY) != len(FIELDS):
    raise RuntimeError("clés de réglage dupliquées dans FIELDS")
for _key in CONTRACT_KEYS:
    if _key not in FIELD_BY_KEY:
        raise RuntimeError(f"clé du contrat client absente du schéma : {_key}")
for _alias, _target in ALIASES.items():
    if _alias in FIELD_BY_KEY or _target not in FIELD_BY_KEY:
        raise RuntimeError(f"alias invalide : {_alias} → {_target}")


# ================================================================================================= préréglages

@dataclass(frozen=True)
class Preset:
    """Préréglage Alpine : buse, hauteur de couche, libellé et écarts par rapport aux défauts du schéma."""

    name: str
    nozzle: str
    layer_height: float
    variant: str
    overrides: dict = field(default_factory=dict)

    def values(self) -> dict:
        """Défauts du schéma complétés par le préréglage (clés typées)."""
        base = {f.key: f.default for f in FIELDS}
        base.update(self.overrides)
        return base


def layer_height_bounds(nozzle) -> tuple[float, float]:
    """Hauteur de couche admise pour une buse : de 20 % (≥ 0,06 mm) à 75 % du diamètre (même règle que machine/)."""
    d = float(nozzle)
    return (round(max(0.06, 0.2 * d), 3), round(0.75 * d, 3))


def _round2(value: float) -> float:
    return round(value + 1e-9, 2)


def _preset(nozzle: str, layer: float, variant: str, *, strength=False) -> Preset:
    """Construit un préréglage : largeurs ≈ 1,05 × buse, murs et surfaces adaptés à la buse et à la couche."""
    d = float(nozzle)
    width = _round2(LINE_WIDTH_FACTOR * d)
    inner = _round2(1.12 * d)
    first = _round2(1.25 * d)
    # Épaisseur visée des coques : plus la buse est grosse, plus la coque est épaisse.
    top_mm, bottom_mm, top_min, bottom_min = {"0.2": (0.6, 0.5, 4, 3), "0.4": (0.8, 0.6, 3, 2),
                                              "0.6": (1.0, 0.8, 3, 2), "0.8": (1.2, 1.0, 2, 2)}[nozzle]
    top = max(top_min, math.ceil(top_mm / layer - 1e-9))
    bottom = max(bottom_min, math.ceil(bottom_mm / layer - 1e-9))
    walls = {"0.2": 3, "0.4": 2, "0.6": 2, "0.8": 2}[nozzle]
    speeds = {"0.2": (100.0, 150.0, 150.0, 120.0, 100.0, 40.0),
              "0.4": (200.0, 300.0, 270.0, 250.0, 200.0, 50.0),
              "0.6": (150.0, 220.0, 200.0, 180.0, 150.0, 40.0),
              "0.8": (120.0, 180.0, 160.0, 150.0, 120.0, 35.0)}[nozzle]
    outer, inner_speed, infill, solid, top_speed, first_speed = speeds
    overrides = {
        "layer_height": layer, "initial_layer_print_height": max(layer, _round2(min(0.75 * d, max(0.2, layer)))),
        "line_width": width, "outer_wall_line_width": width, "inner_wall_line_width": inner,
        "top_surface_line_width": width, "sparse_infill_line_width": inner, "internal_solid_infill_line_width": width,
        "initial_layer_line_width": first, "support_line_width": width,
        "wall_loops": walls, "top_shell_layers": top, "bottom_shell_layers": bottom,
        "top_shell_thickness": top_mm, "bottom_shell_thickness": 0.0,
        "outer_wall_speed": outer, "inner_wall_speed": inner_speed, "sparse_infill_speed": infill,
        "internal_solid_infill_speed": solid, "top_surface_speed": top_speed, "initial_layer_speed": first_speed,
        "initial_layer_infill_speed": _round2(first_speed * 2.1), "gap_infill_speed": solid,
        "support_speed": _round2(infill * 0.55), "support_interface_speed": _round2(infill * 0.3),
        "small_perimeter_speed": _round2(outer * 0.5), "nozzle": nozzle,
        "printer_profile": f"Bambu Lab P1S {nozzle} nozzle", "sparse_infill_density": 15,
        "support_base_pattern_spacing": _round2(max(2.0, 6.0 * d)), "support_object_xy_distance": _round2(max(0.3, 0.8 * d)),
    }
    if strength:
        overrides.update({"wall_loops": walls + 2, "top_shell_layers": top + 2, "bottom_shell_layers": bottom + 1,
                          "sparse_infill_density": 25, "sparse_infill_pattern": "cubic",
                          "outer_wall_speed": _round2(outer * 0.75), "inner_wall_speed": _round2(inner_speed * 0.8),
                          "ensure_vertical_shell_thickness": "enabled"})
    suffix = "" if nozzle == NOZZLE_DEFAULT else f" {nozzle} nozzle"
    name = f"{layer:.2f}mm {variant}{suffix}"
    return Preset(name, nozzle, layer, variant, overrides)


def _build_presets() -> tuple[Preset, ...]:
    table = {
        "0.2": ((0.06, "Extra Fine"), (0.08, "Fine"), (0.10, "Standard"), (0.12, "Draft"), (0.14, "Extra Draft")),
        "0.4": ((0.08, "Extra Fine"), (0.12, "Fine"), (0.16, "Optimal"), (0.20, "Standard"), (0.24, "Draft"),
                (0.28, "Extra Draft")),
        "0.6": ((0.18, "Fine"), (0.24, "Optimal"), (0.30, "Standard"), (0.36, "Draft"), (0.42, "Extra Draft")),
        "0.8": ((0.24, "Fine"), (0.32, "Optimal"), (0.40, "Standard"), (0.48, "Draft"), (0.56, "Extra Draft")),
    }
    presets = []
    for nozzle, rows in table.items():
        for layer, variant in rows:
            presets.append(_preset(nozzle, layer, variant))
            if variant == "Standard":
                presets.append(_preset(nozzle, layer, "Strength", strength=True))
    return tuple(presets)


PRESETS: tuple[Preset, ...] = _build_presets()
PRESET_BY_NAME: dict[str, Preset] = {p.name: p for p in PRESETS}


def normalise_nozzle(nozzle) -> str:
    """« 0.4 », 0.4, « 0,4 », « 0.4 mm » → « 0.4 » ; ``SettingsError`` si la buse n'est pas 0,2 / 0,4 / 0,6 / 0,8."""
    text = str(nozzle if nozzle is not None else "").strip().lower().replace(",", ".").replace("mm", "").strip()
    try:
        value = float(text)
    except ValueError:
        raise SettingsError("nozzle", f"buse inconnue « {nozzle} » (attendu : {', '.join(NOZZLES)}).") from None
    for candidate in NOZZLES:
        if abs(float(candidate) - value) < 1e-6:
            return candidate
    raise SettingsError("nozzle", f"buse {value:g} mm non prise en charge (attendu : {', '.join(NOZZLES)}).")


def presets_for(nozzle) -> list[Preset]:
    """Préréglages d'une buse, par hauteur de couche croissante."""
    nozzle = normalise_nozzle(nozzle)
    return sorted((p for p in PRESETS if p.nozzle == nozzle), key=lambda p: (p.layer_height, p.variant != "Standard"))


def preset_for(nozzle, layer_height=None, *, strength=False, name=None) -> Preset:
    """Préréglage par nom, ou le plus proche d'une hauteur de couche pour la buse (Standard par défaut)."""
    nozzle = normalise_nozzle(nozzle)
    if name:
        preset = PRESET_BY_NAME.get(str(name).strip())
        if preset is None:
            for candidate in PRESETS:
                if candidate.name.lower() == str(name).strip().lower():
                    preset = candidate
                    break
        if preset is None or preset.nozzle != nozzle:
            raise SettingsError("print_settings_id", f"préréglage inconnu pour la buse {nozzle} : « {name} ».")
        return preset
    candidates = [p for p in presets_for(nozzle) if (p.variant == "Strength") == bool(strength)]
    if layer_height is None:
        return next(p for p in candidates if p.variant in ("Standard", "Strength"))
    layer = float(layer_height)
    return min(candidates, key=lambda p: (abs(p.layer_height - layer), p.variant != "Standard"))


# ================================================================================================= conversions

_TRUE = {"1", "true", "yes", "on", "oui", "vrai"}
_FALSE = {"0", "false", "no", "off", "non", "faux", "none", ""}   # « none » : ancienne valeur client de fuzzy_skin
_TIMELAPSE_BY_NUMBER = {"0": "none", "1": "traditional", "2": "smooth"}   # anciens projets : timelapse en chiffre


def _as_bool(key, value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise SettingsError(key, f"valeur booléenne attendue (1/0, true/false), reçu « {value} ».")


def _as_number(key, value, f: Field):
    if isinstance(value, bool):
        raise SettingsError(key, "nombre attendu, reçu un booléen.")
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().lower().replace(",", ".")
        if f.type == "percent" and text.endswith("%"):
            text = text[:-1].strip()
        for unit in ("mm/s²", "mm/s2", "mm/s", "mm³/s", "mm3/s", "mm²", "mm", "°c", "°", "s", "%"):
            if text.endswith(unit):
                text = text[: -len(unit)].strip()
                break
        try:
            number = float(text)
        except ValueError:
            raise SettingsError(key, f"nombre attendu, reçu « {value} ».") from None
    if not math.isfinite(number):
        raise SettingsError(key, "nombre fini attendu.")
    if f.type == "int":
        if abs(number - round(number)) > 1e-6:
            raise SettingsError(key, f"entier attendu, reçu {number:g}.")
        number = int(round(number))
    elif f.type == "percent":
        number = int(round(number)) if abs(number - round(number)) < 1e-6 else number
    if number < f.min or number > f.max:
        raise SettingsError(key, f"{number:g} hors bornes [{f.min:g} ; {f.max:g}]{(' ' + f.unit) if f.unit else ''}.")
    return number


def _as_enum(key, value, f: Field) -> str:
    text = re.sub(r"\s+", " ", str(value).strip())
    for option in f.options:
        if option == text or option.lower() == text.lower() or option.replace("_", " ") == text.lower():
            return option
    if key == "bed_type":
        plate = normalise_plate(text)
        if plate:
            return plate
    if key == "nozzle":
        return normalise_nozzle(text)
    if key == "timelapse_type" and text in _TIMELAPSE_BY_NUMBER:
        return _TIMELAPSE_BY_NUMBER[text]
    raise SettingsError(key, f"valeur « {text[:40]} » inconnue (attendu : {', '.join(f.options)}).")


def coerce(key, value):
    """Valeur typée et bornée d'un réglage du schéma (``SettingsError`` sinon)."""
    f = FIELD_BY_KEY.get(key)
    if f is None:
        raise SettingsError(key, "réglage inconnu.")
    if value is None:
        return f.default
    if f.type == "bool":
        return _as_bool(key, value)
    if f.type == "enum":
        return _as_enum(key, value, f)
    if f.type == "string":
        text = str(value).strip()
        if len(text) > 120:
            raise SettingsError(key, "texte trop long (120 caractères maximum).")
        return text or f.default
    return _as_number(key, value, f)


# ================================================================================================= résolution

_IGNORED_KEYS = ("print_settings_id", "plate_name", "files", "file", "materials", "process_settings")
_PROFILE_NOZZLE_RE = re.compile(r"\s+(\d+(?:[.,]\d+)?)\s*nozzle\s*$", re.I)


def _apply(target: dict, source: dict, warnings: list, origin: str) -> None:
    """Applique des réglages (dict) sur ``target`` après traduction des alias et validation.

    Une valeur ``None`` ou vide vaut « non fournie » (le préréglage reste) ; une clé inconnue est ignorée avec avertissement.
    """
    if not source:
        return
    if not isinstance(source, dict):
        raise SettingsError(origin, "dictionnaire attendu.")
    supports_off = False
    for raw_key, value in source.items():
        key = ALIASES.get(str(raw_key), str(raw_key))
        if key not in FIELD_BY_KEY:
            if str(raw_key) not in _IGNORED_KEYS:
                warnings.append(f"Réglage inconnu ignoré ({origin}) : {str(raw_key)[:60]}")
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if key == "support_type" and str(value).strip().lower() in ("off", "none", "0", "false"):
            supports_off = True   # « off » du client : supports désactivés, quel que soit l'ordre des clés
            continue
        if key == "material" and isinstance(value, (list, tuple)):
            value = value[0] if value else None
            if value is None:
                continue
        target[key] = coerce(key, value)
    if supports_off:
        target["enable_support"] = False


def _as_dict(value, origin: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SettingsError(origin, "dictionnaire attendu.")
    return dict(value)


def _first(key: str, *sources) -> object | None:
    """Première valeur non vide de ``key`` (ou d'un de ses alias) dans les sources, dans l'ordre ; liste → premier élément."""
    names = [key] + [alias for alias, target in ALIASES.items() if target == key]
    for source in sources:
        if not isinstance(source, dict):
            continue
        for name in names:
            value = source.get(name)
            if isinstance(value, (list, tuple)):
                value = value[0] if value else None
            if value is not None and str(value).strip() != "":
                return value
    return None


def _nozzle_of_profile(profile) -> str | None:
    """Buse du nom de préréglage machine (« Bambu Lab P1S 0.6 nozzle » → « 0.6 ») ; None sans suffixe."""
    match = _PROFILE_NOZZLE_RE.search(str(profile or ""))
    if not match:
        return None
    try:
        return normalise_nozzle(match.group(1))
    except SettingsError:
        raise SettingsError("printer_profile", f"buse du préréglage machine non prise en charge : « {str(profile)[:60]} ».") from None


def resolve_with_report(settings=None, process_settings=None, nozzle=None, material=None, bed_type=None, *,
                        preset=None) -> tuple[dict, list[str]]:
    """Comme ``resolve`` et renvoie aussi les avertissements (clés inconnues ignorées, ajustements)."""
    settings = _as_dict(settings, "settings")
    process_settings = _as_dict(process_settings, "process_settings")
    warnings: list[str] = []
    # Buse, matériau, plaque : argument explicite, sinon la demande, sinon les réglages avancés (alias compris : nozzle_diameter,
    # filament_type, curr_bed_type, printer_settings_id), sinon la buse du préréglage machine, sinon le défaut.
    profile = _first("printer_profile", settings, process_settings)
    profile_nozzle = _nozzle_of_profile(profile)
    nozzle_raw = nozzle if nozzle is not None and str(nozzle).strip() else _first("nozzle", settings, process_settings)
    if nozzle_raw is None:
        nozzle_raw = profile_nozzle or NOZZLE_DEFAULT
    nozzle = normalise_nozzle(nozzle_raw)
    if profile_nozzle and profile_nozzle != nozzle:
        raise SettingsError("printer_profile", f"le préréglage machine « {str(profile)[:60]} » est prévu pour une buse de "
                                               f"{profile_nozzle} mm, la demande indique {nozzle} mm.")
    material_raw = material if material is not None and str(material).strip() else _first("material", settings, process_settings)
    if material_raw is None:
        material_raw = _first("materials", settings, process_settings) or "PLA"
    try:
        fiche = resolve_filament(material_raw)
    except MaterialUnknownError as error:
        raise SettingsError("material", str(error)) from None
    bed_raw = bed_type if bed_type is not None and str(bed_type).strip() else _first("bed_type", settings, process_settings)
    if bed_raw is None:
        bed_raw = "Textured PEI Plate"
    plate = normalise_plate(bed_raw)
    if not plate:
        raise SettingsError("bed_type", f"plaque inconnue « {str(bed_raw)[:40]} » (attendu : {', '.join(BED_TYPES)}).")
    # Préréglage : nom demandé (strict pour l'appelant et ``settings``, tolérant pour un ancien nom enregistré dans les
    # réglages avancés), sinon le plus proche de la hauteur de couche demandée.
    requested_layer = _first("layer_height", settings, process_settings)
    layer_guess = None
    if requested_layer is not None:
        try:
            layer_guess = _as_number("layer_height", requested_layer, FIELD_BY_KEY["layer_height"])
        except SettingsError:
            layer_guess = None
    preset_name = preset or _first("print_settings_id", settings)
    chosen = None
    if preset_name:
        chosen = preset_for(nozzle, layer_guess, name=preset_name)
    else:
        old_name = _first("print_settings_id", process_settings)
        if old_name:
            try:
                chosen = preset_for(nozzle, layer_guess, name=old_name)
            except SettingsError:
                warnings.append(f"Préréglage « {str(old_name)[:60]} » inconnu : préréglage Alpine le plus proche utilisé.")
        if chosen is None:
            chosen = preset_for(nozzle, layer_guess)
    resolved = chosen.values()
    # Fiche filament : défauts des réglages marqués « filament » et rétraction spécifique.
    for f in FIELDS:
        if f.filament and fiche.get(f.filament) is not None:
            resolved[f.key] = fiche[f.filament]
    for key in ("retraction_length", "retraction_speed", "deretraction_speed", "z_hop", "wipe"):
        value = fiche.get(f"filament_{key}")
        if value is not None:
            resolved[key] = value
    temps = plate_temperatures(fiche, plate)
    if temps is None:
        raise SettingsError("bed_type", f"la plaque « {plate} » est déconseillée pour le {fiche['type']} "
                                        f"(plaques conseillées : {', '.join(p for p in PLATES if plate_temperatures(fiche, p))}).")
    resolved["bed_temperature_initial_layer"], resolved["bed_temperature"] = temps
    # Demande du client : réglages avancés, puis les clés explicites de la demande (qui priment).
    _apply(resolved, process_settings, warnings, "process_settings")
    _apply(resolved, settings, warnings, "settings")
    resolved["nozzle"], resolved["bed_type"], resolved["material"] = nozzle, plate, fiche["type"]
    if _nozzle_of_profile(resolved["printer_profile"]) is None:
        resolved["printer_profile"] = f"{resolved['printer_profile']} {nozzle} nozzle"   # nom complet attendu par machine/
    _check_consistency(resolved, nozzle, warnings)
    # Clés dérivées (contrat du fichier tranché et du G-code).
    resolved["nozzle_diameter"] = float(nozzle)
    resolved["printer_variant"] = nozzle
    resolved["printer_settings_id"] = resolved["printer_profile"]
    resolved["printer_model"] = printer_model_from_profile(resolved["printer_profile"])
    resolved["print_settings_id"] = chosen.name
    resolved["preset_name"] = chosen.name
    resolved["curr_bed_type"] = plate
    resolved["bed_type_short"] = PLATE_SHORT[plate]
    resolved["filament_type"] = fiche["type"]
    resolved["filament_settings_id"] = fiche["settings_id"]
    resolved["filament_vendor"] = fiche["vendor"]
    resolved["filament_colour"] = fiche["colour"]
    resolved["filament_ids"] = fiche["tray_info_idx"]
    resolved["tray_info_idx"] = fiche["tray_info_idx"]
    resolved["material_requested"] = str(material_raw)
    for plate_name in PLATES:
        key = PLATE_TEMP_KEYS[plate_name]
        resolved[key] = int(fiche.get(key) or 0)
        resolved[f"{key}_initial_layer"] = int(fiche.get(f"{key}_initial_layer") or 0)
    resolved["enable_prime_tower"] = bool(resolved["prime_tower"])
    resolved["nozzle_hardened_required"] = bool(fiche["nozzle_hardened_required"])
    resolved["ams_compatible"] = bool(fiche["ams_compatible"])
    # Clés jumelles lues par gcode.py (SETTINGS_DEFAULTS) sous d'autres noms : mêmes valeurs, pour éviter un défaut
    # silencieux à l'intégration. ``overhang_fan_threshold`` reste en % ici (classe 0–3 côté gcode : à convertir là-bas).
    resolved["aux_fan_speed"] = resolved["additional_cooling_fan_speed"]
    resolved["chamber_fan_speed"] = resolved["exhaust_fan_speed"]
    resolved["bridge_fan_speed"] = resolved["overhang_fan_speed"]
    resolved["filament_flow_ratio"] = resolved["flow_ratio"]
    resolved["z_hop_type"] = resolved["z_hop_types"]
    if fiche["nozzle_hardened_required"]:
        warnings.append(f"{fiche['type']} : filament abrasif, buse en acier trempé recommandée (la buse d'origine s'use).")
    if resolved["use_ams"] and not fiche["ams_compatible"]:
        warnings.append(f"{fiche['type']} : filament déconseillé dans l'AMS, chargement par la bobine externe.")
    return resolved, warnings


def resolve(settings=None, process_settings=None, nozzle=None, material=None, bed_type=None, *, preset=None) -> dict:
    """Réglages résolus (dict plat, valeurs typées) pour une demande de tranchage.

    ``settings`` : les clés du contrat client (25 clés, partielles) ; ``process_settings`` : réglages avancés
    (chaînes « 15% », « 0.2 », « 1 »… ou nombres) ; ``nozzle``/``material``/``bed_type`` : explicites ou lus dans
    ``settings``. Ordre : préréglage (buse × hauteur de couche) → fiche filament → ``process_settings`` →
    ``settings``. Toute valeur hors bornes ou inconnue lève ``SettingsError`` (message en français, ``.key``).
    """
    return resolve_with_report(settings, process_settings, nozzle, material, bed_type, preset=preset)[0]


def _check_consistency(resolved: dict, nozzle: str, warnings: list) -> None:
    """Bornes dépendantes de la buse (hauteurs de couche, largeurs de ligne) et cohérences simples."""
    low, high = layer_height_bounds(nozzle)
    for key in ("layer_height", "initial_layer_print_height"):
        value = float(resolved[key])
        if value < low - 1e-9 or value > high + 1e-9:
            raise SettingsError(key, f"{value:g} mm hors de la plage de la buse {nozzle} mm [{low:g} ; {high:g}].")
    d = float(nozzle)
    for key in ("line_width", "outer_wall_line_width", "inner_wall_line_width", "top_surface_line_width",
                "sparse_infill_line_width", "internal_solid_infill_line_width", "initial_layer_line_width",
                "support_line_width"):
        value = float(resolved[key])
        if value < LINE_WIDTH_RANGE[0] * d - 1e-9 or value > LINE_WIDTH_RANGE[1] * d + 1e-9:
            raise SettingsError(key, f"{value:g} mm hors de la plage de la buse {nozzle} mm "
                                     f"[{LINE_WIDTH_RANGE[0] * d:.2f} ; {LINE_WIDTH_RANGE[1] * d:.2f}].")
    if resolved["bed_temperature_initial_layer"] < resolved["bed_temperature"] - 20:
        warnings.append("Lit : la première couche est bien plus froide que les suivantes.")
    if resolved["enable_support"] and resolved["support_type"].startswith("tree"):
        warnings.append("Supports arborescents : remplacés par des supports normaux en V1.")
    if resolved["print_sequence"] == "by_object":
        warnings.append("Séquence objet par objet : réservée, impression couche par couche en V1.")
    for f in FIELDS:
        if f.reserved and resolved.get(f.key) not in (f.default, None):
            warnings.append(f"Réglage « {f.key} » réservé : sans effet en V1.")


def printer_model_from_profile(profile) -> str:
    """« Bambu Lab P1S 0.4 nozzle » → « Bambu Lab P1S » (le module machine/ fait la correspondance complète)."""
    text = re.sub(r"\s+\d+(?:[.,]\d+)?\s*nozzle\s*$", "", str(profile or "").strip(), flags=re.I)
    return text or "Bambu Lab P1S"


# ================================================================================================= API du site

def _api_value(f: Field, value) -> str:
    if f.type == "bool":
        return "1" if value else "0"
    if f.type == "percent":
        return f"{value:g}%"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def schema_for_api(nozzle=NOZZLE_DEFAULT, preset=None, material=None, bed_type=None) -> dict:
    """``{profile, nozzle, presets, fields:[{key, value, boolean, label, category, group, type, min, max, step, unit,
    options, help, reserved}]}`` pour ``/api/model-studio/process-schema``.

    ``value`` est une chaîne (« 1 »/« 0 » pour un booléen, « 15% » pour un pourcentage), comme le client l'attend.
    Les valeurs sont celles du préréglage de la buse (et, si ``material`` est donné, de la fiche filament).
    """
    nozzle = normalise_nozzle(nozzle)
    chosen = preset_for(nozzle, name=preset) if preset else preset_for(nozzle)
    values = chosen.values()
    if material:
        values = resolve({"material": material, "bed_type": bed_type or "Textured PEI Plate"}, {}, nozzle,
                         preset=chosen.name)
    fields = []
    for f in FIELDS:
        entry = {"key": f.key, "value": _api_value(f, values.get(f.key, f.default)), "boolean": f.type == "bool",
                 "label": f.label, "category": f.category, "group": f.group, "type": f.type, "unit": f.unit,
                 "help": f.help, "reserved": f.reserved}
        if f.is_number():
            entry.update({"min": f.min, "max": f.max, "step": f.step})
        else:
            entry.update({"min": None, "max": None, "step": None})
        if f.type == "enum":
            entry["options"] = list(f.options)
        fields.append(entry)
    return {"profile": f"{chosen.name} (Alpine Model Engine)", "nozzle": nozzle,
            "presets": [p.name for p in presets_for(nozzle)], "categories": list(CATEGORIES), "fields": fields}


__all__ = ["ALIASES", "BED_TYPES", "BRIM_TYPES", "CATEGORIES", "CONTRACT_KEYS", "FIELDS", "FIELD_BY_KEY",
           "INFILL_PATTERNS", "NOZZLES", "PRESETS", "PRESET_BY_NAME", "Field", "Preset", "SEAM_POSITIONS",
           "SUPPORT_TYPES", "SettingsError", "coerce", "layer_height_bounds", "normalise_nozzle", "preset_for",
           "presets_for", "printer_model_from_profile", "resolve", "resolve_with_report", "schema_for_api"]
