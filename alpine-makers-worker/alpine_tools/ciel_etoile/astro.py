# -*- coding: utf-8 -*-
"""Calcul astronomique : portage ligne à ligne de C:\\AlpineGift core Astro.kt (mêmes formules, même ordre des
opérations en double précision).

D'après les formules publiques de J. Meeus (« Astronomical Algorithms », 2e éd.) et les éléments képlériens approchés
du JPL (E. M. Standish, valables de 1800 à 2050). Précision visée : celle d'une carte du ciel (quelques minutes d'arc
pour les étoiles et les planètes, un dixième de degré pour la Lune). Angles en degrés, temps en jours juliens ; UT pour
la rotation de la Terre, TT pour la Lune et les planètes.
"""
import math
from collections import namedtuple

J2000 = 2451545.0
_RAD = math.pi / 180.0

# Position équatoriale (degrés) et distance (UA pour les planètes, km pour la Lune, 0 pour une étoile).
Equatoriales = namedtuple("Equatoriales", "ra dec distance", defaults=(0.0,))
# Hauteur au-dessus de l'horizon et azimut depuis le nord vers l'est (degrés).
Horizontales = namedtuple("Horizontales", "hauteur azimut")
# Lune : position, fraction éclairée (0..1), élongation au Soleil (degrés), croissante.
Lune = namedtuple("Lune", "position fraction elongation croissante")


def _borne(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def rad(d):
    return d * _RAD


def deg(r):
    return r / _RAD


def norm(d):
    """Angle ramené dans [0, 360[."""
    r = d - 360.0 * math.floor(d / 360.0)
    return 0.0 if r >= 360.0 else r


def jour_julien_ms(ms):
    """Jour julien (UT) d'un instant en millisecondes depuis 1970 (UTC)."""
    return ms / 86_400_000.0 + 2440587.5


def jour_julien(annee, mois, jour):
    """Jour julien d'une date du calendrier grégorien (jour décimal), Meeus 7.1."""
    y, m = annee, mois
    if m <= 2:
        y -= 1
        m += 12
    a = math.floor(y / 100.0)
    b = 2 - a + math.floor(a / 4)
    return math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + jour + b - 1524.5


def delta_t(jd_ut):
    """ΔT = TT − UT en secondes (polynômes d'Espenak et Meeus, 1900 à 2150)."""
    y = 2000.0 + (jd_ut - J2000) / 365.25
    if y < 1900:
        return 0.0
    if y < 1920:
        t = y - 1900
        return -2.79 + 1.494119 * t - 0.0598939 * t * t + 0.0061966 * t * t * t - 0.000197 * t * t * t * t
    if y < 1941:
        t = y - 1920
        return 21.20 + 0.84493 * t - 0.076100 * t * t + 0.0020936 * t * t * t
    if y < 1961:
        t = y - 1950
        return 29.07 + 0.407 * t - t * t / 233 + t * t * t / 2547
    if y < 1986:
        t = y - 1975
        return 45.45 + 1.067 * t - t * t / 260 - t * t * t / 718
    if y < 2005:
        t = y - 2000
        return (63.86 + 0.3345 * t - 0.060374 * t * t + 0.0017275 * t * t * t + 0.000651814 * t * t * t * t
                + 0.00002373599 * t * t * t * t * t)
    if y < 2050:
        t = y - 2000
        return 62.92 + 0.32217 * t + 0.005589 * t * t
    u = (y - 1820) / 100
    return -20 + 32 * u * u - 0.5628 * (2150 - y)


def jour_julien_tt(jd_ut):
    return jd_ut + delta_t(jd_ut) / 86400.0


def temps_sideral(jd_ut):
    """Temps sidéral moyen de Greenwich (degrés), Meeus 12.4."""
    t = (jd_ut - J2000) / 36525.0
    return norm(280.46061837 + 360.98564736629 * (jd_ut - J2000) + 0.000387933 * t * t - t * t * t / 38_710_000.0)


def obliquite(jd_tt):
    """Obliquité moyenne de l'écliptique (degrés), Meeus 22.2."""
    t = (jd_tt - J2000) / 36525.0
    return 23.0 + 26.0 / 60 + (21.448 - 46.8150 * t - 0.00059 * t * t + 0.001813 * t * t * t) / 3600.0


def precession(ra_deg, dec_deg, jd_tt):
    """Précession rigoureuse depuis J2000 jusqu'à l'équinoxe de la date (Meeus 21.2 à 21.4)."""
    t = (jd_tt - J2000) / 36525.0
    zeta = rad((2306.2181 * t + 0.30188 * t * t + 0.017998 * t * t * t) / 3600.0)
    z = rad((2306.2181 * t + 1.09468 * t * t + 0.018203 * t * t * t) / 3600.0)
    theta = rad((2004.3109 * t - 0.42665 * t * t - 0.041833 * t * t * t) / 3600.0)
    a0 = rad(ra_deg)
    d0 = rad(dec_deg)
    a = math.cos(d0) * math.sin(a0 + zeta)
    b = math.cos(theta) * math.cos(d0) * math.cos(a0 + zeta) - math.sin(theta) * math.sin(d0)
    c = math.sin(theta) * math.cos(d0) * math.cos(a0 + zeta) + math.cos(theta) * math.sin(d0)
    if abs(c) > 0.99:
        dec = deg(math.atan2(c, math.sqrt(a * a + b * b)))
    else:
        dec = deg(math.asin(c))
    return Equatoriales(norm(deg(math.atan2(a, b) + z)), dec)


def mouvement_propre(ra_deg, dec_deg, pm_ra, pm_dec, annees):
    """Mouvement propre (pm_ra = cos δ · dα/dt, arcsec/an) appliqué pendant ``annees``."""
    c = max(math.cos(rad(dec_deg)), 1e-6)
    return Equatoriales(norm(ra_deg + pm_ra * annees / 3600.0 / c), _borne(dec_deg + pm_dec * annees / 3600.0, -90.0, 90.0))


def horizontales(ra_deg, dec_deg, lat_deg, tsl_deg):
    """Coordonnées horizontales pour la latitude et le temps sidéral local (Meeus 13.5, 13.6)."""
    h = rad(tsl_deg - ra_deg)
    d = rad(dec_deg)
    phi = rad(lat_deg)
    alt = math.asin(_borne(math.sin(phi) * math.sin(d) + math.cos(phi) * math.cos(d) * math.cos(h), -1.0, 1.0))
    # Azimut de Meeus compté depuis le sud ; +180° : depuis le nord, vers l'est.
    a_sud = math.atan2(math.sin(h), math.cos(h) * math.sin(phi) - math.tan(d) * math.cos(phi))
    return Horizontales(deg(alt), norm(deg(a_sud) + 180.0))


def vers_equatoriales(lambda_deg, beta_deg, eps_deg, distance=0.0):
    """Écliptiques (λ, β) → équatoriales pour l'obliquité ``eps_deg`` (Meeus 13.3, 13.4)."""
    l = rad(lambda_deg)
    b = rad(beta_deg)
    e = rad(eps_deg)
    ra = math.atan2(math.sin(l) * math.cos(e) - math.tan(b) * math.sin(e), math.cos(l))
    dec = math.asin(_borne(math.sin(b) * math.cos(e) + math.cos(b) * math.sin(e) * math.sin(l), -1.0, 1.0))
    return Equatoriales(norm(deg(ra)), deg(dec), distance)


def soleil_ecliptique(jd_tt):
    """Soleil : longitude apparente (degrés) et distance (UA), Meeus chapitre 25 (précision basse, ≈ 0,01°)."""
    t = (jd_tt - J2000) / 36525.0
    l0 = 280.46646 + 36000.76983 * t + 0.0003032 * t * t
    m = rad(357.52911 + 35999.05029 * t - 0.0001537 * t * t)
    e = 0.016708634 - 0.000042037 * t - 0.0000001267 * t * t
    c = ((1.914602 - 0.004817 * t - 0.000014 * t * t) * math.sin(m) + (0.019993 - 0.000101 * t) * math.sin(2 * m)
         + 0.000289 * math.sin(3 * m))
    vrai = l0 + c
    nu = m + rad(c)
    r = 1.000001018 * (1 - e * e) / (1 + e * math.cos(nu))
    omega = rad(125.04 - 1934.136 * t)
    return norm(vrai - 0.00569 - 0.00478 * math.sin(omega)), r


def soleil(jd_tt):
    l, r = soleil_ecliptique(jd_tt)
    return vers_equatoriales(l, 0.0, obliquite(jd_tt), r)


# ============================================================================================ Lune
# Termes principaux de Meeus, chapitre 47 (tables 47.A et 47.B) : multiples de D, M, M', F et coefficient.
_TERMES_LR = (
    (0, 0, 1, 0, 6288774, -20905355), (2, 0, -1, 0, 1274027, -3699111), (2, 0, 0, 0, 658314, -2955968),
    (0, 0, 2, 0, 213618, -569925), (0, 1, 0, 0, -185116, 48888), (0, 0, 0, 2, -114332, -3149),
    (2, 0, -2, 0, 58793, 246158), (2, -1, -1, 0, 57066, -152138), (2, 0, 1, 0, 53322, -170733),
    (2, -1, 0, 0, 45758, -204586), (0, 1, -1, 0, -40923, -129620), (1, 0, 0, 0, -34720, 108743),
    (0, 1, 1, 0, -30383, 104755), (2, 0, 0, -2, 15327, 10321), (0, 0, 1, 2, -12528, 0),
    (0, 0, 1, -2, 10980, 79661), (4, 0, -1, 0, 10675, -34782), (0, 0, 3, 0, 10034, -23210),
    (4, 0, -2, 0, 8548, -21636), (2, 1, -1, 0, -7888, 24208), (2, 1, 0, 0, -6766, 30824),
    (1, 0, -1, 0, -5163, -8379), (1, 1, 0, 0, 4987, -16675), (2, -1, 1, 0, 4036, -12831),
    (2, 0, 2, 0, 3994, -10445), (4, 0, 0, 0, 3861, -11650), (2, 0, -3, 0, 3665, 14403),
    (0, 1, -2, 0, -2689, -7003), (2, 0, -1, 2, -2602, 0), (2, -1, -2, 0, 2390, 10056),
    (1, 0, 1, 0, -2348, 6322), (2, -2, 0, 0, 2236, -9884), (0, 1, 2, 0, -2120, 5751),
    (0, 2, 0, 0, -2069, 0), (2, -2, -1, 0, 2048, -4950), (2, 0, 1, -2, -1773, 4130),
    (2, 0, 0, 2, -1595, 0), (4, -1, -1, 0, 1215, -3958), (0, 0, 2, 2, -1110, 0),
    (3, 0, -1, 0, -892, 3258), (2, 1, 1, 0, -810, 2616), (4, -1, -2, 0, 759, -1897),
    (0, 2, -1, 0, -713, -2117), (2, 2, -1, 0, -700, 2354), (2, 1, -2, 0, 691, 0),
    (2, -1, 0, -2, 596, 0), (4, 0, 1, 0, 549, -1423), (0, 0, 4, 0, 537, -1117),
    (4, -1, 0, 0, 520, -1571), (1, 0, -2, 0, -487, -1739), (2, 1, 0, -2, -399, 0),
    (0, 0, 2, -2, -381, -4421), (1, 1, 1, 0, 351, 0), (3, 0, -2, 0, -340, 0),
    (4, 0, -3, 0, 330, 0), (2, -1, 2, 0, 327, 0), (0, 2, 1, 0, -323, 1165),
    (1, 1, -1, 0, 299, 0), (2, 0, 3, 0, 294, 0), (2, 0, -1, -2, 0, 8752),
)
_TERMES_B = (
    (0, 0, 0, 1, 5128122), (0, 0, 1, 1, 280602), (0, 0, 1, -1, 277693), (2, 0, 0, -1, 173237),
    (2, 0, -1, 1, 55413), (2, 0, -1, -1, 46271), (2, 0, 0, 1, 32573), (0, 0, 2, 1, 17198),
    (2, 0, 1, -1, 9266), (0, 0, 2, -1, 8822), (2, -1, 0, -1, 8216), (2, 0, -2, -1, 4324),
    (2, 0, 1, 1, 4200), (2, 1, 0, -1, -3359), (2, -1, -1, 1, 2463), (2, -1, 0, 1, 2211),
    (2, -1, -1, -1, 2065), (0, 1, -1, -1, -1870), (4, 0, -1, -1, 1828), (0, 1, 0, 1, -1794),
    (0, 0, 0, 3, -1749), (0, 1, -1, 1, -1565), (1, 0, 0, 1, -1491), (0, 1, 1, 1, -1475),
    (0, 1, 1, -1, -1410), (0, 1, 0, -1, -1344), (1, 0, 0, -1, -1335), (0, 0, 3, 1, 1107),
    (4, 0, 0, -1, 1021), (4, 0, -1, 1, 833),
)


def lune_ecliptique(jd_tt):
    """Longitude, latitude écliptiques (degrés, sans nutation) et distance (km) de la Lune, Meeus chapitre 47."""
    t = (jd_tt - J2000) / 36525.0
    lp = norm(218.3164477 + 481267.88123421 * t - 0.0015786 * t * t + t * t * t / 538841 - t * t * t * t / 65194000)
    d = norm(297.8501921 + 445267.1114034 * t - 0.0018819 * t * t + t * t * t / 545868 - t * t * t * t / 113065000)
    m = norm(357.5291092 + 35999.0502909 * t - 0.0001536 * t * t + t * t * t / 24490000)
    mp = norm(134.9633964 + 477198.8675055 * t + 0.0087414 * t * t + t * t * t / 69699 - t * t * t * t / 14712000)
    f = norm(93.2720950 + 483202.0175233 * t - 0.0036539 * t * t - t * t * t / 3526000 + t * t * t * t / 863310000)
    e = 1 - 0.002516 * t - 0.0000074 * t * t
    a1 = 119.75 + 131.849 * t
    a2 = 53.09 + 479264.290 * t
    a3 = 313.45 + 481266.484 * t
    sl = sr = sb = 0.0
    for k in _TERMES_LR:
        arg = rad(k[0] * d + k[1] * m + k[2] * mp + k[3] * f)
        fe = e if abs(k[1]) == 1 else e * e if abs(k[1]) == 2 else 1.0
        sl += k[4] * fe * math.sin(arg)
        sr += k[5] * fe * math.cos(arg)
    for k in _TERMES_B:
        arg = rad(k[0] * d + k[1] * m + k[2] * mp + k[3] * f)
        fe = e if abs(k[1]) == 1 else e * e if abs(k[1]) == 2 else 1.0
        sb += k[4] * fe * math.sin(arg)
    sl += 3958 * math.sin(rad(a1)) + 1962 * math.sin(rad(lp - f)) + 318 * math.sin(rad(a2))
    sb += (-2235 * math.sin(rad(lp)) + 382 * math.sin(rad(a3)) + 175 * math.sin(rad(a1 - f)) + 175 * math.sin(rad(a1 + f))
           + 127 * math.sin(rad(lp - mp)) - 115 * math.sin(rad(lp + mp)))
    return norm(lp + sl / 1e6), sb / 1e6, 385000.56 + sr / 1000.0


def lune(jd_tt):
    """Lune géocentrique (équateur de la date), fraction éclairée (Meeus 48.2 à 48.4 simplifiés)."""
    l, b, r = lune_ecliptique(jd_tt)
    ls, rs = soleil_ecliptique(jd_tt)
    pos = vers_equatoriales(l, b, obliquite(jd_tt), r)
    cos_psi = math.cos(rad(b)) * math.cos(rad(l - ls))
    psi = math.acos(_borne(cos_psi, -1.0, 1.0))
    rs_km = rs * 149_597_870.7
    i = math.atan2(rs_km * math.sin(psi), r - rs_km * cos_psi)
    return Lune(pos, (1 + math.cos(i)) / 2, deg(psi), norm(l - ls) < 180.0)


# ============================================================================================ planètes
# Éléments képlériens approchés du JPL (Standish), J2000 : a, e, I, L, ϖ, Ω et leurs variations par siècle.
Elements = namedtuple("Elements", "nom a e i l peri noeud da de di dl dperi dnoeud")

TERRE = Elements("Terre", 1.00000261, 0.01671123, -0.00001531, 100.46457166, 102.93768193, 0.0,
                 0.00000562, -0.00004392, -0.01294668, 35999.37244981, 0.32327364, 0.0)
PLANETES = (
    Elements("Mercure", 0.38709927, 0.20563593, 7.00497902, 252.25032350, 77.45779628, 48.33076593,
             0.00000037, 0.00001906, -0.00594749, 149472.67411175, 0.16047689, -0.12534081),
    Elements("Vénus", 0.72333566, 0.00677672, 3.39467605, 181.97909950, 131.60246718, 76.67984255,
             0.00000390, -0.00004107, -0.00078890, 58517.81538729, 0.00268329, -0.27769418),
    Elements("Mars", 1.52371034, 0.09339410, 1.84969142, -4.55343205, -23.94362959, 49.55953891,
             0.00001847, 0.00007882, -0.00813131, 19140.30268499, 0.44441088, -0.29257343),
    Elements("Jupiter", 5.20288700, 0.04838624, 1.30439695, 34.39644051, 14.72847983, 100.47390909,
             -0.00011607, -0.00013253, -0.00183714, 3034.74612775, 0.21252668, 0.20469106),
    Elements("Saturne", 9.53667594, 0.05386179, 2.48599187, 49.95424423, 92.59887831, 113.66242448,
             -0.00125060, -0.00050991, 0.00193609, 1222.49362201, -0.41897216, -0.28867794),
)


def heliocentrique(el, jd_tt):
    """Position héliocentrique écliptique J2000 (UA)."""
    t = (jd_tt - J2000) / 36525.0
    a = el.a + el.da * t
    e = el.e + el.de * t
    inc = rad(el.i + el.di * t)
    l = el.l + el.dl * t
    peri = el.peri + el.dperi * t
    noeud = el.noeud + el.dnoeud * t
    om = rad(peri - noeud)
    no = rad(noeud)
    m_deg = norm(l - peri)
    if m_deg > 180:
        m_deg -= 360
    m = rad(m_deg)
    ea = m + e * math.sin(m)
    # Comme Kotlin « repeat(30) { …; if (abs(d) < 1e-12) return@repeat } » : le return@repeat ne sort que du tour
    # courant, les 30 itérations sont toujours faites (même résultat au bit près).
    for _ in range(30):
        d = (ea - e * math.sin(ea) - m) / (1 - e * math.cos(ea))
        ea -= d
    xp = a * (math.cos(ea) - e)
    yp = a * math.sqrt(1 - e * e) * math.sin(ea)
    x = ((math.cos(om) * math.cos(no) - math.sin(om) * math.sin(no) * math.cos(inc)) * xp
         + (-math.sin(om) * math.cos(no) - math.cos(om) * math.sin(no) * math.cos(inc)) * yp)
    y = ((math.cos(om) * math.sin(no) + math.sin(om) * math.cos(no) * math.cos(inc)) * xp
         + (-math.sin(om) * math.sin(no) + math.cos(om) * math.cos(no) * math.cos(inc)) * yp)
    z = math.sin(om) * math.sin(inc) * xp + math.cos(om) * math.sin(inc) * yp
    return x, y, z


def planete(el, jd_tt):
    """Planète géocentrique, équateur et équinoxe de la date, corrigée du temps de lumière."""
    terre = heliocentrique(TERRE, jd_tt)
    p = heliocentrique(el, jd_tt)
    for _ in range(2):
        dx, dy, dz = p[0] - terre[0], p[1] - terre[1], p[2] - terre[2]
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        p = heliocentrique(el, jd_tt - dist * 0.0057755183)  # temps de lumière : 1 UA ≈ 0,0057755 jour
    dx, dy, dz = p[0] - terre[0], p[1] - terre[1], p[2] - terre[2]
    eps = rad(23.43928)
    xe = dx
    ye = dy * math.cos(eps) - dz * math.sin(eps)
    ze = dy * math.sin(eps) + dz * math.cos(eps)
    ra = norm(deg(math.atan2(ye, xe)))
    dec = deg(math.atan2(ze, math.sqrt(xe * xe + ye * ye)))
    datee = precession(ra, dec, jd_tt)
    return Equatoriales(datee.ra, datee.dec, math.sqrt(dx * dx + dy * dy + dz * dz))
