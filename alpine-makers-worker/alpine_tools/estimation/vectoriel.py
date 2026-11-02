"""Analyse d'un dessin vectoriel SVG ou DXF ASCII (port de Vectoriel.kt d'Alpine Prism), en millimètres.

Les courbes sont aplaties en segments (flèche ≤ TOL_COURBE), les tracés ouverts qui se touchent sont enchaînés,
puis on mesure encombrement, longueur de coupe et surface pleine (trous déduits). Bibliothèque standard seulement :
XML lu par expat sans jamais charger de ressource externe (DTD ou entité).
"""
import gzip
import math
import re
import zlib
from xml.parsers import expat

TOL_COURBE = 0.01     # mm : écart maximal corde / courbe
TOL_JONCTION = 0.01   # mm : deux extrémités plus proches sont jointes
MAX_POINTS = 2_000_000
MAX_OCTETS = 64 * 1024 * 1024
MAX_SEGMENTS = 4096   # par courbe
MAX_PROFONDEUR = 256  # groupes SVG imbriqués
MM_PAR_PX = 25.4 / 96.0
AUCUN_TRACE = "Aucun tracé lisible dans ce dessin"
SVG_NS = "http://www.w3.org/2000/svg"


class DessinException(ValueError):
    """Dessin refusé ou illisible (message en français)."""


def analyser(octets, nom):
    """{"format", "largeurMm", "hauteurMm", "longueurMm", "nbContoursFermes", "nbCheminsOuverts", "surfaceMm2",
    "avertissement"} ; lève DessinException avec un message lisible."""
    if not octets:
        raise DessinException(AUCUN_TRACE)
    if len(octets) > MAX_OCTETS:
        raise DessinException("Fichier trop volumineux (64 Mo au plus)")
    ext = nom.rsplit(".", 1)[-1].lower() if "." in nom else ""
    fmt = "svg" if ext in ("svg", "svgz") else "dxf" if ext == "dxf" else _deviner_format(octets)
    ctx = _Contexte()
    try:
        if fmt == "svg":
            _LecteurSvg(ctx).lire(octets)
        else:
            _LecteurDxf(ctx).lire(octets)
        return ctx.resultat(fmt)
    except DessinException:
        raise
    except RecursionError:
        raise DessinException("Dessin trop imbriqué pour être analysé") from None
    except Exception as e:  # noqa: BLE001 - message lisible comme l'application
        raise DessinException(f"Dessin illisible ({type(e).__name__})") from None


def _deviner_format(o):
    if len(o) >= 2 and o[0] == 0x1F and o[1] == 0x8B:
        return "svg"
    debut = o[:4096].decode("latin-1")
    if debut.startswith("AutoCAD Binary DXF"):
        return "dxf"
    net = debut[3:] if debut.startswith("ï»¿") else debut
    if net.lstrip().startswith("<"):
        return "svg"
    if "SECTION" in debut:
        return "dxf"
    raise DessinException("Format non reconnu : choisis un fichier .svg ou .dxf")


# ───────────────────────── Géométrie commune ─────────────────────────

def _dist(ax, ay, bx, by):
    return math.hypot(bx - ax, by - ay)


class _Trace:
    """Polyligne en mm."""
    __slots__ = ("ctx", "x", "y", "ferme", "plein")

    def __init__(self, ctx):
        self.ctx = ctx
        self.x = []
        self.y = []
        self.ferme = False
        self.plein = ctx.remplissage if ctx is not None else True

    @property
    def n(self):
        return len(self.x)

    def add(self, px, py):
        if not (math.isfinite(px) and math.isfinite(py)):
            raise DessinException("Coordonnée invalide (infinie ou non numérique) dans le dessin")
        self.x.append(px)
        self.y.append(py)
        if self.ctx is not None:
            self.ctx.compter()

    def longueur(self):
        x, y = self.x, self.y
        s = 0.0
        for i in range(1, len(x)):
            s += math.hypot(x[i] - x[i - 1], y[i] - y[i - 1])
        if self.ferme and len(x) > 1:
            s += math.hypot(x[0] - x[-1], y[0] - y[-1])
        return s


def _segments_arc(rayon_mm, balayage):
    """Nombre de segments d'un arc pour une flèche ≤ TOL_COURBE ; au moins 8 par tour."""
    a = abs(balayage)
    pas = 2 * math.acos(1 - TOL_COURBE / rayon_mm) if rayon_mm > 2 * TOL_COURBE else math.pi / 4
    return max(1, min(MAX_SEGMENTS, math.ceil(a / min(pas, math.pi / 4))))


def _cubique(t, x0, y0, x1, y1, x2, y2, x3, y3):
    m = max(math.hypot(x0 - 2 * x1 + x2, y0 - 2 * y1 + y2), math.hypot(x1 - 2 * x2 + x3, y1 - 2 * y2 + y3))
    n = max(1, min(MAX_SEGMENTS, math.ceil(math.sqrt(0.75 * m / TOL_COURBE))))
    for k in range(1, n + 1):
        u = k / n
        v = 1 - u
        a, b, c, d = v * v * v, 3 * v * v * u, 3 * v * u * u, u * u * u
        t.add(a * x0 + b * x1 + c * x2 + d * x3, a * y0 + b * y1 + c * y2 + d * y3)


def _quadratique(t, x0, y0, x1, y1, x2, y2):
    m = math.hypot(x0 - 2 * x1 + x2, y0 - 2 * y1 + y2)
    n = max(1, min(MAX_SEGMENTS, math.ceil(math.sqrt(m / (4 * TOL_COURBE)))))
    for k in range(1, n + 1):
        u = k / n
        v = 1 - u
        t.add(v * v * x0 + 2 * v * u * x1 + u * u * x2, v * v * y0 + 2 * v * u * y1 + u * u * y2)


class _Contexte:
    """Collecte des tracés, budget de points et avertissements d'une analyse."""

    def __init__(self):
        self.traces = []
        self.remplissage = True
        self.avertissements = []
        self.points = 0

    def nouveau(self):
        return _Trace(self)

    def avert(self, m):
        if m not in self.avertissements:
            self.avertissements.append(m)

    def compter(self):
        self.points += 1
        if self.points > MAX_POINTS:
            raise DessinException("Dessin trop détaillé : plus de 2 000 000 points après aplatissement des courbes")

    def terminer(self, t):
        if t.ferme:
            while t.n > 1 and _dist(t.x[-1], t.y[-1], t.x[0], t.y[0]) <= TOL_JONCTION:
                t.x.pop()
                t.y.pop()
        if t.n < 2 or t.longueur() <= 1e-9:
            return
        self.traces.append(t)

    def resultat(self, fmt):
        tous = [t for t in self.traces if t.ferme]
        tous.extend(_enchainer([t for t in self.traces if not t.ferme]))
        note = " ; ".join(self.avertissements)
        note = note[:1].upper() + note[1:]
        if not tous:
            raise DessinException(AUCUN_TRACE if not note else f"{AUCUN_TRACE} ({note})")
        x0 = min(min(t.x) for t in tous)
        x1 = max(max(t.x) for t in tous)
        y0 = min(min(t.y) for t in tous)
        y1 = max(max(t.y) for t in tous)
        longueur = sum(t.longueur() for t in tous)
        fermes = sum(1 for t in tous if t.ferme)
        return {
            "format": fmt, "largeurMm": x1 - x0, "hauteurMm": y1 - y0, "longueurMm": longueur,
            "nbContoursFermes": fermes, "nbCheminsOuverts": len(tous) - fermes,
            "surfaceMm2": _surface([t for t in tous if t.ferme and t.n >= 3 and t.plein]),
            "avertissement": note or None,
        }


def _enchainer(ouverts):
    """Enchaîne bout à bout les tracés ouverts ; une chaîne qui se referme devient un contour."""
    m = len(ouverts)
    if m == 0:
        return []
    tol = TOL_JONCTION

    def ex(i):
        t = ouverts[i >> 1]
        return t.x[0] if (i & 1) == 0 else t.x[-1]

    def ey(i):
        t = ouverts[i >> 1]
        return t.y[0] if (i & 1) == 0 else t.y[-1]

    grille = {}
    for i in range(2 * m):
        grille.setdefault((math.floor(ex(i) / tol), math.floor(ey(i) / tol)), []).append(i)
    pris = [False] * m

    def chercher(px, py):
        cx, cy = math.floor(px / tol), math.floor(py / tol)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in grille.get((cx + dx, cy + dy), ()):
                    if not pris[i >> 1] and _dist(ex(i), ey(i), px, py) <= tol:
                        return i
        return -1

    res = []
    for i in range(m):
        if pris[i]:
            continue
        pris[i] = True
        t0 = ouverts[i]
        dx0, dy0 = t0.x[0], t0.y[0]
        fx, fy = t0.x[-1], t0.y[-1]
        apres = []
        while _dist(fx, fy, dx0, dy0) > tol:
            ident = chercher(fx, fy)
            if ident < 0:
                break
            j = ident >> 1
            pris[j] = True
            inv = (ident & 1) == 1
            apres.append(j * 2 + (1 if inv else 0))
            u = ouverts[j]
            fx, fy = (u.x[0], u.y[0]) if inv else (u.x[-1], u.y[-1])
        avant = []
        sx, sy = dx0, dy0
        while _dist(fx, fy, sx, sy) > tol:
            ident = chercher(sx, sy)
            if ident < 0:
                break
            j = ident >> 1
            pris[j] = True
            inv = (ident & 1) == 0
            avant.append(j * 2 + (1 if inv else 0))
            u = ouverts[j]
            sx, sy = (u.x[-1], u.y[-1]) if inv else (u.x[0], u.y[0])
        if not avant and not apres:
            r = t0
        else:
            r = _Trace(None)
            r.plein = t0.plein
            for code in list(reversed(avant)) + [i * 2] + apres:
                u = ouverts[code >> 1]
                inv = (code & 1) == 1
                xs = u.x[::-1] if inv else u.x
                ys = u.y[::-1] if inv else u.y
                debut = 0 if r.n == 0 else 1
                r.x.extend(xs[debut:])
                r.y.extend(ys[debut:])
        if r.n >= 3 and _dist(r.x[-1], r.y[-1], r.x[0], r.y[0]) <= tol:
            r.x.pop()
            r.y.pop()
            r.ferme = True
        res.append(r)
    return res


def _dedans(t, px, py):
    """Point dans polygone (pair-impair)."""
    d = False
    x, y = t.x, t.y
    j = len(x) - 1
    for i in range(len(x)):
        if (y[i] > py) != (y[j] > py) and px < (x[j] - x[i]) * (py - y[i]) / (y[j] - y[i]) + x[i]:
            d = not d
        j = i
    return d


def _surface(c):
    """Surface pleine : lacet + imbrication (profondeur paire = plein, impaire = trou)."""
    k = len(c)
    if k == 0:
        return 0.0
    aire, boites = [], []
    for t in c:
        x, y = t.x, t.y
        s = 0.0
        n = len(x)
        for j in range(n):
            jn = 0 if j + 1 == n else j + 1
            s += x[j] * y[jn] - x[jn] * y[j]
        aire.append(abs(s) / 2)
        boites.append((min(x), min(y), max(x), max(y)))
    gx0 = min(b[0] for b in boites)
    gy0 = min(b[1] for b in boites)
    gx1 = max(b[2] for b in boites)
    gy1 = max(b[3] for b in boites)
    g = max(1, min(256, math.ceil(math.sqrt(k))))
    cw = max((gx1 - gx0) / g, 1e-9)
    ch = max((gy1 - gy0) / g, 1e-9)

    def cx(v):
        return max(0, min(g - 1, int((v - gx0) / cw)))

    def cy(v):
        return max(0, min(g - 1, int((v - gy0) / ch)))

    cases = {}
    for j, (bx0, by0, bx1, by1) in enumerate(boites):
        for ix in range(cx(bx0), cx(bx1) + 1):
            for iy in range(cy(by0), cy(by1) + 1):
                cases.setdefault(iy * g + ix, []).append(j)
    total = 0.0
    for i in range(k):
        px, py = c[i].x[0], c[i].y[0]
        liste = cases.get(cy(py) * g + cx(px))
        if not liste:
            continue
        prof = 0
        for j in liste:
            b = boites[j]
            if j != i and aire[j] > aire[i] and b[0] <= px <= b[2] and b[1] <= py <= b[3] and _dedans(c[j], px, py):
                prof += 1
        total += aire[i] if prof % 2 == 0 else -aire[i]
    return max(0.0, total)


# ───────────────────────── SVG ─────────────────────────

class _NombreInvalide(Exception):
    pass


class _Lexeur:
    """Nombres SVG : séparateurs espaces/virgules, signes collés, « 1.5.5 », drapeaux d'arc compacts."""

    def __init__(self, s):
        self.s = s
        self.i = 0

    def sauter(self):
        s = self.s
        while self.i < len(s) and (s[self.i].isspace() or s[self.i] == ","):
            self.i += 1

    def fin(self):
        self.sauter()
        return self.i >= len(self.s)

    def car(self):
        return self.s[self.i]

    def _chiffre(self, k):
        return k < len(self.s) and "0" <= self.s[k] <= "9"

    def nombre(self):
        self.sauter()
        s = self.s
        d = self.i
        if self.i < len(s) and s[self.i] in "+-":
            self.i += 1
        n = 0
        while self._chiffre(self.i):
            self.i += 1
            n += 1
        if self.i < len(s) and s[self.i] == ".":
            self.i += 1
            while self._chiffre(self.i):
                self.i += 1
                n += 1
        if n == 0:
            self.i = d
            raise _NombreInvalide()
        if self.i < len(s) and s[self.i] in "eE":
            j = self.i
            self.i += 1
            if self.i < len(s) and s[self.i] in "+-":
                self.i += 1
            if self._chiffre(self.i):
                while self._chiffre(self.i):
                    self.i += 1
            else:
                self.i = j
        return float(s[d:self.i])

    def drapeau(self):
        self.sauter()
        c = self.s[self.i] if self.i < len(self.s) else ""
        if c in ("0", "1"):
            self.i += 1
            return c == "1"
        raise _NombreInvalide()

    def nombres(self):
        r = []
        try:
            while not self.fin():
                r.append(self.nombre())
        except _NombreInvalide:
            pass
        return r


_ID = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mul(p, q):
    return (p[0] * q[0] + p[2] * q[1], p[1] * q[0] + p[3] * q[1],
            p[0] * q[2] + p[2] * q[3], p[1] * q[2] + p[3] * q[3],
            p[0] * q[4] + p[2] * q[5] + p[4], p[1] * q[4] + p[3] * q[5] + p[5])


def _tr(tx, ty):
    return (1.0, 0.0, 0.0, 1.0, tx, ty)


def _mx(m, x, y):
    return m[0] * x + m[2] * y + m[4]


def _my(m, x, y):
    return m[1] * x + m[3] * y + m[5]


def _echelle_max(m):
    q = m[0] * m[0] + m[1] * m[1] + m[2] * m[2] + m[3] * m[3]
    det = m[0] * m[3] - m[1] * m[2]
    return math.sqrt((q + math.sqrt(max(0.0, q * q - 4 * det * det))) / 2)


_RE_TRANSFORM = re.compile(r"([A-Za-z]+)\s*\(([^)]*)\)")
_RE_LONGUEUR = re.compile(r"\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*([A-Za-z%]*)\s*", re.ASCII)
_RE_DISPLAY_NONE = re.compile(r"(?:^|;)\s*display\s*:\s*none", re.IGNORECASE)
_RE_CACHE = re.compile(r"(?:^|;)\s*visibility\s*:\s*(?:hidden|collapse)", re.IGNORECASE)
_RE_FILL = re.compile(r"(?:^|;)\s*fill\s*:\s*([^;]+)", re.IGNORECASE)
_COMMANDES = "MmZzLlHhVvCcSsQqTtAa"


def _mm_par_unite(u):
    return {"": MM_PAR_PX, "px": MM_PAR_PX, "mm": 1.0, "cm": 10.0, "in": 25.4, "pt": 25.4 / 72, "pc": 25.4 / 6,
            "q": 0.25}.get(u.lower())


class _Element:
    __slots__ = ("ns", "nom", "attrs", "enfants")

    def __init__(self, ns, nom, attrs):
        self.ns = ns
        self.nom = nom
        self.attrs = attrs
        self.enfants = []

    def get(self, n):
        return self.attrs.get(n, "")

    def attr(self, n):
        v = self.attrs.get(n, "")
        return v if v.strip() else None


def _analyser_xml(octets):
    """Arbre XML minimal : DOCTYPE et entités internes tolérés, aucune ressource externe chargée."""
    p = expat.ParserCreate(namespace_separator=" ")
    p.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    p.ExternalEntityRefHandler = lambda *args: 1
    racine = []
    pile = []

    def nommer(brut):
        if " " in brut:
            ns, local = brut.split(" ", 1)
            return ns, local
        return None, brut

    def debut(nom, attrs):
        ns, local = nommer(nom)
        a = {}
        for k, v in attrs.items():
            kns, klocal = nommer(k)
            if kns is None:
                a[klocal] = v
        e = _Element(ns, local, a)
        if pile:
            pile[-1].enfants.append(e)
        else:
            racine.append(e)
        pile.append(e)

    def fin(nom):
        pile.pop()

    p.StartElementHandler = debut
    p.EndElementHandler = fin
    try:
        p.Parse(octets, True)
    except expat.ExpatError as e:
        ligne = f" (ligne {e.lineno})" if getattr(e, "lineno", 0) else ""
        raise DessinException("SVG illisible : fichier XML mal formé" + ligne) from None
    if not racine:
        raise DessinException(AUCUN_TRACE)
    return racine[0]


class _LecteurSvg:
    def __init__(self, ctx):
        self.ctx = ctx

    def lire(self, brut):
        octets = self._degonfler(brut) if len(brut) >= 2 and brut[0] == 0x1F and brut[1] == 0x8B else brut
        racine = _analyser_xml(octets)
        if racine.nom != "svg":
            raise DessinException(f"Ce fichier n'est pas un SVG (élément racine <{racine.nom}>)")
        m = self._matrice_racine(racine)
        t = racine.attr("transform")
        if t:
            m = _mul(m, self._transform(t))
        self._visiter(racine, m, 0, self._remplissage(racine, True))

    @staticmethod
    def _remplissage(e, parent):
        r = _RE_FILL.search(e.get("style"))
        valeur = r.group(1).strip() if r else (e.get("fill").strip() or None)
        if valeur is None:
            return parent
        return valeur.lower() not in ("none", "transparent")

    @staticmethod
    def _degonfler(o):
        try:
            z = zlib.decompressobj(16 + zlib.MAX_WBITS)
            sortie = z.decompress(o, MAX_OCTETS + 1)
            if len(sortie) > MAX_OCTETS or z.unconsumed_tail:
                raise DessinException("SVGZ trop volumineux une fois décompressé")
            return sortie
        except (zlib.error, gzip.BadGzipFile, EOFError):
            raise DessinException("SVGZ illisible : archive compressée endommagée") from None

    def _matrice_racine(self, r):
        vb = None
        if r.attr("viewBox"):
            n = _Lexeur(r.attr("viewBox")).nombres()
            if len(n) >= 4 and n[2] > 0 and n[3] > 0:
                vb = n
        w = self._longueur_mm(r.attr("width"))
        h = self._longueur_mm(r.attr("height"))
        if w is None and h is None:
            self.ctx.avert("dimensions supposées à 96 dpi")
        if vb is None:
            return (MM_PAR_PX, 0.0, 0.0, MM_PAR_PX, 0.0, 0.0)
        sx = w / vb[2] if w is not None else None
        sy = h / vb[3] if h is not None else None
        if sx is None and sy is None:
            sx = sy = MM_PAR_PX
        elif sx is None:
            sx = sy
        elif sy is None:
            sy = sx
        ex, ey = sx, sy
        if not (r.attr("preserveAspectRatio") or "").strip().startswith("none"):
            ex = ey = min(ex, ey)
        return (ex, 0.0, 0.0, ey, -vb[0] * ex, -vb[1] * ey)

    @staticmethod
    def _longueur_mm(s):
        if s is None:
            return None
        g = _RE_LONGUEUR.fullmatch(s)
        if not g:
            return None
        u = _mm_par_unite(g.group(2))
        if u is None:
            return None
        v = float(g.group(1)) * u
        return v if v > 0 else None

    @staticmethod
    def _lg(e, n):
        g = _RE_LONGUEUR.fullmatch(e.get(n))
        if not g:
            return 0.0
        v = float(g.group(1))
        u = _mm_par_unite(g.group(2))
        return v if u is None else v * u / MM_PAR_PX

    @staticmethod
    def _transform(s):
        m = _ID
        for r in _RE_TRANSFORM.finditer(s):
            a = _Lexeur(r.group(2)).nombres()
            if not a:
                continue
            nom = r.group(1)
            t = None
            if nom == "matrix":
                t = tuple(a[:6]) if len(a) >= 6 else None
            elif nom == "translate":
                t = _tr(a[0], a[1] if len(a) > 1 else 0.0)
            elif nom == "scale":
                t = (a[0], 0.0, 0.0, a[1] if len(a) > 1 else a[0], 0.0, 0.0)
            elif nom == "rotate":
                ang = a[0] * math.pi / 180
                c, sn = math.cos(ang), math.sin(ang)
                rot = (c, sn, -sn, c, 0.0, 0.0)
                t = _mul(_mul(_tr(a[1], a[2]), rot), _tr(-a[1], -a[2])) if len(a) >= 3 else rot
            elif nom == "skewX":
                t = (1.0, 0.0, math.tan(a[0] * math.pi / 180), 1.0, 0.0, 0.0)
            elif nom == "skewY":
                t = (1.0, math.tan(a[0] * math.pi / 180), 0.0, 1.0, 0.0, 0.0)
            if t is not None:
                m = _mul(m, t)
        return m

    @staticmethod
    def _masque(e):
        if e.get("display").strip() == "none":
            return True
        if e.get("visibility").strip() in ("hidden", "collapse"):
            return True
        st = e.get("style")
        return bool(st) and bool(_RE_DISPLAY_NONE.search(st) or _RE_CACHE.search(st))

    def _visiter(self, parent, m, prof, rempli):
        if prof > MAX_PROFONDEUR:
            self.ctx.avert("groupes trop imbriqués ignorés")
            return
        for e in parent.enfants:
            self._element(e, m, prof, rempli)

    def _element(self, e, parent, prof, rempli_parent):
        ctx = self.ctx
        if e.ns is not None and e.ns != SVG_NS:
            return
        nom = e.nom
        if self._masque(e):
            return
        if nom in ("text", "flowRoot"):
            ctx.avert("textes ignorés (convertis-les en tracés)")
            return
        if nom == "image":
            ctx.avert("images bitmap ignorées")
            return
        if nom == "use":
            ctx.avert("éléments <use> (clones) ignorés : convertis-les en tracés")
            return
        t = e.attr("transform")
        m = _mul(parent, self._transform(t)) if t else parent
        rempli = self._remplissage(e, rempli_parent)
        ctx.remplissage = rempli
        lg = self._lg
        if nom in ("g", "a", "switch"):
            self._visiter(e, m, prof + 1, rempli)
        elif nom == "svg":
            self._visiter(e, _mul(m, _tr(lg(e, "x"), lg(e, "y"))), prof + 1, rempli)
        elif nom == "path":
            d = e.attr("d")
            if d:
                self._chemin(d, m)
        elif nom == "rect":
            x, y, w, h = lg(e, "x"), lg(e, "y"), lg(e, "width"), lg(e, "height")
            if w > 0 and h > 0:
                tr = ctx.nouveau()
                for px, py in ((x, y), (x + w, y), (x + w, y + h), (x, y + h)):
                    tr.add(_mx(m, px, py), _my(m, px, py))
                tr.ferme = True
                ctx.terminer(tr)
        elif nom == "circle":
            r = lg(e, "r")
            self._ellipse(lg(e, "cx"), lg(e, "cy"), r, r, m)
        elif nom == "ellipse":
            self._ellipse(lg(e, "cx"), lg(e, "cy"), lg(e, "rx"), lg(e, "ry"), m)
        elif nom == "line":
            tr = ctx.nouveau()
            tr.add(_mx(m, lg(e, "x1"), lg(e, "y1")), _my(m, lg(e, "x1"), lg(e, "y1")))
            tr.add(_mx(m, lg(e, "x2"), lg(e, "y2")), _my(m, lg(e, "x2"), lg(e, "y2")))
            ctx.terminer(tr)
        elif nom in ("polyline", "polygon"):
            p = _Lexeur(e.get("points")).nombres()
            tr = ctx.nouveau()
            for k in range(len(p) // 2):
                tr.add(_mx(m, p[2 * k], p[2 * k + 1]), _my(m, p[2 * k], p[2 * k + 1]))
            tr.ferme = nom == "polygon"
            ctx.terminer(tr)

    def _ellipse(self, cx, cy, rx, ry, m):
        if rx <= 0 or ry <= 0:
            return
        n = _segments_arc(max(rx, ry) * _echelle_max(m), 2 * math.pi)
        t = self.ctx.nouveau()
        for k in range(n):
            a = 2 * math.pi * k / n
            px, py = cx + rx * math.cos(a), cy + ry * math.sin(a)
            t.add(_mx(m, px, py), _my(m, px, py))
        t.ferme = True
        self.ctx.terminer(t)

    def _chemin(self, d, m):
        """Données « d » d'un path ; en cas d'erreur, le tracé est gardé jusqu'à l'erreur (comme un navigateur)."""
        ctx = self.ctx
        lx = _Lexeur(d)
        etat = {"cmd": " ", "prec": " ", "cx": 0.0, "cy": 0.0, "sx": 0.0, "sy": 0.0, "kx": 0.0, "ky": 0.0, "t": None}

        def fin(ferme):
            c = etat["t"]
            if c is None:
                return
            c.ferme = ferme
            ctx.terminer(c)
            etat["t"] = None

        def courant():
            if etat["t"] is None:
                t = ctx.nouveau()
                t.add(_mx(m, etat["cx"], etat["cy"]), _my(m, etat["cx"], etat["cy"]))
                etat["t"] = t
            return etat["t"]

        def vers(x, y):
            courant().add(_mx(m, x, y), _my(m, x, y))
            etat["cx"], etat["cy"] = x, y

        def cub(x1, y1, x2, y2, x, y):
            cx, cy = etat["cx"], etat["cy"]
            _cubique(courant(), _mx(m, cx, cy), _my(m, cx, cy), _mx(m, x1, y1), _my(m, x1, y1),
                     _mx(m, x2, y2), _my(m, x2, y2), _mx(m, x, y), _my(m, x, y))
            etat.update(kx=x2, ky=y2, cx=x, cy=y)

        def quad(x1, y1, x, y):
            cx, cy = etat["cx"], etat["cy"]
            _quadratique(courant(), _mx(m, cx, cy), _my(m, cx, cy), _mx(m, x1, y1), _my(m, x1, y1), _mx(m, x, y), _my(m, x, y))
            etat.update(kx=x1, ky=y1, cx=x, cy=y)

        n = lx.nombre
        try:
            while not lx.fin():
                c = lx.car()
                if c.isalpha():
                    if c not in _COMMANDES:
                        raise _NombreInvalide()
                    etat["cmd"] = c
                    lx.i += 1
                elif etat["cmd"] in (" ", "Z", "z"):
                    raise _NombreInvalide()
                cmd = etat["cmd"]
                rel = cmd.islower()
                ox = etat["cx"] if rel else 0.0
                oy = etat["cy"] if rel else 0.0
                maj = cmd.upper()
                prec = etat["prec"]
                if maj == "M":
                    x = n() + ox
                    y = n() + oy
                    fin(False)
                    etat.update(cx=x, cy=y, sx=x, sy=y)
                    etat["cmd"] = "l" if rel else "L"
                elif maj == "Z":
                    if etat["t"] is not None:
                        fin(True)
                    etat["cx"], etat["cy"] = etat["sx"], etat["sy"]
                elif maj == "L":
                    x = n() + ox
                    vers(x, n() + oy)
                elif maj == "H":
                    vers(n() + ox, etat["cy"])
                elif maj == "V":
                    vers(etat["cx"], n() + oy)
                elif maj == "C":
                    a = [n() + (ox if k % 2 == 0 else oy) for k in range(6)]
                    cub(*a)
                elif maj == "S":
                    cx, cy = etat["cx"], etat["cy"]
                    x1 = 2 * cx - etat["kx"] if prec in ("C", "S") else cx
                    y1 = 2 * cy - etat["ky"] if prec in ("C", "S") else cy
                    a = [n() + (ox if k % 2 == 0 else oy) for k in range(4)]
                    cub(x1, y1, *a)
                elif maj == "Q":
                    a = [n() + (ox if k % 2 == 0 else oy) for k in range(4)]
                    quad(*a)
                elif maj == "T":
                    cx, cy = etat["cx"], etat["cy"]
                    x1 = 2 * cx - etat["kx"] if prec in ("Q", "T") else cx
                    y1 = 2 * cy - etat["ky"] if prec in ("Q", "T") else cy
                    a = [n() + (ox if k % 2 == 0 else oy) for k in range(2)]
                    quad(x1, y1, *a)
                elif maj == "A":
                    rx, ry, phi = n(), n(), n()
                    fa, fs = lx.drapeau(), lx.drapeau()
                    x = n() + ox
                    y = n() + oy
                    self._arc(courant(), m, etat["cx"], etat["cy"], rx, ry, phi, fa, fs, x, y)
                    etat["cx"], etat["cy"] = x, y
                etat["prec"] = maj
        except _NombreInvalide:
            ctx.avert("données de tracé SVG invalides (tracé lu jusqu'à l'erreur)")
        fin(False)

    @staticmethod
    def _arc(t, m, x1, y1, rx0, ry0, phi_deg, fa, fs, x2, y2):
        """Arc elliptique SVG (paramétrisation par extrémités → centre, spec SVG F.6.5)."""
        if x1 == x2 and y1 == y2:
            return
        rx, ry = abs(rx0), abs(ry0)
        if rx == 0.0 or ry == 0.0:
            t.add(_mx(m, x2, y2), _my(m, x2, y2))
            return
        phi = phi_deg * math.pi / 180
        cp, sp = math.cos(phi), math.sin(phi)
        dx2, dy2 = (x1 - x2) / 2, (y1 - y2) / 2
        x1p = cp * dx2 + sp * dy2
        y1p = -sp * dx2 + cp * dy2
        lam = x1p * x1p / (rx * rx) + y1p * y1p / (ry * ry)
        if lam > 1:
            s = math.sqrt(lam)
            rx *= s
            ry *= s
        num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
        den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
        coef = (-1 if fa == fs else 1) * math.sqrt(max(0.0, num / den))
        cxp = coef * rx * y1p / ry
        cyp = -coef * ry * x1p / rx
        ccx = cp * cxp - sp * cyp + (x1 + x2) / 2
        ccy = sp * cxp + cp * cyp + (y1 + y2) / 2
        ux, uy = (x1p - cxp) / rx, (y1p - cyp) / ry
        vx, vy = (-x1p - cxp) / rx, (-y1p - cyp) / ry
        th1 = math.atan2(uy, ux)
        dth = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        if not fs and dth > 0:
            dth -= 2 * math.pi
        elif fs and dth < 0:
            dth += 2 * math.pi
        n = _segments_arc(max(rx, ry) * _echelle_max(m), dth)
        for k in range(1, n):
            a = th1 + dth * k / n
            px = ccx + rx * math.cos(a) * cp - ry * math.sin(a) * sp
            py = ccy + rx * math.cos(a) * sp + ry * math.sin(a) * cp
            t.add(_mx(m, px, py), _my(m, px, py))
        t.add(_mx(m, x2, y2), _my(m, x2, y2))


# ───────────────────────── DXF ASCII ─────────────────────────

_RE_ENTIER = re.compile(r"[+-]?\d+", re.ASCII)


def _double(v):
    try:
        return float(v)
    except ValueError:
        return None


class _Entite:
    __slots__ = ("type", "codes", "valeurs")

    def __init__(self, type_):
        self.type = type_
        self.codes = []
        self.valeurs = []

    def ajouter(self, c, v):
        self.codes.append(c)
        self.valeurs.append(v)

    def d(self, c):
        for code, v in zip(self.codes, self.valeurs):
            if code == c:
                return _double(v)
        return None

    def e(self, c):
        v = self.d(c)
        if v is None:
            return None
        return 0 if math.isnan(v) else int(v) if math.isfinite(v) else 0

    def toutes(self, c):
        return [x for x in (_double(v) for code, v in zip(self.codes, self.valeurs) if code == c) if x is not None]


class _LecteurDxf:
    def __init__(self, ctx):
        self.ctx = ctx
        self.u = 1.0
        self.ignores = []

    def lire(self, o):
        binaire = o[:32].decode("latin-1").startswith("AutoCAD Binary DXF") or 0 in o[:2048]
        if binaire:
            raise DessinException("DXF binaire non pris en charge : enregistre-le en DXF ASCII")
        lignes = re.split(r"\r\n|\n|\r", self._decoder(o))
        section = None
        attente_nom = False
        variable = ""
        unites = None
        sections = 0
        ents = []
        cour = None
        i = 0
        while i + 1 < len(lignes):
            brut = lignes[i].strip()
            if not _RE_ENTIER.fullmatch(brut):
                raise DessinException(f"DXF illisible : code de groupe invalide ligne {i + 1}")
            code = int(brut)
            v = lignes[i + 1].strip()
            i += 2
            if code == 0:
                if cour is not None:
                    ents.append(cour)
                cour = None
                if v == "SECTION":
                    attente_nom = True
                    sections += 1
                elif v == "ENDSEC":
                    section = None
                elif v == "EOF":
                    break
                elif section == "ENTITIES":
                    cour = _Entite(v)
                continue
            if attente_nom:
                if code == 2:
                    section = v
                attente_nom = False
                continue
            if section == "HEADER":
                if code == 9:
                    variable = v
                elif code == 70 and variable == "$INSUNITS":
                    unites = int(v) if _RE_ENTIER.fullmatch(v) else None
            elif section == "ENTITIES" and cour is not None:
                cour.ajouter(code, v)
        if cour is not None:
            ents.append(cour)
        if sections == 0:
            raise DessinException("DXF illisible : aucune section trouvée")
        table = {1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0, 8: 25.4e-6, 9: 0.0254, 10: 914.4, 13: 1e-3, 14: 100.0}
        if unites in (None, 0):
            self.ctx.avert("unité non précisée, millimètres supposés")
            self.u = 1.0
        elif unites in table:
            self.u = table[unites]
        else:
            self.ctx.avert(f"unité DXF n° {unites} non gérée, millimètres supposés")
            self.u = 1.0
        k = 0
        while k < len(ents):
            e = ents[k]
            k += 1
            t = e.type
            if t == "LINE":
                self._ligne(e)
            elif t == "CIRCLE":
                self._cercle(e)
            elif t == "ARC":
                self._arc(e)
            elif t == "LWPOLYLINE":
                self._lwpolyligne(e)
            elif t == "POLYLINE":
                vs = []
                while k < len(ents) and ents[k].type == "VERTEX":
                    v = ents[k]
                    k += 1
                    if ((v.e(70) or 0) & 16) != 0:
                        continue
                    x, y = v.d(10), v.d(20)
                    if x is None or y is None:
                        continue
                    vs.append([x, y, v.d(42) or 0.0])
                if k < len(ents) and ents[k].type == "SEQEND":
                    k += 1
                f = e.e(70) or 0
                if (f & (16 | 64)) != 0:
                    self._ignorer("POLYLINE (maillage)")
                else:
                    self._polyligne(vs, (f & 1) != 0, self.u if (f & 8) != 0 else self._ex_ocs(e))
            elif t in ("VERTEX", "SEQEND"):
                pass
            elif t == "ELLIPSE":
                self._ellipse(e)
            elif t == "SPLINE":
                self._spline(e)
            elif t == "INSERT":
                self.ctx.avert("blocs (INSERT) ignorés : décompose-les avant l'export")
            elif t in ("TEXT", "MTEXT", "ATTRIB", "ATTDEF"):
                self.ctx.avert("textes ignorés (convertis-les en tracés)")
            else:
                self._ignorer(t)
        if self.ignores:
            self.ctx.avert("entités ignorées : " + ", ".join(self.ignores))

    def _ignorer(self, t):
        if t not in self.ignores:
            self.ignores.append(t)

    @staticmethod
    def _decoder(o):
        d = 3 if o[:3] == b"\xef\xbb\xbf" else 0
        try:
            return o[d:].decode("utf-8")
        except UnicodeDecodeError:
            return o[d:].decode("latin-1")

    def _ex_ocs(self, e):
        """Échelle X des entités en repère objet : extrusion (0,0,-1) = dessin en miroir."""
        z = e.d(230)
        return -self.u if (z if z is not None else 1.0) < 0 else self.u

    def _ligne(self, e):
        u = self.u
        x1, y1, x2, y2 = e.d(10), e.d(20), e.d(11), e.d(21)
        t = self.ctx.nouveau()
        if x1 is None or y1 is None:
            return
        t.add(x1 * u, y1 * u)
        if x2 is None or y2 is None:
            return
        t.add(x2 * u, y2 * u)
        self.ctx.terminer(t)

    def _cercle(self, e):
        cx, cy, r = e.d(10), e.d(20), e.d(40)
        if cx is None or cy is None or r is None or r <= 0:
            return
        ex, u = self._ex_ocs(e), self.u
        n = _segments_arc(r * u, 2 * math.pi)
        t = self.ctx.nouveau()
        for k in range(n):
            a = 2 * math.pi * k / n
            t.add((cx + r * math.cos(a)) * ex, (cy + r * math.sin(a)) * u)
        t.ferme = True
        self.ctx.terminer(t)

    def _arc(self, e):
        cx, cy, r = e.d(10), e.d(20), e.d(40)
        if cx is None or cy is None or r is None or r <= 0:
            return
        a0 = (e.d(50) or 0.0) * math.pi / 180
        a1 = e.d(51)
        bal = (a1 if a1 is not None else 360.0) * math.pi / 180 - a0
        while bal <= 1e-12:
            bal += 2 * math.pi
        while bal > 2 * math.pi + 1e-9:
            bal -= 2 * math.pi
        ex, u = self._ex_ocs(e), self.u
        n = _segments_arc(r * u, bal)
        t = self.ctx.nouveau()
        for k in range(n + 1):
            a = a0 + bal * k / n
            t.add((cx + r * math.cos(a)) * ex, (cy + r * math.sin(a)) * u)
        self.ctx.terminer(t)

    def _lwpolyligne(self, e):
        vs = []
        for code, brut in zip(e.codes, e.valeurs):
            v = _double(brut)
            if v is None:
                continue
            if code == 10:
                vs.append([v, 0.0, 0.0])
            elif code == 20 and vs:
                vs[-1][1] = v
            elif code == 42 and vs:
                vs[-1][2] = v
        self._polyligne(vs, ((e.e(70) or 0) & 1) != 0, self._ex_ocs(e))

    def _polyligne(self, vs, fermee, ex):
        """Sommets (x, y, bulge) ; bulge = tan(angle/4) du segment vers le sommet suivant."""
        if len(vs) < 2:
            return
        t = self.ctx.nouveau()
        t.add(vs[0][0] * ex, vs[0][1] * self.u)
        for i in range(len(vs) - 1):
            self._segment(t, vs[i], vs[i + 1], True, ex)
        if fermee:
            self._segment(t, vs[-1], vs[0], False, ex)
            t.ferme = True
        self.ctx.terminer(t)

    def _segment(self, t, a, b, fin_incluse, ex):
        u = self.u
        bulge = a[2]
        if abs(bulge) > 1e-9 and (a[0] != b[0] or a[1] != b[1]):
            th = 4 * math.atan(bulge)
            f = (1 - bulge * bulge) / (4 * bulge)
            cx = (a[0] + b[0]) / 2 - (b[1] - a[1]) * f
            cy = (a[1] + b[1]) / 2 + (b[0] - a[0]) * f
            r = _dist(a[0], a[1], cx, cy)
            a0 = math.atan2(a[1] - cy, a[0] - cx)
            n = _segments_arc(r * u, th)
            for k in range(1, n):
                ang = a0 + th * k / n
                t.add((cx + r * math.cos(ang)) * ex, (cy + r * math.sin(ang)) * u)
        if fin_incluse:
            t.add(b[0] * ex, b[1] * u)

    def _ellipse(self, e):
        cx, cy, mx, my = e.d(10), e.d(20), e.d(11), e.d(21)
        if cx is None or cy is None or mx is None or my is None:
            return
        u = self.u
        ratio = e.d(40)
        ratio = 1.0 if ratio is None else ratio
        p0 = e.d(41) or 0.0
        p1 = e.d(42)
        bal = (p1 if p1 is not None else 2 * math.pi) - p0
        while bal <= 1e-12:
            bal += 2 * math.pi
        while bal > 2 * math.pi + 1e-9:
            bal -= 2 * math.pi
        complet = abs(bal - 2 * math.pi) < 1e-6
        z = e.d(230)
        neg = (z if z is not None else 1.0) < 0
        nx = my * ratio if neg else -my * ratio
        ny = -mx * ratio if neg else mx * ratio
        n = _segments_arc(math.hypot(mx, my) * u, bal)
        t = self.ctx.nouveau()
        for k in range(n if complet else n + 1):
            p = p0 + bal * k / n
            t.add((cx + mx * math.cos(p) + nx * math.sin(p)) * u, (cy + my * math.cos(p) + ny * math.sin(p)) * u)
        t.ferme = complet
        self.ctx.terminer(t)

    def _spline(self, e):
        """NURBS évaluée par de Boor si les nœuds sont cohérents ; sinon polyligne des points de passage ou de contrôle."""
        u = self.u
        p = e.e(71)
        p = 3 if p is None else p
        noeuds, poids0 = e.toutes(40), e.toutes(41)
        px, py = e.toutes(10), e.toutes(20)
        nc = min(len(px), len(py))
        t = self.ctx.nouveau()
        if p >= 1 and nc >= p + 1 and len(noeuds) == nc + p + 1:
            w = poids0 if len(poids0) == nc else [1.0] * nc
            dx, dy, dw = [0.0] * (p + 1), [0.0] * (p + 1), [0.0] * (p + 1)
            premier = True
            for s in range(p, nc):
                t0, t1 = noeuds[s], noeuds[s + 1]
                if t1 - t0 <= 1e-12:
                    continue
                longueur = 0.0
                for j in range(s - p + 1, s + 1):
                    longueur += _dist(px[j - 1], py[j - 1], px[j], py[j])
                ns = max(4, min(512, math.ceil(math.sqrt(longueur * u / (8 * TOL_COURBE)))))
                for j in range(0 if premier else 1, ns + 1):
                    tt = t0 + (t1 - t0) * j / ns
                    for q in range(p + 1):
                        idx = s - p + q
                        dx[q], dy[q], dw[q] = px[idx] * w[idx], py[idx] * w[idx], w[idx]
                    for r in range(1, p + 1):
                        for q in range(p, r - 1, -1):
                            den = noeuds[q + 1 + s - r] - noeuds[q + s - p]
                            al = 0.0 if den == 0.0 else (tt - noeuds[q + s - p]) / den
                            dx[q] = (1 - al) * dx[q - 1] + al * dx[q]
                            dy[q] = (1 - al) * dy[q - 1] + al * dy[q]
                            dw[q] = (1 - al) * dw[q - 1] + al * dw[q]
                    if dw[p] != 0.0:
                        t.add(dx[p] / dw[p] * u, dy[p] / dw[p] * u)
                premier = False
        else:
            fx, fy = e.toutes(11), e.toutes(21)
            nf = min(len(fx), len(fy))
            if nf >= 2:
                for j in range(nf):
                    t.add(fx[j] * u, fy[j] * u)
            else:
                for j in range(nc):
                    t.add(px[j] * u, py[j] * u)
        t.ferme = ((e.e(70) or 0) & 1) != 0
        self.ctx.terminer(t)
