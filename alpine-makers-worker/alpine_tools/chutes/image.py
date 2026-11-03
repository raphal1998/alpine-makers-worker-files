"""Images et masques d'Alpine Contour (port d'ImageRgb.kt, Composantes.kt, Feuille.kt), vectorisés avec numpy.

Images : tableaux uint8 (hauteur, largeur, 3). Le pixel (i, j) couvre [i, i+1[ × [j, j+1[ : centre en (i + 0,5 ; j + 0,5).
"""
import numpy as np

F32 = np.float32


# ------------------------------------------------------------------ feuilles de référence (Feuille.kt)

class Reperes:
    """Feuille de repères A4 imprimée (PDF de l'outil) : 4 repères 7 × 7 modules de 2,5 mm."""
    MODULE = 2.5
    COTE = 7 * MODULE
    RETRAIT = 15.0
    ZONE_X0 = 12.0
    ZONE_Y0 = 32.0
    CADRE_X0 = 10.0
    CADRE_Y0 = 30.0
    centres = [(RETRAIT, RETRAIT), (210.0 - RETRAIT, RETRAIT), (210.0 - RETRAIT, 297.0 - RETRAIT), (RETRAIT, 297.0 - RETRAIT)]

    @staticmethod
    def noir(dx, dy):
        m = np.maximum(np.abs(dx), np.abs(dy)) / Reperes.MODULE
        return (m <= 1.5) | ((m > 2.5) & (m <= 3.5))


class FormatFeuille:
    MARGE_SIMPLE = 4.0

    def __init__(self, code, libelle, detail, largeur, hauteur, reperes):
        self.code, self.libelle, self.detail = code, libelle, detail
        self.largeur, self.hauteur, self.reperes = largeur, hauteur, reperes

    def points_reference(self):
        if self.reperes:
            return list(Reperes.centres)
        return [(0.0, 0.0), (self.largeur, 0.0), (self.largeur, self.hauteur), (0.0, self.hauteur)]

    @property
    def zone_utile(self):
        if self.reperes:
            return (Reperes.ZONE_X0, Reperes.ZONE_Y0, self.largeur - Reperes.ZONE_X0, self.hauteur - Reperes.ZONE_Y0)
        m = self.MARGE_SIMPLE
        return (m, m, self.largeur - m, self.hauteur - m)


A4 = FormatFeuille("a4", "A4", "Feuille blanche 210 × 297 mm", 210.0, 297.0, False)
LETTER = FormatFeuille("letter", "Letter", "Feuille blanche 8,5 × 11 po (215,9 × 279,4 mm)", 215.9, 279.4, False)
REPERES_A4 = FormatFeuille("reperes_a4", "Repères A4", "Feuille de repères imprimée (plus précis)", 210.0, 297.0, True)
FORMATS = {f.code: f for f in (A4, LETTER, REPERES_A4)}


# ------------------------------------------------------------------ luminance, Otsu, échantillonnage

def luminance(rgb):
    """Luminance 0..1 (Rec. 709 sur les valeurs sRGB), float32 comme l'application."""
    r = rgb[..., 0].astype(F32)
    g = rgb[..., 1].astype(F32)
    b = rgb[..., 2].astype(F32)
    return (F32(0.2126) * r + F32(0.7152) * g + F32(0.0722) * b) / F32(255.0)


def otsu_histo(histo):
    """Indice qui maximise la variance inter-classes (classe basse = 0..seuil)."""
    h = [float(v) for v in histo]
    total = sum(h)
    if total <= 0:
        return len(h) // 2
    somme_totale = sum(i * v for i, v in enumerate(h))
    poids_bas = somme_bas = 0.0
    meilleur, vmax = 0, -1.0
    for t, v in enumerate(h):
        poids_bas += v
        if poids_bas == 0.0:
            continue
        poids_haut = total - poids_bas
        if poids_haut == 0.0:
            break
        somme_bas += t * v
        mb = somme_bas / poids_bas
        mh = (somme_totale - somme_bas) / poids_haut
        var = poids_bas * poids_haut * (mb - mh) * (mb - mh)
        if var > vmax:
            vmax, meilleur = var, t
    return meilleur


def otsu_valeurs(valeurs):
    """Seuil de valeurs 0..1 sur 256 classes, rendu dans 0..1."""
    q = (np.clip(valeurs, F32(0), F32(1)).astype(F32) * F32(255)).astype(np.int64)
    h = np.bincount(q.ravel(), minlength=256)[:256]
    return F32((otsu_histo(h) + 0.5) / 255.0)


def echantillonner(photo, x, y, dehors=255):
    """Couleur bilinéaire (comme ImageRgb.echantillon) aux points continus (x, y) en pixels → int32 (N, 3)."""
    hp, wp = photo.shape[:2]
    fx = x - 0.5
    fy = y - 0.5
    hors = (fx < -0.5) | (fy < -0.5) | (fx > wp - 0.5) | (fy > hp - 0.5)
    cx = np.clip(fx, 0.0, wp - 1.0)
    cy = np.clip(fy, 0.0, hp - 1.0)
    x0 = np.floor(cx).astype(np.int64)
    y0 = np.floor(cy).astype(np.int64)
    x1 = np.minimum(x0 + 1, wp - 1)
    y1 = np.minimum(y0 + 1, hp - 1)
    tx = (cx - x0)[:, None]
    ty = (cy - y0)[:, None]
    a = photo[y0, x0].astype(np.float64)
    b = photo[y0, x1].astype(np.float64)
    c = photo[y1, x0].astype(np.float64)
    d = photo[y1, x1].astype(np.float64)
    h = a + (b - a) * tx
    bas = c + (d - c) * tx
    v = np.clip(np.trunc(h + (bas - h) * ty + 0.5), 0, 255).astype(np.int32)
    v[hors] = dehors
    return v


def bilineaire(champ, x, y):
    """Valeur bilinéaire float32 d'un champ (h, l) aux points continus (x, y) en pixels (bords prolongés)."""
    h, l = champ.shape
    fx = np.clip(x - 0.5, 0.0, l - 1.0)
    fy = np.clip(y - 0.5, 0.0, h - 1.0)
    x0 = np.floor(fx).astype(np.int64)
    y0 = np.floor(fy).astype(np.int64)
    x1 = np.minimum(x0 + 1, l - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    tx = (fx - x0).astype(F32)
    ty = (fy - y0).astype(F32)
    a = champ[y0, x0] + (champ[y0, x1] - champ[y0, x0]) * tx
    b = champ[y1, x0] + (champ[y1, x1] - champ[y1, x0]) * tx
    return a + (b - a) * ty


# ------------------------------------------------------------------ composantes connexes (Composantes.kt)

def _runs(masque):
    h, w = masque.shape
    p = np.zeros((h, w + 2), dtype=np.int8)
    p[:, 1:-1] = masque
    d = np.diff(p, axis=1)
    rs, s = np.nonzero(d == 1)
    _, e = np.nonzero(d == -1)
    return rs.astype(np.int64), s.astype(np.int64), e.astype(np.int64)


def etiqueter(masque, huit):
    """Étiquettes 1..n des composantes (0 = hors masque), numérotées dans l'ordre de balayage comme l'application.

    Segments horizontaux reliés d'une ligne à la suivante (connexité 4 ou 8), puis composantes du graphe par
    accrochage des racines et compression des chemins.
    """
    masque = np.asarray(masque, dtype=bool)
    h, w = masque.shape
    rs, s, e = _runs(masque)
    nr = rs.size
    et = np.zeros((h, w), dtype=np.int32)
    if nr == 0:
        return et, 0
    c = 1 if huit else 0
    w2 = w + 3
    ks = rs * w2 + s
    ke = rs * w2 + e
    lo = np.searchsorted(ke, (rs + 1) * w2 + s - c, side="right")
    hi = np.searchsorted(ks, (rs + 1) * w2 + e + c, side="left")
    nb = np.maximum(hi - lo, 0)
    total = int(nb.sum())
    lab = np.arange(nr, dtype=np.int64)
    if total:
        a = np.repeat(np.arange(nr, dtype=np.int64), nb)
        debut = np.cumsum(nb) - nb
        b = np.repeat(lo - debut, nb) + np.arange(total, dtype=np.int64)
        while True:
            la, lb = lab[a], lab[b]
            diff = la != lb
            if not diff.any():
                break
            a, b, la, lb = a[diff], b[diff], la[diff], lb[diff]
            np.minimum.at(lab, np.maximum(la, lb), np.minimum(la, lb))
            while True:
                nl = lab[lab]
                if np.array_equal(nl, lab):
                    break
                lab = nl
    racines, inv = np.unique(lab, return_inverse=True)
    num = (inv + 1).astype(np.int32)
    longueurs = e - s
    debut = np.cumsum(longueurs) - longueurs
    idx = np.repeat(rs * w + s - debut, longueurs) + np.arange(int(longueurs.sum()), dtype=np.int64)
    et.ravel()[idx] = np.repeat(num, longueurs)
    return et, int(racines.size)


def aires(et, n):
    return np.bincount(et.ravel(), minlength=n + 1)


def boucher_trous(masque, boucher):
    """Bouche sur place les trous (fond en connexité 4 ne touchant pas le bord) dont ``boucher(aires)`` est vrai."""
    et, n = etiqueter(~masque, False)
    if n == 0:
        return masque
    bord = np.zeros(n + 1, dtype=bool)
    for ligne in (et[0, :], et[-1, :], et[:, 0], et[:, -1]):
        bord[ligne] = True
    a = aires(et, n)
    remplir = np.asarray(boucher(a), dtype=bool) & ~bord
    remplir[0] = False
    masque |= remplir[et]
    return masque
