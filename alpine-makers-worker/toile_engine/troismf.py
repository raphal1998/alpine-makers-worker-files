# -*- coding: utf-8 -*-
"""Écriture du 3MF multi-pièces d'Alpine Toile Creator (bibliothèque standard + numpy, aucune dépendance 3MF).

Paquet zip (OPC) : ``[Content_Types].xml``, ``_rels/.rels``, ``3D/3dmodel.model`` (3MF Core, millimètre) et
``Metadata/model_settings.config`` (format Bambu Studio / OrcaSlicer). Une pièce = un objet maillage portant sa
couleur (``basematerials`` + ``displaycolor``) ; l'objet « toile » les assemble par composants, et le fichier de
réglages attribue à chaque pièce le filament N° index + 1 pour que la toile s'ouvre directement en multicouleur.
Une vignette PNG facultative (``Metadata/thumbnail.png``) sert d'aperçu aux explorateurs de fichiers.
"""
from __future__ import annotations

import zipfile
from xml.sax.saxutils import escape, quoteattr

import numpy as np

NS_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
REL_MODEL = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
REL_THUMBNAIL = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"
MODEL_PATH = "3D/3dmodel.model"
SETTINGS_PATH = "Metadata/model_settings.config"
THUMBNAIL_PATH = "Metadata/thumbnail.png"
APPLICATION = "Alpine Toile Creator"
# Lignes écrites par paquets : un modèle de plusieurs millions de triangles ne passe jamais en entier en mémoire.
PAQUET = 50_000
IDENTITE_BAMBU = "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"


def _nombre(valeurs):
    """Coordonnées en texte compact (6 décimales au plus) : les mêmes valeurs donnent toujours le même texte."""
    return [format(float(valeur), ".6f").rstrip("0").rstrip(".") or "0" for valeur in valeurs]


def _couleur(hex_value):
    return "#" + hex_value.strip().lstrip("#").upper()[:6] + "FF"


def _ecrire_maillage(sortie, sommets, triangles):
    sortie.write(b"<mesh><vertices>")
    for debut in range(0, len(sommets), PAQUET):
        bloc = sommets[debut:debut + PAQUET]
        xs, ys, zs = _nombre(bloc[:, 0]), _nombre(bloc[:, 1]), _nombre(bloc[:, 2])
        sortie.write("".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in zip(xs, ys, zs)).encode("ascii"))
    sortie.write(b"</vertices><triangles>")
    for debut in range(0, len(triangles), PAQUET):
        bloc = np.asarray(triangles[debut:debut + PAQUET], np.int64).tolist()
        sortie.write("".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in bloc).encode("ascii"))
    sortie.write(b"</triangles></mesh>")


def _content_types(vignette):
    lignes = ['<?xml version="1.0" encoding="UTF-8"?>',
              '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
              '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>',
              '<Default Extension="config" ContentType="text/xml"/>']
    if vignette:
        lignes.append('<Default Extension="png" ContentType="image/png"/>')
    lignes.append("</Types>")
    return "\n".join(lignes)


def _rels(vignette):
    lignes = ['<?xml version="1.0" encoding="UTF-8"?>',
              '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
              f'<Relationship Target="/{MODEL_PATH}" Id="rel0" Type="{REL_MODEL}"/>']
    if vignette:
        lignes.append(f'<Relationship Target="/{THUMBNAIL_PATH}" Id="rel1" Type="{REL_THUMBNAIL}"/>')
    lignes.append("</Relationships>")
    return "\n".join(lignes)


def _model_settings(assemblage, nom, pieces, identifiants):
    """Réglages Bambu Studio / OrcaSlicer : un objet, une partie par pièce, filament N° (index de couleur + 1)."""
    lignes = ['<?xml version="1.0" encoding="UTF-8"?>', "<config>", f'  <object id="{assemblage}">',
              f'    <metadata key="name" value={quoteattr(nom)}/>',
              f'    <metadata key="extruder" value="{int(pieces[0].couleur) + 1}"/>']
    for piece, identifiant in zip(pieces, identifiants):
        lignes += [f'    <part id="{identifiant}" subtype="normal_part">',
                   f'      <metadata key="name" value={quoteattr(piece.nom)}/>',
                   f'      <metadata key="matrix" value="{IDENTITE_BAMBU}"/>',
                   f'      <metadata key="extruder" value="{int(piece.couleur) + 1}"/>',
                   "    </part>"]
    lignes += ["  </object>", "</config>"]
    return "\n".join(lignes)


def ecrire(chemin, toile, couleurs, nom="Toile", vignette_png=None):
    """Écrit le 3MF de ``toile`` (mesh.Toile) ; ``couleurs`` = liste {hex, nom} du job. Renvoie le nombre d'objets
    maillage écrits (une par pièce)."""
    pieces = [piece for piece in toile.pieces if len(piece.triangles)]
    if not pieces:
        raise ValueError("Aucune pièce à écrire dans le 3MF.")
    materiaux = 1
    identifiants = list(range(2, 2 + len(pieces)))
    assemblage = 2 + len(pieces)
    with zipfile.ZipFile(chemin, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as paquet:
        paquet.writestr("[Content_Types].xml", _content_types(bool(vignette_png)))
        paquet.writestr("_rels/.rels", _rels(bool(vignette_png)))
        with paquet.open(MODEL_PATH, "w", force_zip64=True) as sortie:
            sortie.write(('<?xml version="1.0" encoding="UTF-8"?>\n'
                          f'<model unit="millimeter" xml:lang="fr-FR" xmlns="{NS_CORE}">\n'
                          f'<metadata name="Title">{escape(nom)}</metadata>\n'
                          f'<metadata name="Application">{APPLICATION}</metadata>\n'
                          '<resources>\n'
                          f'<basematerials id="{materiaux}">').encode("utf-8"))
            for couleur in couleurs:
                sortie.write(f'<base name={quoteattr(couleur["nom"])} displaycolor="{_couleur(couleur["hex"])}"/>'.encode("utf-8"))
            sortie.write(b"</basematerials>\n")
            for piece, identifiant in zip(pieces, identifiants):
                sortie.write(f'<object id="{identifiant}" type="model" name={quoteattr(piece.nom)} '
                             f'pid="{materiaux}" pindex="{int(piece.couleur)}">'.encode("utf-8"))
                _ecrire_maillage(sortie, piece.sommets, piece.triangles)
                sortie.write(b"</object>\n")
            sortie.write(f'<object id="{assemblage}" type="model" name={quoteattr(nom)}><components>'.encode("utf-8"))
            for identifiant in identifiants:
                sortie.write(f'<component objectid="{identifiant}"/>'.encode("ascii"))
            sortie.write(f'</components></object>\n</resources>\n<build><item objectid="{assemblage}"/></build>\n</model>\n'.encode("ascii"))
        paquet.writestr(SETTINGS_PATH, _model_settings(assemblage, nom, pieces, identifiants))
        if vignette_png:
            paquet.writestr(THUMBNAIL_PATH, vignette_png)
    return len(pieces)
