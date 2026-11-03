"""PLACEMENT AUTOMATIQUE dans un polygone irrégulier (port de Placement.kt d'Alpine Chutes, docs/PLACEMENT.md).

1. Grille de la chute au pas p : une cellule est LIBRE seulement si elle tient ENTIÈREMENT dans la matière érodée
   (centre dans la chute, à au moins `marge + kerf/2 − r + p√2/2` des bords et `R + p√2/2` des traits interdits).
2. Empreinte d'une pièce à un angle : cellules dont le centre est à au plus `r + p√2/2` de la pièce, r = (kerf +
   espacement) / 2. Deux empreintes disjointes ⇒ pièces séparées d'au moins kerf + espacement ; empreinte dans les
   cellules libres ⇒ pièce à au moins marge + kerf/2 des bords : garanties EXACTES.
3. Glouton « premier coin », essais (5 ordres × 4 coins), amélioration progressive (échanges aléatoires, graine 7).

Même résultat que l'application : mêmes calculs flottants pour les grilles, mêmes règles de choix, même générateur
pseudo-aléatoire (java.util.Random). Seule la recherche de la première position libre change de technique : au lieu
de tester les positions une à une avec des sauts (Etat.tester), chaque rangée de cellules est un entier Python dont les
bits sont les cellules disponibles ; les positions qui conviennent à toute une rangée de l'empreinte s'obtiennent par
érosion binaire et ET logique. C'est le même ensemble de positions (la première dans l'ordre du balayage est donc la
même), calculé ~50 fois plus vite qu'une boucle Python. L'application arrête l'amélioration au bout d'une durée ; ici,
pour un résultat reproductible, on borne le NOMBRE d'essais (``placer(max_essais=…)``) — même suite d'essais que
l'application qui disposerait d'assez de temps pour ce nombre d'essais.
"""
import math
import time

import numpy as np

from .geometrie import Refus, geo_boite, jround, normaliser, tourner

COINS = (("haut_gauche", True, True), ("haut_droite", True, False), ("bas_gauche", False, True), ("bas_droite", False, False))
MAX_EXEMPLAIRES = 2000
MAX_CELLULES = 9_000_000
MAX_CELLULES_PIECE = 6_000_000
LONG_MAX = (1 << 63) - 1


class ReglagesPlacement:
    def __init__(self, kerf_mm=0.15, espacement_mm=1.0, pas_mm=0.0, pas_angle_deg=15.0):
        self.kerf_mm, self.espacement_mm = float(kerf_mm), float(espacement_mm)
        self.pas_mm, self.pas_angle_deg = float(pas_mm), float(pas_angle_deg)


class Pose:
    """Pièce posée : forme tournée de ``angle`` degrés (sens horaire, y vers le bas), boîte ramenée en (0, 0), puis
    translatée de (x, y) mm dans le repère de la chute."""
    __slots__ = ("piece", "exemplaire", "angle", "x", "y")

    def __init__(self, piece, exemplaire, angle, x, y):
        self.piece, self.exemplaire, self.angle, self.x, self.y = piece, exemplaire, angle, x, y

    def __repr__(self):
        return f"Pose({self.piece}, {self.exemplaire}, {self.angle!r}, {self.x!r}, {self.y!r})"


class Plan:
    def __init__(self, poses, manques, utilisation, pas_mm, essais, coin, complet_apres):
        self.poses, self.manques, self.utilisation = poses, manques, utilisation
        self.pas_mm, self.essais, self.coin, self.complet_apres = pas_mm, essais, coin, complet_apres

    @property
    def non_placees(self):
        return sum(self.manques.values())


class JavaRandom:
    """java.util.Random (générateur congruentiel 48 bits) : mêmes tirages que l'application pour une graine donnée."""
    MULT, ADD, MASQUE = 0x5DEECE66D, 0xB, (1 << 48) - 1

    def __init__(self, graine):
        self.s = (graine ^ self.MULT) & self.MASQUE

    def _suivant(self, bits):
        self.s = (self.s * self.MULT + self.ADD) & self.MASQUE
        return self.s >> (48 - bits)

    def next_int(self, borne):
        r = self._suivant(31)
        m = borne - 1
        if borne & m == 0:
            return (borne * r) >> 31
        u = r
        while True:
            r = u % borne
            if u - r + m < (1 << 31):
                return r
            u = self._suivant(31)


# ------------------------------------------------------------------ rastérisation exacte (Raster)

def remplir(g, x0, y0, pas, boucles):
    """Cellules dont le centre est dans la forme (pair / impair) ; ``g`` : bool (ny, nx), modifié sur place."""
    ny, nx = g.shape
    if not boucles:
        return
    ax = np.concatenate([np.roll(b.x, 1) for b in boucles])
    ay = np.concatenate([np.roll(b.y, 1) for b in boucles])
    bx = np.concatenate([b.x for b in boucles])
    by = np.concatenate([b.y for b in boucles])
    ymin, ymax = np.minimum(ay, by), np.maximum(ay, by)
    j_lo = max(0, int(math.floor((float(ymin.min()) - y0) / pas - 0.5)) - 1)
    j_hi = min(ny - 1, int(math.ceil((float(ymax.max()) - y0) / pas)) + 1)
    for j in range(j_lo, j_hi + 1):
        py = y0 + (j + 0.5) * pas
        c = (ay <= py) != (by <= py)
        k = int(np.count_nonzero(c))
        if k < 2:
            continue
        a_x, a_y = ax[c], ay[c]
        xs = np.sort(a_x + (py - a_y) / (by[c] - a_y) * (bx[c] - a_x)).tolist()
        for q in range(0, k - 1, 2):
            ia = max(0, math.ceil((xs[q] - x0) / pas - 0.5))
            ib = min(nx - 1, math.floor((xs[q + 1] - x0) / pas - 0.5))
            if ia <= ib:
                g[j, ia:ib + 1] = True


def capsules(g, x0, y0, pas, x, y, ferme, rayon, valeur):
    """Marque (ou efface) les cellules dont le centre est à au plus ``rayon`` d'un segment de la polyligne."""
    ny, nx = g.shape
    x = [float(v) for v in x]
    y = [float(v) for v in y]
    n = len(x)
    if n == 0:
        return
    segments = 1 if n == 1 else (n if ferme else n - 1)
    r2 = rayon * rayon
    for s in range(segments):
        ax, ay = x[s], y[s]
        if n == 1:
            bx, by = ax, ay
        else:
            bx, by = x[(s + 1) % n], y[(s + 1) % n]
        i0 = max(0, math.floor((min(ax, bx) - rayon - x0) / pas - 0.5))
        i1 = min(nx - 1, math.ceil((max(ax, bx) + rayon - x0) / pas - 0.5))
        j0 = max(0, math.floor((min(ay, by) - rayon - y0) / pas - 0.5))
        j1 = min(ny - 1, math.ceil((max(ay, by) + rayon - y0) / pas - 0.5))
        if i0 > i1 or j0 > j1:
            continue
        px = x0 + (np.arange(i0, i1 + 1) + 0.5) * pas
        py = (y0 + (np.arange(j0, j1 + 1) + 0.5) * pas)[:, None]
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        if l2 == 0.0:
            ex = np.broadcast_to(px - ax, (j1 - j0 + 1, i1 - i0 + 1))
            ey = np.broadcast_to(py - ay, (j1 - j0 + 1, i1 - i0 + 1))
        else:
            t = np.clip(((px - ax) * dx + (py - ay) * dy) / l2, 0.0, 1.0)
            ex = px - (ax + t * dx)
            ey = py - (ay + t * dy)
        zone = g[j0:j1 + 1, i0:i1 + 1]
        zone[ex * ex + ey * ey <= r2] = valeur


def pas_auto(l, h):
    """0,5 mm jusqu'à ~ 300 × 300 mm, plus grossier au-delà (≈ 400 000 cellules), 2 mm au plus."""
    p = math.sqrt(l * h / 400_000.0)
    return min(max(0.5, math.ceil(p * 4) / 4), 2.0)


def _norm(a):
    v = math.fmod(a, 360.0)
    if v < 0:
        v += 360.0
    return 0.0 if v >= 359.9999 else v


def _entier_de(ligne):
    """Rangée booléenne → entier dont le bit i est la cellule i."""
    return int.from_bytes(np.packbits(ligne, bitorder="little").tobytes(), "little")


def _eroder(r, longueur):
    """Bit i ⇔ bits i … i + longueur − 1 tous à 1 (fenêtres doublées)."""
    e, k = r, 1
    while k < longueur and e:
        s = min(k, longueur - k)
        e &= e >> s
        k += s
    return e


class Empreinte:
    """Empreinte d'une pièce à un angle : rangées de segments [a, b] de cellules, origine décalée de (ou, ov)."""
    __slots__ = ("angle", "forme", "ou", "ov", "nu", "nv", "spans", "cellules", "largeur", "hauteur", "lignes", "cle")

    def __init__(self, angle, forme, ou, ov, nu, nv, spans, cellules):
        self.angle, self.forme, self.ou, self.ov, self.nu, self.nv = angle, forme, ou, ov, nu, nv
        self.spans, self.cellules = spans, cellules
        b = geo_boite(forme)
        self.largeur, self.hauteur = b[2], b[3]
        self.cle = (nu, nv, ou, ov, tuple(tuple(s) for s in spans))
        # Rangées non vides : (v, [(décalage du segment, longueur)], masque des cellules de la rangée).
        lignes = []
        for v, s in enumerate(spans):
            if not s:
                continue
            seg, masque = [], 0
            for k in range(0, len(s), 2):
                a, b2 = s[k], s[k + 1]
                seg.append((ou + a, b2 - a + 1))
                masque |= ((1 << (b2 - a + 1)) - 1) << a
            lignes.append((v, tuple(seg), masque))
        self.lignes = tuple(lignes)


class _Essai:
    __slots__ = ("poses", "manques", "aire", "compacite", "ordre", "coin")

    def __init__(self, poses, manques, aire, compacite, ordre, coin):
        self.poses, self.manques, self.aire, self.compacite, self.ordre, self.coin = poses, manques, aire, compacite, ordre, coin

    def meilleur_que(self, o):
        if o is None:
            return True
        if self.aire > o.aire + 1e-6:
            return True
        if self.aire < o.aire - 1e-6:
            return False
        if len(self.poses) != len(o.poses):
            return len(self.poses) > len(o.poses)
        return self.compacite < o.compacite - 1e-6


class Placement:
    def __init__(self, chute, pieces, reglages=None):
        self.chute = chute
        self.reglages = reglages or ReglagesPlacement()
        self.pieces = [p for p in pieces if p.quantite > 0 and p.boucles]
        rg = self.reglages
        self.r = (max(rg.kerf_mm, 0.0) + max(rg.espacement_mm, 0.0)) / 2
        if not chute.bords:
            raise Refus("Cette chute n'a pas de contour : mesure-la d'abord.")
        b = chute.boite
        l, h = b[2] - b[0], b[3] - b[1]
        self.pas = min(max(rg.pas_mm, 0.1), 2.0) if rg.pas_mm > 0 else pas_auto(l, h)
        pas = self.pas
        self.demi_diag = pas * math.sqrt(2.0) / 2
        self.gx0 = math.floor(b[0] / pas) * pas - pas
        self.gy0 = math.floor(b[1] / pas) * pas - pas
        self.nx = math.ceil((b[2] - self.gx0) / pas) + 2
        self.ny = math.ceil((b[3] - self.gy0) / pas) + 2
        if self.nx * self.ny > MAX_CELLULES:
            raise Refus("Chute trop grande pour ce pas de grille : augmente le pas.")
        self.aire_chute = chute.aire
        self.libre = self._grille_libre()
        self._lignes_libres = [_entier_de(self.libre[j]) for j in range(self.ny)]
        self._erosions_libres = [dict() for _ in range(self.ny)]
        self._cache = {}

    def _grille_libre(self):
        g = np.zeros((self.ny, self.nx), dtype=bool)
        boucles = self.chute.bords
        remplir(g, self.gx0, self.gy0, self.pas, boucles)
        e_bord = max(0.0, max(self.chute.marge_mm, 0.0) + max(self.reglages.kerf_mm, 0.0) / 2 - self.r) + self.demi_diag
        for b in boucles:
            capsules(g, self.gx0, self.gy0, self.pas, b.x, b.y, True, e_bord, False)
        for t in self.chute.interdits:
            if not t.points:
                continue
            capsules(g, self.gx0, self.gy0, self.pas, [p[0] for p in t.points], [p[1] for p in t.points], False,
                     t.rayon_mm + self.demi_diag, False)
        return g

    @property
    def aire_libre(self):
        """Aire utilisable (cellules libres) en mm²."""
        return int(np.count_nonzero(self.libre)) * self.pas * self.pas

    # ------------------------------------------------------------------ empreintes

    def angles(self, p):
        fil = self.chute.fil_deg
        if p.fil and fil is not None:
            return [_norm(fil), _norm(fil + 180)]
        if p.rotation == "aucune":
            return [0.0]
        if p.rotation == "quart":
            return [0.0, 90.0, 180.0, 270.0]
        pas_a = min(max(self.reglages.pas_angle_deg, 1.0), 90.0)
        n = min(max(jround(360.0 / pas_a), 4), 360)
        vus = []
        for k in range(n):
            a = _norm(k * 360.0 / n)
            if a not in vus:
                vus.append(a)
        return vus

    def empreintes(self, p):
        """Empreintes distinctes d'une pièce (angles donnant la même empreinte, par symétrie, regroupés)."""
        liste = self._cache.get(p.id)
        if liste is None:
            liste = []
            for a in self.angles(p):
                e = self.empreinte(p.boucles, a)
                if all(o.cle != e.cle for o in liste):
                    liste.append(e)
            self._cache[p.id] = liste
        return liste

    def empreinte(self, forme, angle):
        f = normaliser(tourner(forme, angle))
        bo = geo_boite(f)
        pas = self.pas
        rr = self.r + self.demi_diag
        bord = math.ceil(rr / pas) + 1
        nu = math.ceil(bo[2] / pas) + 2 * bord + 1
        nv = math.ceil(bo[3] / pas) + 2 * bord + 1
        if nu * nv > MAX_CELLULES_PIECE:
            raise Refus("Pièce trop grande pour ce pas de grille.")
        ox = oy = -bord * pas
        g = np.zeros((nv, nu), dtype=bool)
        remplir(g, ox, oy, pas, f)
        for b in f:
            capsules(g, ox, oy, pas, b.x, b.y, True, rr, True)
        bords = np.diff(np.pad(g.astype(np.int8), ((0, 0), (1, 1))), axis=1)
        spans = []
        for v in range(nv):
            debuts = np.flatnonzero(bords[v] == 1)
            fins = np.flatnonzero(bords[v] == -1) - 1
            s = []
            for a, b2 in zip(debuts.tolist(), fins.tolist()):
                s += [a, b2]
            spans.append(s)
        return Empreinte(angle, f, -bord, -bord, nu, nv, spans, int(np.count_nonzero(g)))

    # ------------------------------------------------------------------ recherche de la première position

    def _rang(self, coin, i, j):
        t = j if coin[1] else self.ny - 1 - j
        u = i if coin[2] else self.nx - 1 - i
        return t * self.nx + u

    def _premiere(self, etat, e, coin, limite, depart):
        """Première position (balayage depuis ``coin``) de rang ≥ ``depart`` ; None si aucune avant la rangée qui
        dépasse ``limite`` (comme Placement.premiere)."""
        nx, ny = self.nx, self.ny
        lignes, erosions = etat
        plein = (1 << nx) - 1
        haut, gauche = coin[1], coin[2]
        t0, u0 = divmod(depart, nx)
        ov = e.ov
        rangees = e.lignes
        for t in range(t0, ny):
            if t * nx > limite:
                return None
            j = t if haut else ny - 1 - t
            acc = plein
            if t == t0 and u0:
                acc = plein ^ ((1 << u0) - 1) if gauche else (1 << (nx - u0)) - 1
            for v, segs, _m in rangees:
                jj = j + ov + v
                if jj < 0 or jj >= ny:
                    acc = 0
                    break
                cache = erosions[jj]
                for s, lg in segs:
                    er = cache.get(lg)
                    if er is None:
                        er = _eroder(lignes[jj], lg)
                        cache[lg] = er
                    acc &= (er >> s) if s >= 0 else (er << -s)
                    if not acc:
                        break
                if not acc:
                    break
            if acc:
                acc &= plein
                if acc:
                    i = (acc & -acc).bit_length() - 1 if gauche else acc.bit_length() - 1
                    return i, j
        return None

    def _poser(self, etat, e, i, j):
        lignes, erosions = etat
        decal = i + e.ou
        for v, _segs, masque in e.lignes:
            jj = j + e.ov + v
            m = masque << decal if decal >= 0 else masque >> -decal
            lignes[jj] &= ~m
            erosions[jj] = {}

    def vers_mm(self, i, j):
        return self.gx0 + i * self.pas, self.gy0 + j * self.pas

    # ------------------------------------------------------------------ essais

    def essai(self, ordre, coin):
        """Un essai glouton : exemplaires dans l'ordre de ``ordre`` (ids de pièces), balayage depuis ``coin``."""
        etat = (list(self._lignes_libres), list(self._erosions_libres))
        par_id = {p.id: p for p in self.pieces}
        poses, manques = [], {}
        aire = 0.0
        bx0 = by0 = math.inf
        bx1 = by1 = -math.inf
        compteur = {}
        reprise = {}
        for pid in ordre:
            p = par_id.get(pid)
            if p is None:
                continue
            k = compteur.get(pid, 0)
            compteur[pid] = k + 1
            meilleur, meilleur_rang = None, LONG_MAX
            for e in self.empreintes(p):
                depart = reprise.get(id(e), 0)
                if depart == LONG_MAX or depart > meilleur_rang:
                    continue
                pos = self._premiere(etat, e, coin, meilleur_rang, depart)
                if pos is None:
                    if meilleur_rang == LONG_MAX:
                        reprise[id(e)] = LONG_MAX
                    continue
                rg = self._rang(coin, pos[0], pos[1])
                reprise[id(e)] = rg
                if rg < meilleur_rang or (rg == meilleur_rang and meilleur is not None and e.hauteur < meilleur[0].hauteur):
                    meilleur_rang, meilleur = rg, (e, pos[0], pos[1])
            if meilleur is None:
                manques[pid] = manques.get(pid, 0) + 1
                continue
            e, i, j = meilleur
            self._poser(etat, e, i, j)
            x, y = self.vers_mm(i, j)
            poses.append(Pose(pid, k, e.angle, x, y))
            aire += p.aire
            bx0, by0 = min(bx0, x), min(by0, y)
            bx1, by1 = max(bx1, x + e.largeur), max(by1, y + e.hauteur)
        compacite = 0.0 if not poses else (bx1 - bx0) * (by1 - by0)
        return _Essai(poses, manques, aire, compacite, list(ordre), coin)

    def _ordre(self, cle):
        tries = sorted(self.pieces, key=lambda p: (-cle(p), p.ordre))
        return [p.id for p in tries for _ in range(p.quantite)]

    def ordres(self):
        cles = (lambda p: p.aire, lambda p: max(p.largeur, p.hauteur), lambda p: p.hauteur, lambda p: p.largeur,
                lambda p: p.largeur + p.hauteur)
        vus = []
        for c in cles:
            o = self._ordre(c)
            if o not in vus:
                vus.append(o)
        return vus

    def placer(self, max_essais=None, graine=7, limite_s=None, progress=None):
        """Essais systématiques (ordres × 4 coins) puis amélioration progressive jusqu'à ``max_essais`` essais au total
        (Placement.placer(maxEssais) de l'application avec assez de temps). ``limite_s`` : garde-fou de durée (le
        meilleur plan déjà trouvé est rendu, ``Plan.complet_apres`` vaut alors False)."""
        if not self.pieces:
            raise Refus("Ajoute au moins une pièce à placer.")
        total = sum(p.quantite for p in self.pieces)
        if total > MAX_EXEMPLAIRES:
            raise Refus(f"{MAX_EXEMPLAIRES} exemplaires au plus par plan.")
        debut = time.monotonic()
        systematiques = [(o, c) for o in self.ordres() for c in COINS]
        if max_essais is None:
            max_essais = len(systematiques)
        max_essais = max(1, int(max_essais))
        prevus = max(1, max_essais)
        meilleur, essais, interrompu = None, 0, False

        def hors_temps():
            return limite_s is not None and essais >= 1 and time.monotonic() - debut > limite_s

        for o, c in systematiques:
            if essais >= max_essais:
                break
            if hors_temps():
                interrompu = True
                break
            e = self.essai(o, c)
            essais += 1
            if e.meilleur_que(meilleur):
                meilleur = e
            if progress:
                progress(essais / prevus)
        rnd = JavaRandom(graine)
        while not interrompu and essais < max_essais and meilleur is not None and len(meilleur.ordre) > 1:
            if hors_temps():
                interrompu = True
                break
            o = list(meilleur.ordre)
            a = rnd.next_int(len(o))
            b = rnd.next_int(len(o))
            if o[a] == o[b]:
                ou = rnd.next_int(len(o))
                o.insert(ou, o.pop(a))
            else:
                o[a], o[b] = o[b], o[a]
            c = COINS[rnd.next_int(4)] if rnd.next_int(4) == 0 else meilleur.coin
            e = self.essai(o, c)
            essais += 1
            if e.meilleur_que(meilleur):
                meilleur = e
            if progress:
                progress(essais / prevus)
        m = meilleur
        utilisation = m.aire / self.aire_chute if self.aire_chute > 0 else 0.0
        return Plan(m.poses, m.manques, utilisation, self.pas, essais, m.coin[0], not interrompu)

    # ------------------------------------------------------------------ formes posées et contrôle

    def forme(self, p, pose):
        """Forme posée (mm, repère de la chute)."""
        return [b.translater(pose.x, pose.y) for b in normaliser(tourner(p.boucles, pose.angle))]
