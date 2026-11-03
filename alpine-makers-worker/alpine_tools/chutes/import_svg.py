"""Import SVG d'Alpine Chutes (objet ``ImportSvg`` d'ImportSvg.kt) : tous les tracés fermés en mm, y vers le bas.

Unités : width/height de la racine (mm, cm, in, pt, pc, px, m) et viewBox ; sans viewBox 96 px = 25,4 mm. Lecteur XML
maison et tolérant (comme l'original : pas de xml.etree), styles, transformations, éléments de base, <use>/<defs>/
<symbol>, <svg> imbriqués. Résultat : ``Dessin`` (boucles, ouvertes, avertissements) ou ``Refus`` (message français).
"""
import math
import re
from collections import deque

from .geometrie import Refus
from .import_commun import (CLASSE_BLANCS, DEG, INF, NAN, PI, PROFONDEUR_MAX, Affine, Avertissements, Lecteur, Plume,
                            SortieImport, blanc, egal_ic, fd_atan2, fd_hypot, finir, jcos, jdiv, jfmod, jmax, jmin, jsin,
                            jsqrt, jtan,
                            maj_char, to_int, trim, utf16, vide)

VISITES_MAX = 500_000
PX_MM = 25.4 / 96
_PROFONDEUR_XML = 200
_ELEMENTS_MAX = 2_000_000

PAS_UN_SVG = "Ce fichier n'est pas un SVG : aucune balise <svg> trouvée."
AUCUNE_FORME = "Aucune forme fermée trouvée dans le fichier."
TROP_COMPLEXE = "SVG trop complexe : trop d'éléments à parcourir (références <use> en cascade ?)."


def lire(texte, tolerance=0.05):
    """Lit un SVG (texte) et rend un ``Dessin`` ; ``tolerance`` : écart d'aplatissement des courbes en mm."""
    try:
        return _Lecture(tolerance).executer(texte)
    except Refus:
        raise
    except RecursionError:
        raise Refus("Le fichier SVG est trop imbriqué pour être lu.") from None
    except MemoryError:
        raise
    except Exception:
        raise Refus("Le fichier SVG est abîmé : lecture impossible.") from None


# ------------------------------------------------------------------ XML minimal

class _Noeud:
    """Élément XML minimal (nom local sans préfixe, attributs décodés dans l'ordre de première apparition)."""
    __slots__ = ("nom", "attributs", "enfants")

    def __init__(self, nom, attributs):
        self.nom = nom
        self.attributs = attributs
        self.enfants = []


_RE_NOM_ELEMENT = re.compile(f"[^{CLASSE_BLANCS}>/]*")
_RE_BLANCS = re.compile(f"[{CLASSE_BLANCS}]*")
_RE_NOM_ATTR = re.compile(f"[^{CLASSE_BLANCS}=>/]*")
_RE_VALEUR_NUE = re.compile(f"[^{CLASSE_BLANCS}>]*")
_RE_DOCTYPE = re.compile(r"[\[\]>]")


def _local(nom):
    return nom.rsplit(":", 1)[-1]


def _fin_doctype(s, debut):
    """Fin d'un « <!DOCTYPE … [ sous-ensemble interne ] > » ou d'une déclaration « <!…> »."""
    crochets = 0
    for m in _RE_DOCTYPE.finditer(s, debut):
        c = m.group()
        if c == "[":
            crochets += 1
        elif c == "]":
            if crochets > 0:
                crochets -= 1
        elif crochets == 0:
            return m.end()
    return len(s)


def _code_point(cp):
    if cp is None or cp <= 0 or cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF:
        return None
    if cp < 0x10000:
        return chr(cp)
    cp -= 0x10000
    return chr(0xD800 + (cp >> 10)) + chr(0xDC00 + (cp & 0x3FF))  # Character.toChars : paire UTF-16


def _entites(v):
    """Décode &amp; &lt; &gt; &quot; &apos; et &#…; / &#x…; (les entités inconnues restent telles quelles)."""
    if "&" not in v:
        return v
    sb = []
    i = 0
    n = len(v)
    while i < n:
        a = v.find("&", i)
        if a < 0:
            sb.append(v[i:])
            break
        if a > i:
            sb.append(v[i:a])
        i = a
        e = v.find(";", i + 1)
        if e > i and e - i <= 12:
            nom = v[i + 1:e]
            if nom == "amp":
                r = "&"
            elif nom == "lt":
                r = "<"
            elif nom == "gt":
                r = ">"
            elif nom == "quot":
                r = "\""
            elif nom == "apos":
                r = "'"
            elif nom.startswith("#x") or nom.startswith("#X"):
                r = _code_point(to_int(nom[2:], 16))
            elif nom.startswith("#"):
                r = _code_point(to_int(nom[1:], 10))
            else:
                r = None
            if r is not None:
                sb.append(r)
                i = e + 1
                continue
        sb.append("&")
        i += 1
    return "".join(sb)


def _lire_xml(s, avert):
    """Analyse XML tolérante : balises, attributs entre guillemets simples, doubles ou sans guillemets, commentaires,
    CDATA, instructions et DOCTYPE ignorés, entités usuelles et numériques. Une balise non fermée est close à la fin du
    texte ; une fermeture orpheline est ignorée. Imbrication et nombre d'éléments bornés (le reste est sauté)."""
    n = len(s)
    pile = []
    racine = None
    sautes = 0
    elements = 0
    i = 0
    while i < n:
        lt = s.find("<", i)
        if lt < 0:
            break
        i = lt
        if s.startswith("<!--", i):
            e = s.find("-->", i + 4)
            i = n if e < 0 else e + 3
        elif s.startswith("<![CDATA[", i):
            e = s.find("]]>", i + 9)
            i = n if e < 0 else e + 3
        elif s.startswith("<?", i):
            e = s.find("?>", i + 2)
            i = n if e < 0 else e + 2
        elif s.startswith("<!", i):
            i = _fin_doctype(s, i + 2)
        elif s.startswith("</", i):
            j = s.find(">", i + 2)
            if j < 0:
                j = n
            nom = _local(trim(s[i + 2:j]))
            i = j + 1
            if sautes > 0:
                sautes -= 1
                continue
            k = len(pile) - 1
            while k >= 0 and pile[k].nom != nom:
                k -= 1
            if k >= 0:
                del pile[k:]
        else:
            j = _RE_NOM_ELEMENT.match(s, i + 1).end()
            nom_brut = s[i + 1:j]
            if not nom_brut or not (nom_brut[0].isalpha() or nom_brut[0] == "_"):
                i += 1
                continue
            attrs = {}
            auto_ferme = False
            while j < n:
                j = _RE_BLANCS.match(s, j).end()
                if j >= n:
                    break
                c = s[j]
                if c == ">":
                    j += 1
                    break
                if c == "/":
                    if j + 1 < n and s[j + 1] == ">":
                        auto_ferme = True
                        j += 2
                        break
                    j += 1
                    continue
                d = j
                j = _RE_NOM_ATTR.match(s, j).end()
                nom_attr = s[d:j]
                if not nom_attr:
                    j += 1
                    continue
                j = _RE_BLANCS.match(s, j).end()
                valeur = ""
                if j < n and s[j] == "=":
                    j = _RE_BLANCS.match(s, j + 1).end()
                    if j < n and (s[j] == "\"" or s[j] == "'"):
                        q = s[j]
                        e = s.find(q, j + 1)
                        valeur = s[j + 1:] if e < 0 else s[j + 1:e]
                        j = n if e < 0 else e + 1
                    else:
                        dv = j
                        j = _RE_VALEUR_NUE.match(s, j).end()
                        valeur = s[dv:j]
                if nom_attr not in attrs:
                    attrs[nom_attr] = _entites(valeur)
            i = j
            if sautes > 0 or len(pile) >= _PROFONDEUR_XML or elements >= _ELEMENTS_MAX or (racine is not None and not pile):
                if len(pile) >= _PROFONDEUR_XML or elements >= _ELEMENTS_MAX:
                    avert.ajouter("Imbrication ou nombre d'éléments excessif : une partie du fichier est ignorée.")
                if not auto_ferme:
                    sautes += 1
                continue
            elements += 1
            noeud = _Noeud(_local(nom_brut), attrs)
            if not pile:
                racine = noeud
            else:
                pile[-1].enfants.append(noeud)
            if not auto_ferme:
                pile.append(noeud)
    return racine


def _hash_java(s):
    h = 0
    for c in s:
        h = (31 * h + ord(c)) & 0xFFFFFFFF
    return h


def _premier_hashmap(cles, candidates):
    """Premier des ``candidates`` dans l'ordre d'itération d'une HashMap Java remplie avec ``cles`` (dans l'ordre)."""
    cap = 16
    while len(cles) > cap * 0.75:
        cap *= 2
    rang = {}
    for k, c in enumerate(cles):
        h = _hash_java(c)
        rang[c] = ((h ^ (h >> 16)) & (cap - 1), k)
    return min(candidates, key=rang.__getitem__)


# ------------------------------------------------------------------ rendu

class _Contexte:
    """Matrice courante, taille de la fenêtre (pour les %), visibilité héritée."""
    __slots__ = ("m", "vw", "vh", "visible")

    def __init__(self, m, vw, vh, visible):
        self.m, self.vw, self.vh, self.visible = m, vw, vh, visible


_UNITES_LONGUEUR = {"": 1.0, "px": 1.0, "mm": 96 / 25.4, "cm": 960 / 25.4, "in": 96.0, "pt": 4.0 / 3, "pc": 16.0,
                    "em": 16.0, "ex": 8.0}


def _longueur(v, ref, defaut=0.0):
    """Longueur d'attribut en unités utilisateur (mm, cm, in, pt, pc, px, em, ex, %)."""
    if v is None:
        return defaut
    t = trim(v)
    l = Lecteur(t)
    x = l.nombre()
    if x is None:
        return defaut
    u = trim(t[l.i:]).lower()
    if u == "%":
        k = ref / 100
    else:
        k = _UNITES_LONGUEUR.get(u, 1.0)
    r = x * k
    return r if -INF < r < INF else defaut


def _longueur_racine_mm(v):
    if v is None:
        return None
    t = trim(v)
    l = Lecteur(t)
    x = l.nombre()
    if x is None:
        return None
    unite = trim(t[l.i:]).lower()
    if unite in ("", "px"):
        mm = x * PX_MM
    elif unite == "mm":
        mm = x
    elif unite == "cm":
        mm = x * 10
    elif unite == "in":
        mm = x * 25.4
    elif unite == "pt":
        mm = x * 25.4 / 72
    elif unite == "pc":
        mm = x * 25.4 / 6
    elif unite == "m":
        mm = x * 1000
    else:
        return None  # %, em… : pas une taille physique
    return mm if (mm > 0 and mm < INF) else None


def _view_box(v):
    if v is None:
        return None
    l = Lecteur(v)
    a = []
    for _ in range(4):
        x = l.nombre()
        if x is None:
            return None
        a.append(x)
    return a if (a[2] > 0 and a[3] > 0) else None


def _propriete(n, nom):
    """Propriété de présentation : la déclaration du style l'emporte sur l'attribut."""
    a = n.attributs
    st = a.get("style")
    if st is not None:
        for decl in st.split(";"):
            k = decl.find(":")
            if k > 0 and egal_ic(trim(decl[:k]), nom):
                return trim(decl[k + 1:].replace("!important", "")).lower()
    v = a.get(nom)
    return None if v is None else trim(v).lower()


def _cache(n):
    return _propriete(n, "display") == "none"


def _transformation(t):
    """Attribut transform : matrix, translate, scale, rotate(a [cx cy]), skewX, skewY, composés de gauche à droite."""
    if t is None or vide(t):
        return Affine.IDENTITE
    m = Affine.IDENTITE
    i = 0
    n = len(t)
    while True:
        while i < n and (blanc(t[i]) or t[i] == ","):
            i += 1
        if i >= n:
            break
        d = i
        while i < n and t[i].isalpha():
            i += 1
        nom = t[d:i]
        while i < n and blanc(t[i]):
            i += 1
        if not nom or i >= n or t[i] != "(":
            return Affine.IDENTITE
        f = t.find(")", i)
        if f < 0:
            return Affine.IDENTITE
        l = Lecteur(t[i + 1:f])
        v = []
        while len(v) < 7:
            x = l.nombre()
            if x is None:
                break
            v.append(x)
        i = f + 1
        k = len(v)
        if nom == "matrix":
            if k != 6:
                return Affine.IDENTITE
            e = Affine(v[0], v[1], v[2], v[3], v[4], v[5])
        elif nom == "translate":
            if k == 1:
                e = Affine.translation(v[0], 0.0)
            elif k == 2:
                e = Affine.translation(v[0], v[1])
            else:
                return Affine.IDENTITE
        elif nom == "scale":
            if k == 1:
                e = Affine.echelle(v[0], v[0])
            elif k == 2:
                e = Affine.echelle(v[0], v[1])
            else:
                return Affine.IDENTITE
        elif nom == "rotate":
            if k == 1:
                e = Affine.rotation(v[0] * DEG)
            elif k == 3:
                e = Affine.translation(v[1], v[2]).fois(Affine.rotation(v[0] * DEG)).fois(Affine.translation(-v[1], -v[2]))
            else:
                return Affine.IDENTITE
        elif nom == "skewX":
            if k != 1:
                return Affine.IDENTITE
            e = Affine(1.0, 0.0, jtan(v[0] * DEG), 1.0, 0.0, 0.0)
        elif nom == "skewY":
            if k != 1:
                return Affine.IDENTITE
            e = Affine(1.0, jtan(v[0] * DEG), 0.0, 1.0, 0.0, 0.0)
        else:
            return Affine.IDENTITE
        m = m.fois(e)
    return m


class _Lecture:
    def __init__(self, tolerance):
        self.avert = Avertissements()
        self.sortie = SortieImport(tolerance)
        self.plume = Plume(self.sortie)
        self.ids = {}
        self.visites = 0

    def executer(self, brut):
        texte = brut[1:] if brut.startswith("﻿") else brut
        if vide(texte):
            raise Refus("Le fichier est vide.")
        texte = utf16(texte)
        doc = _lire_xml(texte, self.avert)
        if doc is None:
            raise Refus(PAS_UN_SVG)
        racine = self._premier_svg(doc)
        if racine is None:
            raise Refus(PAS_UN_SVG)
        self._indexer(racine)
        ctx = self._contexte_racine(racine)
        m = ctx.m.fois(_transformation(racine.attributs.get("transform")))
        c = _Contexte(m, ctx.vw, ctx.vh, True)
        if _cache(racine):
            raise Refus(AUCUNE_FORME)
        for e in racine.enfants:
            self._rendre(e, c, 1)
        self.plume.terminer()
        return finir(self.sortie.fermes, len(self.sortie.ouverts), self.avert, False)

    @staticmethod
    def _premier_svg(doc):
        """Premier élément <svg> en largeur (la racine en général ; un SVG inclus dans un autre document est accepté)."""
        file = deque([doc])
        vus = 0
        while file and vus < 100_000:
            n = file.popleft()
            vus += 1
            if n.nom == "svg":
                return n
            file.extend(n.enfants)
        return None

    def _indexer(self, racine):
        """Table des id (premier gagnant), parcours itératif."""
        ids = self.ids
        pile = [racine]
        while pile:
            n = pile.pop()
            i = n.attributs.get("id")
            if i is not None and i not in ids:
                ids[i] = n
            pile.extend(n.enfants)

    def _contexte_racine(self, r):
        """Échelle mm par unité utilisateur d'après width/height/viewBox de la racine."""
        a = r.attributs
        vb = _view_box(a.get("viewBox"))
        wmm = _longueur_racine_mm(a.get("width"))
        hmm = _longueur_racine_mm(a.get("height"))
        if vb is None:
            vw = (wmm if wmm is not None else 100 * PX_MM) / PX_MM
            vh = (hmm if hmm is not None else 100 * PX_MM) / PX_MM
            return _Contexte(Affine.echelle(PX_MM, PX_MM), vw, vh, True)
        min_x, min_y, w, h = vb
        vers = Affine.translation(-min_x, -min_y)
        if wmm is None and hmm is None:
            self.avert.ajouter("Unités absentes : 96 px par pouce supposés.")
            return _Contexte(Affine.echelle(PX_MM, PX_MM).fois(vers), w, h, True)
        sx = None if wmm is None else wmm / w
        sy = None if hmm is None else hmm / h
        par = trim(a.get("preserveAspectRatio") or "")
        if par.startswith("none") and sx is not None and sy is not None:
            m = Affine.echelle(sx, sy).fois(vers)
        else:
            s = jmin(sx if sx is not None else 1.7976931348623157e308, sy if sy is not None else 1.7976931348623157e308)
            ww = wmm if wmm is not None else w * s
            hh = hmm if hmm is not None else h * s
            m = Affine.translation((ww - w * s) / 2, (hh - h * s) / 2).fois(Affine.echelle(s, s)).fois(vers)
        return _Contexte(m, w, h, True)

    def _rendre(self, n, ctx, prof):
        self.visites += 1
        if self.visites > VISITES_MAX:
            raise Refus(TROP_COMPLEXE)
        if prof > PROFONDEUR_MAX:
            self.avert.ajouter("Groupes ou références imbriqués trop profondément : une partie du dessin est ignorée.")
            return
        if _cache(n):
            return
        vis = _propriete(n, "visibility")
        if vis == "hidden" or vis == "collapse":
            visible = False
        elif vis == "visible":
            visible = True
        else:
            visible = ctx.visible
        c = _Contexte(ctx.m.fois(_transformation(n.attributs.get("transform"))), ctx.vw, ctx.vh, visible)
        nom = n.nom
        if nom in ("g", "a", "switch"):
            for e in n.enfants:
                self._rendre(e, c, prof + 1)
        elif nom == "svg":
            self._rendre_svg_imbrique(n.attributs, n, c, prof, None, None)
        elif nom == "use":
            self._rendre_use(n, c, prof)
        elif nom in ("path", "rect", "circle", "ellipse", "line", "polyline", "polygon"):
            if visible:
                self._forme(n, c)
        elif nom in ("text", "tspan", "textPath"):
            self.avert.ajouter("Textes ignorés : convertis-les en tracés (objet en chemin) dans ton logiciel de dessin.")
        elif nom == "image":
            self.avert.ajouter("Images ignorées : seuls les tracés vectoriels sont importés.")
        # defs, symbol, clipPath, mask, marker, pattern, style, metadata… : pas de rendu direct

    def _rendre_svg_imbrique(self, a, enfants_de, ctx, prof, w_use, h_use):
        """<svg> imbriqué (ou référencé par <use>, ou <symbol>) : nouvelle fenêtre x, y, width, height + viewBox."""
        x = _longueur(a.get("x"), ctx.vw)
        y = _longueur(a.get("y"), ctx.vh)
        w = _longueur(w_use if w_use is not None else a.get("width"), ctx.vw, NAN)
        h = _longueur(h_use if h_use is not None else a.get("height"), ctx.vh, NAN)
        m = ctx.m.fois(Affine.translation(x, y))
        vw = w if -INF < w < INF else ctx.vw
        vh = h if -INF < h < INF else ctx.vh
        vb = _view_box(a.get("viewBox"))
        if vb is not None:
            # width/height absents : 100 % de la fenêtre parente (règle SVG).
            ww = w if (-INF < w < INF and w > 0) else ctx.vw
            hh = h if (-INF < h < INF and h > 0) else ctx.vh
            sx = ww / vb[2]
            sy = hh / vb[3]
            par = trim(a.get("preserveAspectRatio") or "")
            if par.startswith("none"):
                m = m.fois(Affine.echelle(sx, sy))
            else:
                s = jmin(sx, sy)
                m = m.fois(Affine.translation((ww - vb[2] * s) / 2, (hh - vb[3] * s) / 2)).fois(Affine.echelle(s, s))
            m = m.fois(Affine.translation(-vb[0], -vb[1]))
            vw = vb[2]
            vh = vb[3]
        c = _Contexte(m, vw, vh, ctx.visible)
        for e in enfants_de.enfants:
            self._rendre(e, c, prof + 1)

    def _rendre_use(self, n, ctx, prof):
        a = n.attributs
        ref = a.get("href")
        if ref is None:
            ref = a.get("xlink:href")
        if ref is None:
            candidates = [k for k in a if k.endswith(":href")]
            if candidates:
                ref = a[_premier_hashmap(list(a), candidates)]
        if ref is None:
            return
        t = trim(ref)
        if not t.startswith("#"):
            return
        cible = self.ids.get(t[1:])
        if cible is None or cible.nom == "clipPath" or cible.nom == "mask":
            return
        x = _longueur(a.get("x"), ctx.vw)
        y = _longueur(a.get("y"), ctx.vh)
        c = _Contexte(ctx.m.fois(Affine.translation(x, y)), ctx.vw, ctx.vh, ctx.visible)
        if cible.nom in ("symbol", "svg"):
            self.visites += 1
            if self.visites > VISITES_MAX:
                raise Refus(TROP_COMPLEXE)
            if prof + 1 > PROFONDEUR_MAX or _cache(cible):
                return
            m2 = c.m.fois(_transformation(cible.attributs.get("transform")))
            # Pour un symbole, x/y propres sont ignorés : seule la fenêtre width/height de <use> compte.
            if cible.nom == "symbol":
                attrs = {k: v for k, v in cible.attributs.items() if k != "x" and k != "y"}
            else:
                attrs = cible.attributs
            self._rendre_svg_imbrique(attrs, cible, _Contexte(m2, c.vw, c.vh, c.visible), prof + 1,
                                      a.get("width"), a.get("height"))
        else:
            self._rendre(cible, c, prof + 1)

    # ---------- formes

    def _forme(self, n, ctx):
        """Formes de base et tracés."""
        plume = self.plume
        plume.m = ctx.m
        a = n.attributs
        nom = n.nom
        if nom == "path":
            d = a.get("d")
            if d is None:
                return
            self._chemin(d)
        elif nom == "rect":
            x = _longueur(a.get("x"), ctx.vw)
            y = _longueur(a.get("y"), ctx.vh)
            w = _longueur(a.get("width"), ctx.vw)
            h = _longueur(a.get("height"), ctx.vh)
            if not (w > 0 and h > 0):
                return
            rx = _longueur(a.get("rx"), ctx.vw, NAN)
            ry = _longueur(a.get("ry"), ctx.vh, NAN)
            if not (-INF < rx < INF) or rx < 0:
                rx = NAN
            if not (-INF < ry < INF) or ry < 0:
                ry = NAN
            if rx != rx:
                rx = 0.0 if ry != ry else ry
            if ry != ry:
                ry = rx
            rx = jmin(rx, w / 2)
            ry = jmin(ry, h / 2)
            if rx <= 0 or ry <= 0:
                plume.deplacer(x, y)
                plume.ligne(x + w, y)
                plume.ligne(x + w, y + h)
                plume.ligne(x, y + h)
                plume.fermer()
            else:
                plume.deplacer(x + rx, y)
                plume.ligne(x + w - rx, y)
                plume.arc(x + w - rx, y + ry, rx, ry, 0.0, -PI / 2, PI / 2, x + w, y + ry)
                plume.ligne(x + w, y + h - ry)
                plume.arc(x + w - rx, y + h - ry, rx, ry, 0.0, 0.0, PI / 2, x + w - rx, y + h)
                plume.ligne(x + rx, y + h)
                plume.arc(x + rx, y + h - ry, rx, ry, 0.0, PI / 2, PI / 2, x, y + h - ry)
                plume.ligne(x, y + ry)
                plume.arc(x + rx, y + ry, rx, ry, 0.0, PI, PI / 2, x + rx, y)
                plume.fermer()
        elif nom == "circle":
            r = _longueur(a.get("r"), fd_hypot(ctx.vw, ctx.vh) / math.sqrt(2.0))
            if r > 0:
                plume.arc_seul(_longueur(a.get("cx"), ctx.vw), _longueur(a.get("cy"), ctx.vh), r, r, 0.0, 0.0, 2 * PI)
        elif nom == "ellipse":
            rx = _longueur(a.get("rx"), ctx.vw, NAN)
            ry = _longueur(a.get("ry"), ctx.vh, NAN)
            if rx != rx:
                rx = ry
            if ry != ry:
                ry = rx
            if rx > 0 and ry > 0:
                plume.arc_seul(_longueur(a.get("cx"), ctx.vw), _longueur(a.get("cy"), ctx.vh), rx, ry, 0.0, 0.0, 2 * PI)
        elif nom == "line":
            plume.deplacer(_longueur(a.get("x1"), ctx.vw), _longueur(a.get("y1"), ctx.vh))
            plume.ligne(_longueur(a.get("x2"), ctx.vw), _longueur(a.get("y2"), ctx.vh))
            plume.terminer()
        elif nom in ("polyline", "polygon"):
            pts = a.get("points")
            if pts is None:
                return
            l = Lecteur(pts)
            premier = True
            while True:
                px = l.nombre()
                if px is None:
                    break
                py = l.nombre()
                if py is None:
                    break
                if premier:
                    plume.deplacer(px, py)
                    premier = False
                else:
                    plume.ligne(px, py)
            if not premier:
                if nom == "polygon":
                    plume.fermer()
                else:
                    plume.terminer()
        plume.terminer()

    def _chemin(self, d):
        """Données de tracé « d » : toutes les commandes SVG 1.1, absolues et relatives."""
        plume = self.plume
        l = Lecteur(d)
        s = l.s
        nombre = l.nombre
        cmd = " "
        x = y = 0.0  # point courant
        sx = sy = 0.0  # départ du sous-tracé
        qx = qy = 0.0  # dernier point de contrôle (S, T)
        precedente = " "
        abime = False
        while not l.fin():
            c = s[l.i]
            if c.isalpha():
                cmd = c
                l.i += 1
            elif cmd == " " or cmd == "Z" or cmd == "z":
                abime = True
                break
            rel = cmd.islower()
            if rel:
                ox = x
                oy = y
            else:
                ox = oy = 0.0
            up = maj_char(cmd)
            if up == "M":
                a = nombre()
                b = nombre()
                if a is None or b is None:
                    abime = True
                    break
                x = ox + a
                y = oy + b
                sx = x
                sy = y
                plume.deplacer(x, y)
                cmd = "l" if rel else "L"
                precedente = "M"
                continue
            if up == "L":
                a = nombre()
                b = nombre()
                if a is None or b is None:
                    abime = True
                    break
                x = ox + a
                y = oy + b
                plume.ligne(x, y)
            elif up == "H":
                a = nombre()
                if a is None:
                    abime = True
                    break
                x = ox + a
                plume.ligne(x, y)
            elif up == "V":
                a = nombre()
                if a is None:
                    abime = True
                    break
                y = oy + a
                plume.ligne(x, y)
            elif up == "C":
                v = [nombre(), nombre(), nombre(), nombre(), nombre(), nombre()]
                if None in v:
                    abime = True
                    break
                x1 = ox + v[0]
                y1 = oy + v[1]
                x2 = ox + v[2]
                y2 = oy + v[3]
                x = ox + v[4]
                y = oy + v[5]
                plume.cubique(x1, y1, x2, y2, x, y)
                qx = x2
                qy = y2
            elif up == "S":
                v = [nombre(), nombre(), nombre(), nombre()]
                if None in v:
                    abime = True
                    break
                reflet = precedente == "C" or precedente == "S"
                x1 = 2 * x - qx if reflet else x
                y1 = 2 * y - qy if reflet else y
                x2 = ox + v[0]
                y2 = oy + v[1]
                x = ox + v[2]
                y = oy + v[3]
                plume.cubique(x1, y1, x2, y2, x, y)
                qx = x2
                qy = y2
            elif up == "Q":
                v = [nombre(), nombre(), nombre(), nombre()]
                if None in v:
                    abime = True
                    break
                x1 = ox + v[0]
                y1 = oy + v[1]
                x = ox + v[2]
                y = oy + v[3]
                plume.quadratique(x1, y1, x, y)
                qx = x1
                qy = y1
            elif up == "T":
                a = nombre()
                b = nombre()
                if a is None or b is None:
                    abime = True
                    break
                reflet = precedente == "Q" or precedente == "T"
                x1 = 2 * x - qx if reflet else x
                y1 = 2 * y - qy if reflet else y
                x = ox + a
                y = oy + b
                plume.quadratique(x1, y1, x, y)
                qx = x1
                qy = y1
            elif up == "A":
                rx = nombre()
                ry = nombre()
                phi = nombre()
                grand = l.drapeau()
                sens = l.drapeau()
                a = nombre()
                b = nombre()
                if rx is None or ry is None or phi is None or grand is None or sens is None or a is None or b is None:
                    abime = True
                    break
                x2 = ox + a
                y2 = oy + b
                self._arc_svg(x, y, rx, ry, phi, grand, sens, x2, y2)
                x = x2
                y = y2
            elif up == "Z":
                plume.fermer()
                x = sx
                y = sy
            else:
                abime = True
                break
            precedente = up
        plume.terminer()
        if abime:
            self.avert.ajouter("Tracé SVG partiellement illisible : lu jusqu'à la première donnée invalide.")

    def _arc_svg(self, x1, y1, rx_in, ry_in, phi_deg, grand, sens, x2, y2):
        """Arc SVG (paramétrage par extrémités) converti en centre + angles (SVG 1.1, F.6.5)."""
        plume = self.plume
        if x1 == x2 and y1 == y2:
            return
        rx = abs(rx_in)
        ry = abs(ry_in)
        if rx == 0.0 or ry == 0.0:
            plume.ligne(x2, y2)
            return
        phi = jfmod(phi_deg, 360.0) * DEG
        cp = jcos(phi)
        sp = jsin(phi)
        mx = (x1 - x2) / 2
        my = (y1 - y2) / 2
        x1p = cp * mx + sp * my
        y1p = -sp * mx + cp * my
        lam = jdiv(x1p * x1p, rx * rx) + jdiv(y1p * y1p, ry * ry)
        if lam > 1:
            k = math.sqrt(lam)
            rx *= k
            ry *= k
        num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
        den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
        coef = 0.0 if den == 0.0 else jsqrt(jmax(0.0, num / den))
        if grand == sens:
            coef = -coef
        cxp = jdiv(coef * rx * y1p, ry)
        cyp = jdiv(-coef * ry * x1p, rx)
        ccx = cp * cxp - sp * cyp + (x1 + x2) / 2
        ccy = sp * cxp + cp * cyp + (y1 + y2) / 2
        t1 = fd_atan2(jdiv(y1p - cyp, ry), jdiv(x1p - cxp, rx))
        t2 = fd_atan2(jdiv(-y1p - cyp, ry), jdiv(-x1p - cxp, rx))
        dt = t2 - t1
        if sens and dt < 0:
            dt += 2 * PI
        if not sens and dt > 0:
            dt -= 2 * PI
        plume.arc(ccx, ccy, rx, ry, phi, t1, dt, x2, y2)
