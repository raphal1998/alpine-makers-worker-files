# -*- coding: utf-8 -*-
"""Fichiers du dessin d'enfant (formats d'Alpine Gift, FormatModele) : 3MF multicolore (TroisMfPieces, une pièce par
couleur, filament de chaque pièce dans Metadata/model_settings.config), STL unique (le solide fermé), STL par couleur
(ZIP) et OBJ + MTL (ZIP). Les pièces gardent les mêmes coordonnées (mm) dans tous les formats."""
import math
import re
import unicodedata
import zipfile

from .maillage import stl_octets

NS_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
REL_MODEL = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
MODELE = "3D/3dmodel.model"
REGLAGES = "Metadata/model_settings.config"
APPLICATION = "Alpine Makers — Dessin d'enfant"
SIGNATURE = "Alpine Makers"
_IDENTITE = "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"

FORMATS = {
    "3mf": ("3MF multicolore", ".3mf", "Bambu Studio, OrcaSlicer, Creality Print : chaque couleur arrive sur son filament."),
    "stl_unique": ("STL unique", ".stl", "Tout le modèle en une seule pièce : impression d'une seule couleur, ou à peindre."),
    "stl_couleurs": ("STL par couleur (ZIP)", "-stl.zip",
                     "Un STL par couleur : importe-les ensemble dans ton trancheur (PrusaSlicer, Cura…), puis un filament par pièce."),
    "obj": ("OBJ avec couleurs (ZIP)", "-obj.zip",
            "OBJ + MTL : couleurs gardées, pour Blender, PrusaSlicer, Bambu Studio et la plupart des logiciels 3D."),
}


def echapper(t):
    return (str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
            .replace("'", "&apos;"))


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
    """Nom de fichier sûr [a-z0-9-] (IndexCreations.nomSur)."""
    sans = "".join(ch for ch in unicodedata.normalize("NFD", titre) if not unicodedata.combining(ch))
    return re.sub(r"[^A-Za-z0-9]+", "-", sans).strip("-")[:40].strip("-").lower() or "dessin"


def nom_piece(nom):
    """ExportModele.nomSur : nom de pièce sûr dans un ZIP."""
    t = re.sub(r"[^A-Za-z0-9À-ÿ ._+-]", " ", nom)
    return re.sub(r"\s+", " ", t).strip()[:60] or "piece"


def _tableaux(m):
    return m.sommets().astype(float).tolist(), m.triangles().tolist()


def ecrire_3mf(chemin, titre, teintes, pieces):
    """3MF de pièces quelconques : ``teintes`` [(nom, #RRVVBB)], ``pieces`` [(nom, maillage)] (filament i pour la
    pièce i). Rend le nombre de triangles."""
    teintes = teintes[:max(1, len(pieces))]
    ids, infos, total = [], [], 0
    with zipfile.ZipFile(chemin, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("[Content_Types].xml", "\n".join((
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
            '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>',
            '<Default Extension="config" ContentType="text/xml"/>',
            "</Types>")))
        z.writestr("_rels/.rels", "\n".join((
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
            f'<Relationship Target="/{MODELE}" Id="rel0" Type="{REL_MODEL}"/>',
            "</Relationships>")))
        t = [f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="fr-FR" xmlns="{NS_CORE}">\n',
             f'<metadata name="Title">{echapper(titre)}</metadata>\n<metadata name="Application">{echapper(APPLICATION)}</metadata>\n',
             '<resources>\n<basematerials id="1">']
        for nom, hexa in teintes:
            t.append(f'<base name="{echapper(nom)}" displaycolor="{hexa.upper()}FF"/>')
        t.append("</basematerials>\n")
        for n, (nom, m) in enumerate(pieces):
            sommets, tris = _tableaux(m)
            if not tris:
                continue
            filament = min(n, len(teintes) - 1)
            id_ = 2 + len(ids)
            ids.append(id_)
            t.append(f'<object id="{id_}" type="model" name="{echapper(nom)}" pid="1" pindex="{filament}"><mesh><vertices>')
            t.append("".join(f'<vertex x="{nombre(x)}" y="{nombre(y)}" z="{nombre(zz)}"/>' for x, y, zz in sommets))
            t.append("</vertices><triangles>")
            t.append("".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris))
            t.append("</triangles></mesh></object>\n")
            infos.append((nom, filament))
            total += len(tris)
        if not ids:
            raise ValueError("Rien à imprimer.")
        assemblage = 2 + len(ids)
        t.append(f'<object id="{assemblage}" type="model" name="{echapper(titre)}"><components>')
        t.extend(f'<component objectid="{i}"/>' for i in ids)
        t.append(f'</components></object>\n</resources>\n<build><item objectid="{assemblage}"/></build>\n</model>\n')
        z.writestr(MODELE, "".join(t).encode("utf-8"))
        r = ['<?xml version="1.0" encoding="UTF-8"?>\n<config>\n', f'  <object id="{assemblage}">\n',
             f'    <metadata key="name" value="{echapper(titre)}"/>\n',
             f'    <metadata key="extruder" value="{infos[0][1] + 1}"/>\n']
        for k, (nom, filament) in enumerate(infos):
            r.append(f'    <part id="{ids[k]}" subtype="normal_part">\n')
            r.append(f'      <metadata key="name" value="{echapper(nom)}"/>\n')
            r.append(f'      <metadata key="matrix" value="{_IDENTITE}"/>\n')
            r.append(f'      <metadata key="extruder" value="{filament + 1}"/>\n    </part>\n')
        r.append("  </object>\n</config>")
        z.writestr(REGLAGES, "".join(r))
    return total


def ecrire_stl(chemin, titre, m):
    """STL unique : le solide fermé complet."""
    chemin.write_bytes(stl_octets(m.sommets(), m.triangles(), titre))
    return m.nb_triangles


def _lisez_moi(titre, lignes, conseil):
    return "\r\n".join([f"{titre} — {SIGNATURE}", ""] + list(lignes) + ["", conseil])


def ecrire_stl_couleurs(chemin, titre, teintes, pieces):
    """ZIP d'un STL binaire par pièce et d'un LISEZMOI ; rend le nombre total de triangles."""
    total, lignes, pris = 0, [], set()
    with zipfile.ZipFile(chemin, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for i, (nom, m) in enumerate(pieces):
            if not m.nb_triangles:
                continue
            fichier = f"{i + 1:02d}-{nom_piece(nom)}.stl"
            if fichier.lower() in pris:
                fichier = f"{i + 1:02d}-{nom_piece(nom)}-{i}.stl"
            pris.add(fichier.lower())
            z.writestr(fichier, stl_octets(m.sommets(), m.triangles(), nom))
            total += m.nb_triangles
            teinte = teintes[min(i, len(teintes) - 1)]
            lignes.append(f"{fichier} : {teinte[0]} {teinte[1]}")
        z.writestr("LISEZMOI.txt", _lisez_moi(titre, lignes, "Importe tous les STL en une fois comme pièces d'un même objet "
                                              "(ils se superposent exactement), puis attribue à chacun le filament de sa couleur."))
    return total


def ecrire_obj(chemin, titre, teintes, pieces):
    """ZIP d'un OBJ (un objet par pièce), de son MTL (une matière par couleur) et d'un LISEZMOI ; rend les triangles."""
    couleurs = []
    for i in range(len(pieces)):
        h = teintes[min(i, len(teintes) - 1)][1].upper()
        if h not in couleurs:
            couleurs.append(h)
    total = 0
    with zipfile.ZipFile(chemin, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        mtl = [f"# {titre} — {SIGNATURE}\r\n"]
        for k, h in enumerate(couleurs):
            r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
            mtl.append(f"newmtl couleur_{k + 1}\r\nKd {r / 255:.4f} {g / 255:.4f} {b / 255:.4f}\r\n"
                       "Ka 0 0 0\r\nKs 0 0 0\r\nd 1\r\nillum 1\r\n\r\n")
        z.writestr("modele.mtl", "".join(mtl))
        t = [f"# {titre} — {SIGNATURE} (mm)\r\nmtllib modele.mtl\r\n"]
        decal = 0
        for i, (nom, m) in enumerate(pieces):
            sommets, tris = _tableaux(m)
            if not tris:
                continue
            h = teintes[min(i, len(teintes) - 1)][1].upper()
            t.append(f"o {nom_piece(nom).replace(' ', '_')}\r\nusemtl couleur_{couleurs.index(h) + 1}\r\n")
            t.append("".join(f"v {nombre_obj(x)} {nombre_obj(y)} {nombre_obj(zz)}\r\n" for x, y, zz in sommets))
            t.append("".join(f"f {a + 1 + decal} {b + 1 + decal} {c + 1 + decal}\r\n" for a, b, c in tris))
            decal += len(sommets)
            total += len(tris)
        z.writestr("modele.obj", "".join(t).encode("utf-8"))
        z.writestr("LISEZMOI.txt", _lisez_moi(titre, [f"{nom} : {teintes[min(i, len(teintes) - 1)][0]} "
                                                      f"{teintes[min(i, len(teintes) - 1)][1]}"
                                                      for i, (nom, _) in enumerate(pieces)],
                                              "Ouvre modele.obj (garde modele.mtl à côté) : chaque objet a sa couleur."))
    return total
