# -*- coding: utf-8 -*-
"""Fichiers du Tableau couleur : 3MF multicolore (Gift TroisMf.ecrire), STL par couleur, STL unique, OBJ + MTL
(Gift ExportModele.kt). Les pièces gardent les mêmes coordonnées (mm) dans tous les formats.

3MF : 3MF Core en millimètres, une pièce par couleur + plaque de base (basematerials + displaycolor), assemblées par
composants, et ``Metadata/model_settings.config`` qui attribue à chaque pièce le filament N° (index + 1) : le tableau
s'ouvre en multicolore dans Bambu Studio et OrcaSlicer. Écrit en flux, une pièce en mémoire à la fois.
"""
import math
import re
import unicodedata
import zipfile
from dataclasses import dataclass

import numpy as np

from . import carte as K
from . import couleurs as C
from . import maillage as M

NS_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
REL_MODEL = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
REL_VIGNETTE = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"
MODELE = "3D/3dmodel.model"
REGLAGES = "Metadata/model_settings.config"
VIGNETTE = "Metadata/thumbnail.png"
APPLICATION = "Alpine Makers — Tableau couleur"
SIGNATURE = "Alpine Makers"
_IDENTITE = "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"
_MORCEAU = 1 << 16

# Formats d'enregistrement (FormatModele de Gift) : libellé, suffixe du fichier, description.
FORMATS = {
    "3mf": ("3MF multicolore", ".3mf", "Bambu Studio, OrcaSlicer, Creality Print : chaque couleur arrive sur son filament."),
    "stl_couleurs": ("STL par couleur (ZIP)", "-stl.zip",
                     "Un STL par couleur : importe-les ensemble dans ton trancheur (PrusaSlicer, Cura…), puis un filament par pièce."),
    "stl_unique": ("STL unique", ".stl", "Tout le modèle en une seule pièce : impression d'une seule couleur, ou à peindre."),
    "obj": ("OBJ avec couleurs (ZIP)", "-obj.zip",
            "OBJ + MTL : couleurs gardées, pour Blender, PrusaSlicer, Bambu Studio et la plupart des logiciels 3D."),
}


def echapper(t):
    return (str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
            .replace("'", "&apos;"))


def couleur_3mf(hex_):
    return (C.normaliser_hex(hex_) or "#000000") + "FF"


def nombre(v):
    """Nombre compact du 3MF (6 décimales au plus, sans zéros inutiles) : TroisMf.nombre."""
    n = math.floor(v * 1e6 + 0.5)
    signe = "-" if n < 0 else ""
    n = abs(n)
    t = f"{signe}{n // 1_000_000}"
    f = n % 1_000_000
    return t + ("." + f"{f:06d}".rstrip("0") if f else "")


def nombre_obj(v):
    """Nombre de l'OBJ (4 décimales au plus) : ExportModele.nombre."""
    r = math.floor(v * 10_000 + 0.5) / 10_000.0
    return str(int(r)) if r == round(r) else repr(r)


def nom_fichier(titre):
    """Nom de fichier sûr [a-z0-9-] (IndexCreations.nomSur de Gift)."""
    sans = "".join(ch for ch in unicodedata.normalize("NFD", titre) if not unicodedata.combining(ch))
    return re.sub(r"[^A-Za-z0-9]+", "-", sans).strip("-")[:40].strip("-").lower() or "creation"


def nom_piece(nom):
    """ExportModele.nomSur : nom de pièce sûr dans un ZIP."""
    t = re.sub(r"[^A-Za-z0-9À-ÿ ._+-]", " ", nom)
    return re.sub(r"\s+", " ", t).strip()[:60] or "piece"


@dataclass
class PlanPiece:
    nom: str
    couleur: int
    valeur: int
    z0: float
    z1: float


def plan(c, p):
    """Plaque de base puis chaque couleur présente, aux bonnes hauteurs (TroisMf.plan)."""
    base = K.index_base(p, len(c.couleurs))
    if p.face_dessous:
        z_plaque, z_image = (p.couche_mm, p.couche_mm + p.base_mm), (0.0, p.couche_mm)
    else:
        z_plaque, z_image = (0.0, p.base_mm), (p.base_mm, p.base_mm + p.couche_mm)
    pieces = [PlanPiece("Plaque de base", base, -1, *z_plaque)]
    noms = {"Plaque de base"}
    for i, t in enumerate(c.couleurs):
        if c.comptes[i] == 0:
            continue
        nom = f"{t.nom} ({i + 1})" if t.nom in noms else t.nom
        noms.add(nom)
        pieces.append(PlanPiece(nom, i, i, *z_image))
    return pieces


def mailler_piece(c, p, pp, limite=M.TRIANGLES_MAX):
    return M.mailler(c.indices, pp.valeur, pp.z0, pp.z1, c.pas_x, c.pas_y, p.face_dessous, limite)


@dataclass
class InfoPiece:
    nom: str
    couleur: int
    hex: str
    pixels: int
    volume_mm3: float
    grammes: float
    plaque: bool


@dataclass
class Bilan:
    pieces: list
    triangles: int
    epaisseur_mm: float

    @property
    def grammes_total(self):
        return sum(i.grammes for i in self.pieces)


class _Flux:
    """Texte UTF-8 écrit par morceaux dans une entrée de ZIP ouverte."""

    def __init__(self, sortie):
        self.sortie, self.morceaux, self.taille = sortie, [], 0

    def ecrire(self, t):
        self.morceaux.append(t)
        self.taille += len(t)
        if self.taille > _MORCEAU * 16:
            self.vider()

    def vider(self):
        if self.morceaux:
            self.sortie.write("".join(self.morceaux).encode("utf-8"))
        self.morceaux, self.taille = [], 0


def _textes(valeurs):
    return np.array([nombre(float(v)) for v in valeurs], dtype=object)


def ecrire_3mf(chemin, titre, c, p, vignette=None, limite=M.TRIANGLES_MAX, progress=None):
    """Écrit le 3MF de la carte ``c`` ; ``vignette`` : PNG facultatif (octets)."""
    pieces = plan(c, p)
    infos, ids = [], []
    triangles = 0
    with zipfile.ZipFile(chemin, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("[Content_Types].xml", "\n".join(x for x in (
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
            '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>',
            '<Default Extension="config" ContentType="text/xml"/>',
            '<Default Extension="png" ContentType="image/png"/>' if vignette is not None else None,
            "</Types>") if x is not None))
        z.writestr("_rels/.rels", "\n".join(x for x in (
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
            f'<Relationship Target="/{MODELE}" Id="rel0" Type="{REL_MODEL}"/>',
            f'<Relationship Target="/{VIGNETTE}" Id="rel1" Type="{REL_VIGNETTE}"/>' if vignette is not None else None,
            "</Relationships>") if x is not None))
        with z.open(MODELE, "w") as sortie:
            w = _Flux(sortie)
            w.ecrire(f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="fr-FR" xmlns="{NS_CORE}">\n')
            w.ecrire(f'<metadata name="Title">{echapper(titre)}</metadata>\n'
                     f'<metadata name="Application">{echapper(APPLICATION)}</metadata>\n')
            w.ecrire('<resources>\n<basematerials id="1">')
            for t in c.couleurs:
                w.ecrire(f'<base name="{echapper(t.nom)}" displaycolor="{couleur_3mf(t.hex)}"/>')
            w.ecrire("</basematerials>\n")
            xs = _textes(((c.l - np.arange(c.l + 1)) if p.face_dessous else np.arange(c.l + 1)) * c.pas_x)
            ys = _textes((c.h - np.arange(c.h + 1)) * c.pas_y)
            for n, pp in enumerate(pieces):
                if progress:
                    progress(n / len(pieces), f"Pièce « {pp.nom} »")
                m = mailler_piece(c, p, pp, limite - triangles)
                if not len(m.triangles):
                    continue
                triangles += len(m.triangles)
                id_ = 2 + len(ids)
                ids.append(id_)
                w.ecrire(f'<object id="{id_}" type="model" name="{echapper(pp.nom)}" pid="1" pindex="{pp.couleur}">'
                         "<mesh><vertices>")
                zs = np.array([nombre(pp.z0), nombre(pp.z1)], dtype=object)
                for d in range(0, len(m.u), _MORCEAU):
                    sl = slice(d, d + _MORCEAU)
                    w.ecrire("".join(f'<vertex x="{x}" y="{y}" z="{zz}"/>'
                                     for x, y, zz in zip(xs[m.u[sl]], ys[m.ligne[sl]], zs[m.niveau[sl]])))
                w.ecrire("</vertices><triangles>")
                for d in range(0, len(m.triangles), _MORCEAU):
                    w.ecrire("".join(f'<triangle v1="{a}" v2="{b}" v3="{cc}"/>'
                                     for a, b, cc in m.triangles[d:d + _MORCEAU].tolist()))
                w.ecrire("</triangles></mesh></object>\n")
                pixels = c.pixels_pleins if pp.valeur < 0 else c.comptes[pp.valeur]
                volume = pixels * c.pas_x * c.pas_y * (pp.z1 - pp.z0)
                infos.append(InfoPiece(pp.nom, pp.couleur, c.couleurs[pp.couleur].hex, pixels, volume,
                                       volume / 1000.0 * K.DENSITE_PLA, pp.valeur < 0))
                del m
            assemblage = 2 + len(ids)
            w.ecrire(f'<object id="{assemblage}" type="model" name="{echapper(titre)}"><components>')
            for id_ in ids:
                w.ecrire(f'<component objectid="{id_}"/>')
            w.ecrire(f'</components></object>\n</resources>\n<build><item objectid="{assemblage}"/></build>\n</model>\n')
            w.vider()
        r = ['<?xml version="1.0" encoding="UTF-8"?>\n<config>\n', f'  <object id="{assemblage}">\n',
             f'    <metadata key="name" value="{echapper(titre)}"/>\n',
             f'    <metadata key="extruder" value="{infos[0].couleur + 1}"/>\n']
        for k, info in enumerate(infos):
            r.append(f'    <part id="{ids[k]}" subtype="normal_part">\n')
            r.append(f'      <metadata key="name" value="{echapper(info.nom)}"/>\n')
            r.append(f'      <metadata key="matrix" value="{_IDENTITE}"/>\n')
            r.append(f'      <metadata key="extruder" value="{info.couleur + 1}"/>\n    </part>\n')
        r.append("  </object>\n</config>")
        z.writestr(REGLAGES, "".join(r))
        if vignette is not None:
            z.writestr(VIGNETTE, vignette)
    return Bilan(infos, triangles, p.base_mm + p.couche_mm)


# ================================================================================================ STL et OBJ
_STL = np.dtype([("n", "<f4", (3,)), ("v", "<f4", (3, 3)), ("attr", "<u2")])


def _stl_octets(titre, sommets, tris):
    entete = re.sub(r"[^\x20-\x7E]", "_", f"{SIGNATURE} - {titre}").encode("ascii")[:80].ljust(80, b"\0")
    a, b, d = sommets[tris[:, 0]], sommets[tris[:, 1]], sommets[tris[:, 2]]
    nrm = np.cross(b - a, d - a)
    lg = np.sqrt((nrm * nrm).sum(axis=1))
    nrm = np.where(lg[:, None] > 0, nrm / np.where(lg > 0, lg, 1.0)[:, None], nrm)
    rec = np.zeros(len(tris), _STL)
    rec["n"] = nrm
    rec["v"] = np.stack((a, b, d), axis=1)
    return entete + np.uint32(len(tris)).tobytes() + rec.tobytes()


def _lisez_moi(titre, lignes, conseil):
    return "\r\n".join([f"{titre} — {SIGNATURE}", ""] + list(lignes) + ["", conseil])


def _pieces(c, p, progress):
    pieces = plan(c, p)
    for i, pp in enumerate(pieces):
        if progress:
            progress(i / len(pieces), f"Pièce « {pp.nom} »")
        yield pp, c.couleurs[pp.couleur], mailler_piece(c, p, pp)


def ecrire_stl_couleurs(chemin, titre, c, p, progress=None):
    """ZIP d'un STL binaire par pièce et d'un LISEZMOI ; rend le nombre total de triangles."""
    total, lignes, pris = 0, [], set()
    with zipfile.ZipFile(chemin, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for i, (pp, teinte, m) in enumerate(_pieces(c, p, progress)):
            if not len(m.triangles):
                continue
            nom = f"{i + 1:02d}-{nom_piece(pp.nom)}.stl"
            if nom.lower() in pris:
                nom = f"{i + 1:02d}-{nom_piece(pp.nom)}-{i}.stl"
            pris.add(nom.lower())
            z.writestr(nom, _stl_octets(pp.nom, m.sommets, m.triangles))
            total += len(m.triangles)
            lignes.append(f"{nom} : {teinte.nom} {teinte.hex}")
        z.writestr("LISEZMOI.txt", _lisez_moi(titre, lignes, "Importe tous les STL en une fois comme pièces d'un même objet "
                                              "(ils se superposent exactement), puis attribue à chacun le filament de sa couleur."))
    return total


def ecrire_stl_unique(chemin, titre, c, p, progress=None):
    """Toutes les pièces dans un seul STL binaire ; rend le nombre de triangles."""
    sommets, tris, decal = [], [], 0
    for _, _, m in _pieces(c, p, progress):
        sommets.append(m.sommets)
        tris.append(m.triangles + decal)
        decal += len(m.sommets)
    v, t = np.concatenate(sommets), np.concatenate(tris)
    with open(chemin, "wb") as f:
        f.write(_stl_octets(titre, v, t))
    return len(t)


def ecrire_obj(chemin, titre, c, p, progress=None):
    """ZIP d'un OBJ (un objet par pièce), de son MTL (une matière par couleur) et d'un LISEZMOI ; rend les triangles."""
    pieces = plan(c, p)
    hexes = []
    for pp in pieces:
        h = C.normaliser_hex(c.couleurs[pp.couleur].hex) or "#808080"
        if h not in hexes:
            hexes.append(h)
    total = 0
    with zipfile.ZipFile(chemin, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        mtl = [f"# {titre} — {SIGNATURE}\r\n"]
        for k, h in enumerate(hexes):
            r, g, b = C.rgb(h)
            mtl.append(f"newmtl couleur_{k + 1}\r\nKd {r / 255:.4f} {g / 255:.4f} {b / 255:.4f}\r\n"
                       "Ka 0 0 0\r\nKs 0 0 0\r\nd 1\r\nillum 1\r\n\r\n")
        z.writestr("modele.mtl", "".join(mtl))
        with z.open("modele.obj", "w") as sortie:
            w = _Flux(sortie)
            w.ecrire(f"# {titre} — {SIGNATURE} (mm)\r\nmtllib modele.mtl\r\n")
            decal = 0
            for i, pp in enumerate(pieces):     # une pièce en mémoire à la fois
                if progress:
                    progress(i / len(pieces), f"Pièce « {pp.nom} »")
                m = mailler_piece(c, p, pp)
                if not len(m.triangles):
                    continue
                h = C.normaliser_hex(c.couleurs[pp.couleur].hex) or "#808080"
                w.ecrire(f"o {nom_piece(pp.nom).replace(' ', '_')}\r\nusemtl couleur_{hexes.index(h) + 1}\r\n")
                for d in range(0, len(m.sommets), _MORCEAU):
                    w.ecrire("".join(f"v {nombre_obj(x)} {nombre_obj(y)} {nombre_obj(zz)}\r\n"
                                     for x, y, zz in m.sommets[d:d + _MORCEAU].tolist()))
                for d in range(0, len(m.triangles), _MORCEAU):
                    w.ecrire("".join(f"f {a + 1 + decal} {b + 1 + decal} {cc + 1 + decal}\r\n"
                                     for a, b, cc in m.triangles[d:d + _MORCEAU].tolist()))
                decal += len(m.sommets)
                total += len(m.triangles)
            w.vider()
        z.writestr("LISEZMOI.txt", _lisez_moi(titre, [f"{pp.nom} : {c.couleurs[pp.couleur].nom} {c.couleurs[pp.couleur].hex}"
                                                      for pp in pieces],
                                              "Ouvre modele.obj (garde modele.mtl à côté) : chaque objet a sa couleur."))
    return total
