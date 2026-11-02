"""Relief souvenir — vue « Face » (port de ReliefFace.kt).

Le terrain est vu horizontalement depuis la direction vers laquelle la paroi regarde ; la carte de profondeur (distance
du premier point de terrain rencontré par chaque rayon horizontal) devient une plaque en relief. Les parois presque
verticales, écrasées dans une vue de dessus, deviennent la surface lisible de la plaque. La plaque est un terrain
« synthétique » (grille + zone) : tout le reste du relief (tranches fermées, tracé en ruban, texte, socle) s'applique.
Limite : un modèle d'altitudes est 2,5D (aucun surplomb).
"""
import dataclasses
import math

import numpy as np

from . import geo

COLONNES = 360
RELIEF_MIN_MM, RELIEF_MAX_MM = 4.0, 40.0
FOND_MM = 0.8
RAPPORT_MIN, RAPPORT_MAX = 0.15, 2.5
NOMS_DIRECTIONS = ("nord", "nord-est", "est", "sud-est", "sud", "sud-ouest", "ouest", "nord-ouest")


def libelle_orientation(deg):
    k = int((((deg + 22.5) % 360 + 360) % 360) / 45) % 8
    return f"face {NOMS_DIRECTIONS[k]} · {round(deg)}°"


@dataclasses.dataclass
class Face:
    grille: object
    zone: object
    orientation_deg: float
    ciel: bool
    largeur_m: float
    alt_bas: float
    alt_haut: float
    alt_min_vue: float
    alt_max_vue: float
    niveau_ciel: float
    couleur_ciel: tuple
    relief_mm: float
    dx: float
    dy: float

    @property
    def alt_milieu(self):
        return (self.alt_bas + self.alt_haut) / 2

    def projeter(self, x, y, alt):
        """Point du terrain (plan local m, altitude m) → plan de la plaque."""
        return np.asarray(x) * self.dy - np.asarray(y) * self.dx, np.asarray(alt) - self.alt_milieu

    def reglages(self, p):
        """Rectangle, exagération 1, ciel plat dans sa couleur (traité comme un lac), sommet = point le plus haut."""
        return dataclasses.replace(
            p, forme=geo.RECTANGLE, exageration=1.0, lacs=self.ciel, niveau_lac_m=self.niveau_ciel if self.ciel else None,
            couleur_lac=self.couleur_ciel, repartition="surface" if p.repartition == "manuelle" else p.repartition,
            epaisseur_min_mm=max(p.epaisseur_min_mm, 1.0), sommet_par_y=True, trace_surface=False)


def construire(g, zone_terrain, orientation_deg, largeur_mm, relief_mm=16.0, profondeur=1.0, alt_bas_m=None, alt_haut_m=None,
               couleur_ciel=("#9ED2EC", "Glacier")):
    """Plaque de la face vue depuis [orientation_deg] pour une plaque de [largeur_mm] mm de large."""
    orientation = ((orientation_deg % 360) + 360) % 360
    relief_mm = min(RELIEF_MAX_MM, max(RELIEF_MIN_MM, relief_mm))
    profondeur = min(5.0, max(0.3, profondeur))
    largeur = min(250.0, max(40.0, largeur_mm))
    w = min((g.nx - 1) * g.pas, (g.ny - 1) * g.pas) * 0.70
    l = w
    t = math.radians(orientation)
    dx, dy = -math.sin(t), -math.cos(t)
    rx, ry = dy, -dx
    pas_w = g.pas / 2
    nw = max(2, math.ceil(l / pas_w))
    nu = COLONNES
    pas_u = w / (nu - 1)
    u = -w / 2 + np.arange(nu) * pas_u
    sx, sy = rx * u - dx * l / 2, ry * u - dy * l / 2
    wk = np.minimum(l, np.arange(nw + 1) * pas_w)
    prof = g.altitude(sx[:, None] + dx * wk[None, :], sy[:, None] + dy * wk[None, :])
    finis = np.isfinite(prof)
    if not finis.any():
        raise ValueError("Aucune altitude connue dans la zone.")
    min_vue, max_vue = float(prof[finis].min()), float(prof[finis].max())
    avant = prof[:, 0][np.isfinite(prof[:, 0])]
    min_avant = float(avant.min()) if len(avant) else min_vue
    max_fond = max_vue
    h_min, h_max = max(20.0, RAPPORT_MIN * w), RAPPORT_MAX * w
    bas = min(max_vue - 10, max(min_vue - h_max, alt_bas_m if alt_bas_m is not None else min_avant))
    haut = alt_haut_m if alt_haut_m is not None else max_fond + 0.03 * (max_vue - min_vue) + 5
    if haut - bas < h_min:
        haut = bas + h_min
    if haut - bas > h_max:
        if alt_bas_m is None and alt_haut_m is not None:
            bas = haut - h_max
        else:
            haut = bas + h_max
    nv = max(2, math.ceil((haut - bas) / pas_u) + 1)
    haut = bas + (nv - 1) * pas_u
    alts = bas + np.arange(nv) * pas_u
    carte = np.full((nv, nu), np.nan)
    mc = np.maximum.accumulate(np.where(finis, prof, -np.inf), axis=1)
    for i in range(nu):
        k = np.searchsorted(mc[i], alts, side="left")
        dedans = k <= nw
        kk = np.minimum(k, nw)
        hcur = prof[i, kk]
        hp = prof[i, np.maximum(kk - 1, 0)]
        hp = np.where(np.isfinite(hp), hp, hcur)
        diff = hcur - hp
        tt = np.where(diff > 1e-9, np.clip((alts - hp) / np.where(diff > 1e-9, diff, 1.0), 0, 1), 1.0)
        v = np.where(kk == 0, 0.0, np.minimum(l, (kk - 1 + tt) * pas_w))
        carte[:, i] = np.where(dedans, v, np.nan)
    touches = carte[np.isfinite(carte) & (carte > pas_w)]
    centre = float(np.sort(touches)[len(touches) // 2]) if len(touches) else l / 2
    echelle = largeur / w
    r = relief_mm - FOND_MM
    kf = echelle * profondeur / (r / 2)
    ciel = bool(np.isnan(carte).any())
    alt = np.where(np.isnan(carte), 0.0, (FOND_MM + r * (0.5 + 0.5 * np.tanh((centre - np.nan_to_num(carte)) * kf))) / echelle)
    gr = geo.Grille(nu, nv, -(nu - 1) * pas_u / 2, -(nv - 1) * pas_u / 2, pas_u, alt)
    zone = geo.Zone(zone_terrain.lat, zone_terrain.lon, (nu - 1) * pas_u, (nv - 1) * pas_u, geo.RECTANGLE)
    return Face(gr, zone, orientation, ciel, w, bas, haut, min_vue, max_vue, FOND_MM / 2 / echelle, couleur_ciel, relief_mm, dx, dy)


def limites(source, pas_m):
    return [
        "Relief 2,5D : les surplombs ne sont pas représentables ; ce qu'un surplomb cache n'apparaît pas sur la plaque.",
        f"Précision : grille de {round(pas_m)} m ici, sur " + (
            "les altitudes swisstopo (swissALTI3D 2 m) en Suisse." if source == geo.SWISSTOPO
            else "Terrain Tiles hors de Suisse (30 m ou plus grossier) : les petites parois disparaissent."),
        "Le tracé suit la surface de la plaque : une erreur GPS ou d'altitude le déplace sur la paroi.",
    ]
