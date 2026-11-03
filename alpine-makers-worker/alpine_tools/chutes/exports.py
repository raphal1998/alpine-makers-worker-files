"""Exports du plan de découpe (port d'Exports.kt d'Alpine Chutes, docs/ALIGNEMENT.md) : SVG en mm, DXF R12, projet
Alpine Laser Studio (.alsproj), G-code GRBL, et compensation du trait de coupe (Decalage).

Repère commun : mm, Y vers le bas, origine au coin haut-gauche de la feuille (chute photographiée) ou de la boîte de la
chute (chute saisie). SVG et Laser Studio : coin HAUT-gauche, Y vers le bas ; DXF et G-code : coin BAS-gauche, Y vers le
haut (X vers la droite, Y vers l'arrière de la machine).
"""
import json
import math

from .geometrie import Boucle, arrondi, geo_boite, jround, nombre, normaliser, tourner
from .import_commun import fd_atan2, fd_hypot, jcos, jsin

ORIGINES = {"feuille": "Coin de la feuille", "chute": "Coin de la chute"}
EXTENSION_LASER_STUDIO = "alsproj"
MARGE_LASER_STUDIO = 10.0
COULEUR_DECOUPE = "#ff0000"
COULEUR_REPERE = "#0000ff"
COULEUR_INTERDIT = "#a0a0a0"


def nb(v, decimales=3):
    """String.format(Locale.US, "%.Nf") puis zéros finaux retirés ; « -0 » → « 0 »."""
    t = format(arrondi(v, decimales), "f")
    propre = t.rstrip("0").rstrip(".") if "." in t else t
    return "0" if propre in ("-0", "") else propre


class ReglagesExport:
    def __init__(self, svg=True, dxf=True, laser_studio=True, gcode=False, origine="feuille", compenser_kerf=False,
                 repere_chute=True, puissance_pct=80.0, vitesse_mm_min=300.0, passes=1, s_max=1000, air_assist=False):
        if origine not in ORIGINES:
            raise ValueError("Origine inconnue.")
        self.svg, self.dxf, self.laser_studio, self.gcode = svg, dxf, laser_studio, gcode
        self.origine, self.compenser_kerf, self.repere_chute = origine, compenser_kerf, repere_chute
        self.puissance_pct, self.vitesse_mm_min, self.passes = float(puissance_pct), float(vitesse_mm_min), int(passes)
        self.s_max, self.air_assist = int(s_max), air_assist


class PlanExport:
    """Plan prêt à exporter : chute et pièces posées [(nom, boucles mm y vers le bas, repère de la feuille)]."""

    def __init__(self, nom, chute, pieces, kerf_mm):
        self.nom, self.chute, self.pieces, self.kerf_mm = nom, chute, pieces, kerf_mm

    @staticmethod
    def de(nom, chute, pieces, plan, kerf_mm):
        par_id = {p.id: p for p in pieces}
        formes = []
        for pose in plan.poses:
            p = par_id.get(pose.piece)
            if p is None:
                continue
            f = [b.translater(pose.x, pose.y) for b in normaliser(tourner(p.boucles, pose.angle))]
            formes.append((f"{p.nom} #{pose.exemplaire + 1}", f))
        return PlanExport(nom, chute, formes, kerf_mm)


def _decalage(p, o):
    if o == "feuille":
        return 0.0, 0.0
    b = p.chute.boite
    return -b[0], -b[1]


def _hauteur_reference(p, o):
    """Bas de la feuille (chute photographiée, origine feuille) ou de la chute = Y 0 des repères machine."""
    if o == "feuille" and p.chute.hauteur_feuille is not None:
        return p.chute.hauteur_feuille
    b = p.chute.boite
    return b[3] - b[1]


def coupes(p, r):
    """Pièces (compensées du trait de coupe si demandé), dans le repère de la feuille."""
    if not r.compenser_kerf or p.kerf_mm <= 0:
        return p.pieces
    return [(n, matiere(f, p.kerf_mm / 2)) for n, f in p.pieces]


# ------------------------------------------------------------------ SVG

def _chemin(b, dx, dy):
    xs, ys = b.x.tolist(), b.y.tolist()
    return "".join(("M" if k == 0 else "L") + nb(xs[k] + dx) + "," + nb(ys[k] + dy) for k in range(len(xs))) + "Z"


def _echapper(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\"", "&quot;")


def svg(p, r):
    """SVG en millimètres (1 unité = 1 mm) : calques Decoupe (rouge), Repere-chute (bleu, ne pas découper),
    Zones-interdites (gris) et Origine (équerre verte au point (0, 0))."""
    dx, dy = _decalage(p, r.origine)
    cps = coupes(p, r)
    tout = [b for _, f in cps for b in f] + p.chute.boucles
    b = geo_boite(tout)
    x0 = min(0.0, b[0] + dx) - 2
    y0 = min(0.0, b[1] + dy) - 2
    l = max(b[2] + dx, 0.0) + 2 - x0
    h = max(b[3] + dy, 0.0) + 2 - y0
    o = ['<?xml version="1.0" encoding="UTF-8"?>\n',
         '<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" version="1.1" ',
         f'width="{nb(l)}mm" height="{nb(h)}mm" viewBox="{nb(x0)} {nb(y0)} {nb(l)} {nb(h)}">\n',
         f"  <title>{_echapper(p.nom)} — Alpine Chutes</title>\n",
         f"  <desc>Plan de découpe en millimètres (1 unité = 1 mm). Origine (0,0) : {_echapper(ORIGINES[r.origine].lower())}, haut-gauche. ",
         "Rouge : découpe des pièces. Bleu : bord de la chute (repère, ne pas découper). Gris : zones interdites.</desc>\n",
         '  <g id="Decoupe" inkscape:groupmode="layer" inkscape:label="Découpe" fill="none" stroke="#FF0000" stroke-width="0.1" stroke-linejoin="round">\n']
    for nom, f in cps:
        o.append(f'    <path fill-rule="evenodd" data-piece="{_echapper(nom)}" d="{"".join(_chemin(c, dx, dy) for c in f)}"/>\n')
    o.append("  </g>\n")
    if r.repere_chute:
        o.append('  <g id="Repere-chute" inkscape:groupmode="layer" inkscape:label="Repère chute (ne pas découper)" fill="none" stroke="#0000FF" stroke-width="0.2">\n')
        o.append(f'    <path fill-rule="evenodd" d="{"".join(_chemin(c, dx, dy) for c in p.chute.boucles)}"/>\n')
        o.append("  </g>\n")
        if p.chute.interdits:
            o.append('  <g id="Zones-interdites" inkscape:groupmode="layer" inkscape:label="Zones interdites (repère)" fill="none" stroke="#888888" stroke-linecap="round" stroke-linejoin="round" stroke-opacity="0.6">\n')
            for t in p.chute.interdits:
                if not t.points:
                    continue
                d = "".join(("M" if k == 0 else "L") + nb(q[0] + dx) + "," + nb(q[1] + dy) for k, q in enumerate(t.points))
                d += "l0.01,0" if len(t.points) == 1 else ""
                o.append(f'    <path stroke-width="{nb(2 * t.rayon_mm)}" d="{d}"/>\n')
            o.append("  </g>\n")
        ox = 0.0
        o.append('  <g id="Origine" inkscape:groupmode="layer" inkscape:label="Origine (repère)" fill="none" stroke="#00A000" stroke-width="0.2">\n')
        o.append(f'    <path d="M{nb(ox)},{nb(ox)}L{nb(ox + 8)},{nb(ox)}M{nb(ox)},{nb(ox)}L{nb(ox)},{nb(ox + 8)}"/>\n')
        o.append("  </g>\n")
    o.append("</svg>\n")
    return "".join(o)


# ------------------------------------------------------------------ DXF R12

def dxf(p, r):
    """DXF ASCII R12 (AC1009) en mm, Y vers le haut, origine au coin BAS-gauche choisi : calques DECOUPE (rouge),
    REPERE_CHUTE (bleu), ZONES_INTERDITES (gris, polylignes ouvertes), ORIGINE (vert)."""
    dx, dy = _decalage(p, r.origine)
    href = _hauteur_reference(p, r.origine)
    cps = [b for _, f in coupes(p, r) for b in f]

    def X(x):
        return x + dx

    def Y(y):
        return href - (y + dy)

    s = []

    def g(code, valeur):
        s.append(str(code).rjust(3) + "\r\n" + valeur + "\r\n")

    calques = [("DECOUPE", 1, cps)]
    if r.repere_chute:
        calques += [("REPERE_CHUTE", 5, p.chute.boucles), ("ZONES_INTERDITES", 8, []), ("ORIGINE", 3, [])]
    b = geo_boite(cps + p.chute.boucles)
    g(0, "SECTION"); g(2, "HEADER")
    g(9, "$ACADVER"); g(1, "AC1009")
    g(9, "$INSUNITS"); g(70, "4")
    g(9, "$EXTMIN"); g(10, nb(min(0.0, X(b[0])))); g(20, nb(min(0.0, Y(b[3])))); g(30, "0")
    g(9, "$EXTMAX"); g(10, nb(max(0.0, X(b[2])))); g(20, nb(max(0.0, Y(b[1])))); g(30, "0")
    g(0, "ENDSEC")
    g(0, "SECTION"); g(2, "TABLES")
    g(0, "TABLE"); g(2, "LTYPE"); g(70, "1")
    g(0, "LTYPE"); g(2, "CONTINUOUS"); g(70, "0"); g(3, "Solid line"); g(72, "65"); g(73, "0"); g(40, "0.0")
    g(0, "ENDTAB")
    g(0, "TABLE"); g(2, "LAYER"); g(70, str(len(calques)))
    for nom, couleur, _ in calques:
        g(0, "LAYER"); g(2, nom); g(70, "0"); g(62, str(couleur)); g(6, "CONTINUOUS")
    g(0, "ENDTAB")
    g(0, "ENDSEC")
    g(0, "SECTION"); g(2, "ENTITIES")

    def polyligne(calque, xs, ys, fermee):
        g(0, "POLYLINE"); g(8, calque); g(66, "1"); g(10, "0.0"); g(20, "0.0"); g(30, "0.0"); g(70, "1" if fermee else "0")
        for x, y in zip(xs, ys):
            g(0, "VERTEX"); g(8, calque); g(10, nb(x, 4)); g(20, nb(y, 4)); g(30, "0.0")
        g(0, "SEQEND"); g(8, calque)

    for nom, _, boucles in calques:
        for bo in boucles:
            polyligne(nom, [X(v) for v in bo.x.tolist()], [Y(v) for v in bo.y.tolist()], True)
    if r.repere_chute:
        for t in p.chute.interdits:
            if len(t.points) >= 2:
                polyligne("ZONES_INTERDITES", [X(q[0]) for q in t.points], [Y(q[1]) for q in t.points], False)
            elif len(t.points) == 1:
                g(0, "CIRCLE"); g(8, "ZONES_INTERDITES"); g(10, nb(X(t.points[0][0]))); g(20, nb(Y(t.points[0][1]))); g(30, "0.0")
                g(40, nb(t.rayon_mm))
        polyligne("ORIGINE", [8.0, 0.0, 0.0], [0.0, 0.0, 8.0], False)
    g(0, "ENDSEC")
    g(0, "EOF")
    return "".join(s)


# ------------------------------------------------------------------ Alpine Laser Studio (.alsproj)

def _arrondi(v, d=4):
    f = 1.0
    for _ in range(d):
        f *= 10
    return jround(v * f) / f


def _objet_chemin(id, nom, couleur, chemins):
    pts = [q for c, _ in chemins for q in c]
    if len(pts) < 2:
        return None
    x0 = min(q[0] for q in pts)
    y0 = min(q[1] for q in pts)
    w = max(max(q[0] for q in pts) - x0, 0.001)
    h = max(max(q[1] for q in pts) - y0, 0.001)
    paths = [{"points": [[_arrondi((q[0] - x0) / w, 6), _arrondi((q[1] - y0) / h, 6)] for q in c], "closed": fermee}
             for c, fermee in chemins if len(c) >= 2]
    return {"id": id, "type": "path", "name": nom[:60], "x": _arrondi(x0), "y": _arrondi(y0), "w": _arrondi(w), "h": _arrondi(h),
            "rotation": 0, "layer": couleur, "paths": paths}


def laser_studio(p, r, enregistre_le=None):
    """Projet Alpine Laser Studio (contrat de worker_agent/desktop_laser_studio_project.py) : un objet « path » par pièce
    sur le calque rouge (ligne, découpe, actif) ; bord de la chute (bleu) et zones interdites (gris) sur des calques
    DÉSACTIVÉS. Document en mm, Y vers le bas, coin de la boîte de la chute à 10 mm du coin haut-gauche."""
    bc = p.chute.boite
    dx, dy = MARGE_LASER_STUDIO - bc[0], MARGE_LASER_STUDIO - bc[1]

    def pts(b):
        return list(zip((b.x + dx).tolist(), (b.y + dy).tolist()))

    objets = []
    for k, (nom, f) in enumerate(coupes(p, r)):
        o = _objet_chemin(f"chutes-piece-{k + 1}", nom, COULEUR_DECOUPE, [(pts(b), True) for b in f])
        if o is not None:
            objets.append(o)
    if r.repere_chute:
        o = _objet_chemin("chutes-bord", "Bord de la chute (repère)", COULEUR_REPERE, [(pts(b), True) for b in p.chute.boucles])
        if o is not None:
            objets.append(o)
        zones = [([(q[0] + dx, q[1] + dy) for q in t.points], False) for t in p.chute.interdits if len(t.points) >= 2]
        o = _objet_chemin("chutes-interdits", "Zones interdites (repère)", COULEUR_INTERDIT, zones)
        if o is not None:
            objets.append(o)

    def calque(nom, couleur, actif):
        return {"name": nom[:60], "color": couleur, "mode": "line", "line_operation": "cut", "enabled": actif, "visible": True,
                "auto_optimize": actif, "passes": 1, "kerf_offset_mm": 0}

    c = p.chute
    matiere_txt = " ".join(t for t in (c.matiere, nombre(c.epaisseur_mm) + " mm" if c.epaisseur_mm > 0 else "") if t.strip())
    projet = {"version": 1, "app": "alpine-laser-studio"}
    if enregistre_le is not None:
        projet["saved_at"] = enregistre_le
    projet["name"] = (p.nom.strip() or "Plan de découpe")[:60]
    projet["objects"] = objets
    calques = {COULEUR_DECOUPE: calque(f"Découpe des pièces {matiere_txt}".strip(), COULEUR_DECOUPE, True)}
    if r.repere_chute:
        calques[COULEUR_REPERE] = calque("Repère : bord de la chute (ne pas découper)", COULEUR_REPERE, False)
        calques[COULEUR_INTERDIT] = calque("Repère : zones interdites", COULEUR_INTERDIT, False)
    projet["layers"] = calques
    projet["activeLayer"] = COULEUR_DECOUPE
    projet["order"] = "inside-out"
    projet["offset"] = [0, 0]
    projet["source"] = {"app": "alpine-chutes", "chute": c.nom, "matiere": c.matiere, "epaisseur_mm": c.epaisseur_mm, "kerf_mm": p.kerf_mm,
                        "kerf_compense": r.compenser_kerf,
                        "origine": f"coin haut-gauche de la boîte de la chute à {nb(MARGE_LASER_STUDIO)} mm du coin du document"}
    return json.dumps(projet, ensure_ascii=False, separators=(",", ":"))


# ------------------------------------------------------------------ G-code GRBL

def gcode(p, r):
    """G-code GRBL simple pour un laser ($32=1 conseillé) : mm absolus, M4 (puissance dynamique), origine au coin BAS-gauche
    choisi. Contours intérieurs (trous) de toutes les pièces d'abord, puis les extérieurs, au plus proche voisin ; chaque
    boucle répétée ``passes`` fois."""
    dx, dy = _decalage(p, r.origine)
    href = _hauteur_reference(p, r.origine)
    s = int(min(max(r.puissance_pct, 0.0), 100.0) / 100.0 * r.s_max)
    f = min(max(r.vitesse_mm_min, 10.0), 50_000.0)
    passes = min(max(r.passes, 1), 50)
    boucles = [(b, b.aire < 0) for _, forme in coupes(p, r) for b in orienter(forme)]
    interieurs = [b for b, trou in boucles if trou]
    exterieurs = [b for b, trou in boucles if not trou]
    o = []

    def l(t):
        o.append(t + "\n")

    c = p.chute
    l(f"; Alpine Chutes — {p.nom.replace(chr(10), ' ')}")
    l(f"; Chute : {c.nom} ({c.matiere} {nombre(c.epaisseur_mm)} mm)")
    l(f"; Origine : {ORIGINES[r.origine]} (coin bas-gauche, X vers la droite, Y vers l'arrière), mm")
    l(f"; Puissance S{s} / S{r.s_max} (M4 dynamique), vitesse {nb(f, 0)} mm/min, {passes} passe(s), trait de coupe "
      f"{'compensé' if r.compenser_kerf else 'non compensé'}")
    l(f"; {len(p.pieces)} pièce(s), {len(interieurs)} contour(s) intérieur(s) d'abord, puis {len(exterieurs)} extérieur(s)")
    for t in ("G21", "G90", "G17", "G94", "M5"):
        l(t)
    if r.air_assist:
        l("M8")
    courant = [0.0, 0.0]

    def ordonner(liste):
        reste = list(liste)
        sortie = []
        while reste:
            k, dmin, depart = 0, math.inf, 0
            for q, b in enumerate(reste):
                xs, ys = b.x.tolist(), b.y.tolist()
                for i in range(len(xs)):
                    d = fd_hypot(xs[i] + dx - courant[0], href - (ys[i] + dy) - courant[1])
                    if d < dmin:
                        dmin, k, depart = d, q, i
            b = reste.pop(k)
            idx = [(i + depart) % b.n for i in range(b.n)]
            tourne = Boucle(b.x[idx], b.y[idx])
            sortie.append(tourne)
            courant[0] = float(tourne.x[0]) + dx
            courant[1] = href - (float(tourne.y[0]) + dy)
        return sortie

    l("M4 S0")
    for b in ordonner(interieurs) + ordonner(exterieurs):
        xs, ys = b.x.tolist(), b.y.tolist()
        X = [nb(v + dx) for v in xs]
        Y = [nb(href - (v + dy)) for v in ys]
        l(f"G0 X{X[0]} Y{Y[0]}")
        for pas in range(passes):
            l(f"G1 X{X[0]} Y{Y[0]} S{s} F{nb(f, 0)}" + (f" ; passe {pas + 1}" if pas > 0 else ""))
            for i in range(1, len(xs)):
                l(f"G1 X{X[i]} Y{Y[i]}")
            l(f"G1 X{X[0]} Y{Y[0]}")
    l("M5")
    if r.air_assist:
        l("M9")
    l("G0 X0 Y0")
    l("M2")
    return "".join(o)


# ------------------------------------------------------------------ compensation du trait de coupe (Decalage)

def orienter(boucles):
    """Extérieurs en sens direct (aire > 0), trous en sens indirect, d'après l'imbrication (pair / impair)."""
    sortie = []
    for b in boucles:
        px, py = float(b.x[0]), float(b.y[0])
        profondeur = sum(1 for o in boucles if o is not b and abs(o.aire) > abs(b.aire) and o.contient(px, py))
        exterieur = profondeur % 2 == 0
        sortie.append(b if (b.aire > 0) == exterieur else b.inverse())
    return sortie


def matiere(boucles, d):
    """Agrandit la matière de ``d`` mm (> 0) : extérieurs vers l'extérieur, trous rétrécis."""
    return [decaler(b, d) for b in orienter(boucles)]


def decaler(b, d):
    """Décale une boucle orientée de ``d`` mm hors de la matière : angles saillants arrondis par un petit arc, angles
    rentrants en onglet (même calcul que Decalage.decaler)."""
    if d == 0.0 or b.n < 3:
        return b
    signe = 1.0
    X, Y = b.x.tolist(), b.y.tolist()
    n = len(X)
    xs, ys = [], []
    for i in range(n):
        p, s = (i - 1 + n) % n, (i + 1) % n
        e1x, e1y = X[i] - X[p], Y[i] - Y[p]
        l1 = fd_hypot(e1x, e1y)
        e2x, e2y = X[s] - X[i], Y[s] - Y[i]
        l2 = fd_hypot(e2x, e2y)
        if l1 < 1e-12 or l2 < 1e-12:
            continue
        n1x, n1y = signe * e1y / l1, -signe * e1x / l1
        n2x, n2y = signe * e2y / l2, -signe * e2x / l2
        croix = e1x * e2y - e1y * e2x
        saillant = croix > 0
        cos_a = min(max(n1x * n2x + n1y * n2y, -1.0), 1.0)
        if saillant and cos_a < 0.94:
            a1 = fd_atan2(n1y, n1x)
            a2 = fd_atan2(n2y, n2x)
            da = a2 - a1
            while da > math.pi:
                da -= 2 * math.pi
            while da < -math.pi:
                da += 2 * math.pi
            k = max(2, int(abs(da) / (math.pi / 12)))
            for q in range(k + 1):
                a = a1 + da * q / k
                xs.append(X[i] + d * jcos(a))
                ys.append(Y[i] + d * jsin(a))
        else:
            bx, by = n1x + n2x, n1y + n2y
            lb = fd_hypot(bx, by)
            if lb < 1e-9:
                xs.append(X[i] + d * n1x)
                ys.append(Y[i] + d * n1y)
                continue
            m = min(d / (lb / 2), 4 * d)
            xs.append(X[i] + bx / lb * m)
            ys.append(Y[i] + by / lb * m)
    return Boucle(xs, ys)
