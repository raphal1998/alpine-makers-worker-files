"""Import DXF ASCII d'Alpine Chutes (objet ``ImportDxf`` d'ImportDxf.kt).

LWPOLYLINE (renflements), POLYLINE/VERTEX R12, LINE, ARC, CIRCLE, ELLIPSE, SPLINE (NURBS par De Boor), INSERT de blocs
imbriqués (échelles, rotation, grilles), unités $INSUNITS, calques gelés ou éteints, espace papier, fins de ligne CRLF.
Les entités isolées (lignes, arcs, polylignes ouvertes) sont chaînées en boucles par leurs extrémités. Résultat en mm,
y vers le bas (y' = maxY − y), boîte ramenée en (0, 0) : ``Dessin`` ou ``Refus`` (message français).
"""
import math
from bisect import bisect_left

from .geometrie import Refus
from .import_commun import (BLANCS, DEG, INF, JOINT, MAX_POINTS, PI, PROFONDEUR_MAX, TROP_DE_POINTS, Affine,
                            Avertissements, Plume, SortieImport, TraceBrut, egal_ic, fd_atan, fd_atan2, fd_hypot, finir, hypot_seuil,
                            jdiv, jfloor_long, jfmod, jmax, to_double, to_int, utf16, vide)

VISITES_MAX = 2_000_000
TROP_COMPLEXE = "DXF trop complexe : trop d'entités à parcourir (blocs imbriqués en cascade ?)."

_UNITES = {0: 1.0, 4: 1.0, 1: 25.4, 2: 304.8, 3: 1_609_344.0, 5: 10.0, 6: 1000.0, 7: 1_000_000.0, 8: 2.54e-5, 9: 0.0254,
           10: 914.4, 11: 1e-7, 12: 1e-6, 13: 1e-3, 14: 100.0}


def lire(texte, tolerance=0.05):
    """Lit un DXF ASCII (texte) et rend un ``Dessin`` ; ``tolerance`` : écart d'aplatissement des courbes en mm."""
    try:
        return _Lecture(tolerance).executer(texte)
    except Refus:
        raise
    except RecursionError:
        raise Refus("Le fichier DXF est trop imbriqué pour être lu.") from None
    except MemoryError:
        raise
    except Exception:
        raise Refus("Le fichier DXF est abîmé : lecture impossible.") from None


class _Ent:
    """Entité : type (code 0) et plage de ses paires [debut, fin) ; premières valeurs par code, mises en cache."""
    __slots__ = ("type", "debut", "fin", "premiers", "cache")

    def __init__(self, type_, debut, fin):
        self.type = type_
        self.debut = debut
        self.fin = fin
        self.premiers = None
        self.cache = None


class _Bloc:
    __slots__ = ("bx", "by", "entites")

    def __init__(self, bx, by, entites):
        self.bx, self.by, self.entites = bx, by, entites


def _distance_segment(px, py, ax, ay, bx, by):
    """Contours.distanceSegment (Kotlin)."""
    dx = bx - ax
    dy = by - ay
    l2 = dx * dx + dy * dy
    if l2 == 0.0:
        return fd_hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / l2
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    return fd_hypot(px - (ax + t * dx), py - (ay + t * dy))


class _Lecture:
    def __init__(self, tolerance):
        self.avert = Avertissements()
        self.sortie = SortieImport(tolerance)
        self.plume = Plume(self.sortie)
        self.codes = []
        self.valeurs = []
        self.zeros = []
        self.blocs = {}
        self.calques_caches = set()
        self.unite = 1.0
        self.visites = 0
        self.ignorees_calque = 0
        self._dbl = {}
        self._purs = {}

    def executer(self, brut):
        texte = brut[1:] if brut.startswith("﻿") else brut
        if vide(texte):
            raise Refus("Le fichier est vide.")
        texte = utf16(texte)
        if texte.startswith("AutoCAD Binary DXF") or "\x00" in texte[:64]:
            raise Refus("DXF binaire non pris en charge : enregistre le fichier en DXF ASCII (texte) depuis ton logiciel de dessin.")
        self._paires(texte)
        sections = self._sections()
        if not sections:
            raise Refus("Ce fichier n'est pas un DXF ASCII lisible.")
        entites = []
        for nom, debut, fin in sections:
            if nom == "HEADER":
                self._entete(debut, fin)
            elif nom == "TABLES":
                self._calques(debut, fin)
            elif nom == "BLOCKS":
                self._lire_blocs(debut, fin)
            elif nom == "ENTITIES":
                entites = self._entites(debut, fin)
        self._dessiner_liste(entites, Affine.echelle(self.unite, self.unite), 0, None)
        self.plume.terminer()
        if self.ignorees_calque > 0:
            self.avert.ajouter(f"{self.ignorees_calque} entité(s) ignorée(s) sur des calques gelés ou éteints.")
        # Chaînage des tracés ouverts : 0,05 mm, puis jusqu'à 0,2 mm pour ce qui reste ouvert.
        fermes1, restes1 = _assembler(self.sortie.ouverts, JOINT)
        fermes2, restes2 = _assembler(restes1, 0.2)
        if fermes2:
            self.avert.ajouter(f"{len(fermes2)} contour(s) fermé(s) en joignant des extrémités distantes de 0,05 à 0,2 mm.")
        return finir(self.sortie.fermes + fermes1 + fermes2, len(restes2), self.avert, True)

    # ---------- lecture des paires

    def _paires(self, t):
        lignes = t.split("\n")
        if t.endswith("\n"):
            lignes.pop()
        lignes = [l.strip(BLANCS) for l in lignes]  # String.trim() de Kotlin
        nl = len(lignes)
        p = nl // 2
        brutes = lignes[0:2 * p:2]
        conv = {c: to_int(c) for c in set(brutes)}
        codes = [conv[c] for c in brutes]
        valeurs = lignes[1:2 * p:2]
        del lignes[0:2 * p]
        arret = None  # numéro de ligne d'un arrêt prématuré (code illisible ou valeur manquante)
        try:
            k = codes.index(None)
            del codes[k:]
            del valeurs[k:]
            arret = 2 * k + 2
        except ValueError:
            if nl % 2 == 1 and lignes[0] != "":
                arret = nl
        if 999 in codes:  # commentaires
            garde = [c != 999 for c in codes]
            codes = [c for c, g in zip(codes, garde) if g]
            valeurs = [v for v, g in zip(valeurs, garde) if g]
        if arret is not None and codes:
            self.avert.ajouter(f"Fichier DXF tronqué ou abîmé vers la ligne {arret} : lecture arrêtée là.")
        self.codes = codes
        self.valeurs = valeurs
        self.zeros = [k for k, c in enumerate(codes) if c == 0]

    def _sections(self):
        """Sections (nom, début, fin) : paires entre « 2 NOM » et « 0 ENDSEC »."""
        codes, val, zeros = self.codes, self.valeurs, self.zeros
        n = len(codes)
        nz = len(zeros)
        r = []
        zi = 0
        k = 0
        while True:
            zi = bisect_left(zeros, k, zi)
            if zi >= nz:
                break
            k = zeros[zi]
            if egal_ic(val[k], "SECTION") and k + 1 < n and codes[k + 1] == 2:
                nom = val[k + 1].upper()
                zj = bisect_left(zeros, k + 2, zi)
                while zj < nz and not (egal_ic(val[zeros[zj]], "ENDSEC") or egal_ic(val[zeros[zj]], "SECTION")):
                    zj += 1
                e = zeros[zj] if zj < nz else n
                r.append((nom, k + 2, e))
                k = e + 1 if (e < n and egal_ic(val[e], "ENDSEC")) else e
            else:
                k += 1
        return r

    def _entites(self, debut, fin):
        """Entités d'une plage : chaque code 0 ouvre une entité."""
        zeros, val = self.zeros, self.valeurs
        a = bisect_left(zeros, debut)
        b = bisect_left(zeros, fin)
        pos = zeros[a:b]
        return [_Ent(val[k].upper(), k, pos[q + 1] if q + 1 < len(pos) else fin) for q, k in enumerate(pos)]

    # ---------- valeurs

    def _premiers(self, e):
        p = e.premiers
        if p is None:
            d, f = e.debut + 1, e.fin
            p = e.premiers = dict(zip(reversed(self.codes[d:f]), reversed(self.valeurs[d:f])))
        return p

    def _double(self, s):
        """toDoubleOrNull()?.takeIf { it.isFinite() } (None sinon), mémorisé par texte."""
        cache = self._dbl
        v = cache.get(s, 0)
        if v == 0 and s not in cache:
            x = to_double(s)
            v = x if (x is not None and -INF < x < INF) else None
            if len(cache) < 200_000:
                cache[s] = v
        return v

    def _reel(self, e, code, defaut):
        s = (e.premiers if e.premiers is not None else self._premiers(e)).get(code)
        if s is None:
            return defaut
        v = self._double(s)
        return defaut if v is None else v

    def _entier(self, e, code, defaut):
        s = (e.premiers if e.premiers is not None else self._premiers(e)).get(code)
        if s is None:
            return defaut
        r = to_int(s)
        if r is not None:
            return r
        v = self._double(s)
        if v is not None and abs(v) < 1e9:
            return int(v)
        return defaut

    def _texte(self, e, code):
        return (e.premiers if e.premiers is not None else self._premiers(e)).get(code)

    def _reels(self, e, code):
        codes, val = self.codes, self.valeurs
        t = []
        for k in range(e.debut + 1, e.fin):
            if codes[k] == code:
                v = self._double(val[k])
                t.append(0.0 if v is None else v)
        return t

    def _points(self, e, cx, cy):
        """Points successifs : chaque code ``cx`` ouvre un point, ``cy`` fixe son y."""
        codes, val = self.codes, self.valeurs
        xs = []
        ys = []
        y_en_attente = False
        for k in range(e.debut + 1, e.fin):
            c = codes[k]
            if c == cx:
                v = self._double(val[k])
                xs.append(0.0 if v is None else v)
                ys.append(0.0)
                y_en_attente = True
            elif c == cy and y_en_attente:
                v = self._double(val[k])
                ys[-1] = 0.0 if v is None else v
                y_en_attente = False
        return xs, ys

    # ---------- sections

    def _entete(self, debut, fin):
        codes, val = self.codes, self.valeurs
        trouve = False
        for k in range(debut, fin):
            if codes[k] == 9 and val[k] == "$INSUNITS" and k + 1 < fin and codes[k + 1] == 70:
                trouve = True
                u = to_int(val[k + 1])
                if u is None:
                    u = 0
                if u in _UNITES:
                    self.unite = _UNITES[u]
                else:
                    self.avert.ajouter(f"Unité DXF inconnue ({u}) : millimètres supposés.")
                    self.unite = 1.0
                if u == 0:
                    self.avert.ajouter("Unités DXF non précisées : millimètres supposés.")
        if not trouve:
            self.unite = 1.0

    def _calques(self, debut, fin):
        """Table LAYER : calques gelés (70 bit 1) ou éteints (couleur 62 négative)."""
        for e in self._entites(debut, fin):
            if e.type == "LAYER":
                nom = self._texte(e, 2)
                if nom is None:
                    continue
                if (self._entier(e, 70, 0) & 1) != 0 or self._entier(e, 62, 7) < 0:
                    self.calques_caches.add(nom.upper())

    def _lire_blocs(self, debut, fin):
        liste = self._entites(debut, fin)
        i = 0
        n = len(liste)
        while i < n:
            e = liste[i]
            if e.type == "BLOCK":
                t = self._texte(e, 2)
                nom = t.upper() if t is not None else ""
                contenu = []
                i += 1
                while i < n and liste[i].type != "ENDBLK" and liste[i].type != "BLOCK":
                    contenu.append(liste[i])
                    i += 1
                if i < n and liste[i].type == "ENDBLK":
                    i += 1
                if nom and nom not in self.blocs:
                    self.blocs[nom] = _Bloc(self._reel(e, 10, 0.0), self._reel(e, 20, 0.0), contenu)
            else:
                i += 1

    # ---------- dessin des entités

    def _dessiner_liste(self, liste, m, prof, calque_parent):
        """Dessine une liste d'entités avec la matrice ``m`` (unités du fichier → mm)."""
        plume = self.plume
        reel = self._reel
        entier = self._entier
        caches = self.calques_caches
        i = 0
        n = len(liste)
        while i < n:
            e = liste[i]
            i += 1
            self.visites += 1
            if self.visites > VISITES_MAX:
                raise Refus(TROP_COMPLEXE)
            t = e.type
            # Sommets et attributs rattachés à l'entité précédente.
            suite = ()
            if t == "POLYLINE" or (t == "INSERT" and entier(e, 66, 0) == 1):
                d = i
                while i < n and (liste[i].type == "VERTEX" or liste[i].type == "ATTRIB"):
                    i += 1
                suite = liste[d:i]
                if i < n and liste[i].type == "SEQEND":
                    i += 1
            if prof == 0 and entier(e, 67, 0) == 1:
                continue  # espace papier
            c8 = self._texte(e, 8)
            calque = "0" if c8 is None else c8.upper()
            effectif = calque_parent if (calque == "0" and calque_parent is not None) else calque
            if effectif in caches:
                self.ignorees_calque += 1
                continue
            # Extrusion (0, 0, −1) : repère objet retourné, miroir en x.
            m_ocs = m.fois(Affine.echelle(-1.0, 1.0)) if reel(e, 230, 1.0) < 0 else m
            if t == "LINE":
                plume.m = m
                plume.deplacer(reel(e, 10, 0.0), reel(e, 20, 0.0))
                plume.ligne(reel(e, 11, 0.0), reel(e, 21, 0.0))
                plume.terminer()
            elif t == "LWPOLYLINE":
                sommets = []
                x = y = b = 0.0
                ouvert = False
                codes, val = self.codes, self.valeurs
                for k in range(e.debut + 1, e.fin):
                    c = codes[k]
                    if c == 10 or c == 20 or c == 42:
                        v = self._double(val[k])
                        if v is None:
                            v = 0.0
                        if c == 10:
                            if ouvert:
                                sommets.append((x, y, b))
                            x = v
                            y = 0.0
                            b = 0.0
                            ouvert = True
                        elif c == 20:
                            y = v
                        else:
                            b = v
                if ouvert:
                    sommets.append((x, y, b))
                self._polyligne(sommets, (entier(e, 70, 0) & 1) != 0, m_ocs)
            elif t == "POLYLINE":
                drapeaux = entier(e, 70, 0)
                if (drapeaux & (16 | 64)) != 0:
                    continue  # maillages 3D et polyfaces
                sommets = []
                for v in suite:
                    if v.type == "VERTEX":
                        f = entier(v, 70, 0)
                        if (f & 16) != 0 or (f & 128) != 0:
                            continue  # cadre de spline, sommet de polyface
                        sommets.append((reel(v, 10, 0.0), reel(v, 20, 0.0), reel(v, 42, 0.0)))
                self._polyligne(sommets, (drapeaux & 1) != 0, m if (drapeaux & 8) != 0 else m_ocs)
            elif t == "ARC":
                r = reel(e, 40, 0.0)
                if r > 0:
                    a0 = reel(e, 50, 0.0) * DEG
                    a1 = reel(e, 51, 360.0) * DEG
                    dt = jfmod(a1 - a0, 2 * PI)
                    if dt <= 1e-12:
                        dt += 2 * PI
                    plume.m = m_ocs
                    plume.arc_seul(reel(e, 10, 0.0), reel(e, 20, 0.0), r, r, 0.0, a0, dt)
            elif t == "CIRCLE":
                r = reel(e, 40, 0.0)
                if r > 0:
                    plume.m = m_ocs
                    plume.arc_seul(reel(e, 10, 0.0), reel(e, 20, 0.0), r, r, 0.0, 0.0, 2 * PI)
            elif t == "ELLIPSE":
                self._ellipse(e, m)
            elif t == "SPLINE":
                self._spline(e, m)
            elif t == "INSERT":
                self._inserer(e, m_ocs, prof, effectif)
            elif t == "TEXT" or t == "MTEXT":
                self.avert.ajouter("Textes ignorés : convertis-les en tracés (contours) dans ton logiciel de dessin.")
            # HATCH, DIMENSION, POINT, SOLID, VIEWPORT… : pas des contours de découpe
        plume.terminer()

    def _polyligne(self, s, fermee, m):
        """Polyligne à renflements (code 42 : tan(angle/4), positif = sens trigonométrique)."""
        if len(s) < 2:
            return
        plume = self.plume
        plume.m = m
        plume.deplacer(s[0][0], s[0][1])
        segment = self._segment
        for k in range(1, len(s)):
            segment(s[k - 1], s[k])
        if fermee:
            segment(s[-1], s[0])
            plume.fermer()
        else:
            plume.terminer()

    def _segment(self, a, b):
        ax, ay, bu = a
        bx, by = b[0], b[1]
        if abs(bu) < 1e-12 or (ax == bx and ay == by):
            self.plume.ligne(bx, by)
            return
        dx = bx - ax
        dy = by - ay
        # Centre : milieu de la corde + normale (−dy, dx) × (1 − b²) / (4 b).
        k = (1 - bu * bu) / (4 * bu)
        cx = (ax + bx) / 2 - dy * k
        cy = (ay + by) / 2 + dx * k
        r = fd_hypot(ax - cx, ay - cy)
        a0 = fd_atan2(ay - cy, ax - cx)
        self.plume.arc(cx, cy, r, r, 0.0, a0, 4 * fd_atan(bu), bx, by)

    def _ellipse(self, e, m):
        """ELLIPSE (repère général) : centre, extrémité du grand axe relative, rapport, paramètres en radians."""
        mx = self._reel(e, 11, 0.0)
        my = self._reel(e, 21, 0.0)
        rx = fd_hypot(mx, my)
        ry = rx * self._reel(e, 40, 1.0)
        if not (rx > 0 and ry > 0):
            return
        p0 = self._reel(e, 41, 0.0)
        p1 = self._reel(e, 42, 2 * PI)
        dt = jfmod(p1 - p0, 2 * PI)
        if dt <= 1e-12:
            dt += 2 * PI
        # Normale (0, 0, −1) : petit axe = N × grand axe retourné, le paramètre tourne dans l'autre sens.
        inverse = self._reel(e, 230, 1.0) < 0
        self.plume.m = m
        self.plume.arc_seul(self._reel(e, 10, 0.0), self._reel(e, 20, 0.0), rx, ry, fd_atan2(my, mx),
                            -p0 if inverse else p0, -dt if inverse else dt)

    def _spline(self, e, m):
        """SPLINE : B-spline rationnelle évaluée par De Boor ; à défaut, polyligne par les points d'ajustement."""
        deg = min(max(self._entier(e, 71, 3), 1), 11)
        noeuds = self._reels(e, 40)
        cx, cy = self._points(e, 10, 20)
        fx, fy = self._points(e, 11, 21)
        fermee = (self._entier(e, 70, 0) & 1) != 0
        nc = len(cx)
        if nc > MAX_POINTS:
            raise Refus(TROP_DE_POINTS)
        if nc >= deg + 1:
            u = noeuds
            croissants = (len(u) == nc + deg + 1 and all(u[i] >= u[i - 1] for i in range(1, len(u)))
                          and u[nc] > u[deg])
            if not croissants:
                self.avert.ajouter("Spline aux nœuds incohérents : vecteur de nœuds uniforme supposé.")
                u = [0.0 if i <= deg else (1.0 if i >= nc else (i - deg) / (nc - deg)) for i in range(nc + deg + 1)]
            poids = self._reels(e, 41)
            w = poids if (len(poids) == nc and all(p > 0 for p in poids)) else [1.0] * nc
            px = [m.x(cx[i], cy[i]) for i in range(nc)]
            py = [m.y(cx[i], cy[i]) for i in range(nc)]
            self._evaluer_spline(deg, u, px, py, w, fermee)
        elif len(fx) >= 2:
            plume = self.plume
            plume.m = m
            plume.deplacer(fx[0], fy[0])
            for k in range(1, len(fx)):
                plume.ligne(fx[k], fy[k])
            if fermee:
                plume.fermer()
            else:
                plume.terminer()

    def _evaluer_spline(self, p, u, px, py, w, fermee):
        nc = len(px)
        tol = self.sortie.tol
        hx = [0.0] * (p + 1)
        hy = [0.0] * (p + 1)
        hw = [0.0] * (p + 1)
        etapes = [(r, j) for r in range(1, p + 1) for j in range(p, r - 1, -1)]

        def point(k, t):
            base = k - p
            for j in range(p + 1):
                i = base + j
                wi = w[i]
                hw[j] = wi
                hx[j] = px[i] * wi
                hy[j] = py[i] * wi
            for r, j in etapes:
                i = base + j
                den = u[i + p - r + 1] - u[i]
                a = 0.0 if den == 0.0 else (t - u[i]) / den
                hx[j] = (1 - a) * hx[j - 1] + a * hx[j]
                hy[j] = (1 - a) * hy[j - 1] + a * hy[j]
                hw[j] = (1 - a) * hw[j - 1] + a * hw[j]
            return jdiv(hx[p], hw[p]), jdiv(hy[p], hw[p])

        plume = self.plume
        premier = True
        for k in range(p, nc):
            a = u[k]
            b = u[k + 1]
            if b <= a:
                continue
            # Subdivision uniforme de l'intervalle, doublée tant que le milieu de chaque corde s'écarte de la courbe.
            n = 2
            while True:
                xs = []
                ys = []
                for i in range(n + 1):
                    X, Y = point(k, a + (b - a) * i / n)
                    xs.append(X)
                    ys.append(Y)
                ecart = 0.0
                for i in range(n):
                    X, Y = point(k, a + (b - a) * (i + 0.5) / n)
                    ecart = jmax(ecart, _distance_segment(X, Y, xs[i], ys[i], xs[i + 1], ys[i + 1]))
                if not (ecart > tol * 0.9) or n >= 1024:
                    break
                n *= 2
            if premier:
                plume.depart_mm(xs[0], ys[0])
                premier = False
            for i in range(1, n + 1):
                plume.ligne_mm(xs[i], ys[i])
        if premier:
            return
        if fermee:
            plume.fermer()
        else:
            plume.terminer()

    def _inserer(self, e, m, prof, calque):
        """INSERT : bloc placé (10/20), échelles (41/42), rotation (50°), répétitions en grille (70/71, 44/45)."""
        nom = self._texte(e, 2)
        if nom is None:
            return
        bloc = self.blocs.get(nom.upper())
        if bloc is None:
            return
        if prof + 1 > PROFONDEUR_MAX:
            self.avert.ajouter("Blocs imbriqués trop profondément : une partie du dessin est ignorée.")
            return
        reel = self._reel
        base = m.fois(Affine.translation(reel(e, 10, 0.0), reel(e, 20, 0.0))).fois(Affine.rotation(reel(e, 50, 0.0) * DEG))
        echelle = Affine.echelle(reel(e, 41, 1.0), reel(e, 42, 1.0)).fois(Affine.translation(-bloc.bx, -bloc.by))
        colonnes = min(max(self._entier(e, 70, 1), 1), 100)
        rangees = min(max(self._entier(e, 71, 1), 1), 100)
        dc = reel(e, 44, 0.0)
        dr = reel(e, 45, 0.0)
        cle = (id(bloc), prof + 1, calque)
        for r in range(rangees):
            for c in range(colonnes):
                pur = self._purs.get(cle)
                if pur is not None:
                    # Sous-arbre déjà parcouru sans rien dessiner : même effet sans le refaire (visites et calques
                    # comptés, avertissements déjà présents, aucun point) — évite 2 000 000 de visites en Python.
                    self.visites += pur[0]
                    if self.visites > VISITES_MAX:
                        raise Refus(TROP_COMPLEXE)
                    self.ignorees_calque += pur[1]
                    continue
                points, visites, ignorees = self.sortie.points, self.visites, self.ignorees_calque
                self._dessiner_liste(bloc.entites, base.fois(Affine.translation(c * dc, r * dr)).fois(echelle), prof + 1, calque)
                if self.sortie.points == points:
                    self._purs[cle] = (self.visites - visites, self.ignorees_calque - ignorees)


def _assembler(traces, tol):
    """Chaînage des tracés ouverts par appariement des extrémités : grille de pas ``tol`` (9 cases voisines).

    Rend (boucles fermées, chaînes restées ouvertes)."""
    n = len(traces)
    if n == 0:
        return [], []
    masque = 0xFFFFFFFF
    ex = []
    ey = []
    for tr in traces:
        ex.append(tr.x[0])
        ey.append(tr.y[0])
        ex.append(tr.x[-1])
        ey.append(tr.y[-1])
    grille = {}
    for ident in range(2 * n):
        k = ((jfloor_long(ex[ident] / tol) & masque) << 32) | (jfloor_long(ey[ident] / tol) & masque)
        a = grille.get(k)
        if a is None:
            grille[k] = [ident]
        else:
            a.append(ident)
    pris = [False] * n
    hypot = math.hypot  # correctement arrondi : écart d'un ulp au plus avec fdlibm, recalcul exact près du seuil

    def joint(dx, dy):
        d = hypot_seuil(dx, dy, tol)
        return d is not None and d <= tol

    voisins = [(ddx, ddy) for ddx in (-1, 0, 1) for ddy in (-1, 0, 1)]

    def chercher(px, py):
        """Extrémité libre la plus proche de (px, py) à moins de tol, ou −1."""
        ix = jfloor_long(px / tol)
        iy = jfloor_long(py / tol)
        meilleur = -1
        dmin = tol
        for ddx, ddy in voisins:
            a = grille.get((((ix + ddx) & masque) << 32) | ((iy + ddy) & masque))
            if a is None:
                continue
            for ident in a:
                if pris[ident >> 1]:
                    continue
                dx = ex[ident] - px
                dy = ey[ident] - py
                if hypot(dx, dy) <= dmin * 1.0000000000000009 + 5e-324:
                    d = fd_hypot(dx, dy)
                    if d <= dmin:
                        dmin = d
                        meilleur = ident
        return meilleur

    fermes = []
    ouverts = []
    for s in range(n):
        if pris[s]:
            continue
        pris[s] = True
        t0 = traces[s]
        chx = list(t0.x)
        chy = list(t0.y)
        ferme = False
        for _passe in range(2):
            while True:
                if len(chx) >= 3 and joint(chx[-1] - chx[0], chy[-1] - chy[0]):
                    ferme = True
                    break
                ident = chercher(chx[-1], chy[-1])
                if ident < 0:
                    break
                pris[ident >> 1] = True
                tr = traces[ident >> 1]
                if ident & 1 == 0:
                    chx.extend(tr.x[1:])
                    chy.extend(tr.y[1:])
                else:
                    chx.extend(tr.x[-2::-1])
                    chy.extend(tr.y[-2::-1])
            if ferme:
                break
            chx.reverse()  # puis prolongement par l'autre bout
            chy.reverse()
        if ferme:
            if joint(chx[-1] - chx[0], chy[-1] - chy[0]):
                chx.pop()
                chy.pop()
            fermes.append(TraceBrut(chx, chy, True))
        else:
            ouverts.append(TraceBrut(chx, chy, False))
    return fermes, ouverts
