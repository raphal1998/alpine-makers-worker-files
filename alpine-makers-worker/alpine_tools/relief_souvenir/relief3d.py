"""Relief souvenir — vue « Montagne 3D » (port de Relief3D.kt).

Le terrain reste le bloc de la vue Carte, mais le tracé est un TUBE qui épouse la surface en 3D : échantillonné selon
la longueur réelle parcourue (montée comprise), il ressort perpendiculairement au terrain (normale de la surface), donc
à l'horizontale sur une face presque verticale : une via ferrata se lit sur le flanc de la montagne, de côté.
Les repères deviennent des boules posées sur la surface. Limite : le terrain reste 2,5D (aucun surplomb).
Aperçu : bloc en perspective orthographique, rendu logiciel vectorisé avec tampon de profondeur.
"""
import math

import numpy as np

from . import maillage, relief

H_NORMALE = 0.35
COTES = 10
POINTS_MAX = 80_000


def normale(zT, x, y, h=H_NORMALE):
    """Normale unitaire de la surface z = zT(x, y) (tableaux)."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    gx = (zT(x + h, y) - zT(x - h, y)) / (2 * h)
    gy = (zT(x, y + h) - zT(x, y - h)) / (2 * h)
    l = np.sqrt(gx * gx + gy * gy + 1)
    return np.stack([-gx / l, -gy / l, 1 / l], -1)


def chemin(xs, ys, zT, pas):
    """Points (n, 3) posés sur la surface, espacés de [pas] mm le long de la longueur RÉELLE (montée comprise)."""
    xs = np.asarray(xs, dtype=float)
    ys = np.asarray(ys, dtype=float)
    if len(xs) < 2:
        return np.zeros((0, 3))
    fin = max(0.08, min(pas / 3, 0.25))
    rx, ry = relief.reechantillonner(xs, ys, fin, 4 * POINTS_MAX)
    if len(rx) < 2:
        return np.zeros((0, 3))
    k = max(1, int(math.floor(0.5 / fin + 0.5)))
    sx, sy = relief.moyenne_glissante(rx, k), relief.moyenne_glissante(ry, k)
    sz = zT(sx, sy)
    d = np.concatenate([[0.0], np.cumsum(np.sqrt(np.diff(sx) ** 2 + np.diff(sy) ** 2 + np.diff(sz) ** 2))])
    s = np.arange(0.0, d[-1] + 1e-12, pas)[:POINTS_MAX]
    out = np.stack([np.interp(s, d, sx), np.interp(s, d, sy), np.interp(s, d, sz)], 1)
    dernier = np.array([sx[-1], sy[-1], sz[-1]])
    if np.linalg.norm(out[-1] - dernier) > pas * 0.3:
        out = np.vstack([out, dernier])
    return out


def normales(ch, zT, k=3):
    brutes = normale(zT, ch[:, 0], ch[:, 1])
    lisses = np.stack([relief.moyenne_glissante(brutes[:, i], k) for i in range(3)], 1)
    l = np.linalg.norm(lisses, axis=1)
    return lisses / np.where(l > 1e-12, l, 1.0)[:, None]


def tube(xs, ys, p, dedans, zT):
    """Tube du tracé posé sur la surface : section elliptique de largeur_trace (le long de la surface) et de hauteur
    hauteur_trace + enfoncement (le long de la normale), qui dépasse de hauteur_trace au-dessus du terrain.
    Coupé là où [dedans] est faux ; une pièce fermée par morceau."""
    ch = chemin(xs, ys, zT, max(0.3, p.largeur_trace_mm * 0.5))
    if len(ch) < 2:
        return maillage.VIDE
    nrm = normales(ch, zT)
    corps = [_anneaux(ch[i0:i1], nrm[i0:i1], p) for i0, i1 in relief.morceaux(dedans(ch[:, 0], ch[:, 1]))]
    return maillage.assembler(corps)


def _anneaux(ch, nrm, p):
    a = p.largeur_trace_mm / 2
    b = (p.hauteur_trace_mm + relief.ENFONCEMENT_TRACE) / 2
    decalage = p.hauteur_trace_mm - b
    k = len(ch)
    bas = p.socle_mm + 0.05
    i = np.arange(k)
    T = ch[np.minimum(k - 1, i + 1)] - ch[np.maximum(0, i - 1)]
    T /= np.where(np.linalg.norm(T, axis=1) > 1e-12, np.linalg.norm(T, axis=1), 1.0)[:, None]
    B = np.cross(T, nrm)
    bl = np.linalg.norm(B, axis=1)
    prec = np.array([1.0, 0.0, 0.0])
    for q in range(k):  # repère dégénéré : on garde la direction précédente
        if bl[q] < 1e-6:
            B[q] = prec
        else:
            B[q] /= bl[q]
        prec = B[q]
    N = np.cross(B, T)
    C = ch + nrm * decalage
    ang = 2 * math.pi * np.arange(COTES) / COTES
    ca, sb = a * np.cos(ang), b * np.sin(ang)
    s = C[:, None, :] + B[:, None, :] * ca[None, :, None] + N[:, None, :] * sb[None, :, None]
    s[..., 2] = np.maximum(bas, s[..., 2])
    sommets = np.vstack([s.reshape(-1, 3), [[C[0, 0], C[0, 1], max(bas, C[0, 2])], [C[-1, 0], C[-1, 1], max(bas, C[-1, 2])]]])
    c0, c1 = k * COTES, k * COTES + 1
    q, j = np.meshgrid(np.arange(k - 1), np.arange(COTES), indexing="ij")
    j2 = (j + 1) % COTES
    A = lambda qq, jj: qq * COTES + jj  # noqa: E731
    t = [np.stack([A(q, j), A(q + 1, j), A(q + 1, j2)], -1).reshape(-1, 3),
         np.stack([A(q, j), A(q + 1, j2), A(q, j2)], -1).reshape(-1, 3)]
    jj = np.arange(COTES)
    jj2 = (jj + 1) % COTES
    t.append(np.stack([np.full(COTES, c0), A(0, jj), A(0, jj2)], 1))
    t.append(np.stack([np.full(COTES, c1), A(k - 1, jj2), A(k - 1, jj)], 1))
    return sommets, np.vstack(t).astype(np.int64)


def boules(reperes, p, dedans, zT):
    """Repères en boules posées sur la surface (départ, relais plus petits, sommet plus gros)."""
    corps = []
    for x, y, typ in reperes:
        r = relief.rayon_repere(typ, p.largeur_trace_mm) * 0.62
        if not dedans(x, y, r):
            continue
        n = normale(zT, np.array([x]), np.array([y]))[0]
        z0 = float(np.atleast_1d(zT(np.array([x]), np.array([y])))[0])
        corps.append(maillage.sphere(x + n[0] * r * 0.45, y + n[1] * r * 0.45, z0 + n[2] * r * 0.45, r, p.socle_mm + 0.05))
    return maillage.assembler(corps)


def azimut_face(xs, ys, zT):
    """Direction (°, 0 = nord, 90 = est) vers laquelle regarde la face que parcourt le tracé ; None si presque plat."""
    xs = np.asarray(xs, dtype=float)
    ys = np.asarray(ys, dtype=float)
    if len(xs) < 2:
        return None
    pas = max(1, len(xs) // 400)
    n = normale(zT, xs[::pas], ys[::pas], 0.8)
    hz = np.hypot(n[:, 0], n[:, 1])
    sx, sy, poids = float((n[:, 0] * hz).sum()), float((n[:, 1] * hz).sum()), float(hz.sum())
    if poids <= 0 or math.hypot(sx, sy) / poids < 0.15:
        return None
    return (math.degrees(math.atan2(sx, sy)) + 360) % 360


# ================================================================================================ aperçu 3D
class Rasteriseur:
    """Projection orthographique depuis [azimut] (où se tient l'observateur : 0 = au nord) et [elevation], remplissage
    vectorisé des triangles avec tampon de profondeur."""

    def __init__(self, l, h, azimut_deg, elevation_deg, w, pr, z_max, y_min=0.0):
        self.l, self.h = l, h
        th = math.radians(azimut_deg)
        ph = math.radians(min(85.0, max(5.0, elevation_deg)))
        self.e = np.array([math.sin(th) * math.cos(ph), math.cos(th) * math.cos(ph), math.sin(ph)])
        rl = math.hypot(self.e[1], self.e[0]) or 1.0
        self.r = np.array([-self.e[1] / rl, self.e[0] / rl, 0.0])
        self.u = np.array([-self.e[2] * self.r[1], self.e[2] * self.r[0], self.e[0] * self.r[1] - self.e[1] * self.r[0]])
        # Lumière de l'avant-gauche, en hauteur.
        m = np.array([-0.5 * self.r[0] + 0.55 * self.e[0], -0.5 * self.r[1] + 0.55 * self.e[1], 0.4 * self.e[2] + 0.7])
        self.lum = m / np.linalg.norm(m)
        self.c0 = np.array([w / 2, (pr + y_min) / 2, z_max / 2])
        coins = np.array([[x, y, z] for x in (0, w) for y in (y_min, pr) for z in (0, z_max)])
        u, v = self._ecran(coins)
        self.ech = min(l * 0.92 / max(1e-9, u.max() - u.min()), h * 0.92 / max(1e-9, v.max() - v.min()))
        self.ox = l / 2.0 - (u.min() + u.max()) / 2 * self.ech
        self.oy = h / 2.0 + (v.min() + v.max()) / 2 * self.ech
        self.img = np.zeros((h * l, 3))
        self.prof = np.full(h * l, -np.inf)
        self.vu = np.zeros(h * l, dtype=bool)

    def _ecran(self, p):
        d = p - self.c0
        return d @ self.r, d @ self.u

    def triangles(self, A, B, C, couleurs):
        """A, B, C : (n, 3) ; couleurs : (n, 3) ou (3,)."""
        A, B, C = (np.asarray(v, dtype=float).reshape(-1, 3) for v in (A, B, C))
        if not len(A):
            return
        couleurs = np.broadcast_to(np.asarray(couleurs, dtype=float), A.shape)
        n = np.cross(B - A, C - A)
        nl = np.linalg.norm(n, axis=1)
        ok = nl > 1e-12
        A, B, C, n, nl, couleurs = A[ok], B[ok], C[ok], n[ok], nl[ok], couleurs[ok]
        n = n / nl[:, None]
        n = np.where((n @ self.e < 0)[:, None], -n, n)
        f = 0.38 + 0.72 * np.maximum(0.0, n @ self.lum)
        teinte = np.clip(couleurs * f[:, None], 0, 255)
        P = []
        for V in (A, B, C):
            u, v = self._ecran(V)
            P.append((self.ox + u * self.ech, self.oy - v * self.ech, (V - self.c0) @ self.e))
        (ax, ay, az), (bx, by, bz), (qx, qy, qz) = P
        aire = (bx - ax) * (qy - ay) - (by - ay) * (qx - ax)
        ok = np.abs(aire) > 1e-9
        x0 = np.maximum(0, np.floor(np.minimum(np.minimum(ax, bx), qx))).astype(np.int64)
        x1 = np.minimum(self.l - 1, np.ceil(np.maximum(np.maximum(ax, bx), qx))).astype(np.int64)
        y0 = np.maximum(0, np.floor(np.minimum(np.minimum(ay, by), qy))).astype(np.int64)
        y1 = np.minimum(self.h - 1, np.ceil(np.maximum(np.maximum(ay, by), qy))).astype(np.int64)
        ok &= (x1 >= x0) & (y1 >= y0)
        idx = np.nonzero(ok)[0]
        # Groupes par taille de boîte (largeur et hauteur en puissances de 2) : chaque groupe est rastérisé d'un bloc ;
        # les triangles fins des flancs gardent une boîte étroite.
        cw = np.ceil(np.log2(np.maximum(1, x1[idx] - x0[idx] + 1))).astype(np.int64)
        chh = np.ceil(np.log2(np.maximum(1, y1[idx] - y0[idx] + 1))).astype(np.int64)
        classe = cw * 64 + chh
        for cl in np.unique(classe):
            SW, SH = int(2 ** (cl // 64)), int(2 ** (cl % 64))
            grp = idx[classe == cl]
            par_bloc = max(1, 1_000_000 // (SW * SH))
            for k in range(0, len(grp), par_bloc):
                g = grp[k:k + par_bloc]
                di, dj = np.meshgrid(np.arange(SW), np.arange(SH))
                px = x0[g][:, None, None] + di[None]
                py = y0[g][:, None, None] + dj[None]
                cx, cy = px + 0.5, py + 0.5
                ar = aire[g][:, None, None]
                w0 = ((bx[g][:, None, None] - cx) * (qy[g][:, None, None] - cy) - (by[g][:, None, None] - cy) * (qx[g][:, None, None] - cx)) / ar
                w1 = ((qx[g][:, None, None] - cx) * (ay[g][:, None, None] - cy) - (qy[g][:, None, None] - cy) * (ax[g][:, None, None] - cx)) / ar
                w2 = 1 - w0 - w1
                m = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6) & (px <= x1[g][:, None, None]) & (py <= y1[g][:, None, None])
                if not m.any():
                    continue
                z = w0 * az[g][:, None, None] + w1 * bz[g][:, None, None] + w2 * qz[g][:, None, None]
                tri_id = np.broadcast_to(np.arange(len(g))[:, None, None], m.shape)[m]
                pix = (py * self.l + px)[m]
                zz = z[m]
                ordre = np.lexsort((-zz, pix))
                pix, zz, tri_id = pix[ordre], zz[ordre], tri_id[ordre]
                premier = np.concatenate([[True], pix[1:] != pix[:-1]])
                pix, zz, tri_id = pix[premier], zz[premier], tri_id[premier]
                mieux = zz > self.prof[pix]
                pix, zz, tri_id = pix[mieux], zz[mieux], tri_id[mieux]
                self.prof[pix] = zz
                self.img[pix] = teinte[g][tri_id]
                self.vu[pix] = True

    def maillage(self, s, t, couleur):
        if len(t):
            self.triangles(s[t[:, 0]], s[t[:, 1]], s[t[:, 2]], couleur)

    def image(self, fond=(14, 20, 28)):
        out = np.where(self.vu[:, None], self.img, np.asarray(fond, dtype=float)[None, :])
        return np.clip(np.round(out), 0, 255).astype(np.uint8).reshape(self.h, self.l, 3)


def rendre(g, zone, p0, a, trace, l=960, h=720, azimut_deg=200.0, elevation_deg=28.0):
    """Aperçu 3D : bloc du relief (terrain en couleurs de tranches, flancs, socle, bandeau), tube du tracé et repères."""
    c = relief.Contexte(g, zone, p0, a)
    p = c.p
    w, pr = c.w, c.pr
    s = max(w, pr) / 170
    nx = max(2, int(math.floor(w / s + 0.5)) + 1)
    ny = max(2, int(math.floor(pr / s + 0.5)) + 1)
    gx, gy = np.linspace(0, w, nx), np.linspace(0, pr, ny)
    XX, YY = np.meshgrid(gx, gy)
    alt = c.altitude(XX, YY)
    zs = c.z_alt(alt)
    k = relief.tranche_index(a, alt)
    pal = np.array([relief._rgb(t[0]) for t in p.couleurs])
    cs = np.where((k < 0)[..., None], relief._rgb(p.couleur_lac[0]), pal[np.clip(k, 0, len(pal) - 1)])
    socle = relief._rgb(p.couleur_socle[0])
    bandeau = p.bandeau_mm if p.bandeau_mm > 0 and not p.sans_socle else 0.0
    z_max = float(zs.max()) + p.hauteur_trace_mm + 2
    r = Rasteriseur(l, h, azimut_deg, elevation_deg, w, pr, z_max, -bandeau)
    P = np.stack([XX, YY, zs], -1)
    a00, a10, a01, a11 = P[:-1, :-1].reshape(-1, 3), P[:-1, 1:].reshape(-1, 3), P[1:, :-1].reshape(-1, 3), P[1:, 1:].reshape(-1, 3)
    r.triangles(a00, a10, a11, cs[:-1, :-1].reshape(-1, 3))
    r.triangles(a00, a11, a01, cs[1:, 1:].reshape(-1, 3))
    # Flancs (terrain jusqu'au socle) et socle.
    for bord, col in ((P[0, :], cs[0, :]), (P[-1, :], cs[-1, :]), (P[:, 0], cs[:, 0]), (P[:, -1], cs[:, -1])):
        A, B = bord[:-1], bord[1:]
        As, Bs = A.copy(), B.copy()
        As[:, 2] = Bs[:, 2] = p.socle_mm
        r.triangles(As, Bs, B, col[:-1])
        r.triangles(As, B, A, col[:-1])
        if p.socle_mm > 0:
            A0, B0 = As.copy(), Bs.copy()
            A0[:, 2] = B0[:, 2] = 0.0
            r.triangles(A0, B0, Bs, socle)
            r.triangles(A0, Bs, As, socle)
    if bandeau > 0:
        z = p.socle_mm
        r.triangles([[0, -bandeau, z], [0, -bandeau, z]], [[w, -bandeau, z], [w, 0, z]], [[w, 0, z], [0, 0, z]], socle)
        r.triangles([[0, -bandeau, 0], [0, -bandeau, 0]], [[w, -bandeau, 0], [w, -bandeau, z]], [[w, -bandeau, z], [0, -bandeau, z]], socle)
        for x in (0.0, w):
            r.triangles([[x, -bandeau, 0], [x, -bandeau, 0]], [[x, 0, 0], [x, 0, z]], [[x, 0, z], [x, -bandeau, z]], socle)
    if p.trace and trace is not None and len(trace[0]) >= 2:
        xs, ys = c.vers_modele(trace[0], trace[1])
        dedans = lambda x, y: (x >= 0.4) & (x <= w - 0.4) & (y >= 0.4) & (y <= pr - 0.4)  # noqa: E731
        s_, t_ = tube(xs, ys, p, dedans, c.zT)
        r.maillage(s_, t_, relief._rgb(p.couleur_trace[0]))
        if p.reperes:
            rep = relief.placer_reperes(xs, ys, p.relais, False, c.zT)
            s_, t_ = boules(rep, p, lambda x, y, rr: rr <= x <= w - rr and rr <= y <= pr - rr, c.zT)
            r.maillage(s_, t_, relief._rgb(p.couleur_reperes[0]))
    return r.image()
