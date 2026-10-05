# -*- coding: utf-8 -*-
"""Réparation et remaillage de maillages pour l'impression 3D, exécutés sur le Worker (CPU).

Le module s'importe sans aucune dépendance : lecture d'un STL (ASCII ou binaire), d'un OBJ
ou d'un 3MF, analyse topologique et réparation limitée sont écrites en Python pur. Quand
l'environnement du module « Atelier 3D ouvert » (components/open3d-lab/.venv) fournit
trimesh, numpy et pymeshlab, ils sont chargés à l'exécution — jamais à l'import — pour la
réparation complète : bouchage des trous, normales cohérentes, décimation quadrique.

Vocabulaire du rapport d'analyse :
- ``boundary_edges`` : arêtes n'appartenant qu'à une seule face (bord d'un trou) ;
- ``holes`` : nombre de contours de bord distincts ;
- ``non_manifold_edges`` : arêtes partagées par plus de deux faces ;
- ``watertight`` : aucune arête de bord ni non-manifold ;
- ``winding_consistent`` : aucune arête parcourue deux fois dans le même sens ;
- ``volume_mm3`` : volume signé (négatif = normales vers l'intérieur), fiable seulement
  pour un maillage étanche.

Les faces dégénérées (sommets confondus ou aire nulle) sont exclues de la table d'arêtes ;
les faces dupliquées y restent et apparaissent donc comme non-manifold, puis disparaissent
après réparation.

Ligne de commande (dans l'environnement du Worker) :
    python mesh_repair_tool.py analyze piece.stl
    python mesh_repair_tool.py repair piece.stl piece_reparee.stl --target-faces 50000
    python mesh_repair_tool.py job job.json dossier_de_travail
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import re
import struct
import sys
import uuid
import xml.etree.ElementTree as ElementTree
import zipfile
from pathlib import Path

MAX_FILE_BYTES = 512 * 1024 * 1024
MERGE_DECIMALS = 6            # fusion des sommets confondus au micron près
AREA_EPSILON = 1e-9           # ‖(b−a)×(c−a)‖ minimal d'une face non dégénérée (mm²)
MIN_TARGET_FACES = 4
PYTHON_READ_FORMATS = frozenset({"stl", "obj", "3mf"})
PYTHON_WRITE_FORMATS = frozenset({"stl", "3mf"})
TRIMESH_WRITE_FORMATS = frozenset({"stl", "obj", "ply", "glb", "gltf", "off"})
_3MF_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_3MF_UNITS = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4, "foot": 304.8, "meter": 1000.0}
_3MF_CONTENT_TYPES = ('<?xml version="1.0" encoding="UTF-8"?>'
                      '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                      '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
_3MF_RELS = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
             'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
_SAFE_NAME = re.compile(r'^[^\\/:*?"<>|\x00-\x1f]{1,200}$')


class MeshRepairError(ValueError):
    """Erreur métier : fichier illisible, paramètre invalide ou format indisponible."""

    def __init__(self, message, code="MESH_REPAIR_ERROR"):
        super().__init__(message)
        self.code = code


def _optional(name):
    """Bibliothèque optionnelle chargée à l'exécution ; None si absente ou inutilisable."""
    try:
        return importlib.import_module(name)
    except Exception:
        return None


# --- géométrie élémentaire -------------------------------------------------------------

def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(a):
    return math.sqrt(_dot(a, a))


def _is_degenerate(a, b, c, pa, pb, pc):
    return a == b or b == c or a == c or _norm(_cross(_sub(pb, pa), _sub(pc, pa))) < AREA_EPSILON


def _count_loops(edges):
    """Nombre de composantes connexes d'une liste d'arêtes (contours de bord)."""
    parent = {}

    def find(node):
        while parent.setdefault(node, node) != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for u, v in edges:
        parent[find(u)] = find(v)
    return len({find(node) for node in list(parent)})


# --- lecture Python pur ------------------------------------------------------------------

def _extension(path):
    return Path(path).suffix.lower().lstrip(".")


def _check_input(path):
    path = Path(path)
    if not path.is_file():
        raise MeshRepairError("Fichier de maillage introuvable.", "FILE_NOT_FOUND")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise MeshRepairError("Fichier de maillage trop volumineux (limite 512 Mo).", "FILE_TOO_LARGE")
    return path


def _read_stl(data):
    """Triangles d'un STL binaire ou ASCII : liste de (p0, p1, p2)."""
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if 84 + 50 * count == len(data):
            return _read_stl_binary(data, count)
    if data[:80].lstrip().lower().startswith(b"solid"):
        return _read_stl_ascii(data)
    if len(data) >= 84 and (len(data) - 84) % 50 == 0:
        return _read_stl_binary(data, (len(data) - 84) // 50)
    raise MeshRepairError("Fichier STL illisible : ni binaire cohérent, ni ASCII.", "INVALID_MESH")


def _read_stl_binary(data, count):
    return [((r[3], r[4], r[5]), (r[6], r[7], r[8]), (r[9], r[10], r[11]))
            for r in struct.iter_unpack("<12fH", data[84:84 + 50 * count])]


def _read_stl_ascii(data):
    points = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0].lower() == "vertex":
            try:
                points.append((float(parts[1]), float(parts[2]), float(parts[3])))
            except ValueError as error:
                raise MeshRepairError("Coordonnée STL ASCII invalide.", "INVALID_MESH") from error
    if len(points) % 3:
        raise MeshRepairError("STL ASCII incomplet : nombre de sommets non multiple de trois.", "INVALID_MESH")
    return [(points[i], points[i + 1], points[i + 2]) for i in range(0, len(points), 3)]


def _read_obj(text):
    """Sommets et faces d'un OBJ (polygones triangulés en éventail, indices négatifs relatifs)."""
    vertices, faces = [], []
    try:
        for line in text.splitlines():
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "v" and len(parts) >= 4:
                vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
            elif parts[0] == "f" and len(parts) >= 4:
                ids = []
                for token in parts[1:]:
                    raw = int(token.split("/")[0])
                    ids.append(raw - 1 if raw > 0 else len(vertices) + raw)
                faces.extend((ids[0], ids[i], ids[i + 1]) for i in range(1, len(ids) - 1))
    except ValueError as error:
        raise MeshRepairError("Fichier OBJ invalide.", "INVALID_MESH") from error
    if any(index < 0 or index >= len(vertices) for face in faces for index in face):
        raise MeshRepairError("Fichier OBJ invalide : indice de sommet hors limites.", "INVALID_MESH")
    return vertices, faces


def _parse_3mf_transform(text):
    if text is None:
        return None
    values = [float(value) for value in text.split()]
    if len(values) != 12:
        raise MeshRepairError("Transformation 3MF invalide.", "INVALID_MESH")
    return values


def _apply_3mf_transform(m, p):
    return (p[0] * m[0] + p[1] * m[3] + p[2] * m[6] + m[9],
            p[0] * m[1] + p[1] * m[4] + p[2] * m[7] + m[10],
            p[0] * m[2] + p[1] * m[5] + p[2] * m[8] + m[11])


def _parse_3mf_model(data):
    """Objets (maillage ou composants) et éléments de construction d'un fichier .model."""
    root = ElementTree.fromstring(data)
    ns = root.tag[:root.tag.index("}") + 1] if root.tag.startswith("{") else ""
    objects = {}
    for element in root.iter(ns + "object"):
        vertices, triangles, components = [], [], []
        mesh = element.find(ns + "mesh")
        if mesh is not None:
            vertices = [(float(v.get("x")), float(v.get("y")), float(v.get("z"))) for v in mesh.iter(ns + "vertex")]
            triangles = [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3"))) for t in mesh.iter(ns + "triangle")]
            if any(index < 0 or index >= len(vertices) for triangle in triangles for index in triangle):
                raise MeshRepairError("Fichier 3MF invalide : indice de sommet hors limites.", "INVALID_MESH")
        group = element.find(ns + "components")
        if group is not None:
            for component in group.iter(ns + "component"):
                path = next((value for key, value in component.attrib.items() if key.endswith("}path") or key == "path"), None)
                components.append((component.get("objectid"), _parse_3mf_transform(component.get("transform")),
                                   path.lstrip("/") if path else None))
        objects[element.get("id")] = (vertices, triangles, components)
    items = [(item.get("objectid"), _parse_3mf_transform(item.get("transform"))) for item in root.iter(ns + "item")]
    return root.get("unit") or "millimeter", objects, items


def _read_3mf(path):
    """Sommets et faces d'un 3MF : objets du modèle racine, composants (y compris p:path) et transformations."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            root_name = _3mf_root_model(archive, names)
            cache = {}

            def model(name):
                if name not in cache:
                    if name not in names or archive.getinfo(name).file_size > MAX_FILE_BYTES:
                        raise MeshRepairError(f"Modèle 3MF « {name} » absent ou trop volumineux.", "INVALID_MESH")
                    cache[name] = _parse_3mf_model(archive.read(name))
                return cache[name]

            unit, objects, items = model(root_name)
            scale = _3MF_UNITS.get(unit, 1.0)
            vertices, faces = [], []

            def append(name, object_id, chain, depth):
                if depth > 8:
                    raise MeshRepairError("Composants 3MF imbriqués trop profondément.", "INVALID_MESH")
                entry = model(name)[1].get(object_id)
                if entry is None:
                    raise MeshRepairError(f"Objet 3MF « {object_id} » introuvable.", "INVALID_MESH")
                mesh_vertices, mesh_faces, components = entry
                if mesh_faces:
                    offset = len(vertices)
                    for point in mesh_vertices:
                        for transform in chain:
                            point = _apply_3mf_transform(transform, point)
                        vertices.append((point[0] * scale, point[1] * scale, point[2] * scale))
                    faces.extend((a + offset, b + offset, c + offset) for a, b, c in mesh_faces)
                for child_id, transform, child_path in components:
                    append(child_path or name, child_id, ([transform] if transform else []) + chain, depth + 1)

            for object_id, transform in (items or [(object_id, None) for object_id in objects]):
                append(root_name, object_id, [transform] if transform else [], 0)
    except MeshRepairError:
        raise
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError, ValueError, TypeError) as error:
        raise MeshRepairError("Fichier 3MF illisible.", "INVALID_MESH") from error
    return vertices, faces


def _3mf_root_model(archive, names):
    if "_rels/.rels" in names:
        try:
            for relation in ElementTree.fromstring(archive.read("_rels/.rels")).iter():
                if str(relation.get("Type") or "").endswith("/3dmodel") and relation.get("Target"):
                    target = relation.get("Target").lstrip("/")
                    if target in names:
                        return target
        except ElementTree.ParseError:
            pass
    if "3D/3dmodel.model" in names:
        return "3D/3dmodel.model"
    for name in names:
        if name.lower().endswith(".model"):
            return name
    raise MeshRepairError("Fichier 3MF sans modèle 3D.", "INVALID_MESH")


def _merge_vertices(triangles):
    """Indexation des triangles avec fusion des sommets confondus (arrondi au micron).

    Seuls les sommets référencés par une face sont conservés ; le premier point rencontré
    donne les coordonnées du sommet fusionné.
    """
    index, vertices, faces = {}, [], []
    for triangle in triangles:
        ids = []
        for point in triangle:
            key = (round(point[0], MERGE_DECIMALS), round(point[1], MERGE_DECIMALS), round(point[2], MERGE_DECIMALS))
            found = index.get(key)
            if found is None:
                found = index[key] = len(vertices)
                vertices.append((float(point[0]), float(point[1]), float(point[2])))
            ids.append(found)
        faces.append((ids[0], ids[1], ids[2]))
    return vertices, faces


def _load_indexed(path):
    """Maillage indexé (sommets, faces) lu sans dépendance : STL, OBJ ou 3MF."""
    path = _check_input(path)
    extension = _extension(path)
    if extension == "stl":
        triangles = _read_stl(path.read_bytes())
    elif extension == "obj":
        raw_vertices, raw_faces = _read_obj(path.read_text(encoding="utf-8", errors="replace"))
        triangles = ((raw_vertices[a], raw_vertices[b], raw_vertices[c]) for a, b, c in raw_faces)
    elif extension == "3mf":
        raw_vertices, raw_faces = _read_3mf(path)
        triangles = ((raw_vertices[a], raw_vertices[b], raw_vertices[c]) for a, b, c in raw_faces)
    else:
        raise MeshRepairError(f"Format « {extension or '?'} » illisible sans trimesh sur ce Worker.", "FORMAT_UNAVAILABLE")
    vertices, faces = _merge_vertices(triangles)
    if not faces:
        raise MeshRepairError("Le maillage ne contient aucune face.", "INVALID_MESH")
    return vertices, faces


# --- analyse -----------------------------------------------------------------------------

def _report(faces, vertices, kept, volume, low, high, degenerate, duplicate, boundary, non_manifold, holes, winding):
    low, high = [float(v) for v in low], [float(v) for v in high]
    return {
        "faces": int(faces), "vertices": int(vertices),
        "watertight": bool(kept and boundary == 0 and non_manifold == 0),
        "winding_consistent": bool(winding),
        "non_manifold_edges": int(non_manifold), "boundary_edges": int(boundary), "holes": int(holes),
        "volume_mm3": round(float(volume), 3),
        "bounds": {"min": [round(v, 4) for v in low], "max": [round(v, 4) for v in high],
                   "size": [round(high[i] - low[i], 4) for i in range(3)]},
        "duplicate_faces": int(duplicate), "degenerate_faces": int(degenerate),
    }


def _empty_analysis():
    return _report(0, 0, 0, 0.0, [0.0] * 3, [0.0] * 3, 0, 0, 0, 0, 0, True)


def _analyze_python(vertices, faces):
    """Analyse en Python pur d'un maillage indexé (sommets fusionnés au préalable)."""
    if not faces:
        return _empty_analysis()
    edges, seen, directed, referenced = {}, set(), set(), set()
    degenerate = duplicate = 0
    winding = True
    volume = 0.0
    for a, b, c in faces:
        pa, pb, pc = vertices[a], vertices[b], vertices[c]
        referenced.update((a, b, c))
        volume += _dot(pa, _cross(pb, pc))
        if _is_degenerate(a, b, c, pa, pb, pc):
            degenerate += 1
            continue
        key = tuple(sorted((a, b, c)))
        if key in seen:
            duplicate += 1
        else:
            seen.add(key)
            for edge in ((a, b), (b, c), (c, a)):
                if edge in directed:
                    winding = False
                directed.add(edge)
        for u, v in ((a, b), (b, c), (c, a)):
            edge = (u, v) if u < v else (v, u)
            edges[edge] = edges.get(edge, 0) + 1
    boundary = [edge for edge, count in edges.items() if count == 1]
    non_manifold = sum(1 for count in edges.values() if count > 2)
    points = [vertices[i] for i in referenced]
    low = [min(p[axis] for p in points) for axis in range(3)]
    high = [max(p[axis] for p in points) for axis in range(3)]
    return _report(len(faces), len(referenced), len(faces) - degenerate, volume / 6.0, low, high,
                   degenerate, duplicate, len(boundary), non_manifold, _count_loops(boundary), winding)


def _analyze_numpy(np, vertices, faces):
    """Même analyse, vectorisée avec numpy (Worker) ; la fusion des sommets y est refaite."""
    faces = np.asarray(faces, dtype=np.int64).reshape(-1, 3)
    if faces.size == 0:
        return _empty_analysis()
    vertices = np.asarray(vertices, dtype=np.float64).reshape(-1, 3)
    _, first, inverse = np.unique(np.round(vertices, MERGE_DECIMALS), axis=0, return_index=True, return_inverse=True)
    unique = vertices[first]
    faces = np.asarray(inverse).reshape(-1)[faces]
    referenced = unique[np.unique(faces)]
    tri = unique[faces]
    volume = float(np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum() / 6.0)
    area = np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
    degenerate = ((faces[:, 0] == faces[:, 1]) | (faces[:, 1] == faces[:, 2]) | (faces[:, 0] == faces[:, 2])
                  | (area < AREA_EPSILON))
    kept = faces[~degenerate]
    boundary_edges, non_manifold, duplicate, winding = [], 0, 0, True
    if len(kept):
        _, first_faces = np.unique(np.sort(kept, axis=1), axis=0, return_index=True)
        duplicate = int(len(kept) - len(first_faces))
        directed = kept[first_faces][:, [[0, 1], [1, 2], [2, 0]]].reshape(-1, 2)
        _, directed_counts = np.unique(directed, axis=0, return_counts=True)
        winding = bool(directed_counts.max() == 1)
        edges = np.sort(kept[:, [[0, 1], [1, 2], [2, 0]]].reshape(-1, 2), axis=1)
        unique_edges, counts = np.unique(edges, axis=0, return_counts=True)
        boundary_edges = unique_edges[counts == 1].tolist()
        non_manifold = int((counts > 2).sum())
    return _report(len(faces), len(referenced), int(len(kept)), volume, referenced.min(axis=0), referenced.max(axis=0),
                   int(degenerate.sum()), duplicate, len(boundary_edges), non_manifold, _count_loops(boundary_edges), winding)


def _analyze(vertices, faces):
    np = _optional("numpy")
    if np is not None:
        return _analyze_numpy(np, vertices, faces)
    if hasattr(vertices, "tolist"):
        vertices, faces = vertices.tolist(), faces.tolist()
    return _analyze_python(vertices, faces)


def _trimesh_load(trimesh, path):
    """Maillage trimesh d'un fichier : trimesh pour ses formats, lecteur Python pur sinon (3MF sans lxml)."""
    path = _check_input(path)
    extension = _extension(path)
    if extension != "stl" and extension in trimesh.available_formats():
        try:
            loaded = trimesh.load(str(path), file_type=extension, force="mesh", process=False)
        except Exception as error:
            raise MeshRepairError(f"trimesh ne peut pas lire ce fichier : {error}", "INVALID_MESH") from error
        if not isinstance(loaded, trimesh.Trimesh) or len(loaded.faces) == 0:
            raise MeshRepairError("Le maillage ne contient aucune face.", "INVALID_MESH")
        return loaded
    vertices, faces = _load_indexed(path)
    return trimesh.Trimesh(vertices=vertices, faces=faces, process=False)


def analyze_mesh(path):
    """Analyse un maillage : STL en Python pur ; OBJ/3MF/GLB via trimesh si disponible, sinon lecteur Python pur.

    Renvoie ``{faces, vertices, watertight, winding_consistent, non_manifold_edges, boundary_edges,
    holes, volume_mm3, bounds: {min, max, size}, duplicate_faces, degenerate_faces}``.
    """
    path = _check_input(path)
    trimesh = _optional("trimesh")
    if trimesh is not None and _extension(path) != "stl":
        mesh = _trimesh_load(trimesh, path)
        return _analyze(mesh.vertices, mesh.faces)
    return _analyze(*_load_indexed(path))


# --- réparation --------------------------------------------------------------------------

def _validate_options(target_faces, decimate_ratio):
    if target_faces is not None:
        if isinstance(target_faces, bool):
            raise MeshRepairError("Nombre de faces cible invalide.", "INVALID_PARAMETER")
        try:
            target_faces = int(target_faces)
        except (TypeError, ValueError) as error:
            raise MeshRepairError("Nombre de faces cible invalide.", "INVALID_PARAMETER") from error
        if target_faces < MIN_TARGET_FACES:
            raise MeshRepairError(f"Le nombre de faces cible doit être d'au moins {MIN_TARGET_FACES}.", "INVALID_PARAMETER")
    if decimate_ratio is not None:
        try:
            decimate_ratio = float(decimate_ratio)
        except (TypeError, ValueError) as error:
            raise MeshRepairError("Taux de décimation invalide.", "INVALID_PARAMETER") from error
        if not 0.0 < decimate_ratio < 1.0 or math.isnan(decimate_ratio):
            raise MeshRepairError("Le taux de décimation doit être compris entre 0 et 1 exclus.", "INVALID_PARAMETER")
    return target_faces, decimate_ratio


def _decimation_target(face_count, options):
    target = options["target_faces"]
    if target is None and options["decimate_ratio"] is not None:
        target = max(MIN_TARGET_FACES, int(face_count * options["decimate_ratio"]))
    if target is None or target >= face_count:
        return None
    return int(target)


def _repair_python(vertices, faces, options):
    """Réparation limitée sans bibliothèque : faces dégénérées et dupliquées, inversion globale."""
    actions, skipped, details = ["merge_vertices"], [], {}
    kept, seen = [], set()
    removed_degenerate = removed_duplicate = 0
    for a, b, c in faces:
        if options["remove_degenerate"] and _is_degenerate(a, b, c, vertices[a], vertices[b], vertices[c]):
            removed_degenerate += 1
            continue
        key = tuple(sorted((a, b, c)))
        if key in seen:
            removed_duplicate += 1
            continue
        seen.add(key)
        kept.append((a, b, c))
    if options["remove_degenerate"]:
        actions.append("remove_degenerate_faces")
        details["removed_degenerate_faces"] = removed_degenerate
    actions.append("remove_duplicate_faces")
    details["removed_duplicate_faces"] = removed_duplicate
    analysis = _analyze_python(vertices, kept)
    if options["fill_holes"] and analysis["boundary_edges"]:
        skipped.append({"action": "fill_holes", "reason": "Bouchage des trous indisponible sans trimesh sur ce Worker."})
    if options["fix_normals"]:
        if analysis["watertight"] and analysis["winding_consistent"]:
            actions.append("fix_normals")
            details["inverted_faces"] = analysis["volume_mm3"] < 0
            if details["inverted_faces"]:
                kept = [(a, c, b) for a, b, c in kept]
        else:
            skipped.append({"action": "fix_normals",
                            "reason": "Orientation des normales non corrigée sans trimesh (maillage ouvert ou faces retournées)."})
    if options["target_faces"] is not None or options["decimate_ratio"] is not None:
        skipped.append({"action": "decimate", "reason": "Décimation indisponible sans pymeshlab ni trimesh sur ce Worker."})
    vertices, kept = _merge_vertices((vertices[a], vertices[b], vertices[c]) for a, b, c in kept)
    return vertices, kept, "python", actions, skipped, details


def _decimate(trimesh, np, mesh, target):
    """Décimation quadrique : pymeshlab d'abord, trimesh (fast_simplification) sinon."""
    reasons = []
    pymeshlab = _optional("pymeshlab")
    if pymeshlab is not None:
        try:
            meshset = pymeshlab.MeshSet()
            meshset.add_mesh(pymeshlab.Mesh(vertex_matrix=np.asarray(mesh.vertices, dtype=np.float64),
                                            face_matrix=np.asarray(mesh.faces, dtype=np.int32)))
            meshset.meshing_decimation_quadric_edge_collapse(targetfacenum=int(target), preservetopology=True,
                                                             preservenormal=True, planarquadric=True)
            current = meshset.current_mesh()
            return trimesh.Trimesh(vertices=current.vertex_matrix(), faces=current.face_matrix(), process=False), "pymeshlab"
        except Exception as error:
            reasons.append(f"pymeshlab : {error}")
    try:
        return mesh.simplify_quadric_decimation(face_count=int(target)), "trimesh"
    except Exception as error:
        reasons.append(f"trimesh : {error}")
    return None, "Décimation indisponible sur ce Worker (" + " ; ".join(reasons) + ")."


def _repair_trimesh(trimesh, np, path, options):
    mesh = _trimesh_load(trimesh, path)
    actions, skipped, details = ["merge_vertices"], [], {}
    engine = "trimesh"
    mesh.merge_vertices()
    if options["remove_degenerate"]:
        mask = np.asarray(mesh.nondegenerate_faces(), dtype=bool)
        details["removed_degenerate_faces"] = int(len(mask) - mask.sum())
        mesh.update_faces(mask)
        actions.append("remove_degenerate_faces")
    mask = np.asarray(mesh.unique_faces(), dtype=bool)
    details["removed_duplicate_faces"] = int(len(mask) - mask.sum())
    mesh.update_faces(mask)
    mesh.remove_unreferenced_vertices()
    actions.append("remove_duplicate_faces")
    if options["fill_holes"] and not mesh.is_watertight:
        filled = bool(mesh.fill_holes())
        actions.append("fill_holes")
        details["watertight_after_fill"] = filled
        if not filled:
            details["fill_holes_note"] = "Trous restants trop grands pour le bouchage automatique de trimesh."
    if options["fix_normals"]:
        mesh.fix_normals()
        actions.append("fix_normals")
    target = _decimation_target(len(mesh.faces), options)
    if target is not None:
        decimated, outcome = _decimate(trimesh, np, mesh, target)
        if decimated is None:
            skipped.append({"action": "decimate", "reason": outcome})
        else:
            mesh, engine = decimated, outcome
            mesh.merge_vertices()
            mesh.update_faces(np.asarray(mesh.nondegenerate_faces(), dtype=bool))
            mesh.remove_unreferenced_vertices()
            actions.append("decimate")
            details["decimation"] = {"target_faces": target, "engine": outcome, "faces": int(len(mesh.faces))}
    elif options["target_faces"] is not None or options["decimate_ratio"] is not None:
        details["decimation"] = {"note": "Maillage déjà sous le nombre de faces cible : rien à décimer."}
    if len(mesh.faces) == 0:
        raise MeshRepairError("La réparation a supprimé toutes les faces du maillage.", "INVALID_MESH")
    return mesh, engine, actions, skipped, details


def _write_stl(path, vertices, faces):
    with Path(path).open("wb") as handle:
        handle.write(b"Alpine Makers mesh_repair_tool".ljust(80, b"\0"))
        handle.write(struct.pack("<I", len(faces)))
        for a, b, c in faces:
            pa, pb, pc = vertices[a], vertices[b], vertices[c]
            normal = _cross(_sub(pb, pa), _sub(pc, pa))
            length = _norm(normal)
            normal = (normal[0] / length, normal[1] / length, normal[2] / length) if length > 0 else (0.0, 0.0, 0.0)
            handle.write(struct.pack("<12fH", *normal, *pa, *pb, *pc, 0))


def _write_3mf(path, vertices, faces):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<model unit="millimeter" xml:lang="en-US" xmlns="{_3MF_CORE}">',
             '<resources><object id="1" type="model"><mesh><vertices>']
    lines.extend(f'<vertex x="{float(x)!r}" y="{float(y)!r}" z="{float(z)!r}"/>' for x, y, z in vertices)
    lines.append("</vertices><triangles>")
    lines.extend(f'<triangle v1="{int(a)}" v2="{int(b)}" v3="{int(c)}"/>' for a, b, c in faces)
    lines.append('</triangles></mesh></object></resources><build><item objectid="1"/></build></model>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _3MF_CONTENT_TYPES)
        archive.writestr("_rels/.rels", _3MF_RELS)
        archive.writestr("3D/3dmodel.model", "\n".join(lines))


def _write_output(output_path, vertices, faces, mesh=None):
    """Écriture atomique : fichier temporaire de même extension, puis os.replace."""
    output_path = Path(output_path)
    extension = _extension(output_path)
    if hasattr(vertices, "tolist"):
        vertices, faces = vertices.tolist(), faces.tolist()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.stem}.{uuid.uuid4().hex}.tmp{output_path.suffix}")
    try:
        if extension == "3mf":
            _write_3mf(temporary, vertices, faces)
        elif mesh is not None and extension in TRIMESH_WRITE_FORMATS:
            try:
                mesh.export(str(temporary), file_type=extension)
            except Exception as error:
                raise MeshRepairError(f"Export « {extension} » impossible sur ce Worker : {error}", "FORMAT_UNAVAILABLE") from error
        elif extension == "stl":
            _write_stl(temporary, vertices, faces)
        else:
            raise MeshRepairError(f"Format de sortie « {extension or '?'} » indisponible sans trimesh sur ce Worker.",
                                  "FORMAT_UNAVAILABLE")
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)


def repair_mesh(input_path, output_path, target_faces=None, fill_holes=True, remove_degenerate=True,
                fix_normals=True, decimate_ratio=None):
    """Répare un maillage et écrit le résultat ; renvoie ``{before, after, actions, skipped, engine, details, output}``.

    Avec trimesh : fusion des sommets, faces dégénérées/dupliquées, bouchage des trous, normales,
    puis décimation quadrique (pymeshlab, sinon trimesh) si ``target_faces`` ou ``decimate_ratio``.
    Sans bibliothèque : mode Python pur limité (faces dégénérées/dupliquées, inversion globale
    d'un maillage retourné, STL binaire ou 3MF) ; les étapes non réalisées sont listées dans ``skipped``.
    """
    input_path = _check_input(input_path)
    output_path = Path(output_path)
    extension = _extension(output_path)
    target_faces, decimate_ratio = _validate_options(target_faces, decimate_ratio)
    options = {"target_faces": target_faces, "decimate_ratio": decimate_ratio, "fill_holes": bool(fill_holes),
               "remove_degenerate": bool(remove_degenerate), "fix_normals": bool(fix_normals)}
    trimesh = _optional("trimesh")
    np = _optional("numpy") if trimesh is not None else None
    if trimesh is not None and np is not None:
        if extension not in TRIMESH_WRITE_FORMATS | PYTHON_WRITE_FORMATS:
            raise MeshRepairError(f"Format de sortie « {extension or '?'} » non pris en charge.", "FORMAT_UNAVAILABLE")
        before = analyze_mesh(input_path)
        mesh, engine, actions, skipped, details = _repair_trimesh(trimesh, np, input_path, options)
        vertices, faces = mesh.vertices, mesh.faces
        _write_output(output_path, vertices, faces, mesh=mesh)
    else:
        if extension not in PYTHON_WRITE_FORMATS:
            raise MeshRepairError(f"Format de sortie « {extension or '?'} » indisponible sans trimesh sur ce Worker.",
                                  "FORMAT_UNAVAILABLE")
        vertices, faces = _load_indexed(input_path)
        before = _analyze(vertices, faces)
        vertices, faces, engine, actions, skipped, details = _repair_python(vertices, faces, options)
        _write_output(output_path, vertices, faces)
    return {"engine": engine, "before": before, "after": _analyze(vertices, faces), "actions": actions,
            "skipped": skipped, "details": details, "output": str(output_path)}


# --- exécution d'un job Worker ------------------------------------------------------------

def _as_bool(value, default):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "oui"}:
        return True
    if text in {"", "0", "false", "no", "off", "non"}:
        return False
    raise MeshRepairError(f"Valeur booléenne invalide : « {value} ».", "INVALID_PARAMETER")


def _safe_basename(value):
    name = str(value or "").strip()
    if not _SAFE_NAME.fullmatch(name) or name in {".", ".."} or name.endswith((" ", ".")):
        raise MeshRepairError("Nom de fichier de sortie non autorisé.", "INVALID_PARAMETER")
    return name


def run_from_job(job, workdir):
    """Exécute un job de réparation dans ``workdir`` (même forme que les runners du Worker).

    ``job['parameters']`` : ``input_files`` (liste de ``{"path": ...}`` sous ``workdir``) ou
    ``input_file`` ; ``output_format`` (stl par défaut, 3mf, ou obj/ply/glb/off avec trimesh) ;
    ``output_name`` (un seul fichier) ; ``target_faces``, ``decimate_ratio``, ``fill_holes``,
    ``remove_degenerate``, ``fix_normals``. Renvoie ``{outputs: [chemins], artifacts: [noms], report}`` ;
    ``report`` est le rapport du fichier unique, ou ``{"files": [...]}`` pour plusieurs fichiers.
    """
    workdir = Path(workdir).resolve()
    if not workdir.is_dir():
        raise MeshRepairError("Dossier de travail du job introuvable.", "INVALID_PARAMETER")
    parameters = job.get("parameters") if isinstance(job, dict) and isinstance(job.get("parameters"), dict) else {}
    inputs = parameters.get("input_files") if isinstance(parameters.get("input_files"), list) else []
    single = parameters.get("input_file") if isinstance(parameters.get("input_file"), dict) else None
    sources = [item for item in (inputs or ([single] if single else [])) if isinstance(item, dict)]
    if not sources:
        raise MeshRepairError("Aucun maillage transmis au Worker.", "INVALID_PARAMETER")
    output_format = str(parameters.get("output_format") or "").lower().lstrip(".")
    options = {"target_faces": parameters.get("target_faces"), "decimate_ratio": parameters.get("decimate_ratio"),
               "fill_holes": _as_bool(parameters.get("fill_holes"), True),
               "remove_degenerate": _as_bool(parameters.get("remove_degenerate"), True),
               "fix_normals": _as_bool(parameters.get("fix_normals"), True)}
    outputs, artifacts, reports, used = [], [], [], set()
    for item in sources:
        source = Path(str(item.get("path") or "")).resolve()
        if not source.is_file() or workdir not in source.parents:
            raise MeshRepairError("Fichier source absent ou hors du dossier du job.", "INVALID_PARAMETER")
        extension = output_format or _extension(source) or "stl"
        if len(sources) == 1 and parameters.get("output_name"):
            name = _safe_basename(parameters.get("output_name"))
        else:
            name = _safe_basename(f"{source.stem}_repaired.{extension}")
        if not name.lower().endswith(f".{extension}"):
            name += f".{extension}"
        base, counter = name, 2
        while name.lower() in used:
            name, counter = f"{Path(base).stem}_{counter}{Path(base).suffix}", counter + 1
        used.add(name.lower())
        destination = workdir / name
        if destination.resolve().parent != workdir:
            raise MeshRepairError("Destination de sortie non autorisée.", "INVALID_PARAMETER")
        reports.append(repair_mesh(source, destination, **options))
        outputs.append(str(destination))
        artifacts.append(destination.name)
    return {"outputs": outputs, "artifacts": artifacts, "report": reports[0] if len(reports) == 1 else {"files": reports}}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Réparation de maillages Alpine Makers (Worker, CPU).")
    commands = parser.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser("analyze", help="Analyse un maillage et affiche le rapport JSON.")
    analyze.add_argument("path")
    repair = commands.add_parser("repair", help="Répare un maillage vers un nouveau fichier.")
    repair.add_argument("input")
    repair.add_argument("output")
    repair.add_argument("--target-faces", type=int, default=None)
    repair.add_argument("--decimate-ratio", type=float, default=None)
    repair.add_argument("--no-fill-holes", action="store_true")
    repair.add_argument("--keep-degenerate", action="store_true")
    repair.add_argument("--no-fix-normals", action="store_true")
    job = commands.add_parser("job", help="Exécute un job Worker décrit par un fichier JSON.")
    job.add_argument("job_file")
    job.add_argument("workdir")
    args = parser.parse_args(argv)
    try:
        if args.command == "analyze":
            result = analyze_mesh(args.path)
        elif args.command == "repair":
            result = repair_mesh(args.input, args.output, target_faces=args.target_faces, decimate_ratio=args.decimate_ratio,
                                 fill_holes=not args.no_fill_holes, remove_degenerate=not args.keep_degenerate,
                                 fix_normals=not args.no_fix_normals)
        else:
            result = run_from_job(json.loads(Path(args.job_file).read_text(encoding="utf-8")), args.workdir)
    except MeshRepairError as error:
        print(json.dumps({"ok": False, "error": str(error), "code": error.code}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
