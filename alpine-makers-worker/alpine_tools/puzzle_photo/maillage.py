# -*- coding: utf-8 -*-
"""Maillage indexé, prismes et fichiers 3D du puzzle, portés de Maillage.kt, Prismes (Triangulation.kt),
TroisMfPieces (Relief.kt) et ExportModele.kt d'Alpine Gift.

Les sommets de mêmes coordonnées float32 sont fusionnés, dans l'ordre de création de l'application : faces et parois
partagent exactement les mêmes sommets, le solide est fermé (chaque arête entre deux triangles, une fois dans chaque
sens). STL binaire (normales float32 comme StlBinaire), 3MF à pièces avec filaments (Bambu Studio, OrcaSlicer), ZIP d'un
STL par pièce, OBJ + MTL.
"""
import math
import re
import zipfile

import numpy as np

from .geometrie import boucles_de, regrouper
from .triangulation import trianguler

F32 = np.float32


class Maillage:
    def __init__(self):
        self._index = {}
        self.xyz = []
        self.tri = []

    @property
    def nb_sommets(self):
        return len(self.xyz) // 3

    @property
    def nb_triangles(self):
        return len(self.tri) // 3

    def sommet(self, x, y, z):
        """Coordonnées déjà arrondies en float32 (flottants Python) ; −0 et +0 confondus comme floatToIntBits(x + 0f)."""
        cle = (x + 0.0, y + 0.0, z + 0.0)
        i = self._index.get(cle)
        if i is None:
            i = len(self.xyz) // 3
            self._index[cle] = i
            self.xyz.extend((x, y, z))
        return i

    def ajouter(self, a, b, c):
        if a == b or b == c or a == c:
            return
        self.tri.extend((a, b, c))

    def fusionner(self, autre):
        """Ajoute tous les triangles d'un autre maillage (corps séparés dans un même fichier)."""
        v = autre.xyz
        t = autre.tri
        for k in range(0, len(t), 3):
            a, b, c = 3 * t[k], 3 * t[k + 1], 3 * t[k + 2]
            self.ajouter(self.sommet(v[a], v[a + 1], v[a + 2]), self.sommet(v[b], v[b + 1], v[b + 2]),
                         self.sommet(v[c], v[c + 1], v[c + 2]))

    def tableaux(self):
        """Sommets (n, 3) float32 et triangles (m, 3) int64."""
        return (np.asarray(self.xyz, dtype=F32).reshape(-1, 3), np.asarray(self.tri, dtype=np.int64).reshape(-1, 3))

    def controle(self):
        return controle(self.tableaux()[1])

    def boite(self):
        v = self.tableaux()[0]
        return v.min(axis=0).tolist() + v.max(axis=0).tolist()

    def volume(self):
        return volume(*self.tableaux())


def controle(triangles):
    """Maillage.controle : arêtes libres (1 triangle), multiples (> 2), d'orientation incohérente ; fermé si aucune."""
    if len(triangles) == 0:
        return {"triangles": 0, "aretes": 0, "libres": 0, "multiples": 0, "incoherentes": 0, "ferme": False}
    a = triangles.reshape(-1)
    b = np.roll(triangles, -1, axis=1).reshape(-1)
    n = int(max(a.max(), b.max())) + 1
    non_orient = np.minimum(a, b) * n + np.maximum(a, b)
    cles, total = np.unique(non_orient, return_counts=True)
    cles_s, n_sens = np.unique(non_orient[a < b], return_counts=True)
    avant = np.zeros_like(total)
    avant[np.searchsorted(cles, cles_s)] = n_sens
    arriere = total - avant
    libres = int(np.count_nonzero(total == 1))
    multiples = int(np.count_nonzero(total > 2))
    incoherentes = int(np.count_nonzero((total == 2) & ((avant != 1) | (arriere != 1))))
    return {"triangles": int(len(triangles)), "aretes": int(len(cles)), "libres": libres, "multiples": multiples,
            "incoherentes": incoherentes, "ferme": libres == 0 and multiples == 0 and incoherentes == 0}


def volume(sommets, triangles):
    """Volume signé (mm³), somme dans l'ordre de l'application (Maillage.volume)."""
    if len(triangles) == 0:
        return 0.0
    p = sommets.astype(np.float64)[triangles]
    xa, ya, za = p[:, 0, 0], p[:, 0, 1], p[:, 0, 2]
    xb, yb, zb = p[:, 1, 0], p[:, 1, 1], p[:, 1, 2]
    xc, yc, zc = p[:, 2, 0], p[:, 2, 1], p[:, 2, 2]
    t = (xa * (yb * zc - zb * yc) - ya * (xb * zc - zb * xc) + za * (xb * yc - yb * xc)) / 6.0
    return float(np.cumsum(t)[-1])


# --- Prismes (Triangulation.kt) ---------------------------------------------------------------------------------------
def _f32(valeurs):
    return np.asarray(valeurs, dtype=np.float64).astype(F32).tolist()


def face(m, polygones, z, dessus):
    """Face horizontale à ``z`` ; ``dessus`` : normale +z, sinon −z."""
    zf = float(F32(z))
    for ext, trous in polygones:
        boucles = [ext] + list(trous)
        pts = np.vstack(boucles)
        ids = [m.sommet(x, y, zf) for x, y in zip(_f32(pts[:, 0]), _f32(pts[:, 1]))]
        t = trianguler(ext, trous)
        i = 0
        while i + 2 < len(t):
            if dessus:
                m.ajouter(ids[t[i]], ids[t[i + 1]], ids[t[i + 2]])
            else:
                m.ajouter(ids[t[i]], ids[t[i + 2]], ids[t[i + 1]])
            i += 3


def paroi(m, b, z_bas, z_haut, matiere_a_gauche):
    """Paroi verticale le long d'une boucle (sommets créés dans l'ordre de l'application)."""
    zb, zh = float(F32(z_bas)), float(F32(z_haut))
    xs, ys = _f32(b[:, 0]), _f32(b[:, 1])
    n = len(xs)
    for i in range(n):
        j = (i + 1) % n
        ax, ay, bx, by = xs[i], ys[i], xs[j], ys[j]
        a_b = m.sommet(ax, ay, zb)
        b_b = m.sommet(bx, by, zb)
        b_h = m.sommet(bx, by, zh)
        a_h = m.sommet(ax, ay, zh)
        if matiere_a_gauche:
            m.ajouter(a_b, b_b, b_h)
            m.ajouter(a_b, b_h, a_h)
        else:
            m.ajouter(a_b, b_h, b_b)
            m.ajouter(a_b, a_h, b_h)


def prisme(m, polygones, z0, z1):
    face(m, polygones, z0, False)
    face(m, polygones, z1, True)
    for b in boucles_de(polygones):
        paroi(m, b, z0, z1, True)


def socle_avec_motif(m, socle, motif, z_socle, z_motif):
    """Socle (0..z_socle) avec un motif strictement intérieur, en relief (z_motif > z_socle) ou creusé : un seul
    solide fermé."""
    face(m, socle, 0.0, False)
    for b in boucles_de(socle):
        paroi(m, b, 0.0, z_socle, True)
    if not motif:
        face(m, socle, z_socle, True)
        return
    face(m, regrouper(boucles_de(socle) + boucles_de(motif)), z_socle, True)
    if z_motif > z_socle:
        for b in boucles_de(motif):
            paroi(m, b, z_socle, z_motif, True)
    else:
        for b in boucles_de(motif):
            paroi(m, b, z_motif, z_socle, False)
    face(m, motif, z_motif, True)


# --- STL --------------------------------------------------------------------------------------------------------------
SIGNATURE = "Alpine Tools"
_STL = np.dtype([("n", "<f4", (3,)), ("v", "<f4", (3, 3)), ("attr", "<u2")])


def _entete(titre):
    # L'en-tête ne doit pas commencer par « solid » (certains lecteurs le prendraient pour un STL texte).
    return re.sub(r"[^\x20-\x7E]", "_", f"{SIGNATURE} - {titre}").encode("ascii")[:80].ljust(80, b"\0")


def stl_octets(m, titre):
    """StlBinaire.octets : normales unitaires calculées en float32 comme l'application."""
    v, t = m.tableaux()
    p = v[t] if len(t) else np.zeros((0, 3, 3), dtype=F32)
    u = p[:, 1] - p[:, 0]
    w = p[:, 2] - p[:, 0]
    nx = u[:, 1] * w[:, 2] - u[:, 2] * w[:, 1]
    ny = u[:, 2] * w[:, 0] - u[:, 0] * w[:, 2]
    nz = u[:, 0] * w[:, 1] - u[:, 1] * w[:, 0]
    l = np.sqrt(nx * nx + ny * ny + nz * nz)
    ok = l > 0
    with np.errstate(invalid="ignore", divide="ignore"):
        nrm = np.where(ok[:, None], np.stack([nx / l, ny / l, nz / l], axis=1), F32(0))
    rec = np.zeros(len(t), _STL)
    rec["n"] = nrm
    rec["v"] = p
    return _entete(titre) + np.uint32(len(t)).tobytes() + rec.tobytes()


def _stl_pieces(titre, sommets, tris):
    """ExportModele.stl : sommets en double (venus du float32), normales en double puis float32."""
    s = sommets.astype(np.float64)
    a, b, d = s[tris[:, 0]], s[tris[:, 1]], s[tris[:, 2]]
    u, w = b - a, d - a
    nrm = np.stack([u[:, 1] * w[:, 2] - u[:, 2] * w[:, 1], u[:, 2] * w[:, 0] - u[:, 0] * w[:, 2],
                    u[:, 0] * w[:, 1] - u[:, 1] * w[:, 0]], axis=1)
    lg = np.sqrt(nrm[:, 0] * nrm[:, 0] + nrm[:, 1] * nrm[:, 1] + nrm[:, 2] * nrm[:, 2])
    nrm = np.where(lg[:, None] > 0, nrm / np.where(lg > 0, lg, 1.0)[:, None], nrm)
    rec = np.zeros(len(tris), _STL)
    rec["n"] = nrm
    rec["v"] = np.stack((a, b, d), axis=1)
    return _entete(titre) + np.uint32(len(tris)).tobytes() + rec.tobytes()


# --- 3MF à pièces (TroisMfPieces) --------------------------------------------------------------------------------------
NS_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
REL_MODEL = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
REL_VIGNETTE = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"
MODELE = "3D/3dmodel.model"
REGLAGES = "Metadata/model_settings.config"
VIGNETTE = "Metadata/thumbnail.png"
_IDENTITE = "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"


def _entree(nom):
    """Entrée de ZIP à date fixe : mêmes réglages, mêmes octets (fichiers reproductibles)."""
    info = zipfile.ZipInfo(nom, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    return info


def echapper(t):
    return (str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
            .replace("'", "&apos;"))


def nombre_3mf(v):
    """TroisMf.nombre : 6 décimales au plus, sans zéros inutiles (Math.round exact)."""
    x = v * 1e6
    n = math.floor(x)
    n = int(n) + (1 if x - n >= 0.5 else 0)
    signe = "-" if n < 0 else ""
    n = abs(n)
    f = n % 1_000_000
    return f"{signe}{n // 1_000_000}" + ("." + f"{f:06d}".rstrip("0") if f else "")


def _couleur_3mf(hex_):
    return hex_.upper() + "FF"


def ecrire_3mf(chemin, titre, application, teintes, pieces, vignette=None):
    """TroisMfPieces.ecrire : ``teintes`` [(hex, nom)], ``pieces`` [(nom, filament, Maillage)] ; une pièce par objet,
    assemblage par composants, filament de chaque pièce dans Metadata/model_settings.config. Rend [(nom, filament, tri)]."""
    infos, ids = [], []
    with zipfile.ZipFile(chemin, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr(_entree("[Content_Types].xml"), "\n".join(x for x in (
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
            '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>',
            '<Default Extension="config" ContentType="text/xml"/>',
            '<Default Extension="png" ContentType="image/png"/>' if vignette is not None else None,
            "</Types>") if x is not None))
        z.writestr(_entree("_rels/.rels"), "\n".join(x for x in (
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
            f'<Relationship Target="/{MODELE}" Id="rel0" Type="{REL_MODEL}"/>',
            f'<Relationship Target="/{VIGNETTE}" Id="rel1" Type="{REL_VIGNETTE}"/>' if vignette is not None else None,
            "</Relationships>") if x is not None))
        with z.open(_entree(MODELE), "w") as sortie:
            def ecrire(t):
                sortie.write(t.encode("utf-8"))
            ecrire(f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="fr-FR" xmlns="{NS_CORE}">\n')
            ecrire(f'<metadata name="Title">{echapper(titre)}</metadata>\n'
                   f'<metadata name="Application">{echapper(application)}</metadata>\n')
            ecrire('<resources>\n<basematerials id="1">')
            for hex_, nom in teintes:
                ecrire(f'<base name="{echapper(nom)}" displaycolor="{_couleur_3mf(hex_)}"/>')
            ecrire("</basematerials>\n")
            for nom, filament, m in pieces:
                if not m.nb_triangles:
                    continue
                id_ = 2 + len(ids)
                ids.append(id_)
                ecrire(f'<object id="{id_}" type="model" name="{echapper(nom)}" pid="1" pindex="{filament}"><mesh><vertices>')
                v = m.xyz
                textes = [nombre_3mf(c) for c in v]
                morceaux = []
                for k in range(0, len(textes), 3):
                    morceaux.append(f'<vertex x="{textes[k]}" y="{textes[k + 1]}" z="{textes[k + 2]}"/>')
                    if len(morceaux) >= 4096:
                        ecrire("".join(morceaux))
                        morceaux = []
                ecrire("".join(morceaux))
                ecrire("</vertices><triangles>")
                t = m.tri
                morceaux = []
                for k in range(0, len(t), 3):
                    morceaux.append(f'<triangle v1="{t[k]}" v2="{t[k + 1]}" v3="{t[k + 2]}"/>')
                    if len(morceaux) >= 4096:
                        ecrire("".join(morceaux))
                        morceaux = []
                ecrire("".join(morceaux))
                ecrire("</triangles></mesh></object>\n")
                infos.append((nom, filament, m.nb_triangles))
            if not ids:
                raise ValueError("Rien à imprimer.")
            assemblage = 2 + len(ids)
            ecrire(f'<object id="{assemblage}" type="model" name="{echapper(titre)}"><components>')
            for id_ in ids:
                ecrire(f'<component objectid="{id_}"/>')
            ecrire(f'</components></object>\n</resources>\n<build><item objectid="{assemblage}"/></build>\n</model>\n')
        r = ['<?xml version="1.0" encoding="UTF-8"?>\n<config>\n', f'  <object id="{assemblage}">\n',
             f'    <metadata key="name" value="{echapper(titre)}"/>\n',
             f'    <metadata key="extruder" value="{infos[0][1] + 1}"/>\n']
        for k, (nom, filament, _) in enumerate(infos):
            r.append(f'    <part id="{ids[k]}" subtype="normal_part">\n')
            r.append(f'      <metadata key="name" value="{echapper(nom)}"/>\n')
            r.append(f'      <metadata key="matrix" value="{_IDENTITE}"/>\n')
            r.append(f'      <metadata key="extruder" value="{filament + 1}"/>\n    </part>\n')
        r.append("  </object>\n</config>")
        z.writestr(_entree(REGLAGES), "".join(r))
        if vignette is not None:
            z.writestr(_entree(VIGNETTE), vignette)
    return infos


# --- ExportModele : STL unique, STL par pièce (ZIP), OBJ + MTL (ZIP) ---------------------------------------------------
def nom_piece(nom):
    """ExportModele.nomSur : nom de pièce sûr dans un ZIP."""
    t = re.sub(r"[^A-Za-z0-9À-ÿ ._+-]", " ", nom)
    return re.sub(r"\s+", " ", t).strip()[:60] or "piece"


def _lisez_moi(titre, lignes, conseil):
    return "\r\n".join([f"{titre} — Alpine Makers", ""] + list(lignes) + ["", conseil])


def ecrire_stl_unique(chemin, m, titre):
    with open(chemin, "wb") as f:
        f.write(stl_octets(m, titre))
    return m.nb_triangles


def ecrire_stl_pieces(chemin, titre, pieces):
    """``pieces`` [(nom, (hex, nom couleur), Maillage)] → ZIP d'un STL par pièce + LISEZMOI ; rend les triangles."""
    total, lignes, pris = 0, [], set()
    with zipfile.ZipFile(chemin, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for i, (nom, teinte, m) in enumerate(pieces):
            if not m.nb_triangles:
                continue
            fichier = f"{i + 1:02d}-{nom_piece(nom)}.stl"
            if fichier.lower() in pris:
                fichier = f"{i + 1:02d}-{nom_piece(nom)}-{i}.stl"
            pris.add(fichier.lower())
            v, t = m.tableaux()
            z.writestr(_entree(fichier), _stl_pieces(nom, v, t))
            total += len(t)
            lignes.append(f"{fichier} : {teinte[1]} {teinte[0]}")
        z.writestr(_entree("LISEZMOI.txt"), _lisez_moi(titre, lignes, "Importe tous les STL en une fois comme pièces d'un même "
                                              "objet (ils se superposent exactement), puis attribue à chacun le filament de sa couleur."))
    return total


def _nombre_obj(v):
    """ExportModele.nombre : 4 décimales au plus."""
    x = v * 10_000
    n = math.floor(x)
    n = int(n) + (1 if x - n >= 0.5 else 0)
    r = n / 10_000.0
    return str(int(r)) if r == round(r) else repr(r)


def ecrire_obj(chemin, titre, pieces):
    """ZIP d'un OBJ (un objet par pièce), de son MTL (une matière par couleur) et d'un LISEZMOI ; rend les triangles."""
    hexes = []
    for _, (hex_, _n), _m in pieces:
        if hex_ not in hexes:
            hexes.append(hex_)
    total = 0
    with zipfile.ZipFile(chemin, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        mtl = [f"# {titre} — Alpine Makers\r\n"]
        for k, h in enumerate(hexes):
            r, g, b = int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)
            mtl.append(f"newmtl couleur_{k + 1}\r\nKd {r / 255:.4f} {g / 255:.4f} {b / 255:.4f}\r\n"
                       "Ka 0 0 0\r\nKs 0 0 0\r\nd 1\r\nillum 1\r\n\r\n")
        z.writestr(_entree("modele.mtl"), "".join(mtl))
        with z.open(_entree("modele.obj"), "w") as sortie:
            sortie.write(f"# {titre} — Alpine Makers (mm)\r\nmtllib modele.mtl\r\n".encode("utf-8"))
            decal = 0
            for nom, (hex_, _n), m in pieces:
                if not m.nb_triangles:
                    continue
                lignes = [f"o {nom_piece(nom).replace(' ', '_')}\r\nusemtl couleur_{hexes.index(hex_) + 1}\r\n"]
                v = m.xyz
                for k in range(0, len(v), 3):
                    lignes.append(f"v {_nombre_obj(v[k])} {_nombre_obj(v[k + 1])} {_nombre_obj(v[k + 2])}\r\n")
                t = m.tri
                for k in range(0, len(t), 3):
                    lignes.append(f"f {t[k] + 1 + decal} {t[k + 1] + 1 + decal} {t[k + 2] + 1 + decal}\r\n")
                sortie.write("".join(lignes).encode("utf-8"))
                decal += m.nb_sommets
                total += m.nb_triangles
        z.writestr(_entree("LISEZMOI.txt"), _lisez_moi(titre, [f"{nom} : {t[1]} {t[0]}" for nom, t, _ in pieces],
                                              "Ouvre modele.obj (garde modele.mtl à côté) : chaque objet a sa couleur."))
    return total
