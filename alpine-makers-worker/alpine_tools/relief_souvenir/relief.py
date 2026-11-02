"""Relief souvenir — terrain multicolore (port de Relief.kt d'Alpine Gift, calcul vectorisé numpy).

Chaque tranche d'altitude est une PIÈCE FERMÉE distincte (le volume du relief compris entre deux altitudes) qui reçoit
son filament ; le tracé, les repères, les lacs et le texte gardent leur couleur. Coordonnées du modèle en mm : x vers
l'est, y vers le nord, z vers le haut ; le bandeau du texte est devant le relief (y < 0).
"""
import dataclasses
import math

import numpy as np

from . import geo, maillage, police

TRIANGLES_MAX = 2_000_000
COULEURS_MIN, COULEURS_MAX = 2, 8
EPS = 1e-4
ENFONCEMENT_TRACE = 0.4
EPAISSEUR_TEXTE = 0.8
PLATEAU_MM = 256.0

COULEURS_DEFAUT = [("#1D3B2A", "Vert forêt"), ("#8C8C8C", "Gris"), ("#F4F4F4", "Blanc")]


@dataclasses.dataclass
class ParamRelief:
    forme: str = geo.RECTANGLE
    largeur_mm: float = 150.0
    socle_mm: float = 3.0
    sans_socle: bool = False
    epaisseur_min_mm: float = 1.2
    exageration: float = 1.5
    couleurs: list = dataclasses.field(default_factory=lambda: list(COULEURS_DEFAUT))
    repartition: str = "surface"          # surface | altitude | manuelle
    seuils_m: list = dataclasses.field(default_factory=list)
    lacs: bool = False
    niveau_lac_m: float = None
    couleur_lac: tuple = ("#1E5EBE", "Bleu")
    trace: bool = True
    couleur_trace: tuple = ("#BE202E", "Rouge")
    largeur_trace_mm: float = 1.4
    hauteur_trace_mm: float = 0.8
    reperes: bool = False
    trace_surface: bool = False           # vue Montagne 3D : tube posé sur la surface, repères en boules
    relais: int = 0
    couleur_reperes: tuple = ("#F4F4F4", "Blanc")
    sommet_par_y: bool = False            # mode Face : sommet = point le plus haut de la plaque
    texte1: str = ""
    texte2: str = ""
    couleur_texte: tuple = ("#F4F4F4", "Blanc")
    bandeau_mm: float = 14.0
    couleur_socle: tuple = ("#1D3B2A", "Vert forêt")
    finesse_mm: float = 0.5


def _borne(v, lo, hi):
    return min(hi, max(lo, v))


def borner(p):
    q = dataclasses.replace(
        p, couleurs=list(p.couleurs)[:COULEURS_MAX], largeur_mm=_borne(p.largeur_mm, 40.0, 250.0), socle_mm=_borne(p.socle_mm, 1.0, 15.0),
        epaisseur_min_mm=_borne(p.epaisseur_min_mm, 0.6, 10.0), exageration=_borne(p.exageration, 0.5, 10.0),
        largeur_trace_mm=_borne(p.largeur_trace_mm, 0.6, 4.0), hauteur_trace_mm=_borne(p.hauteur_trace_mm, 0.2, 3.0),
        bandeau_mm=0.0 if p.bandeau_mm <= 0 else _borne(p.bandeau_mm, 8.0, 40.0), finesse_mm=_borne(p.finesse_mm, 0.25, 2.0),
        texte1=p.texte1[:40], texte2=p.texte2[:60], relais=int(_borne(p.relais, 0, 12)))
    if q.sans_socle:
        q = dataclasses.replace(q, socle_mm=0.0, bandeau_mm=0.0)
    return q


@dataclasses.dataclass
class Analyse:
    alt_min: float
    alt_max: float
    seuils: np.ndarray
    niveau_lac: float = None


class Contexte:
    """Grille, zone, réglages bornés et analyse : passages plateau (mm) ↔ plan local (m) ↔ altitude ↔ z (mm)."""

    def __init__(self, g, zone, p, a=None):
        self.g, self.zone, self.p, self.a = g, zone, borner(p), a
        b = zone.boite_m
        self.w = self.p.largeur_mm
        self.pr = self.p.largeur_mm * b[1] / b[0]
        self.e = self.p.largeur_mm / b[0]  # mm par m

    def vers_local(self, x, y):
        return (np.asarray(x, dtype=float) - self.w / 2) / self.e, (np.asarray(y, dtype=float) - self.pr / 2) / self.e

    def vers_modele(self, x, y):
        return np.asarray(x, dtype=float) * self.e + self.w / 2, np.asarray(y, dtype=float) * self.e + self.pr / 2

    def altitude(self, x, y, lacs=True):
        lx, ly = self.vers_local(x, y)
        v = self.g.altitude(lx, ly)
        if lacs and self.a is not None and self.a.niveau_lac is not None:
            v = np.maximum(v, self.a.niveau_lac)
        return v

    def z_alt(self, alt):
        p = self.p
        v = p.socle_mm + p.epaisseur_min_mm + (np.asarray(alt, dtype=float) - self.a.alt_min) * self.e * p.exageration
        return np.maximum(p.socle_mm + 0.2, v)

    def zT(self, x, y):
        return self.z_alt(self.altitude(x, y))


# ================================================================================================ plan 2D
class Plan:
    """Maillage plan (mm) : triangles directs (m, 3) et bord (boucle directe des sommets du bord)."""

    def __init__(self, x, y, tri, bord):
        self.x, self.y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
        self.tri = np.asarray(tri, dtype=np.int64).reshape(-1, 3)
        self.bord = np.asarray(bord, dtype=np.int64)
        n = len(self.x)
        a, b = self.bord, np.roll(self.bord, -1)
        self.cles_bord = np.minimum(a, b) * n + np.maximum(a, b)

    @property
    def n(self):
        return len(self.x)

    def contour(self):
        return self.x[self.bord], self.y[self.bord]


def plan2d(forme, largeur, profondeur, pas):
    if forme == geo.RECTANGLE:
        nx = max(2, int(math.floor(largeur / pas + 0.5)))
        ny = max(2, int(math.floor(profondeur / pas + 0.5)))
        px, py = largeur / nx, profondeur / ny
        w = nx + 1
        k = np.arange(w * (ny + 1))
        x, y = (k % w) * px, (k // w) * py
        jj, ii = np.mgrid[0:ny, 0:nx]
        a = (jj * w + ii).ravel()
        b, c = a + 1, a + w
        d = c + 1
        tri = np.concatenate([np.stack([a, b, d], 1), np.stack([a, d, c], 1)])
        bord = list(range(nx)) + [j * w + nx for j in range(ny)] + [ny * w + i for i in range(nx, 0, -1)] + [j * w for j in range(ny, 0, -1)]
        return Plan(x, y, tri, bord)
    r = largeur / 2
    n = max(3, int(math.floor(r / pas + 0.5)))
    s = r / n
    cx, cy = largeur / 2, profondeur / 2
    r3 = math.sqrt(3.0) / 2
    bb, aa = np.mgrid[-n:n + 1, -n:n + 1]
    garde = np.abs(aa + bb) <= n
    a_, b_ = aa[garde], bb[garde]
    dx = s * (a_ + b_ / 2.0)
    dy = s * b_ * r3
    if forme == geo.CERCLE:
        th = np.arctan2(dy, dx)
        phi = np.mod(np.mod(th - math.pi / 6, math.pi / 3) + math.pi / 3, math.pi / 3)
        phi = np.where(phi > math.pi / 6, phi - math.pi / 3, phi)
        f = np.where((a_ != 0) | (b_ != 0), np.cos(phi) / r3, 1.0)
        dx, dy = dx * f, dy * f
    idx = np.full((2 * n + 3, 2 * n + 3), -1, dtype=np.int64)  # décalé de n + 1 (bords à −1)
    idx[b_ + n + 1, a_ + n + 1] = np.arange(len(a_))

    def ident(a, b):
        return idx[b + n + 1, a + n + 1]

    B, A = np.mgrid[-n - 1:n + 1, -n - 1:n + 1]
    A, B = A.ravel(), B.ravel()
    p0, p1 = ident(A, B), ident(A + 1, B)
    p2, p3 = ident(A, B + 1), ident(A + 1, B + 1)
    m1 = (p0 >= 0) & (p1 >= 0) & (p2 >= 0)
    m2 = (p1 >= 0) & (p2 >= 0) & (p3 >= 0)
    tri = np.concatenate([np.stack([p0[m1], p1[m1], p2[m1]], 1), np.stack([p1[m2], p3[m2], p2[m2]], 1)])
    coins = [(n, 0), (0, n), (-n, n), (-n, 0), (0, -n), (n, -n)]
    bord = []
    for c in range(6):
        a0, b0 = coins[c]
        a1, b1 = coins[(c + 1) % 6]
        da, db = int(np.sign(a1 - a0)), int(np.sign(b1 - b0))
        a, b = a0, b0
        for _ in range(n):
            bord.append(int(idx[b + n + 1, a + n + 1]))
            a += da
            b += db
    return Plan(cx + dx, cy + dy, tri, bord)


def contour(forme, largeur, profondeur):
    return plan2d(forme, largeur, profondeur, max(largeur, profondeur) / 48).contour()


def contient(cx, cy, px, py):
    """Points (px, py) dans le polygone (cx, cy) (règle pair-impair), vectorisé."""
    px = np.asarray(px, dtype=float)
    py = np.asarray(py, dtype=float)
    dedans = np.zeros(px.shape, dtype=bool)
    n = len(cx)
    j = n - 1
    for i in range(n):
        xi, yi, xj, yj = cx[i], cy[i], cx[j], cy[j]
        if yj != yi:
            croise = ((yi > py) != (yj > py)) & (px < (xj - xi) * (py - yi) / (yj - yi) + xi)
            dedans ^= croise
        j = i
    return dedans


def distance_segments(px, py, cx, cy):
    """Distance de chaque point au contour fermé (cx, cy)."""
    px = np.asarray(px, dtype=float)
    py = np.asarray(py, dtype=float)
    d = np.full(px.shape, np.inf)
    n = len(cx)
    for k in range(n):
        ax, ay, bx, by = cx[k], cy[k], cx[(k + 1) % n], cy[(k + 1) % n]
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        t = np.zeros_like(px) if l2 <= 0 else np.clip(((px - ax) * dx + (py - ay) * dy) / l2, 0, 1)
        d = np.minimum(d, np.hypot(px - ax - t * dx, py - ay - t * dy))
    return d


def dans_forme(x, y, marge, forme_contour, rectangle, largeur, profondeur):
    """Points (mm) à plus de [marge] du bord de la forme (marge scalaire ou tableau)."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    ok = (x >= marge) & (y >= marge) & (x <= largeur - marge) & (y <= profondeur - marge)
    if rectangle or not ok.any():
        return ok
    cx, cy = forme_contour
    ok &= contient(cx, cy, x, y)
    ok &= distance_segments(x, y, cx, cy) >= marge
    return ok


# ================================================================================================ analyse
def seuils_auto(triees, lo, hi, n, rep, choisis=()):
    """n − 1 seuils strictement croissants dans ]lo, hi[ : quantiles (surfaces égales), paliers égaux ou choisis."""
    if n <= 1 or hi - lo < 1e-6:
        return np.zeros(0)
    if rep == "manuelle" and len(choisis) == n - 1:
        brut = sorted(choisis)
    elif rep == "surface" and len(triees):
        brut = [triees[min(len(triees) - 1, (len(triees) * k) // n)] for k in range(1, n)]
    else:
        brut = [lo + (hi - lo) * k / n for k in range(1, n)]
    ecart = max(0.05, (hi - lo) * 0.002)
    out, prec = [], lo
    for k in range(n - 1):
        v = _borne(brut[k], prec + ecart, hi - ecart * (n - 1 - k))
        out.append(v)
        prec = v
    return np.asarray(out, dtype=float)


def analyser(g, zone, p0):
    """Altitudes dans la forme (160 × 160 échantillons), extrêmes, seuils et niveau des lacs."""
    c = Contexte(g, zone, p0)
    p = c.p
    n = 160
    jj, ii = np.mgrid[0:n, 0:n]
    x = ((ii + 0.5) * c.w / n).ravel()
    y = ((jj + 0.5) * c.pr / n).ravel()
    if p.forme != geo.RECTANGLE:
        cx, cy = contour(p.forme, c.w, c.pr)
        m = contient(cx, cy, x, y)
        x, y = x[m], y[m]
    v = c.altitude(x, y, lacs=False)
    v = np.sort(v[np.isfinite(v)])
    if not len(v):
        raise ValueError("Aucune altitude connue dans la zone.")
    mn, mx = float(v[0]), float(v[-1])
    lac = None
    if p.lacs:
        lac = _borne(p.niveau_lac_m if p.niveau_lac_m is not None else mn + 1.0, mn, mx)
    terre = v[v > lac] if lac is not None else v
    nb = _borne(len(p.couleurs), 1, COULEURS_MAX)
    lo = lac if lac is not None else mn
    seuils = seuils_auto(terre if len(terre) else v, lo, mx, nb, p.repartition, p.seuils_m)
    return Analyse(mn, mx, seuils, lac)


def tranche_index(a, alt):
    """Index de la tranche d'une altitude (0 = la plus basse ; −1 = lac), vectorisé."""
    alt = np.asarray(alt, dtype=float)
    k = np.zeros(alt.shape, dtype=np.int64)
    for s in a.seuils:
        k += alt >= s
    if a.niveau_lac is not None:
        k = np.where(alt <= a.niveau_lac + 1e-6, -1, k)
    return k


# ================================================================================================ tranches
def tranche3d(plan, h, niveaux, i_lo, i_hi, z_bas):
    """Pièce de la tranche [lo, hi] : volume du relief entre ces hauteurs, fermé. Chaque triangle du plan est découpé
    exactement par les deux plans (le terrain y est linéaire) : dessus = terrain ou plafond à hi, dessous plat à lo,
    parois sur le bord de la forme. Les sommets sont identifiés par (sommet du plan | arête coupée, niveau) : deux
    triangles voisins partagent exactement les mêmes sommets. Triangles entièrement dans la tranche : calcul vectorisé ;
    triangles coupés par un niveau ou posés sur le bord : découpe de Sutherland–Hodgman (port direct)."""
    nv = plan.n
    nl = len(niveaux) + 1
    lo = niveaux[i_lo] if i_lo >= 0 else z_bas
    hi = niveaux[i_hi] if i_hi >= 0 else math.inf
    tri = plan.tri
    H = h[tri]
    hmax, hmin = H.max(1), H.min(1)
    garde = hmax >= lo
    e = np.stack([tri, np.roll(tri, -1, axis=1)], 2)
    cles = np.minimum(e[..., 0], e[..., 1]) * nv + np.maximum(e[..., 0], e[..., 1])
    sur_bord = np.isin(cles, plan.cles_bord).any(1)
    simple = garde & ~sur_bord & ((i_lo < 0) | (hmin >= lo)) & ((i_hi < 0) | (hmax <= hi) | (hmin >= hi))
    ts = tri[simple]
    cles_t, coords_t = [], []
    if len(ts):
        dessous = np.stack([ts[:, 0], ts[:, 2], ts[:, 1]], 1)
        cles_t.append((dessous * 2).ravel())
        v = dessous.ravel()
        coords_t.append(np.stack([plan.x[v], plan.y[v], np.full(len(v), lo)], 1))
        cles_t.append((ts * 2 + 1).ravel())
        v = ts.ravel()
        coords_t.append(np.stack([plan.x[v], plan.y[v], np.minimum(h[v], hi)], 1))
    # Triangles découpés : port direct.
    bord = set(plan.cles_bord.tolist())
    px, py = plan.x, plan.y
    coords = {}
    sortie = []

    def cle2d(pt):
        return pt[0] if pt[2] < 0 else nv + ((pt[0] * nv + pt[1]) * nl + pt[2])

    def sommet(pt, dessus):
        haut = dessus and not (pt[2] == i_lo and i_lo >= 0)
        k = cle2d(pt) * 2 + (1 if haut else 0)
        if k not in coords:
            coords[k] = (pt[3], pt[4], min(pt[5], hi) if haut else lo)
        return k

    def ajouter(a, b, c):
        if a != b and b != c and a != c:
            sortie.append((a, b, c))

    def arete(p, q):
        v = {p[0], p[1], q[0], q[1]}
        if len(v) != 2:
            return None
        u, w = sorted(v)
        return u, w

    def couper(p, q, lev):
        u, w = arete(p, q)
        t = (niveaux[lev] - h[u]) / (h[w] - h[u])
        return (u, w, lev, px[u] + t * (px[w] - px[u]), py[u] + t * (py[w] - py[u]), niveaux[lev])

    def decouper(poly, lev, dessus):
        L = niveaux[lev]
        out = []
        n = len(poly)
        for i in range(n):
            A, B = poly[i], poly[(i + 1) % n]
            in_a = A[5] > L if dessus else A[5] < L
            in_b = B[5] > L if dessus else B[5] < L
            sur_a, sur_b = A[2] == lev, B[2] == lev
            if in_a or sur_a:
                out.append(A)
            if not sur_a and not sur_b and in_a != in_b:
                out.append(couper(A, B, lev))
        return out

    def eventail(poly, dessus):
        a = sommet(poly[0], dessus)
        for k in range(1, len(poly) - 1):
            if dessus:
                ajouter(a, sommet(poly[k], True), sommet(poly[k + 1], True))
            else:
                ajouter(a, sommet(poly[k + 1], False), sommet(poly[k], False))

    for t in np.nonzero(garde & ~simple)[0]:
        vs = tri[t]
        p = [(int(v), int(v), -1, float(px[v]), float(py[v]), float(h[v])) for v in vs]
        if i_lo >= 0 and hmin[t] < lo:
            p = decouper(p, i_lo, True)
        if len(p) < 3:
            continue
        eventail(p, False)
        if i_hi >= 0 and any(q[5] > hi for q in p):
            for q in (decouper(p, i_hi, False), decouper(p, i_hi, True)):
                if len(q) >= 3:
                    eventail(q, True)
        else:
            eventail(p, True)
        if not sur_bord[t]:
            continue
        n = len(p)
        for i in range(n):
            j = (i + 1) % n
            e2 = arete(p[i], p[j])
            if e2 is None or (e2[0] * nv + e2[1]) not in bord:
                continue
            aB, bB, aH, bH = sommet(p[i], False), sommet(p[j], False), sommet(p[i], True), sommet(p[j], True)
            if i_hi >= 0 and (p[i][5] - hi) * (p[j][5] - hi) < 0:
                s = sommet(couper(p[i], p[j], i_hi), True)
                ajouter(aB, bB, bH)
                ajouter(aB, bH, s)
                ajouter(aB, s, aH)
            else:
                ajouter(aB, bB, bH)
                ajouter(aB, bH, aH)
    if sortie:
        ks = np.asarray(sortie, dtype=np.int64).ravel()
        cles_t.append(ks)
        coords_t.append(np.asarray([coords[k] for k in ks.tolist()], dtype=float))
    if not cles_t:
        return maillage.VIDE
    toutes = np.concatenate(cles_t)
    xyz = np.vstack(coords_t)
    uniques, premier, inv = np.unique(toutes, return_index=True, return_inverse=True)
    t = inv.reshape(-1, 3).astype(np.int64)
    if len(t) > TRIANGLES_MAX:
        raise ValueError("Relief trop détaillé : augmente le pas du maillage (finesse).")
    return xyz[premier], t


# ================================================================================================ tracé (vue Carte)
def reechantillonner(x, y, pas, maxi=60_000):
    """Points espacés de [pas] le long de la polyligne (premier point gardé)."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    d = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(x), np.diff(y)))])
    if d[-1] <= 0:
        return x[:1], y[:1]
    s = np.arange(0.0, d[-1] + 1e-12, pas)[:maxi]
    return np.interp(s, d, x), np.interp(s, d, y)


def moyenne_glissante(v, k):
    """Moyenne sur ± k points (fenêtre raccourcie aux extrémités)."""
    v = np.asarray(v, dtype=float)
    n = len(v)
    c = np.concatenate([[0.0], np.cumsum(v)])
    i = np.arange(n)
    a, b = np.maximum(0, i - k), np.minimum(n, i + k + 1)
    return (c[b] - c[a]) / (b - a)


def morceaux(ok):
    """Plages [i0, i1) de valeurs vraies consécutives (au moins deux)."""
    out, debut = [], -1
    for i, v in enumerate(list(ok) + [False]):
        if v and debut < 0:
            debut = i
        if not v and debut >= 0:
            if i - debut >= 2:
                out.append((debut, i))
            debut = -1
    return out


def ruban(c, xs, ys, dedans):
    """Tracé en relief vue Carte : ruban de section rectangulaire posé sur le terrain (dessus = point le plus haut sous
    le ruban + hauteur, dessous enfoncé de 0,4 mm), rééchantillonné, lissé, coupé au bord. Une pièce fermée par morceau."""
    p = c.p
    if len(xs) < 2:
        return maillage.VIDE
    rx, ry = reechantillonner(xs, ys, max(0.4, p.largeur_trace_mm * 0.7))
    sx, sy = moyenne_glissante(rx, 2), moyenne_glissante(ry, 2)
    w = p.largeur_trace_mm / 2
    corps = []
    for i0, i1 in morceaux(dedans(sx, sy)):
        X, Y = sx[i0:i1], sy[i0:i1]
        k = len(X)
        a, b = np.maximum(0, np.arange(k) - 1), np.minimum(k - 1, np.arange(k) + 1)
        tx, ty = X[b] - X[a], Y[b] - Y[a]
        l = np.hypot(tx, ty)
        l = np.where(l > 1e-9, l, 1.0)
        tx, ty = tx / l, ty / l
        nx, ny = -ty, tx
        xl, yl, xr, yr = X + nx * w, Y + ny * w, X - nx * w, Y - ny * w
        hs = np.stack([c.zT(xl, yl), c.zT(X, Y), c.zT(xr, yr), c.zT(X + tx * w, Y + ty * w), c.zT(X - tx * w, Y - ty * w)])
        haut = hs.max(0) + p.hauteur_trace_mm
        bas = np.maximum(p.socle_mm + 0.05, hs.min(0) - ENFONCEMENT_TRACE)
        s = np.zeros((k, 4, 3))
        s[:, 0] = np.stack([xl, yl, bas], 1)   # lb
        s[:, 1] = np.stack([xr, yr, bas], 1)   # rb
        s[:, 2] = np.stack([xr, yr, haut], 1)  # rt
        s[:, 3] = np.stack([xl, yl, haut], 1)  # lt
        q = np.arange(k - 1)
        r = q + 1
        lb, rb, rt, lt = 0, 1, 2, 3
        I = lambda qq, v: 4 * qq + v  # noqa: E731
        faces = [
            (I(q, lt), I(q, rt), I(r, rt)), (I(q, lt), I(r, rt), I(r, lt)),
            (I(q, lb), I(r, rb), I(q, rb)), (I(q, lb), I(r, lb), I(r, rb)),
            (I(r, lb), I(q, lb), I(q, lt)), (I(r, lb), I(q, lt), I(r, lt)),
            (I(q, rb), I(r, rb), I(r, rt)), (I(q, rb), I(r, rt), I(q, rt)),
        ]
        t = [np.stack(f, 1) for f in faces]
        e = k - 1
        t.append(np.asarray([(lb, rb, rt), (lb, rt, lt), (I(e, rb), I(e, lb), I(e, lt)), (I(e, rb), I(e, lt), I(e, rt))]))
        corps.append((s.reshape(-1, 3), np.vstack(t)))
    return maillage.assembler(corps)


# ================================================================================================ repères
def rayon_repere(typ, largeur_trace):
    return max(1.6, largeur_trace * 1.3) if typ == 0 else max(1.2, largeur_trace) if typ == 1 else max(2.2, largeur_trace * 1.7)


def placer_reperes(xs, ys, relais, par_y, zT):
    """Départ = premier point, sommet = point le plus haut, [relais] répartis à longueur égale entre les deux."""
    xs = np.asarray(xs, dtype=float)
    ys = np.asarray(ys, dtype=float)
    if len(xs) < 2:
        return []
    v = ys if par_y else zT(xs, ys)
    i_s = int(np.argmax(v))
    if i_s == 0:
        i_s = len(xs) - 1
    out = [(float(xs[0]), float(ys[0]), 0)]
    if relais > 0:
        cum = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(xs[:i_s + 1]), np.diff(ys[:i_s + 1])))])
        for r in range(1, relais + 1):
            s = cum[-1] * r / (relais + 1)
            out.append((float(np.interp(s, cum, xs[:i_s + 1])), float(np.interp(s, cum, ys[:i_s + 1])), 1))
    out.append((float(xs[i_s]), float(ys[i_s]), 2))
    return out


def prismes_reperes(reperes, p, dedans, zT):
    """Prismes posés sur le terrain : hexagone (départ), carré (relais), triangle (sommet)."""
    corps = []
    for x, y, typ in reperes:
        cotes = 6 if typ == 0 else 4 if typ == 1 else 3
        r = rayon_repere(typ, p.largeur_trace_mm)
        if not dedans(x, y, r):
            continue
        ang = math.pi / 2 + 2 * math.pi * np.arange(cotes) / cotes
        px, py = x + r * np.cos(ang), y + r * np.sin(ang)
        zs = np.concatenate([zT(px, py), np.atleast_1d(zT(x, y))])
        corps.append(maillage.prisme_convexe(px, py, max(p.socle_mm + 0.05, zs.min() - ENFONCEMENT_TRACE), zs.max() + p.hauteur_trace_mm + 0.6))
    return maillage.assembler(corps)


# ================================================================================================ modèle
def _norm_hex(h):
    return str(h).upper()


def modele(g, zone, p0, a, trace, limite=TRIANGLES_MAX, progress=None):
    """Pièces du relief : repères et tracé d'abord (ils gardent leur couleur là où ils s'enfoncent), texte, socle,
    bandeau, lac, tranches. [trace] : (x, y) du plan local (m) ou None. Le pas est élargi si le total dépasse [limite]."""
    from . import relief3d
    c = Contexte(g, zone, p0, a)
    p = c.p
    if len(p.couleurs) < COULEURS_MIN:
        raise ValueError("Choisis au moins deux couleurs.")
    w, pr = c.w, c.pr
    avert = []
    niv = []
    if a.niveau_lac is not None:
        niv.append(float(c.z_alt(a.niveau_lac)))
    niv += [float(c.z_alt(s)) for s in a.seuils]
    niveaux = np.asarray(niv, dtype=float)
    filaments = []

    def filament(t):
        hexa = _norm_hex(t[0])
        for i, f in enumerate(filaments):
            if f[0] == hexa:
                return i
        filaments.append((hexa, t[1]))
        return len(filaments) - 1

    f_tranches = [filament(t) for t in p.couleurs]
    f_lac = filament(p.couleur_lac) if a.niveau_lac is not None else -1
    avec_trace = p.trace and trace is not None and len(trace[0]) >= 2
    f_trace = filament(p.couleur_trace) if avec_trace else -1
    avec_texte = p.bandeau_mm > 0 and (p.texte1.strip() or p.texte2.strip())
    f_texte = filament(p.couleur_texte) if avec_texte else -1
    f_socle = 0 if p.sans_socle else filament(p.couleur_socle)
    pas = p.finesse_mm
    nb_tranches = len(niveaux) + 1

    def estimer(pas):
        t2 = 2.0 * (w / pas) * (pr / pas) if p.forme == geo.RECTANGLE else 6.0 * (w / 2 / pas) ** 2
        return int(t2 * (1.6 + nb_tranches * 0.9))

    while estimer(pas) > limite * 0.85 and pas < 2.0:
        pas *= 1.15
    if pas > p.finesse_mm * 1.01:
        avert.append(("Maillage élargi à %.2f mm pour garder un fichier raisonnable." % pas).replace(".", ","))
    pieces = []
    rect = p.forme == geo.RECTANGLE
    forme_c = contour(p.forme, w, pr)
    if avec_trace:
        xs, ys = c.vers_modele(trace[0], trace[1])
        if p.reperes:
            rep = placer_reperes(xs, ys, p.relais, p.sommet_par_y, c.zT)
            dedans_r = lambda x, y, r: bool(dans_forme(x, y, r + 0.4, forme_c, rect, w, pr))  # noqa: E731
            m = relief3d.boules(rep, p, dedans_r, c.zT) if p.trace_surface else prismes_reperes(rep, p, dedans_r, c.zT)
            if len(m[1]):
                pieces.append({"nom": "Repères", "filament": filament(p.couleur_reperes), "sommets": m[0], "triangles": m[1]})
        dedans = lambda x, y: dans_forme(x, y, p.largeur_trace_mm / 2 + 0.6, forme_c, rect, w, pr)  # noqa: E731
        m = relief3d.tube(xs, ys, p, dedans, c.zT) if p.trace_surface else ruban(c, xs, ys, dedans)
        if len(m[1]):
            pieces.append({"nom": "Tracé", "filament": f_trace, "sommets": m[0], "triangles": m[1]})
        else:
            avert.append("Le tracé sort de la zone : il n'est pas imprimé.")
    if progress:
        progress(0.1, "Tracé et texte")
    b = p.bandeau_mm if p.bandeau_mm > 0 else 0.0
    bx0, bx1 = (w * 0.12, w * 0.88) if p.forme == geo.CERCLE else (0.0, w)
    recouvre = b * 0.5 if p.forme == geo.CERCLE else 0.0
    if avec_texte:
        s, t = police.maillage_texte(p.texte1, p.texte2, bx0 + 2, bx1 - 2, -b + 1.2, -1.2, p.socle_mm, p.socle_mm + EPAISSEUR_TEXTE)
        if len(t):
            pieces.append({"nom": "Texte", "filament": f_texte, "sommets": s, "triangles": t})
    plan = plan2d(p.forme, w, pr, pas)
    if not p.sans_socle:
        if rect:
            s, t = maillage.boite(0.0, -b, w, pr, 0.0, p.socle_mm)
            pieces.append({"nom": "Socle", "filament": f_socle, "sommets": s, "triangles": t})
        else:
            cx, cy = plan.contour()
            s, t = maillage.prisme_convexe(cx, cy, 0.0, p.socle_mm)
            pieces.append({"nom": "Socle", "filament": f_socle, "sommets": s, "triangles": t})
            if b > 0:
                s, t = maillage.boite(bx0, -b, bx1, recouvre, 0.0, p.socle_mm)
                pieces.append({"nom": "Bandeau", "filament": f_socle, "sommets": s, "triangles": t})
    h = c.zT(plan.x, plan.y)
    for L in niveaux:
        h = np.where(np.abs(h - L) < EPS, L - EPS, h)
    noms = (["Ciel" if p.sommet_par_y else "Lac"] if a.niveau_lac is not None else []) + \
        [f"Tranche {i + 1} · {t[1]}" for i, t in enumerate(p.couleurs)]
    fils = ([f_lac] if a.niveau_lac is not None else []) + f_tranches[:nb_tranches - (1 if a.niveau_lac is not None else 0)]
    total = sum(len(pc["triangles"]) for pc in pieces)
    for k in range(nb_tranches):
        i_lo, i_hi = k - 1, (k if k < len(niveaux) else -1)
        s, t = tranche3d(plan, h, niveaux, i_lo, i_hi, p.socle_mm)
        total += len(t)
        if total > limite * 1.2:
            raise ValueError("Relief trop détaillé : augmente le pas du maillage (finesse) ou réduis la largeur.")
        if len(t):
            pieces.append({"nom": noms[k], "filament": fils[k], "sommets": s, "triangles": t})
        if progress:
            progress(0.1 + 0.6 * (k + 1) / nb_tranches, f"Tranche {k + 1} / {nb_tranches}")
    hauteur = float(c.z_alt(a.alt_max)) + ((p.hauteur_trace_mm + (0.6 if p.reperes else 0.0)) if avec_trace else 0.0)
    if hauteur > 120:
        avert.append(f"Relief haut de {hauteur:.0f} mm : réduis l'exagération pour une impression plus rapide.")
    if len(filaments) > 4:
        avert.append(f"{len(filaments)} filaments : plus que les 4 emplacements d'un AMS. Réutilise des couleurs ou prévois un second AMS.")
    return {"pieces": pieces, "filaments": filaments, "largeur_mm": w, "profondeur_mm": pr + b, "hauteur_mm": hauteur,
            "finesse_mm": pas, "plateau": plateau(w, pr + b), "avertissements": avert}


def plateau(l, h, cote=PLATEAU_MM):
    def mm(v):
        return ("%.1f" % v).replace(".", ",")
    if l <= cote and h <= cote:
        return {"tient": True, "message": f"Tient sur le plateau P1S ({cote:.0f} × {cote:.0f} mm)."}
    return {"tient": False, "message": f"Le relief ({mm(l)} × {mm(h)} mm) dépasse le plateau P1S ({cote:.0f} × {cote:.0f} mm)."}


# ================================================================================================ aperçu de dessus
def _rgb(hexa):
    hexa = str(hexa).lstrip("#")
    return np.array([int(hexa[i:i + 2], 16) for i in (0, 2, 4)], dtype=float)


def apercu(g, zone, p0, a, trace, l=900):
    """Vue ombrée vue de dessus (tableau (h, l, 3) uint8) : couleur de la tranche × ombrage (lumière du nord-ouest),
    tracé, repères, bandeau et texte. Mêmes seuils que le modèle."""
    from PIL import Image, ImageDraw
    c = Contexte(g, zone, p0, a)
    p = c.p
    w, pr = c.w, c.pr
    b = p.bandeau_mm if p.bandeau_mm > 0 else 0.0
    mm = w / l
    h = max(1, int(math.floor((pr + b) / mm + 0.5)))
    if h > 1200:
        l = max(50, int(l * 1200 / h))
        mm = w / l
        h = max(1, int(math.floor((pr + b) / mm + 0.5)))
    X = (np.arange(l) + 0.5) * mm
    Y = pr - (np.arange(h) + 0.5) * mm
    XX, YY = np.meshgrid(X, Y)
    img = np.zeros((h, l, 3), dtype=float)
    terrain = YY >= 0
    if p.forme != geo.RECTANGLE:
        cx, cy = contour(p.forme, w, pr)
        terrain &= contient(cx, cy, XX, YY)
    xs, ys = XX[terrain], YY[terrain]
    alt = c.altitude(xs, ys)
    k = tranche_index(a, alt)
    couleurs = np.array([_rgb(t[0]) for t in p.couleurs])
    col = np.where((k < 0)[:, None], _rgb(p.couleur_lac[0])[None, :], couleurs[np.clip(k, 0, len(couleurs) - 1)])
    ex = c.e * p.exageration
    dl = max(mm, 0.3)
    gx = (c.altitude(xs + dl, ys) - c.altitude(xs - dl, ys)) * ex / (2 * dl)
    gy = (c.altitude(xs, ys + dl) - c.altitude(xs, ys - dl)) * ex / (2 * dl)
    lx, ly, lz = -0.5, 0.5, math.sqrt(0.5)
    o = np.clip((-gx * lx - gy * ly + lz) / np.sqrt(gx * gx + gy * gy + 1), 0, 1)
    img[terrain] = col * (0.45 + 0.75 * o)[:, None]
    bx0, bx1 = (w * 0.12, w * 0.88) if p.forme == geo.CERCLE else (0.0, w)
    if b > 0:
        zone_b = (YY < 0) & (YY >= -b) & (XX >= bx0) & (XX <= bx1)
        if p.forme == geo.CERCLE:
            zone_b |= ~terrain & (YY >= 0) & (YY <= b * 0.5) & (XX >= bx0) & (XX <= bx1)
        img[zone_b] = _rgb(p.couleur_socle[0])
    if p.trace and trace is not None and len(trace[0]) >= 2:
        tx, ty = c.vers_modele(trace[0], trace[1])
        masque = Image.new("L", (l, h), 0)
        d = ImageDraw.Draw(masque)
        r = max(1.0, p.largeur_trace_mm / 2 / mm)
        pts = list(zip((tx / mm).tolist(), ((pr - ty) / mm).tolist()))
        d.line(pts, fill=255, width=max(2, int(round(2 * r))), joint="curve")
        m = (np.asarray(masque) > 0) & terrain
        img[m] = _rgb(p.couleur_trace[0])
        if p.reperes:
            masque = Image.new("L", (l, h), 0)
            d = ImageDraw.Draw(masque)
            for x, y, typ in placer_reperes(tx, ty, p.relais, p.sommet_par_y, c.zT):
                rr = rayon_repere(typ, p.largeur_trace_mm) / mm
                d.ellipse([x / mm - rr, (pr - y) / mm - rr, x / mm + rr, (pr - y) / mm + rr], fill=255)
            m = (np.asarray(masque) > 0) & terrain
            img[m] = _rgb(p.couleur_reperes[0])
    if b > 0 and (p.texte1.strip() or p.texte2.strip()):
        segs, demis = police.bloc_texte(p.texte1, p.texte2, bx0 + 2, bx1 - 2, -b + 1.2, -1.2)
        if len(segs):
            m = police.raster(segs, demis, 0.0, pr - h * mm, mm, l, h)[::-1] & (YY < 0) & (YY >= -b)
            img[m] = _rgb(p.couleur_texte[0])
    return np.clip(np.round(img), 0, 255).astype(np.uint8)
