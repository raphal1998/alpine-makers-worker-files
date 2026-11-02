"""Relief souvenir — lecture GPX 1.0 / 1.1 sans analyseur XML (port de Gpx.kt).

Aucune entité ni DTD interprétée : points de trace (trkpt), puis d'itinéraire (rtept) à défaut ; préfixes d'espace de
noms tolérés. Bornes : 30 Mo de texte, 200 000 points (au-delà, un point sur k est gardé, le dernier toujours).
"""
import math
import re

TEXTE_MAX = 30_000_000
POINTS_MAX = 200_000
HYSTERESIS_M = 4.0

_RE_GPX = re.compile(r"<(?:\w+:)?gpx\b", re.I)
_RE_POINT = re.compile(r"<(?:\w+:)?(trkpt|rtept)\b([^>]*?)(/>|>(.*?)</(?:\w+:)?\1\s*>)", re.S)
_RE_ATTR = re.compile(r"\b(lat|lon)\s*=\s*[\"']\s*([-+0-9.eE]+)\s*[\"']")
_RE_ELE = re.compile(r"<(?:\w+:)?ele\s*>\s*([-+0-9.eE]+)\s*<")
_RE_TEMPS = re.compile(r"<(?:\w+:)?time\s*>\s*([^<\s]+)\s*<")
_RE_CDATA = re.compile(r"<!\[CDATA\[(.*?)]]>", re.S)


def _re_nom(bloc):
    return re.compile(r"<(?:\w+:)?" + bloc + r"\b[^>]*>\s*(?:<(?!(?:\w+:)?name\b)[^>]*>[^<]*)*?<(?:\w+:)?name\s*>\s*(.*?)\s*</(?:\w+:)?name\s*>", re.S)


def _nombre(t):
    try:
        v = float(t)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _nettoyer(t):
    t = _RE_CDATA.sub(r"\1", t)
    for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&amp;", "&")):
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


def lire(texte):
    """Rend {"nom", "points": [(lat, lon, ele|None, temps|None)]}. ValueError en français si le fichier ne convient pas."""
    if len(texte) > TEXTE_MAX:
        raise ValueError("Fichier GPX trop gros (30 Mo au plus).")
    if not _RE_GPX.search(texte[:4096]):
        raise ValueError("Ce fichier n'est pas un GPX.")
    trk, rte = [], []
    for m in _RE_POINT.finditer(texte):
        lat = lon = None
        for a in _RE_ATTR.finditer(m.group(2)):
            v = _nombre(a.group(2))
            if a.group(1) == "lat":
                lat = v
            else:
                lon = v
        if lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            continue
        corps = m.group(4) or ""
        e = _RE_ELE.search(corps)
        ele = _nombre(e.group(1)) if e else None
        if ele is not None and not -500 <= ele <= 9000:
            ele = None
        t = _RE_TEMPS.search(corps)
        (trk if m.group(1) == "trkpt" else rte).append((lat, lon, ele, t.group(1) if t else None))
    points = trk if len(trk) >= 2 else rte
    if len(points) < 2:
        raise ValueError("Le GPX ne contient pas de tracé (au moins deux points).")
    if len(points) > POINTS_MAX:
        k = (len(points) + POINTS_MAX - 1) // POINTS_MAX
        points = [p for i, p in enumerate(points) if i % k == 0 or i == len(points) - 1]
    nom = None
    for bloc in ("trk", "rte", "metadata"):
        m = _re_nom(bloc).search(texte)
        if m and _nettoyer(m.group(1)):
            nom = _nettoyer(m.group(1))
            break
    return {"nom": (nom or "Mon tracé")[:60], "points": points}


def distance(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_008.8 * math.asin(min(1.0, math.sqrt(h)))


def distance_totale(points):
    return sum(distance(a[0], a[1], b[0], b[1]) for a, b in zip(points, points[1:]))


def a_des_altitudes(points):
    return bool(points) and sum(1 for p in points if p[2] is not None) >= len(points) / 2


def denivele(altitudes, seuil=HYSTERESIS_M):
    """Dénivelé positif avec hystérésis : une montée ne compte qu'une fois l'écart au dernier repère ≥ seuil."""
    if not altitudes:
        return 0.0
    repere, total = altitudes[0], 0.0
    for a in altitudes:
        if a - repere >= seuil:
            total += a - repere
            repere = a
        elif repere - a >= seuil:
            repere = a
    return total


def date(points):
    """Date du premier point horodaté (« 12.08.2026 ») ou None."""
    for p in points:
        if p[3]:
            m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", p[3])
            return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else None
    return None


def km(m):
    if m >= 100_000:
        return f"{round(m / 1000):,}".replace(",", " ") + " km"
    return ("%.1f km" % (m / 1000)).replace(".", ",")
