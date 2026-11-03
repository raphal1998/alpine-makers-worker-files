"""Mesure d'une chute par photo (port de Mesure.kt d'Alpine Chutes), de la photo au bord en mm (y vers le bas, repère
de la feuille) :

1. homographie mm de la feuille → pixels de la photo (4 coins de la feuille ou 4 repères détectés, ou indiqués) ;
2. redressement : la feuille (petite chute posée dessus) ou toute la partie visible du plan (grande chute, feuille
   posée sur la chute), de face, à ``ppm`` px/mm ;
3. détourage : sur la feuille, écart au papier (detourage.py, repris d'Alpine Contour) ; sur une grande chute, distance
   de couleur à la matière vue autour de la feuille (médiane et écart absolu médian), seuil d'Otsu, composante qui
   porte la feuille ;
4. petits trous bouchés, bord au sous-pixel (marching squares), lissé, simplifié à 0,03 mm.

Comme l'application : aperçu à 3 px/mm sur la photo réduite, puis mesure à 8 px/mm sur la photo source avec le seuil
(et la zone) de l'aperçu. Les retouches au doigt de l'application ne sont pas portées.
"""
import math

import numpy as np

from . import detection, detourage as dt, image as im, trace as tr
from .geometrie import Boucle, Homographie, Refus, aire_matiere, mm2, simplifier_boucle

F32 = np.float32
PX_MM_APERCU = 3.0
PX_MM_FINAL = 8.0
PIXELS_MAX = 8_000_000
ETENDUE_MAX = 1200.0
DEHORS_RGB = (32, 32, 32)
MODES = ("sur_feuille", "feuille_sur_chute")


class Zone:
    __slots__ = ("x0", "y0", "x1", "y1")

    def __init__(self, x0, y0, x1, y1):
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1

    @property
    def largeur(self):
        return self.x1 - self.x0

    @property
    def hauteur(self):
        return self.y1 - self.y0

    def liste(self):
        return [self.x0, self.y0, self.x1, self.y1]

    @staticmethod
    def de(l):
        if l and len(l) == 4 and l[2] > l[0] and l[3] > l[1]:
            return Zone(*l)
        return None


class Resultat:
    """Image redressée de la ``zone`` à ``ppm`` px/mm (uint8, pixel (c, r) centré en (x0 + (c + ½)/ppm ; y0 + (r + ½)/ppm)),
    ``visible`` (pixels dans la photo), masque de la chute, bords en mm de la feuille, seuil retenu, ``touche_bord`` si la
    chute sort de la zone analysée, ``autres`` objets trouvés."""

    def __init__(self, image, visible, zone, ppm, chute, bords, seuil, touche_bord, autres):
        self.image, self.visible, self.zone, self.ppm, self.chute = image, visible, zone, ppm, chute
        self.bords, self.seuil, self.touche_bord, self.autres = bords, seuil, touche_bord, autres

    @property
    def aire(self):
        return aire_matiere(self.bords)


def homographie(quad, fmt):
    return Homographie.depuis4(fmt.points_reference(), quad)


def _echantillons(photo, hh, X, Y):
    """Couleurs bilinéaires (N, 3) des points (mm) par l'homographie ``hh`` et masque « hors de la photo »."""
    hp, wp = photo.shape[:2]
    w = hh[6] * X + hh[7] * Y + hh[8]
    ok = w > 1e-12
    ws = np.where(ok, w, 1.0)
    px = (hh[0] * X + hh[1] * Y + hh[2]) / ws
    py = (hh[3] * X + hh[4] * Y + hh[5]) / ws
    fx, fy = px - 0.5, py - 0.5
    hors = ~ok | ~np.isfinite(px) | ~np.isfinite(py) | (fx < -0.5) | (fy < -0.5) | (fx > wp - 0.5) | (fy > hp - 0.5)
    px = np.where(hors, 0.5, px)
    py = np.where(hors, 0.5, py)
    return im.echantillonner(photo, px, py, 255), hors


def orienter(photo, quad, fmt):
    """Sens de la feuille de repères : le haut porte le titre et les consignes, plus d'encre que la règle du bas ;
    quadrilatère tourné de 180° si la feuille est à l'envers. Une feuille blanche n'a pas de sens."""
    if not fmt.reperes:
        return quad
    try:
        h = homographie(quad, fmt)
    except ValueError:
        return quad

    def encre(x0, y0, x1, y1):
        xs = x0 + 0.5 * np.arange(int(round((x1 - x0) / 0.5)) + 1)
        ys = y0 + 0.5 * np.arange(int(round((y1 - y0) / 0.5)) + 1)
        Y, X = np.meshgrid(ys, xs, indexing="ij")
        rgb, hors = _echantillons(photo, h.h, X.ravel(), Y.ravel())
        rgb[hors] = 255
        v = im.luminance(rgb)
        return float(np.sum((1.0 - v[v < F32(0.55)].astype(np.float64))))

    haut = encre(28.0, 8.0, 170.0, 28.0)
    bas = encre(40.0, 269.0, 182.0, 289.0)
    return quad[2:] + quad[:2] if bas > haut * 1.15 else quad


def detecter(photo, fmt):
    """Détection automatique (feuille blanche ou repères) puis sens de la feuille de repères ; None si rien n'est trouvé."""
    q = detection.detecter(photo, fmt)
    return None if q is None else orienter(photo, [tuple(map(float, p)) for p in q], fmt)


def zone_feuille(fmt):
    return Zone(0.0, 0.0, fmt.largeur, fmt.hauteur)


def zone_visible(largeur_photo, hauteur_photo, mm_vers_photo, fmt):
    """Partie du plan visible dans la photo (grande chute), bornée à ETENDUE_MAX mm autour de la feuille."""
    inv = mm_vers_photo.inverse()
    x0, y0, x1, y1 = 0.0, 0.0, fmt.largeur, fmt.hauteur
    n = 48
    for k in range(n + 1):
        t = k / n
        for px, py in ((t * largeur_photo, 0.0), (t * largeur_photo, float(hauteur_photo)), (0.0, t * hauteur_photo),
                       (float(largeur_photo), t * hauteur_photo)):
            hh = inv.h
            w = hh[6] * px + hh[7] * py + hh[8]
            if w <= 1e-9:
                continue
            qx, qy = inv.appliquer(px, py)
            if not (math.isfinite(qx) and math.isfinite(qy)):
                continue
            x0, y0, x1, y1 = min(x0, qx), min(y0, qy), max(x1, qx), max(y1, qy)
    e = ETENDUE_MAX
    return Zone(max(x0, -e), max(y0, -e), min(x1, fmt.largeur + e), min(y1, fmt.hauteur + e))


def px_par_mm(zone, mm_vers_photo, fmt, voulu):
    """``voulu`` px/mm, réduit pour tenir dans PIXELS_MAX et sans dépasser 1,5 × la finesse de la photo."""
    par_pixels = math.sqrt(PIXELS_MAX / max(1.0, zone.largeur * zone.hauteur))
    finesse = mm_vers_photo.echelle_locale(fmt.largeur / 2, fmt.hauteur / 2)
    return max(min(voulu, min(par_pixels, max(2.0, 1.5 * finesse))), 0.5)


def redresser(photo, mm_vers_photo, zone, ppm):
    """Redressement d'une zone quelconque du plan : image uint8 et masque des pixels vus dans la photo."""
    l = max(2, dt.arrondi_entier(zone.largeur * ppm))
    h = max(2, dt.arrondi_entier(zone.hauteur * ppm))
    finesse = mm_vers_photo.echelle_locale((zone.x0 + zone.x1) / 2, (zone.y0 + zone.y1) / 2) / ppm
    ss = 2 if finesse > 1.6 else 1
    hh = mm_vers_photo.h
    sortie = np.empty((h, l, 3), dtype=np.uint8)
    visible = np.empty((h, l), dtype=bool)
    cols = np.arange(l, dtype=np.float64)
    bande = max(1, 400_000 // max(1, l * ss * ss))
    for r0 in range(0, h, bande):
        r1 = min(h, r0 + bande)
        rows = np.arange(r0, r1, dtype=np.float64)
        acc = np.zeros(((r1 - r0) * l, 3), dtype=np.int32)
        dehors = np.zeros((r1 - r0) * l, dtype=bool)
        for sy in range(ss):
            for sx in range(ss):
                X = np.broadcast_to(zone.x0 + (cols + (sx + 0.5) / ss) / ppm, (r1 - r0, l)).ravel()
                Y = np.broadcast_to((zone.y0 + (rows + (sy + 0.5) / ss) / ppm)[:, None], (r1 - r0, l)).ravel()
                v, hors = _echantillons(photo, hh, X, Y)
                acc += v
                dehors |= hors
        n = ss * ss
        moy = np.clip((acc + n // 2) // n, 0, 255).astype(np.uint8)
        moy[dehors] = DEHORS_RGB
        sortie[r0:r1] = moy.reshape(r1 - r0, l, 3)
        visible[r0:r1] = ~dehors.reshape(r1 - r0, l)
    return sortie, visible


def _finir(boucles, lissage):
    """Lissage léger, simplification à 0,03 mm, boucles dégénérées retirées."""
    sortie = []
    for b in boucles:
        s = simplifier_boucle(tr.lisser(b, lissage), 0.03, 0.03)
        if s is not None and s.n >= 3 and abs(s.aire) > 1.0:
            sortie.append(s)
    return sortie


class ReglagesMesure:
    def __init__(self, mode="sur_feuille", fmt=None, sensibilite=0.0, ombres=True, seuil=None, zone=None, lissage_mm=0.15):
        if mode not in MODES:
            raise Refus("Mode de mesure inconnu.")
        self.mode, self.format, self.sensibilite, self.ombres = mode, fmt or im.REPERES_A4, float(sensibilite), bool(ombres)
        self.seuil, self.zone, self.lissage_mm = seuil, zone, float(lissage_mm)


def calculer(photo, quad, r, ppm):
    """Chaîne complète ; ``quad`` : points de référence en PIXELS de ``photo`` ; seuil de l'aperçu réutilisé s'il est donné."""
    h = homographie(quad, r.format)
    if r.mode == "sur_feuille":
        return _sur_feuille(photo, h, r, ppm)
    return _feuille_sur_chute(photo, h, r, ppm)


# ------------------------------------------------------------------ petite chute sur la feuille

def _sur_feuille(photo, h, r, ppm):
    f = r.format
    img = dt.redresser(photo, h, f, ppm)
    a = dt.analyser(img, f, ppm)
    det = dt.ReglagesDetourage(sensibilite=r.sensibilite, ombres=r.ombres, taille_min_mm2=40.0, trous_min_mm2=8.0)
    m = dt.masque(a, det, (), r.seuil)
    chute = None
    for o in m.objets:
        if chute is None or o.aire_mm2 > chute.aire_mm2:
            chute = o
    if chute is None:
        raise Refus("Aucune chute trouvée sur la feuille : vérifie le cadrage ou augmente la sensibilité.")
    if chute.aire_mm2 < 100:
        raise Refus(f"La chute trouvée est minuscule ({mm2(chute.aire_mm2)}) : vérifie le cadrage ou la sensibilité.")
    masque = m.etiquettes == chute.id
    brut = tr.extraire(m, chute.id)
    hauteur = m.hauteur / ppm
    bords = _finir([Boucle(b.x.copy(), hauteur - b.y) for b in brut], r.lissage_mm)
    return Resultat(img, np.ones(masque.shape, dtype=bool), zone_feuille(f), ppm, masque, bords, m.seuil, chute.touche_bord,
                    len(m.objets) - 1)


# ------------------------------------------------------------------ grande chute, feuille posée dessus

def _mediane_ecart(hist, n):
    """Médiane et écart absolu médian d'un histogramme de 256 classes (valeurs 0..1), float32 comme l'application."""
    def quantile(hh, q):
        c, cible = 0, int(n * q)
        for i, v in enumerate(hh.tolist()):
            c += v
            if c > cible:
                return i
        return len(hh) - 1
    med = quantile(hist, 0.5)
    ecarts = np.zeros(256, dtype=np.int64)
    np.add.at(ecarts, np.minimum(np.abs(np.arange(256) - med), 255), hist)
    mad = quantile(ecarts, 0.5)
    return (F32(med) + F32(0.5)) / F32(255), F32(mad) / F32(255)


def _feuille_sur_chute(photo, h, r, ppm_voulu):
    f = r.format
    zone = Zone.de(r.zone) or zone_visible(photo.shape[1], photo.shape[0], h, f)
    ppm = px_par_mm(zone, h, f, ppm_voulu)
    img, visible = redresser(photo, h, zone, ppm)
    ht, l = visible.shape

    def col(x):
        return (x - zone.x0) * ppm

    def lig(y):
        return (y - zone.y0) * ppm

    feuille = np.zeros((ht, l), dtype=bool)
    fx0, fx1, fy0, fy1 = int(col(-0.5)), int(col(f.largeur + 0.5)), int(lig(-0.5)), int(lig(f.hauteur + 0.5))
    if max(0, fy0) <= min(ht - 1, fy1) and max(0, fx0) <= min(l - 1, fx1):
        feuille[max(0, fy0):min(ht - 1, fy1) + 1, max(0, fx0):min(l - 1, fx1) + 1] = True
    # Matière : anneau de 4 à 20 mm autour de la feuille.
    rr = img[..., 0].astype(np.int32)
    gg = img[..., 1].astype(np.int32)
    bb = img[..., 2].astype(np.int32)
    s = (rr + gg + bb + 3).astype(F32)
    cr = np.where(visible, rr.astype(F32) / s, F32(0))
    cg = np.where(visible, gg.astype(F32) / s, F32(0))
    lu = np.where(visible, im.luminance(img), F32(0))
    xm = zone.x0 + (np.arange(l) + 0.5) / ppm
    ym = zone.y0 + (np.arange(ht) + 0.5) / ppm
    dx = np.maximum(-xm, xm - f.largeur)[None, :]
    dy = np.maximum(-ym, ym - f.hauteur)[:, None]
    d = np.maximum(dx, dy)
    anneau = visible & (d >= 4.0) & (d <= 20.0)
    n_anneau = int(np.count_nonzero(anneau))
    if n_anneau < 200:
        raise Refus("La matière n'est pas visible autour de la feuille : recule pour que toute la chute soit dans la photo.")

    def histo(v):
        q = np.clip((v[anneau] * F32(255)).astype(np.int64), 0, 255)
        return np.bincount(q, minlength=256)

    mr, er = _mediane_ecart(histo(cr), n_anneau)
    mg, eg = _mediane_ecart(histo(cg), n_anneau)
    ml, el = _mediane_ecart(histo(lu), n_anneau)
    sc = max(max(er, eg) * F32(1.4826), F32(0.010))
    sl = max(el * F32(1.4826), F32(0.035))
    a = (cr - mr) / sc
    b = (cg - mg) / sc
    c = F32(0.35) * (lu - ml) / sl
    q = np.clip(np.floor(np.sqrt(a * a + b * b + c * c) * F32(20) + F32(0.5)), 0, 255).astype(np.int32)
    q[~visible] = 0
    hist = np.bincount(q[visible & ~feuille], minlength=256)
    otsu = max(im.otsu_histo(hist), 70)
    t = r.seuil if r.seuil is not None else min(250, max(30, dt.arrondi_entier(otsu * (1.0 + 0.5 * min(max(r.sensibilite, -1.0), 1.0)))))
    m = visible & (feuille | (q <= t))
    m = dt.ouvrir(m)
    m |= feuille & visible
    trou_min = 30.0 * ppm * ppm
    im.boucher_trous(m, lambda aires: aires < trou_min)
    et, nb = im.etiqueter(m, True)
    cx = min(max(int(col(f.largeur / 2)), 0), l - 1)
    cy = min(max(int(lig(f.hauteur / 2)), 0), ht - 1)
    ident = int(et[cy, cx])
    if ident == 0:
        raise Refus("La feuille n'a pas été retrouvée sur la chute : vérifie les coins ou les repères de la feuille.")
    chute = et == ident
    ys, xs = np.nonzero(chute)
    # Bord : pixel au bord de l'image redressée ou voisin d'un pixel hors de la photo.
    hors = ~visible
    voisin = np.zeros_like(hors)
    voisin[:, 1:] |= hors[:, :-1]
    voisin[:, :-1] |= hors[:, 1:]
    voisin[1:, :] |= hors[:-1, :]
    voisin[:-1, :] |= hors[1:, :]
    voisin[0, :] = voisin[-1, :] = True
    voisin[:, 0] = voisin[:, -1] = True
    bord = bool(np.any(chute & voisin))
    etiquettes = chute.astype(np.int32)
    objet = dt.Objet(1, xs.size / (ppm * ppm), [xs.min() / ppm, ys.min() / ppm, (xs.max() + 1) / ppm, (ys.max() + 1) / ppm],
                     ((cx + 0.5) / ppm, (cy + 0.5) / ppm), bord)
    masque = dt.Masque(l, ht, ppm, f, etiquettes, [objet], t)
    brut = tr.extraire(masque, 1)
    hauteur = ht / ppm
    bords = _finir([Boucle(zone.x0 + b.x, zone.y0 + hauteur - b.y) for b in brut], r.lissage_mm)
    return Resultat(img, visible, zone, ppm, chute, bords, t, bord, max(0, nb - 1))
