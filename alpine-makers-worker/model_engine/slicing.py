"""Découpe des maillages en couches (spécification §5.1 de ``docs/MODEL_ENGINE_ARCHITECTURE.md``).

Méthode (code Alpine Makers, numpy + bibliothèque standard) :
1. ``layer_heights`` : pile de couches (première couche à part, dernière couche bornée à z_max ± 1 µm).
2. ``slice_mesh`` : intersection triangle–plan vectorisée. Les faces sont rangées par z min / z max ; chaque
   paire (triangle, plan) avec ``z_min < z ≤ z_max`` donne un segment : le sommet « isolé » (seul de son côté)
   et les deux arêtes qui en partent sont interpolées en flottant puis arrondies au µm. Convention sur le plan :
   un sommet avec ``z == plan`` compte comme « au-dessus », ce qui traite les sommets posés sur le plan sans
   doublon et ignore les triangles coplanaires. Le segment est orienté solide à gauche à partir de la normale
   du triangle (faces orientées vers l'extérieur) : extérieurs CCW, trous CW.
3. Chaînage des segments en boucles par clés de sommets (dict de voisinage vectorisé par ``searchsorted``),
   fermeture des boucles ouvertes dont l'écart est ≤ ``gap_closing_um`` (maillages non étanches) : les morceaux
   sont rejoints bout à bout dans les deux sens (une face isolée à l'envers donne un morceau inversé), la boucle
   obtenue est orientée par la longueur majoritaire des morceaux pris en sens direct ; abandon comptabilisé des
   autres, simplification Douglas–Peucker (``resolution_um``), nettoyage puis ``union`` pour résoudre
   recouvrements et coques accolées. Si un maillage a toutes ses normales rentrées, la coupe est vide après
   union : les boucles sont alors inversées une fois (avertissement).
4. ``slice_scene`` : construit les ``Layer`` de ``layers.py`` (z de coupe = milieu de la couche). Les volumes
   ``modifier`` / ``support_blocker`` / ``support_enforcer`` sont découpés mais rangés dans ``Layer.regions``
   sous des **clés négatives** (``volume_key(object_id, volume_type)``, décodage ``decode_volume_key``), jamais
   dans ``all_regions`` ; les clés positives sont les objets du modèle (position 1..n dans la scène), toujours
   présentes (liste vide si l'objet est absent de la couche). La découpe se fait par bandes de plans pour une
   progression régulière.

Unités : µm entiers (int64) ; mm seulement en entrée (sommets, réglages). Déterministe : même entrée → même sortie.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .geometry import RESOLUTION_UM, SCALE, boolean, poly
from .layers import Layer
from .mesh import Mesh
from .scene import Scene

INT = np.int64
DEFAULT_GAP_CLOSING_UM = 100         # réglage ``slice_gap_closing_radius`` (0,10 mm)
DEFAULT_LAYER_MM = 0.2
Z_TOLERANCE_UM = 1                   # dépassement toléré de z_max par la dernière couche
MIN_LAST_LAYER_RATIO = 0.25          # couche résiduelle au sommet plus fine que 25 % de la couche : fusionnée
PAIR_CHUNK = 1_000_000               # paires (triangle, plan) par bloc : mémoire bornée (≈ 150 Mo par bloc)
MAX_OPEN_CHAINS = 20_000             # au-delà, la fermeture des boucles ouvertes est abandonnée (maillage ruiné)
PROGRESS_BAND = 100                  # plans par appel de slice_mesh dans slice_scene (progression régulière)
_KEY_OFFSET = 1 << 30                # |x|, |y| < 2^30 µm (≈ 1 km) pour les clés de sommets
_KEY_MULT = 1 << 31
VOLUME_CODES = {"modifier": 1, "support_blocker": 2, "support_enforcer": 3}
_CODE_VOLUMES = {code: name for name, code in VOLUME_CODES.items()}
_EMPTY_SEG = np.zeros((0, 2, 2), dtype=INT)

__all__ = ["DEFAULT_GAP_CLOSING_UM", "SliceStats", "layer_heights", "slice_mesh", "slice_scene", "volume_key",
           "decode_volume_key", "model_object_ids", "volume_regions"]


# ---------------------------------------------------------------------------------------------- clés des volumes

def volume_key(object_id: int, volume_type: str) -> int:
    """Clé de ``Layer.regions`` d'un volume non imprimé (négative) ; ``object_id`` lui-même pour ``normal``."""
    if volume_type == "normal":
        return int(object_id)
    code = VOLUME_CODES.get(volume_type)
    if code is None:
        raise ValueError(f"type de volume inconnu : {volume_type!r}")
    return -(int(object_id) * 4 + code)


def decode_volume_key(key: int) -> tuple[int, str]:
    """``(object_id, volume_type)`` d'une clé de ``Layer.regions``."""
    key = int(key)
    if key > 0:
        return key, "normal"
    k = -key
    return k // 4, _CODE_VOLUMES.get(k % 4, "normal")


def model_object_ids(layer: Layer) -> list[int]:
    """Identifiants des objets du modèle (clés positives), triés."""
    return sorted(int(k) for k in layer.regions if int(k) > 0)


def volume_regions(layer: Layer, object_id: int, volume_type: str) -> list:
    """Paths d'un volume (``normal`` ou non) pour une couche ; liste vide si absent."""
    return layer.regions.get(volume_key(object_id, volume_type), [])


# ---------------------------------------------------------------------------------------------- pile de couches

def layer_heights(z_max_mm, layer_mm, first_layer_mm=None) -> list[tuple[int, int]]:
    """Couches ``(z_bas_um, z_haut_um)`` : la première de ``first_layer_mm`` (défaut : ``layer_mm``), les suivantes
    de ``layer_mm`` ; la dernière est ramenée à z_max si elle le dépasse de plus de 1 µm. Une couche résiduelle
    plus fine que ``MIN_LAST_LAYER_RATIO`` × ``layer_mm`` est fusionnée avec la précédente (qui monte alors
    jusqu'à z_max : au plus 1,25 couche). Vide si z_max ≤ 1 µm."""
    z_max = int(round(float(z_max_mm) * SCALE))
    layer = int(round(float(layer_mm) * SCALE))
    first = layer if first_layer_mm is None else int(round(float(first_layer_mm) * SCALE))
    if layer <= 0 or first <= 0:
        raise ValueError("hauteur de couche invalide (doit être > 0)")
    if z_max <= Z_TOLERANCE_UM:
        return []
    if first >= z_max - Z_TOLERANCE_UM:
        return [(0, min(first, z_max) if first > z_max + Z_TOLERANCE_UM else first)]
    count = 1 + int(math.ceil((z_max - Z_TOLERANCE_UM - first) / layer))
    tops = first + layer * np.arange(count, dtype=INT)
    if int(tops[-1]) > z_max + Z_TOLERANCE_UM:
        tops[-1] = z_max
    if count >= 2 and int(tops[-1] - tops[-2]) < layer * MIN_LAST_LAYER_RATIO:
        tops = tops[:-1]                                       # résidu trop fin : la couche précédente monte à z_max
        tops[-1] = z_max
    bottoms = np.concatenate([[0], tops[:-1]])
    return [(int(b), int(t)) for b, t in zip(bottoms.tolist(), tops.tolist())]


# ---------------------------------------------------------------------------------------------- statistiques

@dataclass
class SliceStats:
    """Comptes d'une découpe (``slice_mesh(..., stats=)``) ; ``warnings`` : index de plan → messages."""

    segments: int = 0
    loops: int = 0
    gaps_closed: int = 0
    loops_dropped: int = 0
    pieces_reversed: int = 0
    flipped_planes: int = 0
    unresolved_planes: int = 0
    warnings: dict = field(default_factory=dict)

    def warn(self, plane: int, message: str) -> None:
        self.warnings.setdefault(int(plane), []).append(message)


# ---------------------------------------------------------------------------------------------- triangle–plan

def _plane_segments(vertices_um: np.ndarray, faces: np.ndarray, zs: np.ndarray) -> list:
    """Segments de coupe int64 ``(k, 2, 2)`` par plan (``zs`` trié, flottants µm), solide à gauche."""
    nplanes = len(zs)
    buckets = [[] for _ in range(nplanes)]
    m = len(faces)
    if m == 0 or nplanes == 0:
        return [_EMPTY_SEG] * nplanes
    tri = vertices_um[faces]                                   # (m, 3, 3)
    tz = tri[:, :, 2]
    lo = np.searchsorted(zs, tz.min(axis=1), side="right")     # premier plan > z_min
    hi = np.searchsorted(zs, tz.max(axis=1), side="right")     # premier plan > z_max
    counts = np.maximum(hi - lo, 0)
    normal = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])[:, :2]
    counts[(normal == 0).all(axis=1)] = 0                      # triangles dégénérés ou horizontaux
    cum = np.cumsum(counts)
    start = 0
    while start < m:
        base = int(cum[start - 1]) if start else 0
        end = int(np.searchsorted(cum, base + PAIR_CHUNK, side="right"))
        end = min(max(end, start + 1), m)
        c = counts[start:end]
        p = int(c.sum())
        if p == 0:
            start = end
            continue
        fi = np.repeat(np.arange(start, end), c)
        offs = np.repeat(np.cumsum(c) - c, c)
        pi = lo[fi] + (np.arange(p) - offs)
        zp = zs[pi]
        t = tri[fi]
        above = t[:, :, 2] >= zp[:, None]
        lone = np.where(above.sum(axis=1) == 1, above.argmax(axis=1), (~above).argmax(axis=1))
        rows = np.arange(p)
        pl = t[rows, lone]
        p1 = t[rows, (lone + 1) % 3]
        p2 = t[rows, (lone + 2) % 3]
        t1 = (zp - pl[:, 2]) / (p1[:, 2] - pl[:, 2])
        t2 = (zp - pl[:, 2]) / (p2[:, 2] - pl[:, 2])
        a = pl[:, :2] + t1[:, None] * (p1[:, :2] - pl[:, :2])
        b = pl[:, :2] + t2[:, None] * (p2[:, :2] - pl[:, :2])
        n = normal[fi]
        d = b - a
        s = n[:, 0] * d[:, 1] - n[:, 1] * d[:, 0]               # > 0 : solide à gauche de a → b
        swap = (s < 0)[:, None]
        seg = np.rint(np.stack([np.where(swap, b, a), np.where(swap, a, b)], axis=1)).astype(INT)
        keep = (s != 0) & (seg[:, 0] != seg[:, 1]).any(axis=1)
        seg, pi = seg[keep], pi[keep]
        order = np.argsort(pi, kind="stable")
        seg, pi = seg[order], pi[order]
        edges = np.searchsorted(pi, np.arange(nplanes + 1))
        for j in np.nonzero(edges[1:] > edges[:-1])[0].tolist():
            buckets[j].append(seg[edges[j]:edges[j + 1]])
        start = end
    return [np.concatenate(b) if len(b) > 1 else (b[0] if b else _EMPTY_SEG) for b in buckets]


# ---------------------------------------------------------------------------------------------- chaînage

def _keys(points: np.ndarray) -> np.ndarray:
    return (points[:, 0] + _KEY_OFFSET) * _KEY_MULT + (points[:, 1] + _KEY_OFFSET)


def _chain(seg: np.ndarray, gap_um: int) -> tuple[list, int, int, int]:
    """Boucles fermées (rings int64) à partir des segments ;
    ``(rings, écarts refermés, boucles abandonnées, morceaux pris à l'envers)``."""
    n = len(seg)
    if n == 0:
        return [], 0, 0, 0
    ks, ke = _keys(seg[:, 0]), _keys(seg[:, 1])
    order = np.lexsort((ke, ks))
    dup = np.zeros(n, dtype=bool)
    dup[1:] = (ks[order][1:] == ks[order][:-1]) & (ke[order][1:] == ke[order][:-1])
    if dup.any():                                              # segments identiques (faces dupliquées)
        keep = np.ones(n, dtype=bool)
        keep[order[dup]] = False
        seg, ks, ke = seg[keep], ks[keep], ke[keep]
        n = len(seg)
    so = np.argsort(ks, kind="stable")
    kss = ks[so]
    pos = np.minimum(np.searchsorted(kss, ke), n - 1)
    ok = kss[pos] == ke
    nxt = np.where(ok, so[pos], -1)
    has_pred = np.zeros(n, dtype=bool)
    has_pred[nxt[ok]] = True
    nxt_l = nxt.tolist()
    used = [False] * n
    loops, opens = [], []
    for candidates in (np.nonzero(~has_pred)[0].tolist(), range(n)):
        for i in candidates:
            if used[i]:
                continue
            chain = [i]
            used[i] = True
            j = nxt_l[i]
            while j >= 0 and not used[j]:
                used[j] = True
                chain.append(j)
                j = nxt_l[j]
            (loops if j == i else opens).append(chain)
    rings = [seg[chain, 0] for chain in loops]
    if not opens:
        return rings, 0, 0, 0
    polylines = [np.vstack([seg[chain, 0], seg[chain[-1]:chain[-1] + 1, 1]]) for chain in opens]
    closed, dropped, reversed_ = _close_open_chains(polylines, rings, gap_um)
    return rings, closed, dropped, reversed_


def _polyline_length(points: np.ndarray) -> float:
    d = np.diff(points, axis=0).astype(np.float64)
    return float(np.sqrt((d * d).sum(axis=1)).sum())


def _close_open_chains(polylines: list, rings: list, gap_um: int) -> tuple[int, int, int]:
    """Rejoint bout à bout les polylignes ouvertes (écart ≤ gap), par leur départ ou, à l'envers, par leur fin
    (face isolée inversée) ; la boucle obtenue est orientée par la longueur majoritaire des morceaux pris en sens
    direct. Ajoute les boucles à ``rings`` ; ``(écarts refermés, boucles abandonnées, morceaux pris à l'envers)``.
    Recherche vectorisée du plus proche voisin (masque) : O(n²) numpy, borné par ``MAX_OPEN_CHAINS``."""
    n = len(polylines)
    if n > MAX_OPEN_CHAINS:
        return 0, n, 0
    starts = np.array([p[0] for p in polylines], dtype=INT)
    ends = np.array([p[-1] for p in polylines], dtype=INT)
    lengths = [_polyline_length(p) for p in polylines]
    avail = np.ones(n, dtype=bool)
    gap2 = float(gap_um) ** 2
    closed = dropped = reversed_ = 0
    for i in range(n):
        if not avail[i]:
            continue
        avail[i] = False
        cur, forward, backward = polylines[i], lengths[i], 0.0
        n_forward, n_backward = 1, 0                             # morceaux pris dans le sens de la chaîne / à l'envers
        while True:
            end = cur[-1]
            d_self = float(((cur[0] - end) ** 2).sum())
            d_start = np.where(avail, ((starts - end) ** 2).sum(axis=1), np.inf)
            d_end = np.where(avail, ((ends - end) ** 2).sum(axis=1), np.inf)
            js, je = int(np.argmin(d_start)), int(np.argmin(d_end))
            best_s, best_e = float(d_start[js]), float(d_end[je])
            best = min(best_s, best_e)
            if d_self <= gap2 and d_self <= best:
                ring = cur[:-1] if d_self == 0 else cur
                if len(ring) >= 3:
                    flip = backward > forward                   # la boucle suit la longueur majoritaire
                    rings.append(np.ascontiguousarray(ring[::-1] if flip else ring))
                    closed += int(d_self > 0)
                    reversed_ += n_forward if flip else n_backward
                else:
                    dropped += 1
                break
            if best <= gap2:
                if best_s <= best_e:                            # jonction directe : fin → départ de l'autre
                    other, forward, n_forward = polylines[js], forward + lengths[js], n_forward + 1
                    avail[js] = False
                else:                                           # jonction inversée : fin → fin de l'autre
                    other, backward, n_backward = polylines[je][::-1], backward + lengths[je], n_backward + 1
                    avail[je] = False
                cur = np.vstack([cur, other[1:] if best == 0 else other])
                closed += int(best > 0)
                continue
            dropped += 1
            break
    return closed, dropped, reversed_


# ---------------------------------------------------------------------------------------------- résolution

def _resolve(rings: list, resolution_um: int) -> tuple[list, bool, bool]:
    """Simplifie (tolérance ≥ 1 µm : bruit d'arrondi des diagonales), nettoie et unit les boucles ;
    ``(paths, normales inversées, union impossible)``."""
    paths = []
    tolerance = max(1, int(resolution_um))
    for ring in rings:
        if len(ring) > 3:
            ring = poly.simplify(ring, tolerance)
        cleaned = poly.clean(ring, 1)
        if cleaned is not None:
            paths.append(cleaned)
    if not paths:
        return [], False, False
    try:
        result = boolean.union(paths)
        if not result and sum(poly.area2(r) for r in paths) < 0:
            result = boolean.union([poly.reverse(r) for r in paths])
            return result, True, False
        return result, False, False
    except ValueError:                                         # coordonnées hors de la borne du module booléen
        return paths, False, True


def slice_mesh(mesh: Mesh, z_list_um, *, gap_closing_um: int = DEFAULT_GAP_CLOSING_UM,
               resolution_um: int = RESOLUTION_UM, stats: SliceStats | None = None) -> list:
    """Coupe ``mesh`` (mm) à chaque z de ``z_list_um`` : une entrée ``Paths`` propre par z (ordre d'entrée)."""
    z = np.asarray(z_list_um, dtype=INT).reshape(-1)
    count = len(z)
    if count == 0:
        return []
    order = np.argsort(z, kind="stable")
    zs = z[order].astype(np.float64)
    vertices = np.asarray(mesh.vertices, dtype=np.float64) * SCALE
    faces = np.asarray(mesh.faces, dtype=INT)
    if len(vertices) and float(np.abs(vertices[:, :2]).max()) >= _KEY_OFFSET - 1:
        raise ValueError("maillage hors de ±1 m en X/Y : impossible à découper")
    gap = max(0, int(gap_closing_um))
    resolution = max(1, int(resolution_um))
    per_plane = _plane_segments(vertices, faces, zs)
    result = [[] for _ in range(count)]
    for k, seg in enumerate(per_plane):
        rings, closed, dropped, reversed_ = _chain(seg, gap)
        paths, flipped, unresolved = _resolve(rings, resolution)
        index = int(order[k])
        result[index] = paths
        if stats is None:
            continue
        stats.segments += len(seg)
        stats.loops += len(rings)
        stats.gaps_closed += closed
        stats.loops_dropped += dropped
        stats.pieces_reversed += reversed_
        if closed:
            stats.warn(index, f"{closed} boucle(s) refermée(s) (écart ≤ {gap} µm, maillage non étanche)")
        if reversed_:
            stats.warn(index, f"{reversed_} morceau(x) de contour pris à l'envers (faces inversées)")
        if dropped:
            stats.warn(index, f"{dropped} boucle(s) ouverte(s) abandonnée(s) (écart > {gap} µm)")
        if flipped:
            stats.flipped_planes += 1
            stats.warn(index, "normales rentrées : boucles inversées")
        if unresolved:
            stats.unresolved_planes += 1
            stats.warn(index, "recouvrements non résolus (coordonnées hors de ±524 mm)")
    return result


# ---------------------------------------------------------------------------------------------- scène

def _setting(settings: dict, keys: tuple, default: float) -> float:
    for key in keys:
        value = settings.get(key)
        if value is not None and value != "":
            try:
                return float(value)
            except (TypeError, ValueError):
                raise ValueError(f"réglage {key} invalide : {value!r}") from None
    return float(default)


def _merge(parts: list) -> list:
    parts = [p for p in parts if p]
    if not parts:
        return []
    if len(parts) == 1:
        return list(parts[0])
    return boolean.union_many(parts)


def slice_scene(scene_or_meshes, settings: dict | None = None, *, progress=None) -> list[Layer]:
    """Découpe une ``Scene`` (ou une liste de ``Mesh``) en ``Layer`` selon ``settings`` (clés Alpine :
    ``layer_height``, ``initial_layer_print_height``, ``resolution``, ``slice_gap_closing_radius``, en mm).

    Objets = groupes de la scène (parts d'un même ``object_id`` 3MF réunies), ``object_id`` = position 1..n ;
    les parts ``normal`` d'un objet sont unies, les autres volumes vont sous ``volume_key``. ``all_regions`` =
    union des objets du modèle. ``progress(fraction, message)`` est appelé par objet s'il est fourni."""
    settings = dict(settings or {})
    scene = scene_or_meshes if isinstance(scene_or_meshes, Scene) else Scene(objects=list(scene_or_meshes))
    groups = scene.groups()
    layer_mm = _setting(settings, ("layer_height",), DEFAULT_LAYER_MM)
    first_mm = _setting(settings, ("initial_layer_print_height", "first_layer_height"), layer_mm)
    resolution_um = int(round(_setting(settings, ("resolution",), RESOLUTION_UM / SCALE) * SCALE))
    gap_um = int(round(_setting(settings, ("slice_gap_closing_radius",), DEFAULT_GAP_CLOSING_UM / SCALE) * SCALE))
    z_max_mm, z_min_mm = 0.0, 0.0
    for group in groups:
        bounds = group.bounds()
        if bounds is not None:
            z_max_mm = max(z_max_mm, float(bounds[1][2]))
            z_min_mm = min(z_min_mm, float(bounds[0][2]))
    heights = layer_heights(z_max_mm, layer_mm, first_mm)
    layers = [Layer(index=i + 1, z_um=top, height_um=top - bottom) for i, (bottom, top) in enumerate(heights)]
    if not layers:
        return layers
    z_cuts = [(bottom + top) // 2 for bottom, top in heights]
    if z_min_mm < -1e-6:
        layers[0].warnings.append(f"modèle sous le plateau de {-z_min_mm:.3f} mm : partie ignorée")
    total = max(1, len(groups))
    nz = len(z_cuts)
    bands = [(b, min(b + PROGRESS_BAND, nz)) for b in range(0, nz, PROGRESS_BAND)]
    for oid, group in enumerate(groups, 1):
        if progress is not None:
            progress((oid - 1) / total, f"découpe de l'objet {oid}/{total}")
        meshes = [m for m in group.meshes if len(m.faces)]
        by_type: dict = {}
        for mi, mesh in enumerate(meshes):
            label = f"objet {oid}" if mesh.volume_type == "normal" else f"objet {oid} ({mesh.volume_type})"
            per_z = []
            for b0, b1 in bands:                                # bandes de plans : même résultat, progression régulière
                stats = SliceStats()
                per_z.extend(slice_mesh(mesh, z_cuts[b0:b1], gap_closing_um=gap_um, resolution_um=resolution_um,
                                        stats=stats))
                for k, messages in sorted(stats.warnings.items()):
                    layers[b0 + k].warnings.extend(f"{label} : {m}" for m in messages)
                if progress is not None and (b1 < nz or mi + 1 < len(meshes)):
                    progress((oid - 1 + (mi + b1 / nz) / len(meshes)) / total,
                             f"découpe de l'objet {oid}/{total} : couche {b1}/{nz}")
            by_type.setdefault(mesh.volume_type, []).append(per_z)
        for vtype, lists in by_type.items():
            key = volume_key(oid, vtype)
            for k, layer in enumerate(layers):
                layer.regions[key] = _merge([lst[k] for lst in lists])
        for layer in layers:
            layer.regions.setdefault(oid, [])                   # clé toujours présente, même objet vide
    for layer in layers:
        layer.all_regions = _merge([layer.regions[k] for k in model_object_ids(layer)])
    if progress is not None:
        progress(1.0, "découpe terminée")
    return layers
