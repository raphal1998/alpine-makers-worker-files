"""Supports « normaux » (§5.5 de docs/MODEL_ENGINE_ARCHITECTURE.md).

Méthode :
1. surplomb(i) = regions(i) − dilate(regions(i−1), tol) avec tol = hauteur / tan(seuil) (seuil mesuré depuis
   l'horizontale : une surface plus plate que le seuil reçoit un support ; 0 ou ≥ 90 = tout ce qui dépasse la
   couche d'en dessous) ; zones < 1 mm² ignorées, puis élargissement d'une demi-largeur de ligne (une bande plus
   fine qu'une ligne ne s'imprime pas et les bandes voisines fusionnent) ;
2. projection descendante : support(i) = (support(i+1) ∪ surplomb(i+1+k)) − dilate(regions(i−k_b..i+k), xy), k =
   distance Z du dessus arrondie à la couche (les k couches sous un surplomb restent vides : la pièce des k couches
   du dessus fait partie du clip, sinon un support projeté sous une autre partie de la pièce s'y collerait) ;
   blockers soustraits ;
3. un support qui rencontre la pièce s'y pose : la pièce dilatée des k_b couches du dessous fait partie du clip
   (jeu du dessous) et celle des n_b couches suivantes entre dans la fenêtre d'interface ; avec
   ``support_on_build_plate_only`` une passe montante retire toute colonne qui ne touche pas le plateau ;
4. interface = les n couches de support juste sous un surplomb (et juste au-dessus de la pièce), base = le reste ;
5. traits : hachures (``hatch`` + ``clip_segments``) à phase absolue (colonnes alignées entre couches),
   interface perpendiculaire aux colonnes, parcours en zigzag.

Dilatation : courbe d'offset brute (arêtes décalées à droite du sens de parcours, arcs aux sommets convexes,
raccord direct aux sommets réflexes qui s'inverseraient) dont la région d'enroulement positif est la dilatation ;
elle sert directement d'opérande des booléennes (règle positive), ce qui épargne une union par appel.
Les régions sont simplifiées à ``resolution`` (copie locale) avant tout calcul.
Supports arborescents : traités comme des supports normaux en V1 (``is_tree=True`` + avertissement).
"""
from __future__ import annotations

import math
import time

import numpy as np

from .geometry import SCALE
from .geometry.boolean import difference, intersection, union
from .geometry.lines import clip_segments, hatch
from .geometry.poly import INT, area2, as_ring, bbox, from_polytree, simplify, to_polytree
from .layers import SupportLayer

# Défauts de la spécification (clés du schéma profiles/process.py), en mm / degrés / couches.
DEFAULTS = {
    "enable_support": False,
    "support_type": "normal(auto)",
    "support_threshold_angle": 30,
    "support_base_pattern": "rectilinear",
    "support_base_pattern_spacing": 2.5,
    "support_interface_top_layers": 2,
    "support_interface_bottom_layers": 2,
    "support_interface_spacing": 0.5,
    "support_top_z_distance": 0.2,
    "support_bottom_z_distance": 0.2,
    "support_object_xy_distance": 0.35,
    "support_on_build_plate_only": False,
    "support_line_width": 0.42,
    "support_angle": 0.0,
    "support_interface_not_for_body": False,
    "layer_height": 0.2,
    "resolution": 0.012,
}
MIN_AREA_UM2 = 1_000_000          # 1 mm² : en dessous, une zone de support ou de surplomb est ignorée
CLOSING_RINGS = 3                 # au-delà de ce nombre de rings, le support d'une couche subit une fermeture (w/2)
ARC_TOLERANCE_UM = 20.0           # flèche maximale des arcs de la dilatation brute
_EMPTY = []
LAST_STATS = {}                   # compteurs de la dernière exécution de build_supports


# ---------------------------------------------------------------------------------------------------------------
# Réglages
# ---------------------------------------------------------------------------------------------------------------

def _get(settings, key):
    value = settings.get(key) if settings else None
    return DEFAULTS[key] if value is None else value


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "oui")
    return bool(value)


def _um(value_mm) -> int:
    return int(round(float(value_mm) * SCALE))


def _spacing_um(value, line_width_um: int) -> int:
    """Espacement des colonnes : en mm, ou en « densité » 0–100 (valeur > 10, héritage du client)."""
    v = float(value)
    if v <= 0:
        return line_width_um
    if v > 10.0:
        return max(line_width_um, int(round(line_width_um * 100.0 / min(v, 100.0))))
    return max(line_width_um, _um(v))


def _xy_tolerance_um(height_um: int, angle_deg: float) -> int:
    """Avance horizontale tolérée par couche : hauteur / tan(seuil) ; 0 (ou ≥ 90°) = rien n'est toléré."""
    a = float(angle_deg)
    if a <= 0.0 or a >= 90.0:
        return 0
    return int(round(height_um / math.tan(math.radians(a))))


# ---------------------------------------------------------------------------------------------------------------
# Dilatation brute (courbe d'offset non ébarbée, enroulement positif = dilatation)
# ---------------------------------------------------------------------------------------------------------------

def _raw_offset_ring(ring: np.ndarray, delta: float, arc_tol: float):
    """Courbe d'offset brute d'un ring vers la droite du parcours (extérieur CCW → dehors, trou CW → dedans)."""
    p = as_ring(ring).astype(np.float64)
    if len(p) < 3:
        return None
    d = np.roll(p, -1, axis=0) - p
    length = np.hypot(d[:, 0], d[:, 1])
    keep = length > 0.0
    if not keep.all():
        p = p[keep]
        if len(p) < 3:
            return None
        d = np.roll(p, -1, axis=0) - p
        length = np.hypot(d[:, 0], d[:, 1])
    t = d / length[:, None]
    nrm = np.column_stack([t[:, 1], -t[:, 0]])          # normale à droite de l'arête i (p_i → p_i+1)
    n_prev = np.roll(nrm, 1, axis=0)                     # normale de l'arête qui arrive en p_i
    l_prev = np.roll(length, 1)
    dot = (n_prev * nrm).sum(axis=1)
    cross = n_prev[:, 0] * nrm[:, 1] - n_prev[:, 1] * nrm[:, 0]
    theta = np.arctan2(cross, dot)                       # rotation de la normale ; > 0 = virage à gauche (convexe)
    denom = 1.0 + dot
    safe = denom > 1e-9
    denom_safe = np.where(safe, denom, 1.0)
    bis = (n_prev + nrm) / denom_safe[:, None]           # point de miter = p + delta · bis
    tan_half = np.abs(cross) / denom_safe                 # tan(|theta| / 2)
    convex = theta > 0.0
    if delta > arc_tol:
        dphi = 2.0 * math.acos(max(0.0, 1.0 - arc_tol / delta))
        arc = convex & (theta > dphi)
        sharp = np.zeros(len(p), dtype=bool)
    else:
        dphi = math.pi
        arc = np.zeros(len(p), dtype=bool)
        sharp = convex & (tan_half > 4.0)                # chanfrein aux pointes très aiguës (delta minuscule)
    inversion = (~convex) & (delta * tan_half > 0.5 * np.minimum(l_prev, length))
    two = (~arc) & ((~safe) | inversion | sharp)
    miter = ~(arc | two)
    m = np.where(arc, np.ceil(theta / dphi), 0.0).astype(np.int64)
    counts = np.where(arc, m + 1, np.where(two, 2, 1))
    starts = np.cumsum(counts) - counts
    out = np.empty((int(counts.sum()), 2), dtype=np.float64)
    out[starts[miter]] = p[miter] + delta * bis[miter]
    out[starts[two]] = p[two] + delta * n_prev[two]
    out[starts[two] + 1] = p[two] + delta * nrm[two]
    ai = np.nonzero(arc)[0]
    if len(ai):
        reps = m[ai] + 1
        vi = np.repeat(ai, reps)
        j = np.arange(int(reps.sum())) - np.repeat(np.cumsum(reps) - reps, reps)
        phi = np.arctan2(n_prev[vi, 1], n_prev[vi, 0]) + theta[vi] * j / m[vi]
        out[np.repeat(starts[ai], reps) + j] = p[vi] + delta * np.column_stack([np.cos(phi), np.sin(phi)])
    return np.rint(out).astype(INT)


def raw_dilation(paths, delta_um, arc_tolerance_um: float = ARC_TOLERANCE_UM) -> list:
    """Rings bruts dont la région d'enroulement positif est ``paths`` dilaté de ``delta_um`` (≥ 0). Utilisable tel
    quel comme opérande de ``difference`` / ``intersection`` ; ``dilate`` en donne la forme propre."""
    delta = float(delta_um)
    if delta <= 0.0:
        return [as_ring(r) for r in paths if len(r) >= 3]
    out = []
    for r in paths:
        raw = _raw_offset_ring(r, delta, float(arc_tolerance_um))
        if raw is not None:
            out.append(raw)
    return out


def dilate(paths, delta_um, arc_tolerance_um: float = ARC_TOLERANCE_UM) -> list:
    """Dilatation propre (union de la courbe brute) ; delta ≤ 0 renvoie l'union de l'entrée."""
    raw = raw_dilation(paths, delta_um, arc_tolerance_um)
    return union(raw) if raw else []


# ---------------------------------------------------------------------------------------------------------------
# Outils géométriques
# ---------------------------------------------------------------------------------------------------------------

def simplify_ring(ring, tol_um: float) -> np.ndarray:
    """Simplification à ``tol_um`` : deux passes locales vectorisées (sommets à moins de tol/8 de la corde de
    leurs voisins, jamais deux voisins à la fois) puis Douglas–Peucker (``poly.simplify``) à 3·tol/4 ; erreur
    totale ≤ tol. Beaucoup moins coûteux que Douglas–Peucker seul sur un contour sur-échantillonné."""
    r = as_ring(ring)
    if tol_um <= 0 or len(r) <= 3:
        return r
    eighth = float(tol_um) / 8.0
    for pass_index in range(2):
        n = len(r)
        if n <= 3:
            break
        prev_ = np.roll(r, 1, axis=0)
        next_ = np.roll(r, -1, axis=0)
        chord = (next_ - prev_).astype(np.float64)
        rel = (r - prev_).astype(np.float64)
        cross = np.abs(rel[:, 0] * chord[:, 1] - rel[:, 1] * chord[:, 0])
        clen = np.hypot(chord[:, 0], chord[:, 1])
        dist = np.where(clen > 0, cross / np.where(clen > 0, clen, 1.0), np.hypot(rel[:, 0], rel[:, 1]))
        remove = (dist < eighth) & ((np.arange(n) % 2) == (pass_index % 2))
        if n - int(remove.sum()) < 3 or not remove.any():
            break
        r = r[~remove]
    if len(r) > 3:
        r = simplify(r, 0.75 * float(tol_um))
    return np.ascontiguousarray(r)


def _close(paths, radius_um: int, clip=None) -> list:
    """Fermeture morphologique (dilatation puis érosion de ``radius``) d'un Paths propre : les fentes plus étroites
    que 2·radius entre zones de support sont comblées. L'érosion est la différence avec la dilatation brute du
    complémentaire (boîte englobante + rings inversés) ; ``clip`` (rings bruts, règle positive) est retiré dans la
    même booléenne pour restaurer le jeu de la pièce. Deux booléennes."""
    if not paths or radius_um <= 0:
        return paths
    grown = dilate(paths, radius_um)
    if not grown:
        return paths
    x0, y0, x1, y1 = bbox(grown)
    pad = 2 * radius_um + 1
    box = np.array([[x0 - pad, y0 - pad], [x1 + pad, y0 - pad], [x1 + pad, y1 + pad], [x0 - pad, y1 + pad]], dtype=INT)
    complement = [box] + [np.ascontiguousarray(r[::-1]) for r in grown]
    eroded_band = raw_dilation(complement, radius_um)
    return difference(grown, eroded_band + list(clip or ()))


def _drop_small(paths, min_area_um2: int = MIN_AREA_UM2) -> list:
    """Retire les polygones (extérieur − trous) d'aire < min ; entrée propre attendue."""
    if not paths:
        return []
    areas = [area2(r) for r in paths]
    if all(a > 0 for a in areas):
        kept = [r for r, a in zip(paths, areas) if a >= 2 * min_area_um2]
        return kept if len(kept) < len(paths) else list(paths)
    tree = to_polytree(paths)
    kept = []
    for outer, holes in tree:
        net = abs(area2(outer)) - sum(abs(area2(h)) for h in holes)
        if net >= 2 * min_area_um2:
            kept.append((outer, holes))
    return from_polytree(kept) if len(kept) < len(tree) else list(paths)


def _area2_sum(paths) -> int:
    return sum(area2(r) for r in paths)


def _boxes_overlap(a, b) -> bool:
    return a is not None and b is not None and a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _same_paths(a, b) -> bool:
    if len(a) != len(b):
        return False
    return all(x.shape == y.shape and np.array_equal(x, y) for x, y in zip(a, b))


def _layer_regions(layer, resolution_um: int = 0) -> list:
    """Coupe de tous les objets d'une couche (``all_regions`` si fourni, sinon union des objets), simplifiée à
    ``resolution_um`` (copie : les couches ne sont pas modifiées)."""
    if layer.all_regions:
        paths = list(layer.all_regions)
    else:
        groups = [list(p) for k, p in layer.regions.items() if p and int(k) > 0]
        if not groups:
            return []
        if len(groups) == 1:
            paths = groups[0]
        else:
            merged = []
            for g in groups:
                merged.extend(g)
            paths = union(merged)
    out = []
    for r in paths:
        r = as_ring(r)
        if resolution_um > 0 and len(r) > 3:
            r = simplify_ring(r, resolution_um)
        if len(r) >= 3 and area2(r) != 0:
            out.append(r)
    return out


def _raw_regions(layer) -> list:
    return list(layer.all_regions) if layer.all_regions else [r for k, p in layer.regions.items() if int(k) > 0 for r in p]


def _all_regions(layers, resolution_um: int) -> list:
    """Coupes simplifiées de toutes les couches ; une couche identique à la précédente partage son résultat."""
    out, prev_raw, prev_out = [], None, None
    for layer in layers:
        raw = _raw_regions(layer)
        if prev_raw is not None and _same_paths(raw, prev_raw):
            out.append(prev_out)
            continue
        prev_raw, prev_out = raw, _layer_regions(layer, resolution_um)
        out.append(prev_out)
    return out


def _per_layer(arg, layers) -> list:
    """Zones par couche (dict indexé par ``layer.index`` ou liste alignée sur ``layers``) → liste alignée."""
    n = len(layers)
    if not arg:
        return [_EMPTY] * n
    if isinstance(arg, dict):
        return [list(arg.get(layer.index) or ()) for layer in layers]
    if len(arg) != n:
        raise ValueError("blockers/enforcers : une liste alignée sur les couches est attendue")
    return [list(p or ()) for p in arg]


def _hatch_lines(area, angle_rad: float, pitch_um: int) -> list:
    """Hachures d'angle et de pas donnés, phase absolue (colonnes alignées d'une couche à l'autre), parcourues
    en zigzag ; liste de polylignes ``(2, 2)``."""
    if not area or pitch_um <= 0:
        return []
    seg = hatch(bbox(area), angle_rad, pitch_um)
    if not len(seg):
        return []
    seg = seg.copy()
    seg[1::2] = seg[1::2, ::-1]
    clipped = clip_segments(seg, area)
    return [clipped[i] for i in range(len(clipped))]


def _support_lines(area, pattern: str, angle_rad: float, pitch_um: int) -> list:
    lines = _hatch_lines(area, angle_rad, pitch_um)
    if pattern == "grid":
        lines.extend(_hatch_lines(area, angle_rad + math.pi / 2.0, pitch_um))
    return lines


# ---------------------------------------------------------------------------------------------------------------
# Surplombs
# ---------------------------------------------------------------------------------------------------------------

def _overhangs(layers, settings, regions, enforcers) -> list:
    angle = float(_get(settings, "support_threshold_angle"))
    manual = "manual" in str(_get(settings, "support_type")).lower()
    enf = _per_layer(enforcers, layers)
    out = [_EMPTY] * len(layers)
    for l in range(1, len(layers)):
        cur, prev = regions[l], regions[l - 1]
        if not cur:
            continue
        if not prev:
            over = list(cur)
        elif _same_paths(cur, prev):
            over = []
        else:
            tol = _xy_tolerance_um(layers[l].height_um, angle)
            over = difference(cur, raw_dilation(prev, tol))
        zone = enf[l]
        if manual:
            over = intersection(over, zone) if (over and zone) else []
        elif zone:
            forced = difference(intersection(cur, zone), prev) if prev else intersection(cur, zone)
            if forced:
                over = union(over + forced) if over else forced
        out[l] = _drop_small(over)
    return out


def overhang_regions(layers, settings, *, enforcers=None, expand_um: int = 0) -> list:
    """Surplomb de chaque couche (liste alignée sur ``layers``, Paths propres) : parties de la couche non
    soutenues par la précédente dilatée de la tolérance ; couche 1 = aucun ; zones < 1 mm² ignorées.
    ``normal(manual)`` : seulement dans les enforcers ; sinon les enforcers imposent un support sous tout ce qui
    n'est pas directement au-dessus de la couche d'en dessous. ``expand_um`` élargit chaque surplomb."""
    regions = _all_regions(layers, _um(_get(settings, "resolution")))
    out = _overhangs(layers, settings, regions, enforcers)
    if expand_um > 0:
        out = [dilate(o, expand_um) if o else o for o in out]
    return out


# ---------------------------------------------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------------------------------------------

def build_supports(layers, settings, *, blockers=None, enforcers=None) -> None:
    """Remplit ``layer.support`` (``SupportLayer``) pour chaque couche. ``blockers`` / ``enforcers`` : zones par
    couche (dict ``{layer.index: Paths}`` ou liste alignée). Les compteurs de la dernière exécution (couches,
    couches soutenues, couches à surplomb, booléennes, secondes) sont dans ``LAST_STATS`` pour les journaux."""
    started = time.perf_counter()
    n = len(layers)
    stats = {"layers": n, "supported_layers": 0, "overhang_layers": 0, "booleans": 0, "seconds": 0.0}
    LAST_STATS.clear()
    LAST_STATS.update(stats)
    if n == 0:
        return None
    enabled = _as_bool(_get(settings, "enable_support"))
    support_type = str(_get(settings, "support_type")).lower()
    is_tree = support_type.startswith("tree")
    if not enabled:
        for layer in layers:
            layer.support = SupportLayer()
        LAST_STATS["seconds"] = time.perf_counter() - started
        return None
    if is_tree:
        message = "Supports arborescents : générés comme des supports normaux en V1."
        if message not in layers[0].warnings:
            layers[0].warnings.append(message)

    layer_h = _um(_get(settings, "layer_height")) or (layers[-1].height_um or 200)
    k_top = int(round(_um(_get(settings, "support_top_z_distance")) / layer_h))
    k_bottom = int(round(_um(_get(settings, "support_bottom_z_distance")) / layer_h))
    n_top = max(0, int(_get(settings, "support_interface_top_layers")))
    n_bottom = max(0, int(_get(settings, "support_interface_bottom_layers")))
    if _as_bool(_get(settings, "support_interface_not_for_body")):
        n_bottom = 0
    xy = _um(_get(settings, "support_object_xy_distance"))
    plate_only = _as_bool(_get(settings, "support_on_build_plate_only"))
    line_w = _um(_get(settings, "support_line_width"))
    if line_w <= 0:
        line_w = _um(DEFAULTS["support_line_width"])   # largeur nulle ou négative : défaut du schéma
    base_pattern = str(_get(settings, "support_base_pattern")).lower()
    base_pitch = _spacing_um(_get(settings, "support_base_pattern_spacing"), line_w)
    iface_pitch = line_w + max(0, _um(_get(settings, "support_interface_spacing")))
    angle = math.radians(float(_get(settings, "support_angle")))
    blk = _per_layer(blockers, layers)

    regions = _all_regions(layers, _um(_get(settings, "resolution")))
    over = _overhangs(layers, settings, regions, enforcers)
    stats["overhang_layers"] = sum(1 for o in over if o)
    # Contribution de chaque surplomb : élargi d'une demi-ligne, sous forme brute (règle positive).
    contrib_raw = [raw_dilation(o, line_w // 2) if o else _EMPTY for o in over]

    # Projection descendante. Le « clip » d'une couche l est la pièce dilatée de xy sur les couches l−k_b..l+k_top :
    # la couche elle-même, les k_b couches du dessous (jeu sous un support posé sur la pièce) et les k_top couches du
    # dessus (jeu sous la pièce : un surplomb projeté sous une autre partie de la pièce ne doit pas s'y coller) ;
    # rings bruts en règle positive.
    depth_below = 0 if plate_only else k_bottom

    def clip_range(l):
        return range(max(0, l - depth_below), min(n, l + k_top + 1))

    def clip_layers(l):
        """Coupes distinctes (non vides) qui composent le clip de la couche l."""
        seen = []
        for j in clip_range(l):
            if regions[j] and not any(_same_paths(regions[j], s) for s in seen):
                seen.append(regions[j])
        return seen

    def model_clip(l):
        parts = []
        for paths in clip_layers(l):
            parts.extend(raw_dilation(paths, xy))
        return parts

    def same_clip(l):
        """Vrai si le clip de la couche l est identique à celui de la couche l+1 (coupes textuellement égales)."""
        for j in clip_range(l):
            b = regions[j + 1] if j + 1 < n else _EMPTY
            if not _same_paths(regions[j], b):
                return False
        return True

    support = [_EMPTY] * n
    booleans = 0
    for l in range(n - 2, -1, -1):
        above = support[l + 1]
        contrib = contrib_raw[l + 1 + k_top] if l + 1 + k_top < n else _EMPTY
        if not above and not contrib:
            continue
        if above and not contrib and not blk[l] and same_clip(l):
            support[l] = above            # même pièce qu'au-dessus : la colonne descend telle quelle
            continue
        clip = model_clip(l)
        if clip:
            cur = difference(above + contrib, clip)
        elif contrib:
            cur = union(above + contrib)
        else:
            cur = list(above)
        booleans += 1
        if len(cur) > CLOSING_RINGS:
            # Anneaux qui s'accumulent (marches d'un cône : chaque contribution est coupée à xy par la pièce du dessus
            # et reste séparée de la suivante par moins d'une ligne) : fermeture morphologique de rayon w/2, le jeu
            # de la pièce étant restauré dans la même booléenne.
            cur = _close(cur, line_w // 2, clip)
            booleans += 2
        if cur and blk[l]:
            cur = difference(cur, blk[l])
            booleans += 1
        support[l] = _drop_small(cur)
    if plate_only:
        # Passe montante : une colonne qui ne repose pas sur la couche d'en dessous (donc sur la pièce) est retirée.
        for l in range(1, n):
            if not support[l]:
                continue
            if not support[l - 1]:
                support[l] = []
            elif not _same_paths(support[l], support[l - 1]):
                support[l] = _drop_small(intersection(support[l], support[l - 1]))
                booleans += 1

    # Interface / base et traits. Les traits d'une colonne partagée entre couches identiques sont calculés une fois.
    line_cache = {}

    def lines_for(zone, pattern, ang, pitch):
        key = (id(zone), pattern, ang, pitch)
        hit = line_cache.get(key)
        if hit is not None and hit[0] is zone:
            return hit[1]
        lines = _support_lines(zone, pattern, ang, pitch) if pattern else _hatch_lines(zone, ang, pitch)
        line_cache[key] = (zone, lines)
        return lines

    for l, layer in enumerate(layers):
        area = support[l]
        if not area:
            layer.support = SupportLayer(is_tree=is_tree)
            continue
        window = []
        for t in range(n_top):
            j = l + 1 + k_top + t
            if j < n and contrib_raw[j]:
                window.extend(contrib_raw[j])
        if n_bottom and not plate_only:
            # Pièce sous le jeu du dessous ; une coupe identique à celle d'une couche du clip ne peut rien toucher.
            seen = clip_layers(l)
            for t in range(k_bottom + 1, k_bottom + n_bottom + 1):
                j = l - t
                if j < 0 or not regions[j] or any(_same_paths(regions[j], s) for s in seen):
                    continue
                seen.append(regions[j])
                window.extend(raw_dilation(regions[j], xy))
        if window:
            iface = _drop_small(intersection(area, window), MIN_AREA_UM2 // 10)
            booleans += 1
            if not iface:
                base = area
            elif _area2_sum(iface) >= _area2_sum(area):
                base = []
            else:
                base = _drop_small(difference(area, iface), MIN_AREA_UM2 // 10)
                booleans += 1
        else:
            iface, base = [], area
        layer.support = SupportLayer(
            base_area=base, interface_area=iface,
            base_lines=lines_for(base, base_pattern, angle, base_pitch) if base else [],
            interface_lines=lines_for(iface, "", angle + math.pi / 2.0, iface_pitch) if iface else [],
            is_tree=is_tree)
        stats["supported_layers"] += 1
    stats["booleans"] = booleans
    stats["seconds"] = time.perf_counter() - started
    LAST_STATS.update(stats)
    return None


__all__ = ["DEFAULTS", "LAST_STATS", "MIN_AREA_UM2", "build_supports", "overhang_regions", "raw_dilation", "dilate",
           "simplify_ring"]
