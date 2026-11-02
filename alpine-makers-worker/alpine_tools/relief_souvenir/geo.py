"""Relief souvenir — données géographiques sans réseau (port de Geo.kt et de la partie OSM de ReliefFace.kt).

Projections (MN95 swisstopo, équirectangulaire locale), zone, grille d'altitudes, plan des requêtes et lecture des
réponses des sources ouvertes et sans clé :
  - swisstopo (Suisse) : service de profil api3.geo.admin.ch, modèle COMB (swissALTI3D 2 m, DHM25 à défaut) ;
  - Terrain Tiles (monde) : tuiles terrarium d'AWS Open Data (Mapzen), SRTM, GMTED, ETOPO1 et modèles nationaux ;
  - OpenStreetMap (Overpass) : via ferrata (highway=via_ferrata).
Le téléchargement est dans terrain.py.
"""
import json
import math
from dataclasses import dataclass
from urllib.parse import quote_plus

import numpy as np

RAYON_TERRE = 6_371_008.8
M_PAR_DEGRE = 111_194.93

SWISSTOPO = "swisstopo"
MONDE = "monde"
ATTRIBUTIONS = {
    SWISSTOPO: "Altitudes © swisstopo (swissALTI3D / DHM25, données ouvertes OGD)",
    MONDE: "Altitudes : Terrain Tiles, AWS Open Data (Mapzen) — SRTM (NASA/USGS), GMTED2010, ETOPO1 (NOAA) et modèles nationaux",
}
LIBELLES_SOURCE = {SWISSTOPO: "swisstopo (Suisse)", MONDE: "Terrain Tiles (monde)"}

RECTANGLE, CERCLE, HEXAGONE = "rectangle", "cercle", "hexagone"


def arrondi(v):
    """Arrondi « au plus proche, demi vers le haut » (roundToInt de Kotlin), scalaire ou tableau."""
    return np.floor(np.asarray(v, dtype=float) + 0.5).astype(np.int64)


# ================================================================================================ projections
def lv95_depuis_wgs84(lat, lon):
    """Formules approchées officielles de swisstopo (WGS84 → MN95), précision du mètre en Suisse."""
    p = (np.asarray(lat, dtype=float) * 3600 - 169028.66) / 10000
    l = (np.asarray(lon, dtype=float) * 3600 - 26782.5) / 10000
    e = 2600072.37 + 211455.93 * l - 10938.51 * l * p - 0.36 * l * p * p - 44.54 * l * l * l
    n = 1200147.07 + 308807.95 * p + 3745.25 * l * l + 76.63 * p * p - 194.56 * l * l * p + 119.79 * p * p * p
    return e, n


def lv95_vers_wgs84(e, n):
    y = (np.asarray(e, dtype=float) - 2600000) / 1e6
    x = (np.asarray(n, dtype=float) - 1200000) / 1e6
    l = 2.6779094 + 4.728982 * y + 0.791484 * y * x + 0.1306 * y * x * x - 0.0436 * y * y * y
    p = 16.9023892 + 3.238272 * x - 0.270978 * y * y - 0.002528 * x * x - 0.0447 * y * y * x - 0.0140 * x * x * x
    return p * 100 / 36, l * 100 / 36


def en_suisse(lat, lon):
    """Boîte large de la couverture swisstopo (Suisse et Liechtenstein, avec les bords)."""
    return 45.80 <= lat <= 47.83 and 5.94 <= lon <= 10.50


class ProjectionSuisse:
    """MN95 décalé sur le centre (le maillage suit la grille swisstopo). x vers l'est, y vers le nord (m)."""

    def __init__(self, lat0, lon0):
        e, n = lv95_depuis_wgs84(lat0, lon0)
        self.e0, self.n0 = float(e), float(n)

    def vers_local(self, lat, lon):
        e, n = lv95_depuis_wgs84(lat, lon)
        return e - self.e0, n - self.n0

    def vers_geo(self, x, y):
        return lv95_vers_wgs84(self.e0 + np.asarray(x, dtype=float), self.n0 + np.asarray(y, dtype=float))


class ProjectionLocale:
    """Équirectangulaire locale (zones de quelques dizaines de km : écart < 0,1 %)."""

    def __init__(self, lat0, lon0):
        self.lat0, self.lon0 = lat0, lon0
        self.m_lat = M_PAR_DEGRE
        self.m_lon = M_PAR_DEGRE * math.cos(math.radians(lat0))

    def vers_local(self, lat, lon):
        return (np.asarray(lon, dtype=float) - self.lon0) * self.m_lon, (np.asarray(lat, dtype=float) - self.lat0) * self.m_lat

    def vers_geo(self, x, y):
        return self.lat0 + np.asarray(y, dtype=float) / self.m_lat, self.lon0 + np.asarray(x, dtype=float) / self.m_lon


# ================================================================================================ zone
COTE_MIN_M = 300.0
COTE_MAX_M = 60_000.0


@dataclass
class Zone:
    """Centre (WGS84) et étendue (m) : largeur est-ouest × hauteur nord-sud ; diamètre pour le cercle ; largeur de pointe
    à pointe pour l'hexagone (hauteur = largeur × √3 / 2)."""
    lat: float
    lon: float
    largeur_m: float
    hauteur_m: float
    forme: str = RECTANGLE

    @property
    def boite_m(self):
        if self.forme == CERCLE:
            return self.largeur_m, self.largeur_m
        if self.forme == HEXAGONE:
            return self.largeur_m, self.largeur_m * math.sqrt(3.0) / 2
        return self.largeur_m, self.hauteur_m

    def projection(self, source):
        return ProjectionSuisse(self.lat, self.lon) if source == SWISSTOPO else ProjectionLocale(self.lat, self.lon)


def zone_autour(lats, lons, forme, marge=0.12):
    """Zone qui englobe un tracé avec une marge (part de l'étendue de chaque côté, 200 m au moins)."""
    lats = np.asarray(lats, dtype=float)
    lons = np.asarray(lons, dtype=float)
    lat0 = (lats.min() + lats.max()) / 2
    lon0 = (lons.min() + lons.max()) / 2
    p = ProjectionLocale(lat0, lon0)
    x, y = p.vers_local(lats, lons)
    x0, x1, y0, y1 = x.min(), x.max(), y.min(), y.max()
    clat, clon = p.vers_geo((x0 + x1) / 2, (y0 + y1) / 2)
    w, h = x1 - x0, y1 - y0
    m = max(200.0, max(w, h) * marge)
    if forme == CERCLE:
        lw = lh = math.hypot(w, h) + 2 * m
    elif forme == HEXAGONE:
        lw = lh = (math.hypot(w, h) + 2 * m) / (math.sqrt(3.0) / 2)
    else:
        lw, lh = w + 2 * m, h + 2 * m
    borne = lambda v: min(COTE_MAX_M, max(COTE_MIN_M, v))  # noqa: E731
    return Zone(float(clat), float(clon), borne(lw), borne(lh), forme)


# ================================================================================================ grille
COTE_MAX = 160
COTE_MIN = 24
COTE_FACE = 240


class Grille:
    """Altitudes (m) sur une grille régulière du plan local : x = x0 + i·pas, y = y0 + j·pas (j = 0 au sud). NaN = inconnu.
    Tableau ``alt`` de forme (ny, nx)."""

    def __init__(self, nx, ny, x0, y0, pas, alt=None):
        self.nx, self.ny, self.x0, self.y0, self.pas = int(nx), int(ny), float(x0), float(y0), float(pas)
        self.alt = np.full((self.ny, self.nx), np.nan, dtype=np.float64) if alt is None else np.asarray(alt, dtype=np.float64).reshape(self.ny, self.nx)

    @staticmethod
    def pour(zone, cote=COTE_MAX):
        w, h = zone.boite_m
        c = min(COTE_FACE, max(COTE_MIN, int(cote)))
        pas = max(w, h) / (c - 1)
        nx = max(2, math.ceil(w / pas) + 1)
        ny = max(2, math.ceil(h / pas) + 1)
        return Grille(nx, ny, -(nx - 1) * pas / 2, -(ny - 1) * pas / 2, pas)

    @property
    def inconnues(self):
        return int(np.isnan(self.alt).sum())

    def altitude(self, x, y):
        """Altitude bilinéaire au point (x, y) du plan local, bornée à la grille (scalaire ou tableaux)."""
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        fx = np.clip((x - self.x0) / self.pas, 0.0, self.nx - 1.0)
        fy = np.clip((y - self.y0) / self.pas, 0.0, self.ny - 1.0)
        i = np.clip(np.floor(fx).astype(np.int64), 0, max(0, self.nx - 2))
        j = np.clip(np.floor(fy).astype(np.int64), 0, max(0, self.ny - 2))
        tx = fx - i
        ty = fy - j
        a = self.alt
        i1 = np.minimum(i + 1, self.nx - 1)
        j1 = np.minimum(j + 1, self.ny - 1)
        v = (a[j, i] * (1 - tx) + a[j, i1] * tx) * (1 - ty) + (a[j1, i] * (1 - tx) + a[j1, i1] * tx) * ty
        return v

    def boucher(self):
        """Bouche les trous isolés (moyenne des voisins connus) ; faux s'il en reste trop (> 2 % de la grille)."""
        n = self.inconnues
        if n == 0:
            return True
        if n > self.alt.size * 0.02:
            return False
        for _ in range(64):
            copie = self.alt
            p = np.pad(copie, 1, constant_values=np.nan)
            voisins = np.stack([p[1:-1, :-2], p[1:-1, 2:], p[:-2, 1:-1], p[2:, 1:-1]])
            connus = ~np.isnan(voisins)
            k = connus.sum(axis=0)
            s = np.where(connus, voisins, 0.0).sum(axis=0)
            trous = np.isnan(copie)
            remplir = trous & (k > 0)
            nouveau = copie.copy()
            nouveau[remplir] = s[remplir] / k[remplir]
            self.alt = nouveau
            if not np.isnan(self.alt).any():
                return True
        return self.inconnues == 0


# ================================================================================================ swisstopo
SWISSTOPO_URL = "https://api3.geo.admin.ch/rest/services/profile.json"
POINTS_PAR_REQUETE = 5000


def _fixe(v):
    return "%.3f" % v


def swisstopo_requetes(g, p):
    """Requêtes au service de profil : lignes de la grille parcourues en serpentin. Rend [(j0, j1, url)]."""
    lignes = max(1, POINTS_PAR_REQUETE // g.nx)
    out = []
    j = 0
    while j < g.ny:
        j1 = min(g.ny, j + lignes)
        xg = p.e0 + g.x0
        xd = p.e0 + g.x0 + (g.nx - 1) * g.pas
        coords = []
        for r in range(j, j1):
            y = p.n0 + g.y0 + r * g.pas
            a, b = (xg, xd) if (r - j) % 2 == 0 else (xd, xg)
            coords.append(f"[{_fixe(a)},{_fixe(y)}],[{_fixe(b)},{_fixe(y)}]")
        geom = '{"type":"LineString","coordinates":[' + ",".join(coords) + "]}"
        points = (j1 - j) * g.nx
        url = f"{SWISSTOPO_URL}?sr=2056&offset=0&distinct_points=false&nb_points={points}&geom={quote_plus(geom)}"
        out.append((j, j1, url))
        j = j1
    return out


def swisstopo_lire(texte, g, p):
    """Range les altitudes d'une réponse dans la grille d'après easting/northing de chaque point. Rend les points placés."""
    try:
        tab = json.loads(texte)
    except (ValueError, TypeError):
        raise ValueError("Réponse swisstopo illisible : réessaie plus tard.") from None
    if not isinstance(tab, list):
        raise ValueError("Réponse swisstopo illisible : réessaie plus tard.")
    places = 0
    for o in tab:
        if not isinstance(o, dict):
            continue
        e, n, alts = o.get("easting"), o.get("northing"), o.get("alts")
        if not isinstance(e, (int, float)) or not isinstance(n, (int, float)) or not isinstance(alts, dict):
            continue
        v = next((alts[k] for k in ("COMB", "DTM2", "DTM25") if isinstance(alts.get(k), (int, float))), None)
        if v is None or not math.isfinite(v) or not -500 < v < 5000:
            continue
        i = int(math.floor((e - p.e0 - g.x0) / g.pas + 0.5))
        j = int(math.floor((n - p.n0 - g.y0) / g.pas + 0.5))
        if 0 <= i < g.nx and 0 <= j < g.ny:
            g.alt[j, i] = float(v)
            places += 1
    return places


# ================================================================================================ terrarium
TERRARIUM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
TUILES_MAX = 16
ZOOM_MAX = 14
TAILLE_TUILE = 256


def pixel_monde_x(lon, z):
    return (np.asarray(lon, dtype=float) + 180) / 360 * TAILLE_TUILE * 2.0 ** z


def pixel_monde_y(lat, z):
    r = np.radians(np.clip(np.asarray(lat, dtype=float), -85.0, 85.0))
    return (1 - np.log(np.tan(r) + 1 / np.cos(r)) / math.pi) / 2 * TAILLE_TUILE * 2.0 ** z


def terrarium_altitude(rgb):
    """Altitude = R·256 + G + B/256 − 32768 (tableau (…, 3) d'octets)."""
    rgb = np.asarray(rgb, dtype=np.float64)
    return rgb[..., 0] * 256.0 + rgb[..., 1] + rgb[..., 2] / 256.0 - 32768.0


def _bornes(g, p, z):
    xs, ys = [], []
    for cx in (0, g.nx - 1):
        for cy in (0, g.ny - 1):
            lat, lon = p.vers_geo(g.x0 + cx * g.pas, g.y0 + cy * g.pas)
            xs.append(float(pixel_monde_x(lon, z)))
            ys.append(float(pixel_monde_y(lat, z)))
    t = TAILLE_TUILE
    return (math.floor((min(xs) - 1) / t), math.floor((min(ys) - 1) / t), math.floor((max(xs) + 1) / t), math.floor((max(ys) + 1) / t))


def terrarium_tuiles(g, p, z):
    b = _bornes(g, p, z)
    n = 1 << z
    return [(z, tx % n, ty) for ty in range(max(0, b[1]), min(n - 1, b[3]) + 1) for tx in range(b[0], b[2] + 1)]


def terrarium_plan(g, p, lat0):
    """Plus petit zoom dont le pixel est ≤ au pas de la grille, puis réduit tant qu'il faut plus de 16 tuiles."""
    z = 1
    while z < ZOOM_MAX and 156_543.03 * math.cos(math.radians(lat0)) / 2.0 ** z > g.pas:
        z += 1
    while True:
        t = terrarium_tuiles(g, p, z)
        if len(t) <= TUILES_MAX or z <= 1:
            return z, t
        z -= 1


def terrarium_url(tuile):
    z, x, y = tuile
    return TERRARIUM_URL.format(z=z, x=x, y=y)


def terrarium_echantillonner(g, p, z, pixels):
    """Remplit la grille par interpolation bilinéaire dans la mosaïque ([pixels] : {tuile: altitudes 256 × 256})."""
    n = 1 << z
    t = TAILLE_TUILE
    jj, ii = np.mgrid[0:g.ny, 0:g.nx]
    lat, lon = p.vers_geo(g.x0 + ii * g.pas, g.y0 + jj * g.pas)
    fx = pixel_monde_x(lon, z) - 0.5
    fy = pixel_monde_y(lat, z) - 0.5
    x = np.floor(fx).astype(np.int64)
    y = np.floor(fy).astype(np.int64)
    tx, ty = fx - x, fy - y

    def valeur(px, py):
        out = np.full(px.shape, np.nan)
        tuile_x = np.floor_divide(px, t)
        tuile_y = np.floor_divide(py, t)
        for (zz, ax, ay), alt in pixels.items():
            if zz != z:
                continue
            m = ((tuile_x % n) == ax) & (tuile_y == ay)
            if m.any():
                out[m] = alt[py[m] - ay * t, px[m] - tuile_x[m] * t]
        return out

    v = (valeur(x, y) * (1 - tx) + valeur(x + 1, y) * tx) * (1 - ty) + (valeur(x, y + 1) * (1 - tx) + valeur(x + 1, y + 1) * tx) * ty
    g.alt = np.where(np.isfinite(v), v, np.nan)


# ================================================================================================ distances
def distance(lat1, lon1, lat2, lon2):
    """Distance (m) du grand cercle (haversine), scalaire ou tableaux."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = p2 - p1
    dl = np.radians(np.asarray(lon2, dtype=float) - np.asarray(lon1, dtype=float))
    h = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * RAYON_TERRE * np.arcsin(np.minimum(1.0, np.sqrt(h)))


def longueur(points):
    """Longueur (m) d'une suite de (lat, lon)."""
    if len(points) < 2:
        return 0.0
    a = np.asarray(points, dtype=float)
    return float(distance(a[:-1, 0], a[:-1, 1], a[1:, 0], a[1:, 1]).sum())


# ================================================================================================ via ferrata (OSM)
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OSM_ATTRIBUTION = "Via ferrata : © contributeurs OpenStreetMap, ODbL"
RAYON_MAX_M = 6000.0
VOIES_MAX = 60
JOINT_M = 40.0


def osm_boite(lat, lon, demi_cote_m):
    """Boîte (sud, ouest, nord, est) d'au plus 2 × 6 km de côté autour du centre."""
    r = min(RAYON_MAX_M, max(200.0, demi_cote_m))
    d_lat = r / M_PAR_DEGRE
    d_lon = r / (M_PAR_DEGRE * math.cos(math.radians(lat)))
    return lat - d_lat, lon - d_lon, lat + d_lat, lon + d_lon


def osm_requete(b):
    f = lambda v: "%.5f" % v  # noqa: E731
    return f'[out:json][timeout:25];way["highway"="via_ferrata"]({f(b[0])},{f(b[1])},{f(b[2])},{f(b[3])});out tags geom {VOIES_MAX};'


def osm_url(b):
    return OVERPASS_URL + "?data=" + quote_plus(osm_requete(b))


def osm_lire(texte):
    """Lit la réponse Overpass (way + geometry), regroupe les tronçons de même nom bout à bout ; plus longues d'abord.
    Rend [{"nom", "niveau", "points": [(lat, lon)], "longueur_m"}]."""
    try:
        racine = json.loads(texte)
    except (ValueError, TypeError):
        raise ValueError("OpenStreetMap (Overpass) a rendu une réponse illisible : réessaie plus tard.") from None
    if not isinstance(racine, dict):
        raise ValueError("OpenStreetMap (Overpass) a rendu une réponse illisible : réessaie plus tard.")
    elements = racine.get("elements")
    if not isinstance(elements, list):
        return []
    troncons = []
    for o in elements:
        if not isinstance(o, dict) or o.get("type") != "way" or not isinstance(o.get("geometry"), list):
            continue
        pts = []
        for q in o["geometry"]:
            if not isinstance(q, dict):
                continue
            la, lo = q.get("lat"), q.get("lon")
            if isinstance(la, (int, float)) and isinstance(lo, (int, float)) and -90 <= la <= 90 and -180 <= lo <= 180:
                pts.append((float(la), float(lo)))
        if len(pts) < 2:
            continue
        tags = o.get("tags") if isinstance(o.get("tags"), dict) else {}
        ident = o.get("id") if isinstance(o.get("id"), int) else 0
        nom = str(tags.get("name") or "").strip()[:60] or f"Via ferrata sans nom ({ident})"
        niveau = str(tags.get("via_ferrata_scale") or "").strip()[:10] or None
        troncons.append({"nom": nom, "niveau": niveau, "pts": pts})

    def dist(a, b):
        return float(distance(a[0], a[1], b[0], b[1]))

    groupes = {}
    for t in troncons:
        groupes.setdefault(t["nom"], []).append(t)
    sortie = []
    for nom, groupe in groupes.items():
        reste = sorted(groupe, key=lambda t: -len(t["pts"]))
        niveau = next((t["niveau"] for t in groupe if t["niveau"]), None)
        while reste:
            chaine = list(reste.pop(0)["pts"])
            while True:
                meilleur, cas, dmin = -1, 0, JOINT_M
                for k, t in enumerate(reste):
                    c = (dist(chaine[-1], t["pts"][0]), dist(chaine[-1], t["pts"][-1]),
                         dist(chaine[0], t["pts"][-1]), dist(chaine[0], t["pts"][0]))
                    for q, v in enumerate(c):
                        if v < dmin:
                            dmin, meilleur, cas = v, k, q
                if meilleur < 0:
                    break
                t = reste.pop(meilleur)["pts"]
                if cas == 0:
                    chaine += t
                elif cas == 1:
                    chaine += t[::-1]
                elif cas == 2:
                    chaine = t + chaine
                else:
                    chaine = t[::-1] + chaine
            sortie.append({"nom": nom, "niveau": niveau, "points": chaine, "longueur_m": longueur(chaine)})
    sortie.sort(key=lambda v: -v["longueur_m"])
    return sortie[:VOIES_MAX]
