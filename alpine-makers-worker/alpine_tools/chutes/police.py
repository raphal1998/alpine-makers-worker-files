"""Police au trait « Alpine Trait » (port de PoliceTrait.kt d'Alpine Chutes, C:\\AlpineChutes\\core).

Police dessinée pour Alpine Makers (aucune police existante copiée ni dérivée) : capitales « bâton » sur une grille de
10 unités de haut (y vers le BAS : 0 = haut des capitales, 10 = ligne de base), accents au-dessus (y < 0). Les courbes
sont des « galets » : quarts de superellipse d'exposant 2,5, échantillonnés tous les 5°. Signes d'identité : barre du A
en chevron, M à creux profond, 0 barré, 1 à empattement de pied.

Un texte devient des traits (polylignes en mm) puis, épaissi, des contours fermés : champ de distance sur grille
(e/2 − distance au trait le plus proche, seaux spatiaux) et marching squares interpolé. Variante ``pochoir`` : ponts
qui coupent les traits pour qu'aucun îlot (centre d'un O) ne tombe d'une plaque ajourée.

Mêmes opérations flottantes que le Kotlin (champ en float32 comme FloatArray, troncatures ``toInt``) : les boucles
rendues, leur ordre et leurs sommets sont ceux de l'application.
"""
import math
from decimal import Decimal, localcontext

import numpy as np

from .champ import Grille
from .geometrie import Boucle, Refus, boite, simplifier_boucle

CAPITALE = 10.0
"""Hauteur des capitales en unités de dessin."""
APPROCHE = 1.5
"""Approche entre deux lettres, en unités (hors épaisseur du trait)."""
ESPACE = 3.0
"""Chasse de l'espace en unités, approche non comprise."""
INTERLIGNE = 1.5
"""Interligne, en multiple de la hauteur des capitales."""
GALET = 2.5
"""Exposant des « galets » (superellipses)."""
NOEUDS_MAX = 8_000_000
"""Garde-fou mémoire du champ (≈ 32 Mo de flottants)."""

_F32 = np.float32
_FLOAT_MAX = 3.4028234663852886e38  # Float.MAX_VALUE
_DEG = 0.017453292519943295  # Math.toRadians (JDK 9 et suivants) : angle × (π / 180)


# ------------------------------------------------------------------------------------------------ dessin des glyphes

class _Glyphe:
    __slots__ = ("traits", "xmin", "xmax")

    def __init__(self, traits):
        self.traits = traits
        self.xmin = min(min(q[0] for q in t) for t in traits)
        self.xmax = max(max(q[0] for q in t) for t in traits)


def _p(*c):
    """Polyligne depuis des coordonnées (x, y) alternées, en unités."""
    return [(float(c[i]), float(c[i + 1])) for i in range(0, len(c), 2)]


def _pt(x, y):
    """Point isolé (un point épaissi = un disque du diamètre du trait)."""
    return [(float(x), float(y))]


# cos, sin et pow correctement arrondis (Decimal à 60 chiffres) : c'est ce que rendent Math.cos / Math.sin / Math.pow
# de la JVM sur les 204 angles des galets (recoupé avec l'oracle), alors que le CRT de Windows s'écarte parfois d'un
# ulp pour cos et sin. Le dessin ne dépend ainsi ni du CRT ni du processeur du Worker.
_PI_DEC = Decimal("3.14159265358979323846264338327950288419716939937510582097494459230781640628620899863")
_FONCTIONS = {}


def _cos_sin(r):
    cs = _FONCTIONS.get(("cs", r))
    if cs is None:
        with localcontext() as ctx:
            ctx.prec = 60
            x = Decimal(r) % (2 * _PI_DEC)
            c = s = Decimal(0)
            terme = Decimal(1)  # x^k / k!
            seuil = Decimal("1e-75")
            k = 0
            while True:
                q = k % 4
                if q == 0:
                    c += terme
                elif q == 1:
                    s += terme
                elif q == 2:
                    c -= terme
                else:
                    s -= terme
                k += 1
                terme = terme * x / k
                if k > 8 and abs(terme) < seuil:
                    break
            cs = (float(c), float(s))
        _FONCTIONS[("cs", r)] = cs
    return cs


def _pow(x, e):
    """x^e (x > 0) correctement arrondi ; e = 1 rend x comme Math.pow."""
    if e == 1.0 or x == 1.0:
        return x
    v = _FONCTIONS.get(("p", x, e))
    if v is None:
        with localcontext() as ctx:
            ctx.prec = 60
            v = float((Decimal(x).ln() * Decimal(e)).exp())
        _FONCTIONS[("p", x, e)] = v
    return v


def _sur_galet(cx, cy, a, b, t, n=GALET):
    """Point d'un galet (superellipse) de centre (cx, cy), demi-axes a, b, à l'angle t (degrés, sens visuel direct)."""
    e = 2.0 / n
    r = t * _DEG
    co, si = _cos_sin(r)
    x = 0.0 if abs(co) < 1e-12 else math.copysign(1.0, co) * _pow(abs(co), e)
    y = 0.0 if abs(si) < 1e-12 else math.copysign(1.0, si) * _pow(abs(si), e)
    return (cx + a * x, cy - b * y)


def _ga(cx, cy, a, b, t0, t1, n=GALET):
    """Arc de galet de t0 à t1 (degrés ; croissant = sens visuel direct), un point tous les 5° au plus."""
    m = max(1, int(math.ceil(abs(t1 - t0) / 5.0)))
    return [_sur_galet(cx, cy, a, b, t0 + (t1 - t0) * k / m, n) for k in range(m + 1)]


def _ch(*morceaux):
    """Enchaîne des morceaux en une polyligne (points consécutifs confondus retirés)."""
    r = []
    for m in morceaux:
        for q in m:
            if not r or math.hypot(r[-1][0] - q[0], r[-1][1] - q[1]) > 1e-9:
                r.append(q)
    return r


def _miroir(t):
    return [[(-x, y) for x, y in l] for l in t]


def _demi_tour(t, l):
    return [[(l - x, CAPITALE - y) for x, y in s] for s in t]


def _construire():
    m = {}

    def g(c, *t):
        m[c] = _Glyphe(list(t))

    def gl(c, t):
        m[c] = _Glyphe(list(t))

    p, pt, ga, ch = _p, _pt, _ga, _ch

    # ---- Capitales
    g('A', p(0, 10, 3.5, 0, 7, 10), p(1.19, 6.6, 3.5, 5.7, 5.81, 6.6))
    g('B', p(0, 0, 0, 10),
      ch(p(0, 0, 3.0, 0), ga(3.0, 2.35, 2.4, 2.35, 90.0, -90.0), p(0, 4.7)),
      ch(p(0, 4.7, 3.2, 4.7), ga(3.2, 7.35, 2.4, 2.65, 90.0, -90.0), p(0, 10)))
    g('C', ga(3.5, 5.0, 3.5, 5.0, 40.0, 320.0))
    g('D', p(0, 0, 0, 10), ch(p(0, 0), ga(2.4, 5.0, 4.0, 5.0, 90.0, -90.0), p(0, 10)))
    g('E', p(5, 0, 0, 0, 0, 10, 5, 10), p(0, 4.9, 4.3, 4.9))
    g('F', p(4.8, 0, 0, 0, 0, 10), p(0, 4.9, 4.2, 4.9))
    g('G', ch(ga(3.5, 5.0, 3.5, 5.0, 40.0, 360.0), p(3.9, 5.0)))
    g('H', p(0, 0, 0, 10), p(6, 0, 6, 10), p(0, 5, 6, 5))
    g('I', p(0, 0, 0, 10))
    g('J', ch(p(4.5, 0), ga(2.25, 7.0, 2.25, 3.0, 0.0, -160.0)))
    g('K', p(0, 0, 0, 10), p(5.4, 0, 0, 6.2), p(2.0, 3.9, 5.6, 10))
    g('L', p(0, 0, 0, 10, 4.8, 10))
    g('M', p(0, 10, 0, 0, 3.7, 6.6, 7.4, 0, 7.4, 10))
    g('N', p(0, 10, 0, 0, 6, 10, 6, 0))
    g('O', ga(3.8, 5.0, 3.8, 5.0, 0.0, 360.0))
    g('P', p(0, 0, 0, 10), ch(p(0, 0, 2.8, 0), ga(2.8, 2.65, 2.6, 2.65, 90.0, -90.0), p(0, 5.3)))
    g('Q', ga(3.8, 5.0, 3.8, 5.0, 0.0, 360.0), p(4.7, 7.1, 7.5, 10.5))
    g('R', p(0, 0, 0, 10), ch(p(0, 0, 2.8, 0), ga(2.8, 2.65, 2.6, 2.65, 90.0, -90.0), p(0, 5.3)), p(2.6, 5.3, 5.6, 10))
    g('S', ch(ga(2.8, 2.5, 2.6, 2.5, 30.0, 270.0), ga(2.8, 7.5, 2.8, 2.5, 90.0, -150.0)))
    g('T', p(0, 0, 6, 0), p(3, 0, 3, 10))
    g('U', ch(p(0, 0), ga(3.0, 6.5, 3.0, 3.5, 180.0, 360.0), p(6, 0)))
    g('V', p(0, 0, 3.3, 10, 6.6, 0))
    g('W', p(0, 0, 2.35, 10, 4.7, 2.0, 7.05, 10, 9.4, 0))
    g('X', p(0, 0, 6.2, 10), p(6.2, 0, 0, 10))
    g('Y', p(0, 0, 3.2, 5.2, 6.4, 0), p(3.2, 5.2, 3.2, 10))
    g('Z', p(0, 0, 5.8, 0, 0, 10, 5.8, 10))

    # ---- Chiffres (chasse 5,6 sauf le 1)
    a225 = _sur_galet(2.8, 5.0, 2.8, 5.0, 225.0)
    a45 = _sur_galet(2.8, 5.0, 2.8, 5.0, 45.0)
    g('0', ga(2.8, 5.0, 2.8, 5.0, 0.0, 360.0), [a225, a45])
    g('1', p(0, 2.4, 2.4, 0, 2.4, 10), p(0, 10, 4.8, 10))
    g('2', ch(ga(2.8, 2.9, 2.8, 2.9, 155.0, -35.0), p(0, 10, 5.8, 10)))
    g('3', ch(ga(2.6, 2.5, 2.6, 2.5, 150.0, -90.0), p(1.6, 5.0)), ch(p(1.6, 5.0), ga(2.6, 7.5, 2.8, 2.5, 90.0, -150.0)))
    g('4', p(4.2, 10, 4.2, 0, 0, 7.0, 6.0, 7.0))
    g('5', ch(p(5.0, 0, 0.5, 0, 0.3, 4.7), ga(2.6, 7.35, 3.0, 2.65, 90.0, -150.0)))
    six = [ch(ga(2.8, 5.0, 2.8, 5.0, 60.0, 180.0), p(0, 7.2)), ga(2.8, 7.2, 2.8, 2.8, 0.0, 360.0)]
    gl('6', six)
    g('7', p(0, 0, 5.6, 0, 1.8, 10))
    g('8', ga(2.8, 2.45, 2.4, 2.45, 0.0, 360.0), ga(2.8, 7.3, 2.8, 2.7, 0.0, 360.0))
    gl('9', _demi_tour(six, 5.6))

    # ---- Ponctuation et signes
    g('.', pt(0.0, 10.0))
    g(',', p(0.7, 9.6, 0, 11.6))
    g(';', pt(0.7, 4.0), p(0.7, 9.6, 0, 11.6))
    g(':', pt(0.0, 4.0), pt(0.0, 10.0))
    g('!', p(0, 0, 0, 7), pt(0.0, 10.0))
    g('?', ch(ga(2.5, 2.6, 2.5, 2.6, 155.0, -60.0), p(2.5, 5.9, 2.5, 7.0)), pt(2.5, 10.0))
    g('\'', p(0, 0, 0, 2.8))
    g('"', p(0, 0, 0, 2.8), p(1.7, 0, 1.7, 2.8))
    g('-', p(0, 5.6, 3.4, 5.6))
    g('\u2013', p(0, 5.6, 5.6, 5.6))  # tiret demi-cadratin
    g('\u2014', p(0, 5.6, 9.0, 5.6))  # tiret cadratin
    g('+', p(0, 5.4, 5, 5.4), p(2.5, 2.9, 2.5, 7.9))
    g('=', p(0, 4.1, 5, 4.1), p(0, 6.9, 5, 6.9))
    g('/', p(0, 10, 4.4, 0))
    g('&', ch(p(6.4, 10.0), ga(2.9, 2.2, 1.8, 2.2, 215.0, -55.0), ga(2.9, 7.6, 2.9, 2.4, 150.0, 335.0), p(6.3, 5.9)))
    paren = ga(3.5, 5.2, 3.5, 6.4, 118.0, 242.0, 2.0)
    g('(', paren)
    gl(')', _miroir([paren]))
    g('[', p(2.0, -0.5, 0, -0.5, 0, 10.5, 2.0, 10.5))
    gl(']', _miroir([p(2.0, -0.5, 0, -0.5, 0, 10.5, 2.0, 10.5)]))
    g('#', p(2.3, 0.6, 1.5, 9.4), p(4.9, 0.6, 4.1, 9.4), p(0.4, 3.4, 6.0, 3.4), p(0, 6.6, 5.6, 6.6))
    g('@', ga(4.3, 5.6, 1.8, 2.1, 0.0, 360.0),
      ch(p(6.1, 3.4, 6.1, 7.2), ga(7.4, 7.2, 1.3, 1.0, 180.0, 360.0), ga(4.5, 5.0, 4.5, 5.0, -15.0, 300.0)))
    g('*', p(2.2, 0.2, 2.2, 4.6), p(0.3, 1.3, 4.1, 3.5), p(0.3, 3.5, 4.1, 1.3))
    g('_', p(0, 11.2, 5.6, 11.2))
    g('\u00b0', ga(1.4, 1.4, 1.4, 1.4, 0.0, 360.0, 2.0))  # °
    g('\u20ac', ga(4.0, 5.0, 3.4, 5.0, 40.0, 320.0), p(0, 4.0, 4.6, 4.0), p(0, 6.0, 4.2, 6.0))  # €
    g('%', ga(1.3, 1.7, 1.3, 1.7, 0.0, 360.0), ga(5.1, 8.3, 1.3, 1.7, 0.0, 360.0), p(0.4, 10, 6.0, 0))
    g('<', p(4.6, 2.6, 0, 5.4, 4.6, 8.2))
    g('>', p(0, 2.6, 4.6, 5.4, 0, 8.2))
    g('\u00ab', p(2.2, 3.2, 0, 5.4, 2.2, 7.6), p(4.8, 3.2, 2.6, 5.4, 4.8, 7.6))  # «
    g('\u00bb', p(0, 3.2, 2.2, 5.4, 0, 7.6), p(2.6, 3.2, 4.8, 5.4, 2.6, 7.6))  # »

    # ---- Ligatures et lettres composées
    g('\u00c6', p(0, 10, 4.6, 0, 9.0, 0), p(4.6, 0, 4.6, 10, 9.0, 10), p(4.6, 4.9, 8.4, 4.9), p(1.56, 6.6, 4.6, 6.6))  # Æ
    g('\u0152', ch(p(5.2, 0, 3.6, 0), ga(3.6, 5.0, 3.6, 5.0, 90.0, 270.0), p(5.2, 10)),
      p(9.6, 0, 5.2, 0, 5.2, 10, 9.6, 10), p(5.2, 4.9, 9.0, 4.9))  # Œ
    g('\u00d8', ga(3.8, 5.0, 3.8, 5.0, 0.0, 360.0), p(0.4, 10.3, 7.2, -0.3))  # Ø

    # ---- Accents (au-dessus des capitales, centrés sur la lettre)
    def accent(c, base, sorte):
        b = m[base]
        x = (b.xmin + b.xmax) / 2
        if sorte == 'a':
            signe = [p(x - 0.7, -1.4, x + 0.9, -3.2)]
        elif sorte == 'g':
            signe = [p(x - 0.9, -3.2, x + 0.7, -1.4)]
        elif sorte == 'c':
            signe = [p(x - 1.6, -1.4, x, -3.0, x + 1.6, -1.4)]
        elif sorte == 't':
            signe = [pt(x - 1.3, -2.0), pt(x + 1.3, -2.0)]
        elif sorte == 'n':
            signe = [p(x - 1.7, -1.6, x - 0.85, -2.6, x + 0.85, -1.8, x + 1.7, -2.8)]
        elif sorte == 'r':
            signe = [ga(x, -2.3, 1.0, 0.9, 0.0, 360.0, 2.0)]
        else:
            signe = [ch(p(x + 0.2, 10.0, x + 0.2, 11.0), ga(x + 0.2, 11.8, 1.0, 0.8, 90.0, -150.0))]
        m[c] = _Glyphe(b.traits + signe)

    for c, base, sorte in (
            ('\u00c0', 'A', 'g'), ('\u00c1', 'A', 'a'), ('\u00c2', 'A', 'c'), ('\u00c4', 'A', 't'), ('\u00c3', 'A', 'n'),
            ('\u00c5', 'A', 'r'),
            ('\u00c7', 'C', 'd'),
            ('\u00c8', 'E', 'g'), ('\u00c9', 'E', 'a'), ('\u00ca', 'E', 'c'), ('\u00cb', 'E', 't'),
            ('\u00cc', 'I', 'g'), ('\u00cd', 'I', 'a'), ('\u00ce', 'I', 'c'), ('\u00cf', 'I', 't'),
            ('\u00d1', 'N', 'n'),
            ('\u00d2', 'O', 'g'), ('\u00d3', 'O', 'a'), ('\u00d4', 'O', 'c'), ('\u00d6', 'O', 't'), ('\u00d5', 'O', 'n'),
            ('\u00d9', 'U', 'g'), ('\u00da', 'U', 'a'), ('\u00db', 'U', 'c'), ('\u00dc', 'U', 't'),
            ('\u00dd', 'Y', 'a'), ('\u0178', 'Y', 't'),
    ):
        accent(c, base, sorte)
    return m


_TABLE = _construire()
_FONCTIONS.clear()

jeu = "".join(_TABLE)
"""Tous les caractères dessinés (hors espace), dans l'ordre de la table."""

# Équivalents typographiques ramenés à un caractère dessiné (la table Kotlin associe aussi trois espaces à l'espace).
_ALIAS = {
    '\u2019': "'", '\u2018': "'", '\u00b4': "'", '`': "'", '\u201a': ',', '\u201c': '"', '\u201d': '"', '\u201e': '"',
    '\u2212': '-', '\u2010': '-', '\u2011': '-', '\t': ' ', ' ': ' ', '\u00ba': '\u00b0',
}


def _normaliser(c):
    if c == '\u00df' or c == '\u1e9e':  # ß, ẞ
        return "SS"
    c = _ALIAS.get(c, c)
    u = c.upper()
    # Char.uppercaseChar() : correspondance simple (un caractère) ; une majuscule en plusieurs lettres (ﬁ → FI)
    # n'existe pas en Kotlin, le caractère reste tel quel.
    return u if len(u) == 1 else c


def connait(c):
    """Caractère dessiné (après normalisation : minuscules → capitales, accents gardés si dessinés) ?
    L'espace est connu (non dessiné). Une chaîne de plusieurs caractères : tous connus."""
    return all(all(n == ' ' or n in _TABLE for n in _normaliser(k)) for k in c)


def absents_liste(texte):
    """Caractères du texte que la police ne sait pas dessiner, sans doublon, dans l'ordre d'apparition
    (ordre du LinkedHashSet Kotlin : ``" ".join(absents_liste(t))`` = ``absents(t).joinToString(" ")``)."""
    vus = {}
    for c in texte:
        if c != '\n' and c != '\r' and c not in vus and not connait(c):
            vus[c] = None
    return list(vus)


def absents(texte):
    """Caractères du texte que la police ne sait pas dessiner (affichés à l'utilisateur)."""
    return set(absents_liste(texte))


# ------------------------------------------------------------------------------------------------ mise en page

def traits(texte, hauteur_mm, interlettrage=0.0, alignement=1):
    """Traits (polylignes [(x, y), …] en mm, y vers le BAS, origine en haut à gauche de la ligne de texte) et largeur
    totale en mm. Plusieurs lignes séparées par '\\n', interligne 1,5 × hauteur ; ``alignement`` 0 gauche, 1 centre,
    2 droite. ``hauteur_mm`` = hauteur des capitales ; ``interlettrage`` (mm) s'ajoute à l'approche. Les caractères
    inconnus sont ignorés (voir ``absents``)."""
    return _disposer(texte, float(hauteur_mm), float(interlettrage), 0.0, alignement)


def _disposer(texte, h, espacement, interligne, alignement):
    if not math.isfinite(h) or h <= 0:
        raise Refus("La hauteur du texte doit être un nombre positif.")
    if not math.isfinite(espacement):
        raise Refus("Interlettrage invalide.")
    u = h / CAPITALE
    lignes = texte.replace("\r", "").split("\n")
    par_ligne = []
    largeurs = []
    dessine = False
    for k, ligne in enumerate(lignes):
        y0 = k * (INTERLIGNE * h + interligne)
        tr = []
        x = 0.0
        fin = 0.0
        for c in ligne:
            for n in _normaliser(c):
                if n == ' ':
                    fin = x + ESPACE * u
                    x = fin + APPROCHE * u + espacement
                    continue
                g = _TABLE.get(n)
                if g is None:
                    continue
                xmin = g.xmin
                for t in g.traits:
                    tr.append([(x + (px - xmin) * u, y0 + py * u) for px, py in t])
                dessine = True
                fin = x + (g.xmax - xmin) * u
                x = fin + APPROCHE * u + espacement
        par_ligne.append(tr)
        largeurs.append(fin if fin > 0.0 else 0.0)
    if not dessine:
        raise Refus("Aucun caractère dessinable : la police trace les capitales A–Z (minuscules converties), les "
                    "chiffres, les accents français et la ponctuation courante.")
    total = max(largeurs)
    a = min(max(int(alignement), 0), 2)
    tous = []
    for k, tr in enumerate(par_ligne):
        dx = (total - largeurs[k]) * a / 2.0
        if dx == 0.0:
            tous.extend(tr)
        else:
            tous.extend([[(px + dx, py) for px, py in t] for t in tr])
    return tous, total


# ------------------------------------------------------------------------------------------------ épaississement

def contours(texte, hauteur_mm, epaisseur_mm, interlettrage=0.0, alignement=1, pas=0.0):
    """Contours fermés (liste de Boucle, mm, y vers le bas, boîte ramenée en (0, 0)) du texte épaissi : union des
    traits épaissis de ``epaisseur_mm`` (bouts et angles arrondis), contours extérieurs et trous. ``pas`` = pas de la
    grille de calcul en mm (précision ≈ pas / 4 ; 0 = automatique). L'épaisseur s'ajoute à l'approche et à
    l'interligne : l'écart visible entre lettres et entre lignes ne dépend pas de l'épaisseur. Aire signée positive
    pour un contour extérieur, négative pour un trou."""
    g, c, p = _champ(texte, float(hauteur_mm), float(epaisseur_mm), float(interlettrage), alignement, float(pas))
    return _finir(_contours(c, g), p)


def pochoir(texte, hauteur_mm, epaisseur_mm, pont_mm=0.0, interlettrage=0.0, alignement=1, pas=0.0):
    """Variante pochoir de ``contours`` : chaque trou (centre d'un O, d'un A…) est relié au dehors par des ponts de
    ``pont_mm`` (0 = automatique, max(1 mm, 0,6 × épaisseur)) qui coupent le trait vers le haut et vers le bas.
    Découpées dans une plaque, les lettres laissent ainsi leurs îlots tenus par la plaque : plus aucun trou."""
    pont_mm = float(pont_mm)
    if math.isnan(pont_mm) or pont_mm < 0 or math.isinf(pont_mm):
        raise Refus("Largeur de pont invalide.")
    e = float(epaisseur_mm)
    g, c, p = _champ(texte, float(hauteur_mm), e, float(interlettrage), alignement, float(pas))
    pont = pont_mm if pont_mm > 0 else max(1.0, 0.6 * e)
    brutes = _contours(c, g)
    trous = [b for b in brutes if b.aire < 0]
    if not trous:
        return _finir(brutes, p)
    v = c.copy()
    for t in trous:
        _couper(g, c, v, t, pont)
    return _finir(_contours(v, g), p)


def _noeuds(b, r, p):
    return ((b[2] - b[0] + 2 * (r + 2 * p)) / p + 3) * ((b[3] - b[1] + 2 * (r + 2 * p)) / p + 3)


def _choisir_pas(b, r, e, h, pas):
    """Pas de la grille : ``pas`` imposé, sinon min(e/5, h/60) borné à [0,05 ; 0,5] puis ×1,25 tant que la grille
    dépasse NOEUDS_MAX nœuds (un pas imposé trop fin est refusé)."""
    p = pas if pas > 0 else min(max(min(e / 5, h / 60), 0.05), 0.5)
    if _noeuds(b, r, p) > NOEUDS_MAX:
        if pas > 0:
            raise Refus("Texte trop grand pour ce pas de calcul : augmente le pas ou raccourcis le texte.")
        while _noeuds(b, r, p) > NOEUDS_MAX:
            p *= 1.25
    return p


def _champ(texte, h, e, interlettrage, alignement, pas):
    """Grille, champ float32 v[j, i] (e/2 − distance au trait) et pas."""
    if not math.isfinite(e) or e <= 0:
        raise Refus("L'épaisseur du trait doit être un nombre positif.")
    if math.isnan(pas) or pas < 0 or math.isinf(pas):
        raise Refus("Pas de calcul invalide.")
    tr, _ = _disposer(texte, h, interlettrage + e, e, alignement)
    r = e / 2
    x0 = y0 = 1.7976931348623157e308
    x1 = y1 = -1.7976931348623157e308
    for t in tr:
        for qx, qy in t:
            x0 = min(x0, qx)
            y0 = min(y0, qy)
            x1 = max(x1, qx)
            y1 = max(y1, qy)
    bo = [x0, y0, x1, y1]
    p = _choisir_pas(bo, r, e, h, pas)
    g = Grille.sur(bo, p, r + 2 * p)
    return g, _remplir(g, tr, r), p


def _remplir(g, tr, r):
    """v = e/2 − distance au trait le plus proche (positive dans la matière), exacte jusqu'à e/2 + 2 pas, bornée
    au-delà. Mêmes seaux que le Kotlin : un nœud ne voit que les segments rangés dans son seau ; les nœuds dont le seau
    contient un segment forment un rectangle de la grille (seaux monotones en i et en j), parcouru d'un bloc."""
    ax, ay, bx, by = [], [], [], []
    for t in tr:
        if len(t) == 1:
            ax.append(t[0][0]); ay.append(t[0][1]); bx.append(t[0][0]); by.append(t[0][1])
        else:
            for i in range(len(t) - 1):
                ax.append(t[i][0]); ay.append(t[i][1]); bx.append(t[i + 1][0]); by.append(t[i + 1][1])
    ax, ay, bx, by = (np.array(a, dtype=np.float64) for a in (ax, ay, bx, by))
    pas = g.pas
    taille = r + 2 * pas
    sbx = int((g.nx - 1) * pas / taille) + 1
    sby = int((g.ny - 1) * pas / taille) + 1
    xs = g.xs()
    ys = g.ys()
    # Seau de chaque colonne / rangée de nœuds (sx, sy du Kotlin).
    cx = np.clip(((xs - g.x0) / taille).astype(np.int64), 0, sbx - 1)
    cy = np.clip(((ys - g.y0) / taille).astype(np.int64), 0, sby - 1)
    # Seaux de chaque segment : tous ceux à moins de [taille] de lui.
    i0 = np.clip(((np.minimum(ax, bx) - taille - g.x0) / taille).astype(np.int64), 0, sbx - 1)
    i1 = np.clip(((np.maximum(ax, bx) + taille - g.x0) / taille).astype(np.int64), 0, sbx - 1)
    j0 = np.clip(((np.minimum(ay, by) - taille - g.y0) / taille).astype(np.int64), 0, sby - 1)
    j1 = np.clip(((np.maximum(ay, by) + taille - g.y0) / taille).astype(np.int64), 0, sby - 1)
    ia = np.searchsorted(cx, i0, side="left").tolist()
    ib = np.searchsorted(cx, i1, side="right").tolist()
    ja = np.searchsorted(cy, j0, side="left").tolist()
    jb = np.searchsorted(cy, j1, side="right").tolist()
    dxs = bx - ax
    dys = by - ay
    l2s = dxs * dxs + dys * dys
    d2 = np.full((g.ny, g.nx), taille * taille)
    yc = ys[:, None]
    for s, (sax, say, sdx, sdy, sl2) in enumerate(zip(ax.tolist(), ay.tolist(), dxs.tolist(), dys.tolist(), l2s.tolist())):
        a, b, c, d = ia[s], ib[s], ja[s], jb[s]
        if a >= b or c >= d:
            continue
        X = xs[a:b]
        Y = yc[c:d]
        if sl2 == 0.0:
            ex = X - (sax + 0.0 * sdx)
            ey = Y - (say + 0.0 * sdy)
            dist = ex * ex + ey * ey
        else:
            u = ((X - sax) * sdx + (Y - say) * sdy) / sl2
            np.clip(u, 0.0, 1.0, out=u)
            ex = X - (sax + u * sdx)
            ey = Y - (say + u * sdy)
            dist = ex * ex + ey * ey
        bloc = d2[c:d, a:b]
        np.minimum(bloc, dist, out=bloc)
    return (r - np.sqrt(d2)).astype(_F32)


def _finir(brutes, p):
    """Boucles simplifiées (Douglas–Peucker ≈ pas / 4), ≥ 3 sommets, boîte ramenée en (0, 0)."""
    bonnes = []
    for b in brutes:
        s = simplifier_boucle(b, p / 4, p * 0.3)
        if s is not None and s.n >= 3 and abs(s.aire) > p * p * 0.25:
            bonnes.append(s)
    if not bonnes:
        raise Refus("Le texte épaissi ne donne aucun contour.")
    bo = boite(bonnes)
    return [b.translater(-bo[0], -bo[1]) for b in bonnes]


def _couper(g, c, v, trou, pont):
    """Pont du pochoir pour ``trou`` : bande verticale de largeur ``pont`` centrée sur le point le plus creux du trou,
    menée vers le haut et vers le bas jusqu'au premier vide qui n'est pas ce même trou (dehors ou trou voisin).
    ``c`` : champ d'origine (lu), ``v`` : champ modifié."""
    nx, ny, x0, y0, pas = g.nx, g.ny, g.x0, g.y0, g.pas
    bt = trou.boite()
    i0 = max(0, int((bt[0] - x0) / pas))
    i1 = min(nx - 1, int((bt[2] - x0) / pas) + 1)
    j0 = max(0, int((bt[1] - y0) / pas))
    j1 = min(ny - 1, int((bt[3] - y0) / pas) + 1)
    # Graine : point le plus creux de la colonne médiane du trou (pont centré, symétrique) ; à défaut (trou en
    # croissant), point le plus creux de tout le trou.
    gi = gj = -1
    creux = _FLOAT_MAX
    im = ((bt[0] + bt[2]) / 2 - x0) / pas
    k = int(im)
    candidats = [i for i in sorted((k, k + 1), key=lambda i: abs(i - im))[:1] if 0 <= i < nx]
    for i in candidats:
        col = c[j0:j1 + 1, i].tolist()
        xi = x0 + i * pas
        for j in range(j0, j1 + 1):
            f = col[j - j0]
            if f < creux and trou.contient(xi, y0 + j * pas):
                creux, gi, gj = f, i, j
    if gi < 0:
        bloc = c[j0:j1 + 1, i0:i1 + 1].tolist()
        for j in range(j0, j1 + 1):
            ligne = bloc[j - j0]
            for i in range(i0, i1 + 1):
                f = ligne[i - i0]
                if f < creux and trou.contient(x0 + i * pas, y0 + j * pas):
                    creux, gi, gj = f, i, j
    if gi < 0:
        return
    xc = x0 + gi * pas
    demi = pont / 2
    ia = max(0, int((xc - demi - 2 * pas - x0) / pas))
    ib = min(nx - 1, int((xc + demi + 2 * pas - x0) / pas) + 1)
    for i in range(ia, ib + 1):
        xi = x0 + i * pas
        gv = float(_F32(abs(xi - xc) - demi))
        col = c[:, i].tolist()
        vcol = v[:, i].tolist()
        for sens in (-1, 1):
            j = gj
            matiere = False
            while 0 <= j < ny:
                f = col[j]
                if f > 0.0:
                    matiere = True
                elif matiere:
                    if trou.contient(xi, y0 + j * pas):
                        matiere = False
                    else:
                        break
                if gv < vcol[j]:
                    vcol[j] = gv
                j += sens
        v[:, i] = vcol


# ------------------------------------------------------------------------------------------------ marching squares

def _tables():
    """(arête de départ, arête d'arrivée) par configuration des coins et signe du centre (MarchingSquares.kt)."""
    tables = {}
    for code in range(1, 15):
        pos = [bool(code >> k & 1) for k in range(4)]
        coupe = [pos[k] != pos[(k + 1) % 4] for k in range(4)]
        debut = [pos[k] and not pos[(k + 1) % 4] for k in range(4)]
        for centre in (True, False):
            if sum(debut) == 1:
                s = debut.index(True)
                segs = [(s, next(k for k in range(4) if coupe[k] and not debut[k]))]
            else:
                segs = [(s, (s + 1) % 4 if centre else (s + 3) % 4) for s in range(4) if debut[s]]
            tables[(code, centre)] = segs
    return tables


_TABLES = _tables()


def _contours(v, g):
    """Contours au niveau 0 de v[j, i] (matière > 0 à gauche), grille bordée de vide, exactement comme
    MarchingSquares.contours : mêmes identifiants d'arêtes (ordre des boucles), point d'une arête calculé dans le sens
    de la dernière cellule qui l'écrit (arête horizontale de (i, j) vers (i+1, j), verticale de (i, j+1) vers (i, j))."""
    ny, nx = v.shape
    W = nx + 2
    V = np.full((ny + 2, nx + 2), _F32(-1.0), dtype=_F32)
    V[1:-1, 1:-1] = np.where(v == 0, _F32(-1e-7), v)
    pos = V > 0
    code = (pos[:-1, :-1].astype(np.int8) | (pos[:-1, 1:].astype(np.int8) << 1)
            | (pos[1:, 1:].astype(np.int8) << 2) | (pos[1:, :-1].astype(np.int8) << 3))
    cj, ci = np.nonzero((code != 0) & (code != 15))
    if cj.size == 0:
        return []
    cs = code[cj, ci]
    centre = ((V[cj, ci] + V[cj, ci + 1] + V[cj + 1, ci + 1] + V[cj + 1, ci]) / _F32(4)) > 0
    base = cj.astype(np.int64) * W + ci
    ids = np.stack([2 * base, 2 * (base + 1) + 1, 2 * (base + W), 2 * base + 1], axis=1).ravel()
    src, dst = [], []
    for (cd, ctr), segs in _TABLES.items():
        sel = np.flatnonzero((cs == cd) & (centre == ctr))
        if sel.size == 0:
            continue
        for s, e in segs:
            src.append(sel * 4 + s)
            dst.append(sel * 4 + e)
    src_id = ids[np.concatenate(src)]
    dst_id = ids[np.concatenate(dst)]
    ordre = np.argsort(src_id, kind="stable")
    src_id, dst_id = src_id[ordre], dst_id[ordre]
    nxt = np.searchsorted(src_id, dst_id)
    nxt = np.where((nxt < src_id.size) & (src_id[np.minimum(nxt, src_id.size - 1)] == dst_id), nxt, -1)
    # Point de chaque arête coupée ; (pi, pj) = nœud (i + 1, j + 1) de la grille bordée.
    vert = (src_id & 1).astype(bool)
    b = src_id >> 1
    pj, pi = b // W, b % W
    va = np.where(vert, V[np.minimum(pj + 1, ny + 1), pi], V[pj, pi])
    vb = np.where(vert, V[pj, pi], V[pj, np.minimum(pi + 1, nx + 1)])
    t = (va / (va - vb)).astype(np.float64)
    xa = g.x0 + (pi - 1) * g.pas
    xb = np.where(vert, xa, g.x0 + pi * g.pas)
    ya = np.where(vert, g.y0 + pj * g.pas, g.y0 + (pj - 1) * g.pas)
    yb = np.where(vert, g.y0 + (pj - 1) * g.pas, ya)
    px = xa + t * (xb - xa)
    py = ya + t * (yb - ya)
    suivant = nxt.tolist()
    vu = bytearray(len(suivant))
    boucles = []
    for depart in range(len(suivant)):
        if vu[depart]:
            continue
        chemin = []
        i = depart
        while i >= 0 and not vu[i]:
            vu[i] = 1
            chemin.append(i)
            i = suivant[i]
        if len(chemin) >= 3:
            sel = np.array(chemin)
            boucles.append(Boucle(px[sel], py[sel]))
    return boucles
