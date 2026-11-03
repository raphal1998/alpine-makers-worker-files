"""Types partagés entre les étapes du tranchage (découpe → murs → surfaces → remplissage → supports → adhérence →
planification). Conventions de ``docs/MODEL_ENGINE_ARCHITECTURE.md`` §3 : coordonnées en micromètres (int64),
``Paths`` = liste de rings propres (extérieurs CCW, trous CW, règle positive), ``Polyline`` = int64 (k, 2) ouvert.

Chaque étape ne remplit que ses champs ; aucune ne modifie ceux d'une autre. Les listes sont indexées par objet
(``object_id`` = position de l'objet dans la scène, 1 = premier) quand la donnée dépend de l'objet.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# Classes de surplomb d'un mur (fraction de la largeur de ligne non soutenue par la couche précédente).
OVERHANG_NONE = 0      # soutenu
OVERHANG_LOW = 1       # 0–25 %
OVERHANG_MEDIUM = 2    # 25–50 %
OVERHANG_HIGH = 3      # 50–75 %
OVERHANG_FULL = 4      # 75–100 % (pont ou surplomb total)

FEATURES = ("OuterWall", "InnerWall", "TopSurface", "BottomSurface", "InternalSolidInfill", "SparseInfill", "Bridge",
            "Support", "SupportInterface", "Brim", "Skirt", "GapFill", "Custom")


@dataclass
class WallLoop:
    """Un mur fermé (``points`` int64 (k, 2), premier point = couture, à refermer par l'émetteur)."""

    points: np.ndarray
    line_width_um: int
    is_outer: bool
    island: int
    object_id: int = 1
    overhang_class: np.ndarray | None = None   # int8 (k,) par segment partant du point i ; None = tout soutenu
    closed: bool = True


@dataclass
class Walls:
    """Résultat de ``perimeters.build_walls`` pour une couche et un objet."""

    loops: list = field(default_factory=list)          # list[WallLoop], ordre d'impression proposé
    gap_fill: list = field(default_factory=list)       # list[Polyline] traits médians des interstices
    thin_walls: list = field(default_factory=list)     # list[tuple[Polyline, int line_width_um]]
    inner_area: list = field(default_factory=list)     # Paths : zone restante pour le remplissage
    islands: int = 0


@dataclass
class Surfaces:
    """Résultat de ``surfaces.classify`` pour une couche et un objet (tous des ``Paths`` résolus)."""

    top_solid: list = field(default_factory=list)
    bottom_solid: list = field(default_factory=list)
    internal_solid: list = field(default_factory=list)
    bridge: list = field(default_factory=list)
    bridge_angle_rad: float = 0.0
    sparse: list = field(default_factory=list)


@dataclass
class SupportLayer:
    """Supports d'une couche (toutes zones en ``Paths``, traits en ``Polyline``)."""

    base_area: list = field(default_factory=list)
    interface_area: list = field(default_factory=list)
    base_lines: list = field(default_factory=list)
    interface_lines: list = field(default_factory=list)
    is_tree: bool = False


@dataclass
class Layer:
    """Une couche de la pile. ``z_um`` = haut de la couche ; ``regions`` = coupe du modèle par objet
    (``dict[object_id, Paths]``) ; ``all_regions`` = union de tous les objets."""

    index: int                      # 1 = première couche
    z_um: int
    height_um: int
    regions: dict = field(default_factory=dict)       # object_id -> Paths
    all_regions: list = field(default_factory=list)   # Paths
    walls: dict = field(default_factory=dict)         # object_id -> Walls
    surfaces: dict = field(default_factory=dict)      # object_id -> Surfaces
    infill_lines: dict = field(default_factory=dict)  # object_id -> dict[feature, list[Polyline]]
    support: SupportLayer | None = None
    adhesion: list = field(default_factory=list)      # list[tuple[feature, Polyline, line_width_um]] (brim, jupe)
    warnings: list = field(default_factory=list)

    @property
    def z_mm(self) -> float:
        return self.z_um / 1000.0

    @property
    def height_mm(self) -> float:
        return self.height_um / 1000.0


def feature_is_solid(feature: str) -> bool:
    return feature in ("TopSurface", "BottomSurface", "InternalSolidInfill", "Bridge")
