"""Lithophanie en volume, portée d'Alpine Gift 1.0.8 (core LithoVolume.kt, Lithophanie.planifier pour la zone photo).

La photo (épaisseurs, négatif, détail comme une lithophanie plate) est enroulée sur une surface intérieure lisse
(cylindre, cône, sphère, cœur, boîte à coins arrondis) ; la surface extérieure est décalée de l'épaisseur le long de la
normale. Le maillage est une suite d'anneaux de même nombre de sommets (extérieur de bas en haut, chapeau, intérieur de
haut en bas, pied) reliés deux à deux par des bandes de quadrilatères, avec des faces planes triangulées pour les
fonds : chaque arête est partagée par exactement deux triangles. Repère : z vers le haut, pied à z = 0, axe en (0, 0).

Mêmes calculs que l'application (doubles, puis sommets en float32 comme ``toFloat()``), même ordre des triangles :
le STL écrit est celui de Gift au bit près (hors en-tête). Erreur de saisie → ValueError (SaisieException de Gift).
"""
import dataclasses
import math

import numpy as np

from . import image as img
from .triangulation import trianguler

F32 = np.float32
PAS_MIN = 0.15                 # Lithophanie.PAS_MIN
TRIANGLES_MAX = 1_500_000      # Lithophanie.TRIANGLES_MAX
PAS_MAX = 1.2                  # au-delà, le relief n'a plus de détail lisible : refus plutôt qu'un pas plus large
TAILLE_MAX = 250.0
RAYON_COIN_BOITE = 2.0         # rayon intérieur des coins de la boîte (mm)
PIXELS_ETIRES_MAX = 4_000_000  # Squelettes.etirer
MU_PLA = 1.35                  # atténuation du PLA blanc, par mm (ordre de grandeur)

# FormeVolume : libellé, détail.
FORMES = {
    "cylindre": ("Cylindre", "Photophore ou lampe : la photo fait tout le tour."),
    "abat_jour": ("Abat-jour", "Cylindre ou cône tronqué, plaque percée pour la douille en option."),
    "globe": ("Globe", "Sphère partielle posée sur son ouverture, lumière par-dessous."),
    "coeur": ("Cœur creux", "Lanterne en forme de cœur, la photo fait le tour."),
    "boite": ("Boîte 4 faces", "Une photo qui fait le tour, ou une photo par face."),
}


@dataclasses.dataclass(frozen=True)
class Param:
    """ParamLithoVolume (dimensions extérieures en mm, mêmes valeurs par défaut)."""
    forme: str = "cylindre"
    diametre: float = 80.0          # cylindre, globe ; abat-jour : diamètre du bas
    diametre_haut: float = 60.0     # abat-jour
    largeur: float = 80.0           # cœur : largeur extérieure ; boîte : côté extérieur
    hauteur: float = 100.0          # sauf globe
    ep_min: float = 0.8
    ep_max: float = 3.0
    pas: float = 0.25
    negatif: bool = False
    cadre: float = 3.0              # bandes pleines en haut et en bas ; boîte à 4 photos : cadre de chaque face
    fond: bool = False
    ep_fond: float = 2.0
    douille: float = 0.0            # abat-jour : trou de douille dans une plaque en haut (0 : sans plaque)
    ep_plaque: float = 2.5
    ouverture_bas: float = 40.0     # globe
    ouverture_haut: float = 0.0     # globe (0 : calotte fermée)


def fixe(v, decimales):
    """« %.nf » de Java (Locale.FRANCE) : arrondi demi vers le haut de l'écriture décimale (0,15 → « 0,2 »)."""
    from decimal import ROUND_HALF_UP, Decimal
    d = Decimal(repr(float(v))).quantize(Decimal(1).scaleb(-decimales), rounding=ROUND_HALF_UP)
    return f"{d:.{decimales}f}".replace(".", ",")


def f1(v):
    return fixe(v, 1)


def f2(v):
    return fixe(v, 2)


def entier(n):
    """Formats.entier : « 1 234 567 »."""
    return f"{int(n):,}".replace(",", " ")


def _arrondi(x):
    return int(math.floor(x + 0.5))


# --- Validation (LithoVolume.valider) --------------------------------------------------------------------------------

def valider(p, nb_images=1, apercu=False):
    def exiger(c, m):
        if not c:
            raise ValueError(m)
    exiger(0.4 <= p.ep_min <= 3.0, "Épaisseur minimale de 0,4 à 3 mm.")
    exiger(p.ep_max > p.ep_min and p.ep_max <= 6.0, "L'épaisseur maximale doit dépasser la minimale (6 mm au plus).")
    exiger(apercu or p.pas <= PAS_MAX, f"Pas du relief de {f1(PAS_MIN)} à {f1(PAS_MAX)} mm.")
    exiger(0.0 <= p.cadre <= 15.0, "Bandes pleines de 0 à 15 mm.")
    exiger(not p.fond or 0.8 <= p.ep_fond <= 5.0, "Fond de 0,8 à 5 mm.")
    attendues = "1 ou 4 photos" if p.forme == "boite" else "une photo"
    exiger(nb_images == 1 or (p.forme == "boite" and nb_images == 4), f"Il faut {attendues}.")
    if p.forme not in FORMES:
        raise ValueError("Forme inconnue.")
    if p.forme == "cylindre":
        exiger(30.0 <= p.diametre <= TAILLE_MAX, "Diamètre de 30 à 250 mm.")
        _hauteur(p)
    elif p.forme == "abat_jour":
        exiger(30.0 <= p.diametre <= TAILLE_MAX and 30.0 <= p.diametre_haut <= TAILLE_MAX, "Diamètres de 30 à 250 mm.")
        _hauteur(p)
        if p.douille > 0:
            exiger(p.douille >= 10.0, "Trou de douille de 10 mm au moins (E14 ≈ 29 mm, E27 ≈ 42 mm).")
            exiger(1.0 <= p.ep_plaque <= 5.0, "Plaque de douille de 1 à 5 mm.")
            r_int = p.diametre_haut / 2 - p.ep_max / cos_cone(p)
            exiger(p.douille / 2 <= r_int - 3.0,
                   f"Trou de douille trop grand pour le haut de l'abat-jour ({f1(2 * (r_int - 3.0))} mm au plus).")
    elif p.forme == "globe":
        exiger(40.0 <= p.diametre <= 220.0, "Diamètre du globe de 40 à 220 mm.")
        d_int = p.diametre - 2 * p.ep_max
        exiger(p.ouverture_bas >= 10.0 and p.ouverture_bas <= 0.85 * d_int, f"Ouverture du bas de 10 à {f1(0.85 * d_int)} mm.")
        exiger(p.ouverture_haut == 0.0 or (p.ouverture_haut >= 5.0 and p.ouverture_haut <= 0.85 * d_int),
               f"Ouverture du haut : 0 (fermée) ou de 5 à {f1(0.85 * d_int)} mm.")
    elif p.forme == "coeur":
        exiger(50.0 <= p.largeur <= TAILLE_MAX, "Largeur du cœur de 50 à 250 mm.")
        _hauteur(p)
    else:
        exiger(40.0 <= p.largeur <= TAILLE_MAX, "Côté de la boîte de 40 à 250 mm.")
        _hauteur(p)


def _hauteur(p):
    if not 20.0 <= p.hauteur <= TAILLE_MAX:
        raise ValueError("Hauteur de 20 à 250 mm.")


def cos_cone(p):
    """Cosinus du demi-angle du cône de l'abat-jour (1 pour un cylindre)."""
    dr = (p.diametre - p.diametre_haut) / 2
    return p.hauteur / math.hypot(p.hauteur, dr)


def _pas(p):
    return max(p.pas, PAS_MIN)


def proportion(p, nb_images=1):
    """Proportion largeur / hauteur de la zone photo (une face pour la boîte à 4 photos) : à donner au recadrage."""
    l, h = zone(p, nb_images)
    return l / h


def pas_apercu(p, cote=360):
    """Pas d'aperçu raisonnable (≈ cote nœuds sur le tour)."""
    return min(max(PAS_MIN, zone(p, 1)[0] / cote), PAS_MAX)


def conseils(p):
    c = ["Remplissage 100 % (ou 99 % rectiligne) : la lumière doit traverser une matière pleine.",
         "Hauteur de couche 0,12 mm, buse 0,4 mm, vitesse réduite ; PLA blanc pour le meilleur contraste."]
    if p.forme in ("cylindre", "coeur", "boite"):
        c.append("Imprime debout, " + ("fond sur le plateau" if p.fond else "ouverture vers le haut")
                 + ", sans support ; bordure (brim) de 5 mm.")
    elif p.forme == "abat_jour":
        c.append("Imprime retourné, plaque de douille sur le plateau : elle s'imprime sans support." if p.douille > 0
                 else "Imprime debout, le grand diamètre sur le plateau.")
        pente = math.degrees(math.acos(cos_cone(p)))
        if pente > 30:
            c.append(f"Paroi inclinée de {_arrondi(pente)}° : ralentis les parois extérieures ; au-delà de 45° il faut des supports.")
    else:
        c.append("Imprime posé sur l'ouverture du bas, sans bordure intérieure.")
        c.append("Le haut ouvert limite le surplomb ; active des supports arborescents seulement si le trancheur signale des surplombs."
                 if p.ouverture_haut > 0 else
                 "La calotte du haut est en fort surplomb : supports arborescents à l'intérieur (ou ouverture du haut d'au moins 30 mm).")
    c.append("Éclaire avec une LED blanche (bougie LED, ruban, ampoule LED) : jamais d'ampoule à incandescence, le PLA ramollit vers 60 °C.")
    c.append(f"Épaisseurs {f1(p.ep_min)}–{f1(p.ep_max)} mm" + (" (négatif : clair = épais)." if p.negatif else "."))
    return c


# --- Courbes planes (LithoVolume.Courbe) -----------------------------------------------------------------------------

class Courbe:
    """Courbe plane fermée (sens direct) faite de segments et d'arcs, échantillonnée à abscisse curviligne constante."""

    def __init__(self):
        self.pieces = []  # (arc, a, b, c, d, e, longueur)

    def segment(self, x0, y0, x1, y1):
        self.pieces.append((False, x0, y0, x1, y1, 0.0, math.hypot(x1 - x0, y1 - y0)))
        return self

    def arc(self, cx, cy, r, a0, a1):
        """Arc de centre (cx, cy), rayon r, de l'angle a0 à a1 (a1 > a0 : sens direct ; a1 < a0 : sens indirect)."""
        self.pieces.append((True, cx, cy, r, a0, a1, r * abs(a1 - a0)))
        return self

    @property
    def longueur(self):
        return sum(pc[6] for pc in self.pieces)

    def debut_piece(self, i):
        return sum(pc[6] for pc in self.pieces[:i])

    def longueur_piece(self, i):
        return self.pieces[i][6]

    def point(self, s):
        """Point à l'abscisse s (modulo la longueur) : x, y, normale sortante (nx, ny)."""
        l = self.longueur
        r = math.fmod(math.fmod(s, l) + l, l)
        dernier = len(self.pieces) - 1
        for n, (arc, a, b, c, d, e, lg) in enumerate(self.pieces):
            if r <= lg or n == dernier:
                t = min(max(r / lg, 0.0), 1.0) if lg > 0 else 0.0
                if arc:
                    sens = 1.0 if e >= d else -1.0
                    ang = d + (e - d) * t
                    x, y = a + c * math.cos(ang), b + c * math.sin(ang)
                    tx, ty = -math.sin(ang) * sens, math.cos(ang) * sens
                else:
                    x, y = a + (c - a) * t, b + (d - b) * t
                    tx, ty = (c - a) / lg, (d - b) / lg
                return x, y, ty, -tx
            r -= lg
        raise ValueError("Courbe vide.")


def courbe_boite(c, r):
    """Carré intérieur de côté c à coins arrondis de rayon r, sens direct, en partant du coin avant gauche."""
    h = c / 2
    pi = math.pi
    return (Courbe()
            .segment(-h + r, -h, h - r, -h).arc(h - r, -h + r, r, -pi / 2, 0.0)
            .segment(h, -h + r, h, h - r).arc(h - r, h - r, r, 0.0, pi / 2)
            .segment(h - r, h, -h + r, h).arc(-h + r, h - r, r, pi / 2, pi)
            .segment(-h, h - r, -h, -h + r).arc(-h + r, -h + r, r, pi, 3 * pi / 2))


def courbe_coeur(w, epaisseur):
    """Cœur intérieur de largeur w : deux lobes circulaires, deux tangentes vers la pointe (arrondie), creux du haut
    arrondi d'un rayon supérieur à l'épaisseur (la surface extérieure décalée ne se recoupe pas). Lobes vers +y.
    Pièces : 0 arrondi de pointe, 1 tangente droite, 2 lobe droit, 3 arrondi du creux, 4 lobe gauche, 5 tangente gauche."""
    a = w / 3.1
    c = 0.55 * a
    h = 1.8 * a
    r_creux = max(epaisseur + 2.0, 0.15 * a)
    r_pointe = max(1.5, 0.08 * a)
    # Tangente de la pointe P = (0, −h) au lobe droit (centre (c, 0)), côté extérieur.
    dx, dy = -c, -h
    d = math.hypot(dx, dy)
    beta = math.atan2(dy, dx)
    gamma = math.acos(a / d)
    a_t = beta + gamma
    trx = c + a * math.cos(a_t)
    try_ = a * math.sin(a_t)
    # Arrondi de la pointe.
    ux = trx / math.hypot(trx, try_ + h)
    uy = (try_ + h) / math.hypot(trx, try_ + h)
    sin_phi = ux
    tan_phi = ux / uy
    dist = r_pointe / tan_phi
    brx = ux * dist
    bry = -h + uy * dist
    fby = -h + r_pointe / sin_phi
    a_br = math.atan2(bry - fby, brx)
    a_bl = math.atan2(bry - fby, -brx)
    # Arrondi du creux : cercle tangent extérieurement aux deux lobes, centre sur l'axe.
    yf = math.sqrt((a + r_creux) * (a + r_creux) - c * c)
    nrx = c + a * (0 - c) / (a + r_creux)
    nry = a * yf / (a + r_creux)
    a_nr = math.atan2(nry, nrx - c)
    a_tr = a_t
    if a_nr < a_tr:
        a_nr += 2 * math.pi
    a_creux_d = math.atan2(nry - yf, nrx)
    a_creux_g = math.atan2(nry - yf, -nrx)
    a_nl = math.atan2(nry, -nrx + c)
    a_tl = math.atan2(try_, -trx + c)
    if a_tl < a_nl:
        a_tl += 2 * math.pi
    if a_tr > a_nr:
        a_tr -= 2 * math.pi
    return (Courbe()
            .arc(0.0, fby, r_pointe, a_bl, a_br)
            .segment(brx, bry, trx, try_)
            .arc(c, 0.0, a, a_tr, a_nr)
            .arc(0.0, yf, r_creux, a_creux_d, a_creux_g)
            .arc(-c, 0.0, a, a_nl, a_tl)
            .segment(-trx, try_, -brx, bry))


class Profil:
    """Profil horizontal (sommets d'un anneau), normale sortante, photo et colonne de chaque sommet."""

    def __init__(self, n):
        self.n = n
        self.px, self.py = np.zeros(n), np.zeros(n)
        self.nx, self.ny = np.zeros(n), np.zeros(n)
        self.source = np.zeros(n, dtype=np.int64)    # −1 : épaisseur maximale (coins de la boîte)
        self.colonne = np.zeros(n, dtype=np.int64)

    def mettre(self, k, pt):
        self.px[k], self.py[k], self.nx[k], self.ny[k] = pt

    @staticmethod
    def enroule(c, n, depart):
        """n points à pas constant sur c, en partant de l'abscisse depart ; colonne k ↔ colonne k du plan 0."""
        p = Profil(n)
        l = c.longueur
        for k in range(n):
            p.mettre(k, c.point(depart + l * k / n))
            p.colonne[k] = k
        return p


class Rangs:
    """Rangs du méridien : intérieur = (s·px, s·py, z), extérieur = intérieur + t·(nh·nx, nh·ny, nz)."""

    def __init__(self, n):
        self.n = n
        self.s, self.z = np.zeros(n), np.zeros(n)
        self.nh, self.nz = np.zeros(n), np.zeros(n)
        self.ligne = np.full(n, -1, dtype=np.int64)   # rang du plan photo (−1 : bande pleine)
        self.point = np.zeros(n, dtype=bool)          # pôle (anneau réduit à un point)


# --- Zone photo : Lithophanie.planifier (forme rectangle) ------------------------------------------------------------

class PlanPhoto:
    """Grille de la zone photo : z[j, i] (float32, rangée 0 en bas), pas en mm."""

    def __init__(self, nx, ny, pas, z):
        self.nx, self.ny, self.pas, self.z = nx, ny, pas, z


def _champ_rectangle(nx, ny, pas, largeur, hauteur):
    """Forme.RectArrondi (rayon 0) : distance signée positive dedans, float32 [j, i]."""
    xs = np.arange(nx, dtype=np.float64) * pas
    ys = np.arange(ny, dtype=np.float64) * pas
    cx, cy = largeur / 2, hauteur / 2
    qx = np.abs(xs[None, :] - cx) - largeur / 2
    qy = np.abs(ys[:, None] - cy) - hauteur / 2
    dehors = np.hypot(np.maximum(qx, 0.0), np.maximum(qy, 0.0)) + np.minimum(np.maximum(qx, qy), 0.0)
    return (-dehors).astype(F32)


def plan_rectangle(image, largeur_mm, ep_min, ep_max, cadre_mm, pas_mm, negatif, triangles_max):
    """Lithophanie.planifier d'un rectangle (seuls nœuds et hauteurs servent au volume)."""
    if not 10.0 <= largeur_mm <= 400.0:
        raise ValueError("Largeur de 10 à 400 mm.")
    h_img, l_img = image.shape
    hauteur_voulue = largeur_mm * h_img / l_img
    pas = max(pas_mm, PAS_MIN)
    if int(4.0 * 1.0 * largeur_mm * hauteur_voulue / (pas * pas)) > triangles_max:
        pas = math.sqrt(4.0 * 1.0 * largeur_mm * hauteur_voulue / triangles_max) * 1.02
    nx = max(3, _arrondi(largeur_mm / pas) + 1)
    pas = largeur_mm / (nx - 1)
    ny = max(3, _arrondi(hauteur_voulue / pas) + 1)
    hauteur = (ny - 1) * pas
    g = img.echantillon_grille(image, np.arange(nx) / (nx - 1), 1.0 - np.arange(ny) / (ny - 1))
    g = np.clip(g.astype(np.float64), 0.0, 1.0)
    sombre = g if negatif else 1.0 - g
    z = (ep_min + (ep_max - ep_min) * sombre).astype(F32)
    if cadre_mm > 0:
        champ = (_champ_rectangle(nx, ny, pas, largeur_mm, hauteur) + F32(pas * 0.5 + 1e-6)).astype(F32)
        z[champ.astype(np.float64) < max(cadre_mm, 0.0) + pas * 0.5] = F32(ep_max)
    return PlanPhoto(nx, ny, pas, z)


# --- Squelettes (construction de chaque forme) ----------------------------------------------------------------------

class _Zone:
    def __init__(self, longueur, hauteur, bas, haut):
        self.longueur, self.hauteur, self.bas, self.haut = longueur, hauteur, bas, haut


def _face_droite(p):
    return p.largeur - 2 * p.ep_max - 2 * RAYON_COIN_BOITE


def _t_moy(p):
    return (p.ep_min + p.ep_max) / 2


def _courbe(p):
    if p.forme == "coeur":
        return courbe_coeur(p.largeur - 2 * p.ep_max, p.ep_max)
    if p.forme == "boite":
        return courbe_boite(p.largeur - 2 * p.ep_max, RAYON_COIN_BOITE)
    return None


def _bandes(p, pas):
    bas = max(max(p.cadre, pas), p.ep_fond + pas if (p.fond and p.forme != "globe") else 0.0)
    haut = max(max(p.cadre, pas), p.ep_plaque + pas if (p.forme == "abat_jour" and p.douille > 0) else 0.0)
    return bas, haut


def _angles_globe(p, ri):
    t0 = math.asin(min(p.ouverture_bas / 2 / ri, 1.0))
    t1 = math.pi - math.asin(min(p.ouverture_haut / 2 / ri, 1.0)) if p.ouverture_haut > 0 else math.pi
    return t0, t1


def _zone_enroulee(p, pas):
    bas, haut = _bandes(p, pas)
    if p.forme == "globe":
        ri = p.diametre / 2 - p.ep_max
        t0, t1 = _angles_globe(p, ri)
        za, zb = t0 + bas / ri, t1 - haut / ri
        if zb - za < 10.0 / ri:
            raise ValueError("Ouvertures trop grandes : il ne reste pas de place pour la photo.")
        return _Zone(2 * math.pi * (ri + _t_moy(p)), ri * (zb - za), bas, haut)
    if p.forme == "abat_jour":
        c = cos_cone(p)
        r_moy = (p.diametre + p.diametre_haut) / 4 - p.ep_max / c
        h = p.hauteur - bas - haut
        if h < 10:
            raise ValueError("Abat-jour trop bas pour ses bandes pleines.")
        return _Zone(2 * math.pi * (r_moy + _t_moy(p) / c), h / c, bas, haut)
    courbe = _courbe(p)
    l = courbe.longueur if courbe is not None else 2 * math.pi * (p.diametre / 2 - p.ep_max)
    h = p.hauteur - bas - haut
    if h < 10:
        raise ValueError("Trop bas pour les bandes pleines (et le fond) : il ne reste pas de place pour la photo.")
    return _Zone(l + 2 * math.pi * _t_moy(p), h, bas, haut)


def zone(p, nb_images=1):
    """Largeur et hauteur de la zone photo (mm) pour nb_images photos."""
    if p.forme == "boite" and nb_images == 4:
        return _face_droite(p), p.hauteur
    z = _zone_enroulee(p, _pas(p))
    return z.longueur, z.hauteur


def _etirer(image, l, h, pas):
    """Photo étirée à la proportion de la zone, à la résolution utile (≈ un pixel par nœud)."""
    lp, hp = _arrondi(l / pas) + 1, _arrondi(h / pas) + 1
    if float(lp) * hp > PIXELS_ETIRES_MAX:
        f = math.sqrt(PIXELS_ETIRES_MAX / (float(lp) * hp))
        lp, hp = int(lp * f), int(hp * f)
    return img.redimensionner(image, max(8, lp), max(8, hp))


def _plan_photo(p, image, l, h, cadre, triangles_max, avert, apercu=False, memo=None):
    """Plan de la zone l × h mm : grande zone ramenée à 400 mm de large (mêmes nœuds, même relief)."""
    pas = _pas(p)
    f = max(1.0, l / 400.0)
    cle = (id(image), l, h, cadre, triangles_max)
    if memo is not None and cle in memo:
        plan = memo[cle]
    else:
        etiree = _etirer(image, l, h, pas)
        plan = plan_rectangle(etiree, l / f, p.ep_min, p.ep_max, cadre / f, pas / f, p.negatif, triangles_max)
        if memo is not None:
            memo[cle] = plan
    reel = plan.pas * f
    if not apercu and reel > PAS_MAX + 1e-9:
        raise ValueError(f"Volume trop grand pour {entier(triangles_max)} triangles : le détail tomberait à {f2(reel)} mm. "
                         "Réduis la taille.")
    if avert is not None and reel > pas * 1.01:
        avert.append(f"Détail réduit à {f2(reel)} mm pour garder un fichier raisonnable ({entier(triangles_max)} triangles au plus).")
    return plan, reel


class _Squelette:
    def __init__(self, profil, rangs, plans, avertissements, rang_fond, rang_plaque, rayon_trou, raccord_sphere,
                 perimetre, hauteur_image, pas_reel):
        self.profil, self.rangs, self.plans, self.avertissements = profil, rangs, plans, avertissements
        self.pas_reel = pas_reel
        self.rang_fond, self.rang_plaque, self.rayon_trou = rang_fond, rang_plaque, rayon_trou
        self.raccord_sphere, self.perimetre, self.hauteur_image = raccord_sphere, perimetre, hauteur_image

    def construire(self, p):
        pr, rg = self.profil, self.rangs
        nu, nv = pr.n, rg.n
        t = np.full((nv, nu), F32(p.ep_max), dtype=F32)
        lignes = np.arange(nv)
        rangs_photo = (rg.ligne >= 0) & (lignes > 0) & (lignes < nv - 1)
        jj = lignes[rangs_photo]
        for s, plan in enumerate(self.plans):
            kk = np.nonzero(pr.source == s)[0]
            if jj.size and kk.size:
                t[np.ix_(jj, kk)] = plan.z[np.ix_(rg.ligne[jj], pr.colonne[kk])]
        ext = self._anneaux(t.astype(np.float64))
        intr = self._anneaux(np.zeros((nv, nu)))
        suite, points = [], []
        for j in range(nv):
            suite.append(ext[j])
            points.append(bool(rg.point[j]))
        haut = self.rang_plaque if self.rang_plaque >= 0 else nv - 1
        if self.rang_plaque >= 0:
            suite += [self._cercle(self.rayon_trou, ext[nv - 1][0, 2]),
                      self._cercle(self.rayon_trou, F32(rg.z[self.rang_plaque]))]
            points += [False, False]
        elif self.raccord_sphere and not rg.point[nv - 1]:
            suite.append(_raccord(intr[nv - 1], ext[nv - 1]))
            points.append(False)
        bas = self.rang_fond if self.rang_fond >= 0 else 0
        for j in range(haut, bas - 1, -1):
            suite.append(intr[j])
            points.append(bool(rg.point[j]))
        if self.rang_fond < 0 and self.raccord_sphere:
            suite.append(_raccord(intr[0], ext[0]))
            points.append(False)
        e64 = ext.astype(np.float64)
        boite = [float(e64[..., c].min()) for c in range(3)] + [float(e64[..., c].max()) for c in range(3)]
        plan = PlanVolume(p, nu, nv, t, np.stack(suite), np.array(points, dtype=bool), self.rang_fond >= 0,
                          list(self.avertissements), boite, self.perimetre, self.hauteur_image)
        plan.pas_reel = self.pas_reel
        return plan

    def _anneaux(self, e):
        """Anneaux (nv, nu, 3) float32 : sommet = (s·px + e·nh·nx, s·py + e·nh·ny, z + e·nz) arrondi en float32."""
        pr, rg = self.profil, self.rangs
        s, nh = rg.s[:, None], rg.nh[:, None]
        x = s * pr.px[None, :] + e * nh * pr.nx[None, :]
        y = s * pr.py[None, :] + e * nh * pr.ny[None, :]
        z = rg.z[:, None] + e * rg.nz[:, None]
        return np.stack([x, y, z], -1).astype(F32)

    def _cercle(self, r, z):
        """Anneau du trou de douille (profil circulaire unité) à la hauteur z (float32)."""
        pr = self.profil
        return np.stack([(r * pr.px).astype(F32), (r * pr.py).astype(F32), np.full(pr.n, z, dtype=F32)], -1)


def _raccord(interieur, exterieur):
    """Anneau intermédiaire : position horizontale de l'intérieur, hauteur de l'extérieur (bord plat)."""
    a = interieur.copy()
    a[:, 2] = exterieur[:, 2]
    return a


def _enroule(p, image, triangles_max, apercu=False):
    """Une photo enroulée (cylindre, abat-jour, globe, cœur, boîte une photo)."""
    pas = _pas(p)
    z_ = _zone_enroulee(p, pas)
    avert = []
    plan, reel = _plan_photo(p, image, z_.longueur, z_.hauteur, 0.0, int(triangles_max * 0.85), avert, apercu)
    nu = plan.nx - 1
    c = _courbe(p)
    if c is None:
        profil = Profil.enroule(Courbe().arc(0.0, 0.0, 1.0, 0.0, 2 * math.pi), nu, math.pi / 2)
    elif p.forme == "coeur":
        profil = Profil.enroule(c, nu, c.debut_piece(3) + c.longueur_piece(3) / 2)
    else:
        profil = Profil.enroule(c, nu, 0.0)
    dv = z_.hauteur / (plan.ny - 1)
    if p.forme == "globe":
        return _globe(p, profil, plan, z_, avert, reel)
    zs, lignes = [], []
    z_bas, z_haut = z_.bas, p.hauteur - z_.haut
    c0 = cos_cone(p) if p.forme == "abat_jour" else 1.0
    pas_z = dv * c0
    fixes = [0.0, z_bas - pas_z, z_haut + pas_z, p.hauteur]
    z_fond = p.ep_fond if p.fond else -1.0
    z_plaque = p.hauteur - p.ep_plaque if (p.forme == "abat_jour" and p.douille > 0) else -1.0
    if z_fond > 0:
        fixes.append(z_fond)
    if z_plaque > 0:
        fixes.append(z_plaque)
    vus = []
    for z in fixes:
        if 0 <= z <= p.hauteur and (z < z_bas - 1e-6 or z > z_haut + 1e-6) and z not in vus:
            vus.append(z)
    for z in vus:
        zs.append(z)
        lignes.append(-1)
    for r in range(plan.ny):
        zs.append(z_bas + (z_haut - z_bas) * r / (plan.ny - 1))
        lignes.append(r)
    ordre = sorted(range(len(zs)), key=zs.__getitem__)
    garde = []
    for i in ordre:
        if not garde or zs[i] - zs[garde[-1]] > 1e-6:
            garde.append(i)
    rangs = Rangs(len(garde))
    cone = p.forme == "abat_jour"
    ri0 = p.diametre / 2 - p.ep_max / c0
    ri1 = (p.diametre_haut if cone else p.diametre) / 2 - p.ep_max / c0
    rang_fond = rang_plaque = -1
    for j, i in enumerate(garde):
        z = zs[i]
        rangs.z[j] = z
        rangs.ligne[j] = lignes[i]
        rangs.s[j] = ri0 + (ri1 - ri0) * z / p.hauteur if c is None else 1.0
        rangs.nh[j] = 1.0 / c0
        rangs.nz[j] = 0.0
        if z_fond > 0 and abs(z - z_fond) < 1e-6:
            rang_fond = j
        if z_plaque > 0 and abs(z - z_plaque) < 1e-6:
            rang_plaque = j
    if cone and math.degrees(math.acos(c0)) > 45:
        avert.append("Paroi inclinée de plus de 45° : l'impression demande des supports.")
    r_trou = p.douille / 2 if rang_plaque >= 0 else 0.0
    return _Squelette(profil, rangs, [plan], avert, rang_fond, rang_plaque, r_trou, False, z_.longueur, z_.hauteur, reel)


def _globe(p, profil, plan, z_, avert, reel):
    ri = p.diametre / 2 - p.ep_max
    t0, t1 = _angles_globe(p, ri)
    ta, tb = t0 + z_.bas / ri, t1 - z_.haut / ri
    dt = (tb - ta) / (plan.ny - 1)
    th, lignes = [], []
    t = t0
    while t < ta - 0.5 * dt:
        th.append(t)
        lignes.append(-1)
        t += dt
    for r in range(plan.ny):
        th.append(ta + (tb - ta) * r / (plan.ny - 1))
        lignes.append(r)
    t = tb + dt
    while t < t1 - 0.5 * dt:
        th.append(t)
        lignes.append(-1)
        t += dt
    th.append(t1)
    lignes.append(-1)
    rangs = Rangs(len(th))
    decalage = (ri + p.ep_max) * math.cos(t0)
    for j, a in enumerate(th):
        pole = p.ouverture_haut <= 0 and j == len(th) - 1
        rangs.ligne[j] = lignes[j]
        rangs.s[j] = 0.0 if pole else ri * math.sin(a)
        rangs.z[j] = -ri * math.cos(a) + decalage
        rangs.nh[j] = 0.0 if pole else math.sin(a)
        rangs.nz[j] = -math.cos(a)
        rangs.point[j] = pole
    return _Squelette(profil, rangs, [plan], avert, -1, -1, 0.0, True, z_.longueur, z_.hauteur, reel)


def _boite_faces(p, images, triangles_max, apercu=False):
    """Boîte à quatre photos : une face droite par photo (avec son cadre), coins arrondis pleins."""
    a = _face_droite(p)
    avert = []
    plans, reel = [], 0.0
    memo = {}
    for f, im in enumerate(images):
        pl, reel = _plan_photo(p, im, a, p.hauteur, p.cadre, int(triangles_max * 0.8 / 4), avert if f == 0 else None,
                               apercu, memo)
        plans.append(pl)
    nx, ny = plans[0].nx, plans[0].ny
    if not all(pl.nx == nx and pl.ny == ny for pl in plans):
        raise RuntimeError("Faces incohérentes.")
    c = courbe_boite(p.largeur - 2 * p.ep_max, RAYON_COIN_BOITE)
    arc = c.longueur_piece(1)
    mc = max(2, int(math.ceil(arc / reel)))
    nu = 4 * (nx + mc - 1)
    profil = Profil(nu)
    k = 0
    for f in range(4):
        d0 = c.debut_piece(2 * f)
        for i in range(nx):
            profil.mettre(k, c.point(d0 + a * i / (nx - 1)))
            profil.source[k], profil.colonne[k] = f, i
            k += 1
        d1 = c.debut_piece(2 * f + 1)
        for q in range(1, mc):
            profil.mettre(k, c.point(d1 + arc * q / mc))
            profil.source[k] = -1
            k += 1
    rangs = Rangs(ny)
    rang_fond = -1
    for j in range(ny):
        rangs.z[j] = p.hauteur * j / (ny - 1)
        rangs.s[j] = 1.0
        rangs.nh[j] = 1.0
        rangs.ligne[j] = j
        if p.fond and rang_fond < 0 and j > 0 and rangs.z[j] >= p.ep_fond - 1e-9:
            rang_fond = j
    if p.fond and rang_fond >= ny - 2:
        raise ValueError("Fond trop épais pour cette hauteur.")
    return _Squelette(profil, rangs, plans, avert, rang_fond, -1, 0.0, False, a, p.hauteur, reel)


def planifier(p, images, triangles_max=TRIANGLES_MAX, apercu=False):
    """Prépare le volume (LithoVolume.planifier). images : une photo en gris (float32, déjà recadrée et réglée) ou,
    pour la boîte, quatre (devant, droite, arrière, gauche). Si le détail demandé dépasse triangles_max, le pas est
    élargi (avertissement) ; au-delà de PAS_MAX ou si le compte exact dépasse encore la borne : ValueError.
    apercu=True (aperçu seulement) : pas élargi sans limite, jamais de refus pour la taille."""
    valider(p, len(images), apercu)
    if p.forme == "boite" and len(images) == 4:
        squelette = _boite_faces(p, images, triangles_max, apercu)
    else:
        squelette = _enroule(p, images[0], triangles_max, apercu)
    plan = squelette.construire(p)
    n = plan.triangles()
    if not apercu and n > triangles_max:
        raise ValueError(f"Volume trop lourd ({entier(n)} triangles, {entier(triangles_max)} au plus) : augmente le pas "
                         "ou réduis la taille.")
    return plan


# --- Volume préparé (PlanVolume) -------------------------------------------------------------------------------------

class PlanVolume:
    """Épaisseurs de la surface extérieure (rang j de bas en haut, colonne k autour) et suite d'anneaux à relier.
    anneaux : (m, nu, 3) float32 ; points[i] : anneau réduit à un point (pôle) ; boite : [xmin, ymin, zmin, xmax, …]."""

    def __init__(self, param, nu, nv, epaisseurs, anneaux, points, avec_fond, avertissements, boite, perimetre,
                 hauteur_image):
        self.param, self.nu, self.nv, self.epaisseurs = param, nu, nv, epaisseurs
        self.anneaux, self.points, self.avec_fond = anneaux, points, avec_fond
        self.avertissements, self.boite = avertissements, boite
        self.perimetre_mm, self.hauteur_image_mm = perimetre, hauteur_image
        self.pas_reel = None

    @property
    def largeur_mm(self):
        return self.boite[3] - self.boite[0]

    @property
    def profondeur_mm(self):
        return self.boite[4] - self.boite[1]

    @property
    def hauteur_mm(self):
        return self.boite[5] - self.boite[2]

    def triangles(self):
        """Nombre exact de triangles du STL."""
        m, nu = len(self.anneaux), self.nu
        n = 0
        for i in range(m - 1 if self.avec_fond else m):
            a, b = self.points[i], self.points[(i + 1) % m]
            n += 0 if (a and b) else nu if (a or b) else 2 * nu
        if self.avec_fond:
            n += 2 * (nu - 2)
        return n

    def sommets(self):
        """Tous les sommets des anneaux, (m·nu, 3) float32 : l'anneau r, sommet k, est à l'indice r·nu + k."""
        return self.anneaux.reshape(-1, 3)

    def indices(self):
        """Triangles (T, 3) en indices de ``sommets()``, dans l'ordre exact de PlanVolume.mailler (normales sortantes)."""
        m, nu = len(self.anneaux), self.nu
        k = np.arange(nu, dtype=np.int64)
        k2 = np.where(k + 1 == nu, 0, k + 1)
        morceaux = []
        for i in range(m - 1 if self.avec_fond else m):
            a, b = i, (i + 1) % m
            pa, pb = bool(self.points[a]), bool(self.points[b])
            A, B = a * nu, b * nu
            if pa and pb:
                continue
            if pb:
                morceaux.append(np.stack([A + k, A + k2, np.full(nu, B)], 1))
            elif pa:
                morceaux.append(np.stack([np.full(nu, A), B + k2, B + k], 1))
            else:
                t1 = np.stack([A + k, A + k2, B + k2], 1)
                t2 = np.stack([A + k, B + k2, B + k], 1)
                morceaux.append(np.stack([t1, t2], 1).reshape(-1, 3))
        if self.avec_fond:
            # Dessous du fond (premier anneau, normale −z) et dessus du fond (dernier anneau, normale +z).
            morceaux.append(self._fond(0, False))
            morceaux.append(self._fond(m - 1, True))
        return np.concatenate(morceaux, 0) if morceaux else np.zeros((0, 3), dtype=np.int64)

    def _fond(self, r, dessus):
        a = self.anneaux[r].astype(np.float64)
        t = np.array(trianguler(a[:, 0], a[:, 1]), dtype=np.int64).reshape(-1, 3)
        if not dessus:
            t = t[:, [0, 2, 1]]
        return t + r * self.nu

    def transmission(self):
        """Lumière transmise de la surface déroulée (Lithophanie.Plan.transmission, Beer–Lambert), colonne 0 répétée à
        droite (tour complet), ligne du haut d'abord : (nv, nu + 1) float32."""
        p = self.param
        z = np.concatenate([self.epaisseurs, self.epaisseurs[:, :1]], 1).astype(np.float64)
        i_max, i_min = math.exp(-MU_PLA * p.ep_min), math.exp(-MU_PLA * p.ep_max)
        v = (((np.exp(-MU_PLA * z) - i_min) / (i_max - i_min)) * 0.92 + 0.04).astype(F32)
        return np.clip(v, F32(0), F32(1))[::-1].copy()

    def apercu_argb(self):
        """PlanVolume.apercu : teinte de LED blanche chaude, (nv, nu + 1, 3) uint8 RGB."""
        v = np.clip(self.transmission().astype(np.float64), 0.0, 1.0)
        r = np.floor(255 * v ** 0.85 + 0.5)
        g = np.floor(238 * v + 0.5)
        b = np.floor(205 * v ** 1.15 + 0.5)
        return np.clip(np.stack([r, g, b], -1), 0, 255).astype(np.uint8)
