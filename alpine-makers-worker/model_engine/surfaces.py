"""Classification des surfaces d'une couche (§5.3 de docs/MODEL_ENGINE_ARCHITECTURE.md) : dessus, dessous, ponts,
plein interne et creux, par objet, à partir de ``layer.walls[oid].inner_area`` et des ``regions`` des couches voisines.

Méthode (code Alpine Makers, µm int64, Paths propres) :
- ``top_solid(i) = inner(i) − ∩_{k=1..Nt} regions(i+k)`` : un point est « dessus » dès qu'une des Nt couches au-dessus
  ne le couvre pas (l'union de la spécification ne donnerait qu'une seule couche pleine sous chaque surface) ;
  ``bottom_solid(i) = inner(i) − ∩_{k=1..Nb} regions(i−k)``. Nt/Nb = nombre de couches, augmenté au besoin pour
  atteindre ``top/bottom_shell_thickness`` (épaisseurs cumulées des couches réelles). Première couche : tout dessous.
- ``bridge = bottom_solid − offset(regions(i−1) près du pont, +ε)`` (ε = demi-largeur de ligne) ; la bande ε qui
  borde le soutien reste en « dessous » (elle est en l'air, mais une ligne pleine y recouvre le soutien de w/2 : c'est
  l'ancrage du pont). Direction : angle par pas de 15° qui maximise la couverture des lignes ancrées aux deux bouts
  (puis à un bout), puis minimise la plus longue portée non soutenue, puis la portée moyenne.
- ``internal_solid`` (coque verticale garantie) : sur les couches voisines d'une bande dessus/dessous, la bande est
  élargie vers l'intérieur de ``(N−k)·w`` (k = écart en couches) ; seule la couronne ajoutée compte (la bande même a
  déjà N couches pleines au-dessus ; l'inclure rendrait pleines les N−1 couches sous chaque dessus plat). Les lamelles
  de creux plus étroites que w laissées entre deux couronnes passent aussi en plein. Les îlots creux d'aire
  < ``minimum_sparse_infill_area`` passent en plein interne.
- ``sparse = inner − top − bottom − bridge − internal_solid``. Les cinq zones sont disjointes (priorité pont > dessus >
  dessous > plein interne > creux) et leur somme vaut ``inner`` : ``intersection(top, bottom)`` ne reste jamais creux.

Performance : couches identiques (prismes) détectées par égalité exacte des Paths → aucune booléenne ; sinon un seul
arrangement de balayage par couche pour dessus / dessous / creux / pont (internes de ``boolean``, repli sur l'API
publique), et les dilatations de pont limitées au voisinage du pont.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .geometry import RESOLUTION_UM, boolean, lines
from .geometry.offset import offset
from .geometry.poly import INT, area2, as_ring, bbox, from_polytree, perimeter_um, simplify, to_polytree
from .layers import Surfaces

# Défauts de la spécification, utilisés seulement si la clé manque dans ``settings``.
DEFAULT_TOP_LAYERS = 4
DEFAULT_BOTTOM_LAYERS = 3
DEFAULT_TOP_THICKNESS_MM = 0.8
DEFAULT_BOTTOM_THICKNESS_MM = 0.0
DEFAULT_WALL_LOOPS = 2
DEFAULT_LINE_WIDTH_MM = 0.42
DEFAULT_MIN_SPARSE_AREA_MM2 = 15.0
DEFAULT_VERTICAL_SHELL = "enabled"
VERTICAL_SHELL_MODES = ("none", "partial", "enabled")
BRIDGE_ANGLE_STEP_DEG = 15
BRIDGE_SCORE_LINES = 48        # lignes de hachure par angle pour le choix de direction (bornées par la largeur de ligne)
MAX_GROUPS = 12                # opérandes par arrangement (comme union_many)

_HAS_INTERNALS = all(hasattr(boolean, n) for n in ("_edges_from_groups", "_subdivide", "_arrangement", "_chain"))


# ---------------------------------------------------------------------------------------------------------------
# Réglages
# ---------------------------------------------------------------------------------------------------------------

def _get(settings, key, default):
    if settings is None:
        return default
    if isinstance(settings, dict):
        value = settings.get(key, default)
    else:
        value = getattr(settings, key, default)
    return default if value is None else value


def _num(settings, key, default) -> float:
    value = _get(settings, key, default)
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        value = value.strip().replace(",", ".").rstrip("%").strip()
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _mode(settings, key, default) -> str:
    value = _get(settings, key, default)
    if isinstance(value, bool):
        return "enabled" if value else "none"
    text = str(value).strip().lower()
    if text in ("1", "true", "on", "oui", "yes"):
        return "enabled"
    if text in ("0", "false", "off", "non", "no"):
        return "none"
    return text if text in VERTICAL_SHELL_MODES else default


def _um(value_mm: float) -> int:
    return int(round(float(value_mm) * 1000.0))


@dataclass(frozen=True)
class Params:
    """Réglages résolus pour la classification (unités internes : µm, radians)."""

    top_layers: int
    bottom_layers: int
    top_thickness_um: int
    bottom_thickness_um: int
    line_width_um: int           # largeur du plein interne (élargissement de coque, marge de pont)
    wall_inset_um: int           # épaisseur des murs : inner_area = regions rétrécies d'autant si walls absent
    vertical_shell: str          # none | partial | enabled
    bridge_angle_rad: float | None   # None = automatique
    min_sparse_area_um2: float
    bridge_margin_um: int        # ε : dilatation du soutien sous un pont

    @staticmethod
    def from_settings(settings) -> "Params":
        nozzle = _num(settings, "nozzle", 0.4)
        line_default = round(1.05 * nozzle, 2) if nozzle > 0 else DEFAULT_LINE_WIDTH_MM
        line = _num(settings, "line_width", line_default)
        solid = _num(settings, "internal_solid_infill_line_width", line)
        outer = _num(settings, "outer_wall_line_width", line)
        inner = _num(settings, "inner_wall_line_width", line)
        loops = max(0, int(round(_num(settings, "wall_loops", DEFAULT_WALL_LOOPS))))
        inset = outer + max(0, loops - 1) * inner if loops else 0.0
        angle = _num(settings, "bridge_angle", 0.0) % 360.0
        return Params(
            top_layers=max(0, int(round(_num(settings, "top_shell_layers", DEFAULT_TOP_LAYERS)))),
            bottom_layers=max(0, int(round(_num(settings, "bottom_shell_layers", DEFAULT_BOTTOM_LAYERS)))),
            top_thickness_um=max(0, _um(_num(settings, "top_shell_thickness", DEFAULT_TOP_THICKNESS_MM))),
            bottom_thickness_um=max(0, _um(_num(settings, "bottom_shell_thickness", DEFAULT_BOTTOM_THICKNESS_MM))),
            line_width_um=max(1, _um(solid)),
            wall_inset_um=max(0, _um(inset)),
            vertical_shell=_mode(settings, "ensure_vertical_shell_thickness", DEFAULT_VERTICAL_SHELL),
            bridge_angle_rad=None if angle == 0.0 else math.radians(angle),
            min_sparse_area_um2=max(0.0, _num(settings, "minimum_sparse_infill_area", DEFAULT_MIN_SPARSE_AREA_MM2)) * 1e6,
            bridge_margin_um=max(1, _um(solid) // 2),
        )


# ---------------------------------------------------------------------------------------------------------------
# Outils Paths
# ---------------------------------------------------------------------------------------------------------------

def _same(a, b) -> bool:
    """Égalité exacte de deux Paths (mêmes rings dans le même ordre)."""
    if a is b:
        return True
    if len(a) != len(b):
        return False
    return all(np.array_equal(np.asarray(x), np.asarray(y)) for x, y in zip(a, b))


def paths_area_um2(paths) -> float:
    """Aire signée totale d'un Paths propre (extérieurs − trous) en µm²."""
    return sum(area2(r) for r in paths) / 2.0


def _rect(x0, y0, x1, y1) -> np.ndarray:
    return np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=INT)


def _winding(points, paths) -> np.ndarray:
    """Indice d'enroulement de chaque point (int64 (p, 2)) autour d'un Paths, vectorisé par blocs."""
    pts = np.asarray(points, dtype=INT).reshape(-1, 2)
    out = np.zeros(len(pts), dtype=INT)
    if not len(pts):
        return out
    for ring in paths:
        r = as_ring(ring)
        if len(r) < 3:
            continue
        x1, y1 = r[:, 0], r[:, 1]
        x2, y2 = np.roll(x1, -1), np.roll(y1, -1)
        step = max(1, (1 << 21) // len(r))
        for c in range(0, len(pts), step):
            px = pts[c:c + step, 0][:, None]
            py = pts[c:c + step, 1][:, None]
            up = (y1 <= py) & (y2 > py)
            down = (y2 <= py) & (y1 > py)
            cross = (x2 - x1) * (py - y1) - (px - x1) * (y2 - y1)
            out[c:c + step] += (up & (cross > 0)).sum(axis=1) - (down & (cross < 0)).sum(axis=1)
    return out


def _combine(groups, specs):
    """Plusieurs résultats booléens sur un même arrangement. ``groups[0]`` est le sujet ; chaque spec est
    ``("minus_all", cols)`` (sujet − ∩cols), ``("all", cols)`` (sujet ∩ ∩cols) ou ``("minus_any", cols)`` (sujet − ∪cols).
    Repli sur l'API publique si les internes de ``boolean`` manquent."""
    if not _HAS_INTERNALS:
        return [_combine_public(groups, spec) for spec in specs]
    T = len(groups)
    P, Q, tag = boolean._edges_from_groups(groups)
    if not len(P):
        return [[] for _ in specs]
    P, Q, tag = boolean._subdivide(P, Q, tag)
    arr = boolean._arrangement(P, Q, tag, T)
    if arr is None:
        return [[] for _ in specs]
    nodes, tail, head, angle, order, label, w, assigned = arr
    flip = label[np.arange(len(label)) ^ 1]
    inside = w > 0
    out = []
    for kind, cols in specs:
        inc = inside[:, 0].copy()
        if kind in ("any_pair", "minus_pairs"):
            # cols = [(g, b), …] : ∪ (g − b)
            hit = np.zeros(len(inc), dtype=bool)
            for g, b in cols:
                hit |= inside[:, g] & ~inside[:, b]
            inc &= hit if kind == "any_pair" else ~hit
        elif cols:
            block = inside[:, list(cols)]
            if kind == "minus_all":
                inc &= ~block.all(axis=1)
            elif kind == "all":
                inc &= block.all(axis=1)
            elif kind == "minus_any":
                inc &= ~block.any(axis=1)
            elif kind == "any":
                inc &= block.any(axis=1)
            else:
                raise ValueError(f"spec inconnue : {kind}")
        elif kind == "any":
            inc[:] = False   # ∪ de rien = vide
        inc &= assigned
        res = inc[label] & ~inc[flip]
        out.append(boolean._chain(nodes, tail, head, angle, order, res) if res.any() else [])
    return out


def _combine_public(groups, spec):
    """Même sémantique que ``_combine`` pour une spec, avec les seules fonctions publiques de ``boolean``."""
    kind, cols = spec
    subject = groups[0]
    if not cols:
        return [] if kind in ("any", "any_pair") else boolean.union(subject)
    if kind in ("any_pair", "minus_pairs"):
        rings = boolean.union_many([boolean.difference(groups[g], groups[b]) for g, b in cols])
        if kind == "any_pair":
            return boolean.intersection(subject, rings) if rings else []
        return boolean.difference(subject, rings) if rings else boolean.union(subject)
    if kind == "minus_any":
        return boolean.difference(subject, boolean.union_many([groups[c] for c in cols]))
    if kind == "any":
        return boolean.intersection(subject, boolean.union_many([groups[c] for c in cols]))
    acc = groups[cols[0]]
    for c in cols[1:]:
        acc = boolean.intersection(acc, groups[c])
        if not acc:
            break
    if kind == "all":
        return boolean.intersection(subject, acc) if acc else []
    if kind == "minus_all":
        return boolean.difference(subject, acc) if acc else boolean.union(subject)
    raise ValueError(f"spec inconnue : {kind}")


# ---------------------------------------------------------------------------------------------------------------
# Direction de pont
# ---------------------------------------------------------------------------------------------------------------

def bridge_direction(bridge, support, line_width_um: int, step_deg: int = BRIDGE_ANGLE_STEP_DEG) -> float:
    """Angle (radians, dans [0, π)) des lignes de pont sur ``bridge`` : parmi les angles par pas de ``step_deg``,
    celui qui maximise la part des lignes ancrées aux deux bouts dans ``support`` (zone soutenue dilatée), puis à un
    bout, puis minimise la plus longue portée non soutenue, puis la portée moyenne ; à égalité, le plus petit angle."""
    box = bbox(bridge)
    if box is None:
        return 0.0
    x0, y0, x1, y1 = (float(v) for v in box)
    diag = math.hypot(x1 - x0, y1 - y0)
    if diag <= 0:
        return 0.0
    spacing = max(float(line_width_um), diag / BRIDGE_SCORE_LINES)
    probe = max(10.0, line_width_um / 4.0)
    quantum = max(1.0, 0.02 * diag)
    best_key, best_angle = None, 0.0
    corners = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.float64)
    for deg in range(0, 180, max(1, int(step_deg))):
        ang = math.radians(deg)
        c, s = math.cos(ang), math.sin(ang)
        nmin = float((-corners[:, 0] * s + corners[:, 1] * c).min())
        phase = (nmin % spacing) + spacing / 2.0   # première ligne à une demi-maille du bord
        seg = lines.clip_segments(lines.hatch(box, ang, spacing, phase), bridge)
        if not len(seg):
            continue
        d = (seg[:, 1] - seg[:, 0]).astype(np.float64)
        length = np.hypot(d[:, 0], d[:, 1])
        keep = length > 0
        if not keep.any():
            continue
        seg, d, length = seg[keep], d[keep], length[keep]
        u = d / length[:, None]
        if support:
            p0 = np.rint(seg[:, 0] - u * probe).astype(INT)
            p1 = np.rint(seg[:, 1] + u * probe).astype(INT)
            anchors = (_winding(p0, support) > 0).astype(np.int64) + (_winding(p1, support) > 0).astype(np.int64)
        else:
            anchors = np.zeros(len(seg), dtype=np.int64)
        total = float(length.sum())
        cover2 = float(length[anchors == 2].sum()) / total
        cover1 = float(length[anchors >= 1].sum()) / total
        ranked = length[anchors >= 1] if (anchors >= 1).any() else length
        key = (-round(cover2, 2), -round(cover1, 2), round(float(ranked.max()) / quantum),
               round(float(ranked.mean()) / quantum), deg)
        if best_key is None or key < best_key:
            best_key, best_angle = key, ang
    return best_angle


# ---------------------------------------------------------------------------------------------------------------
# Pile d'un objet
# ---------------------------------------------------------------------------------------------------------------

class _Stack:
    """Accès aux couches d'un objet avec caches (zones internes, résultats de la passe 1)."""

    def __init__(self, layers, object_id, params: Params):
        self.layers = layers
        self.oid = object_id
        self.p = params
        self.n = len(layers)
        self._inner = {}
        self._pass1 = {}

    # -- données d'entrée ------------------------------------------------------------------------------------------
    def regions(self, i):
        if i < 0 or i >= self.n:
            return []
        paths = self.layers[i].regions.get(self.oid)
        return list(paths) if paths else []

    def has_walls(self, i) -> bool:
        return self.oid in self.layers[i].walls

    def inner(self, i):
        """Zone de remplissage : ``walls.inner_area`` sinon regions rétrécies de l'épaisseur des murs (cache par
        égalité avec la couche précédente)."""
        if i in self._inner:
            return self._inner[i]
        walls = self.layers[i].walls.get(self.oid)
        if walls is not None:
            area = getattr(walls, "inner_area", None) or []
            result = list(area)
        else:
            regions = self.regions(i)
            if not regions:
                result = []
            elif i - 1 in self._inner and not self.has_walls(i - 1) and _same(regions, self.regions(i - 1)):
                result = self._inner[i - 1]
            elif self.p.wall_inset_um > 0:
                result = offset(regions, -self.p.wall_inset_um, resolve=True)
            else:
                result = boolean.union(regions)
        self._inner[i] = result
        return result

    # -- nombre de couches pleines -------------------------------------------------------------------------------
    def shell_count(self, i, count: int, thickness_um: int, direction: int) -> int:
        """``count`` augmenté pour que l'épaisseur cumulée des couches voisines atteigne ``thickness_um`` (0 = ignoré)."""
        if count <= 0 or thickness_um <= 0:
            return max(0, count)
        own = max(1, int(self.layers[i].height_um))
        total, m, j = 0, 0, i
        while total < thickness_um - 1:
            j += direction
            total += int(self.layers[j].height_um) if 0 <= j < self.n else own
            m += 1
        return max(count, m)

    # -- passe 1 : dessus, dessous, pont, creux brut -------------------------------------------------------------
    def pass1(self, i):
        if i in self._pass1:
            return self._pass1[i]
        result = self._compute_pass1(i)
        self._pass1[i] = result
        return result

    def _compute_pass1(self, i):
        inner = self.inner(i)
        empty = {"top": [], "bottom": [], "bridge": [], "sparse": [], "angle": 0.0}
        if not inner:
            return empty
        if i == 0 or self.layers[i].index == 1:
            return {"top": [], "bottom": inner, "bridge": [], "sparse": [], "angle": 0.0}
        current = self.regions(i)
        nt = self.shell_count(i, self.p.top_layers, self.p.top_thickness_um, +1)
        nb = self.shell_count(i, self.p.bottom_layers, self.p.bottom_thickness_um, -1)

        groups, cols = [inner], {}

        def column(paths):
            for key, (ref, c) in cols.items():
                if _same(ref, paths):
                    return c
            if len(groups) >= MAX_GROUPS:
                return None
            groups.append(paths)
            cols[len(cols)] = (paths, len(groups) - 1)
            return len(groups) - 1

        def status(neighbours):
            """'all' : une couche vide → la zone entière ; 'none' : toutes identiques à la couche courante ;
            sinon la liste des colonnes (couches identiques dédoublonnées)."""
            columns = []
            for paths in neighbours:
                if not paths:
                    return "all"
                if current and _same(paths, current):
                    continue
                c = column(paths)
                if c is None:
                    return "overflow", columns
                if c not in columns:
                    columns.append(c)
            return "none" if not columns else columns

        # nt = 0 (ou nb = 0) : aucune couche pleine de ce côté, comme si tout était couvert ("none").
        st_a = status([self.regions(i + k) for k in range(1, nt + 1)]) if nt else "none"
        st_b = status([self.regions(i - k) for k in range(1, nb + 1)]) if nb else "none"
        if isinstance(st_a, tuple) or isinstance(st_b, tuple):
            return self._pass1_public(i, inner, current, nt, nb)

        prev = self.regions(i - 1)
        specs, want = [], {}
        top = bottom = sparse = bridge_raw = None
        if st_a == "all":
            top = inner
        elif st_a == "none":
            top = []
        else:
            want["top"] = len(specs)
            specs.append(("minus_all", tuple(st_a)))
        if st_b == "all":
            bottom = inner
        elif st_b == "none":
            bottom = []
        else:
            want["bottom"] = len(specs)
            specs.append(("minus_all", tuple(st_b)))
        if st_a == "all" or st_b == "all":
            sparse = []
        else:
            ca = [] if st_a == "none" else list(st_a)
            cb = [] if st_b == "none" else list(st_b)
            if not ca and not cb:
                sparse = inner
            else:
                want["sparse"] = len(specs)
                specs.append(("all", tuple(ca + cb)))
        # Pont brut : partie du dessous sans rien juste en dessous.
        if bottom == [] or nb == 0:
            bridge_raw = []
        elif not prev:
            bridge_raw = None if bottom is None else bottom   # tout le dessous est en l'air
            if bottom is None:
                want["bridge_raw"] = want["bottom"]
        elif current and _same(prev, current):
            bridge_raw = []
        else:
            c = column(prev)
            if c is None:
                return self._pass1_public(i, inner, current, nt, nb)
            want["bridge_raw"] = len(specs)
            specs.append(("minus_all", (c,)))
        if specs:
            results = _combine(groups, specs)
            if top is None:
                top = results[want["top"]]
            if bottom is None:
                bottom = results[want["bottom"]]
            if sparse is None:
                sparse = results[want["sparse"]]
            if bridge_raw is None:
                bridge_raw = results[want["bridge_raw"]]
        return self._finish_pass1(i, inner, top, bottom, sparse, bridge_raw, prev)

    def _pass1_public(self, i, inner, current, nt, nb):
        """Chemin de repli (trop d'opérandes) : opérations publiques enchaînées, même sémantique que la passe rapide."""
        def meet(neighbours):
            """∩ des voisines ; ``[]`` si l'une est vide (rien ne couvre) ; ``None`` si toutes sont identiques à la
            couche courante (tout est couvert : la zone interne est incluse dans la coupe courante)."""
            acc = None
            for paths in neighbours:
                if not paths:
                    return []
                if current and _same(paths, current):
                    continue
                acc = paths if acc is None else boolean.intersection(acc, paths)
                if not acc:
                    return []
            return acc

        cover_a = meet([self.regions(i + k) for k in range(1, nt + 1)]) if nt else None
        cover_b = meet([self.regions(i - k) for k in range(1, nb + 1)]) if nb else None

        def uncovered(cover):
            if cover is None:
                return []
            return boolean.difference(inner, cover) if cover else inner

        top, bottom = uncovered(cover_a), uncovered(cover_b)
        if (cover_a is not None and not cover_a) or (cover_b is not None and not cover_b):
            sparse = []
        else:
            sparse = inner
            for cover in (cover_a, cover_b):
                if cover:
                    sparse = boolean.intersection(sparse, cover)
        prev = self.regions(i - 1)
        if not bottom or nb == 0:
            bridge_raw = []
        elif not prev:
            bridge_raw = bottom
        else:
            bridge_raw = boolean.difference(bottom, prev)
        return self._finish_pass1(i, inner, top, bottom, sparse, bridge_raw, prev)

    def _finish_pass1(self, i, inner, top, bottom, sparse, bridge_raw, prev):
        bridge, angle = [], 0.0
        if bridge_raw:
            support = self._support_near(prev, bridge_raw)
            bridge = boolean.difference(bridge_raw, support) if support else bridge_raw
            if bridge:
                bottom = boolean.difference(bottom, bridge)
                angle = self.p.bridge_angle_rad
                if angle is None:
                    angle = bridge_direction(bridge, support, self.p.line_width_um)
                if top:
                    top = boolean.difference(top, bridge)
        if top and bottom:
            bottom = boolean.difference(bottom, top)   # priorité au dessus sur les plaques fines
        return {"top": top, "bottom": bottom, "bridge": bridge, "sparse": sparse, "angle": float(angle or 0.0)}

    def _support_near(self, prev, bridge_raw):
        """Régions de la couche inférieure près du pont, dilatées de ε (zone qui soutient)."""
        if not prev:
            return []
        eps = self.p.bridge_margin_um
        margin = eps + max(10, self.p.line_width_um) + 2
        box, pbox = bbox(bridge_raw), bbox(prev)
        if box is None or pbox is None:
            return []   # rings vides ou dégénérés : rien ne soutient
        x0, y0, x1, y1 = box
        px0, py0, px1, py1 = pbox
        if px0 >= x0 - margin and py0 >= y0 - margin and px1 <= x1 + margin and py1 <= y1 + margin:
            near, external = prev, True
        else:
            near, external = boolean.intersection(prev, [_rect(x0 - margin, y0 - margin, x1 + margin, y1 + margin)]), False
        if not near:
            return []
        return offset(near, eps, resolve=external)

    # -- passe 2 : plein interne, petits îlots, creux ------------------------------------------------------------
    def classify(self, i) -> Surfaces:
        base = self.pass1(i)
        inner = self.inner(i)
        if not inner:
            return Surfaces()
        top, bottom, bridge, sparse = base["top"], base["bottom"], base["bridge"], base["sparse"]
        internal = []
        if sparse:
            internal, sparse = self._vertical_shell(i, sparse)
            if internal and sparse:
                internal, sparse = self._merge_slivers(internal, sparse)
            if sparse and self.p.min_sparse_area_um2 > 0:
                keep, moved = [], []
                for outer, holes in to_polytree(sparse):
                    area = abs(area2(outer)) / 2.0 - sum(abs(area2(h)) for h in holes) / 2.0
                    (moved if area < self.p.min_sparse_area_um2 else keep).append((outer, holes))
                if moved:
                    sparse = from_polytree(keep)
                    small = from_polytree(moved)
                    internal = boolean.union(internal + small) if internal else small
        return Surfaces(top_solid=top, bottom_solid=bottom, internal_solid=internal, bridge=bridge,
                        bridge_angle_rad=base["angle"], sparse=sparse)

    def _merge_slivers(self, internal, sparse):
        """Les îlots de creux de largeur moyenne ≤ w (aire ≤ w · périmètre / 2) laissés entre les couronnes de la coque
        verticale ne peuvent pas recevoir de remplissage creux : ils rejoignent le plein interne (union, donc les
        bandes voisines fusionnent). Renvoie ``(plein interne, creux restant)``."""
        w = float(self.p.line_width_um)
        keep, moved = [], []
        for outer, holes in to_polytree(sparse):
            area = abs(area2(outer)) / 2.0 - sum(abs(area2(h)) for h in holes) / 2.0
            perimeter = perimeter_um(outer) + sum(perimeter_um(h) for h in holes)
            (moved if area <= w * perimeter / 2.0 else keep).append((outer, holes))
        if not moved:
            return internal, sparse
        return boolean.union(internal + from_polytree(moved)), from_polytree(keep)

    def _vertical_shell(self, i, sparse):
        """Coque verticale garantie : part du creux située dans la couronne ``offset(bande, +(N−k)·w) − bande`` des
        bandes dessus (couches i+k) et dessous (couches i−k). Renvoie ``(plein interne, creux restant)``.
        La bande est simplifiée (Douglas-Peucker à d/8) avant le décalage, qui coûte O(sommets · d / arête) ; la
        soustraction de la bande exacte et l'intersection avec le creux restent exactes, en un seul balayage."""
        mode = self.p.vertical_shell
        if mode == "none":
            return [], sparse
        w = self.p.line_width_um
        nt = self.shell_count(i, self.p.top_layers, self.p.top_thickness_um, +1)
        nb = self.shell_count(i, self.p.bottom_layers, self.p.bottom_thickness_um, -1)
        ks_top = range(1, nt) if mode == "enabled" else range(1, min(nt, 2))
        ks_bot = range(1, nb) if mode == "enabled" else range(1, min(nb, 2))
        candidates = []   # (bande, distance, bande = zone interne entière de sa couche)
        for k in ks_top:
            if i + k < self.n:
                band = self.pass1(i + k)["top"]
                candidates.append((band, (nt - k) * w, band is self.inner(i + k)))
        for k in ks_bot:
            if i - k >= 0:
                base = self.pass1(i - k)
                whole = base["bottom"] is self.inner(i - k)
                band = base["bottom"] + base["bridge"] if base["bridge"] else base["bottom"]
                candidates.append((band, (nb - k) * w, whole))
        sbox = bbox(sparse)
        inner_i = self.inner(i)
        pairs = []
        for band, d, whole in candidates:
            if not band or d <= 0 or sbox is None:
                continue
            if whole and _same(band, inner_i):
                continue   # couches identiques : la couronne est hors de la zone interne
            x0, y0, x1, y1 = bbox(band)
            if x1 + d < sbox[0] or x0 - d > sbox[2] or y1 + d < sbox[1] or y0 - d > sbox[3]:
                continue
            tol = max(RESOLUTION_UM, d / 8.0)
            rough = [s for s in (simplify(r, tol) for r in band) if len(s) >= 3]
            grown = offset(rough, d, resolve=True) if rough else []
            if grown:
                pairs.append((grown, band))
        if not pairs:
            return [], sparse
        per_sweep = max(1, (MAX_GROUPS - 1) // 2)   # paires (élargie, bande) par balayage
        parts = []
        for start in range(0, len(pairs), per_sweep):
            chunk = pairs[start:start + per_sweep]
            groups, cols = [sparse], []
            for grown, band in chunk:
                groups.extend((grown, band))
                cols.append((len(groups) - 2, len(groups) - 1))
            if len(pairs) <= per_sweep:
                internal, rest = _combine(groups, [("any_pair", cols), ("minus_pairs", cols)])
                return (internal, rest) if internal else ([], sparse)
            parts.append(_combine(groups, [("any_pair", cols)])[0])
        internal = boolean.union_many([p for p in parts if p])
        if not internal:
            return [], sparse
        return internal, boolean.difference(sparse, internal)


# ---------------------------------------------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------------------------------------------

def _object_ids(layers):
    seen = []
    for layer in layers:
        for oid in list(layer.regions) + list(layer.walls):
            if oid not in seen:
                seen.append(oid)
    return seen


def classify(layers, settings) -> None:
    """Remplit ``layer.surfaces[object_id]`` (``Surfaces``) pour toutes les couches et tous les objets présents."""
    params = Params.from_settings(settings)
    for oid in _object_ids(layers):
        stack = _Stack(layers, oid, params)
        for i, layer in enumerate(layers):
            if oid in layer.regions or oid in layer.walls:
                layer.surfaces[oid] = stack.classify(i)


def classify_layer(layers, i: int, settings, object_id=1) -> Surfaces:
    """Surfaces de la couche en position ``i`` (0 = première de la liste) pour un objet, sans modifier ``layers``."""
    if not (0 <= i < len(layers)):
        raise IndexError(f"couche {i} hors de la pile ({len(layers)} couches)")
    stack = _Stack(layers, object_id, Params.from_settings(settings))
    return stack.classify(i)


__all__ = ["Params", "classify", "classify_layer", "bridge_direction", "paths_area_um2"]
