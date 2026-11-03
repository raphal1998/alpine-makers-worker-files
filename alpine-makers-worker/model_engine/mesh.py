"""Maillages du trancheur Alpine Model Engine (spécification §4.1).

Lecture STL (binaire et ASCII, tolérante), OBJ et 3MF (Core 2015/02, composants, extension production ``p:path``,
``transform`` à 12 flottants, ``build/item``, ``Metadata/model_settings.config``, ``paint_color``), écriture STL
binaire / ASCII et conteneur 3MF ; classe ``Mesh`` : transformations, boîte, volume signé, étanchéité, nettoyage,
fusion des sommets, orientation des faces par propagation.

Code Alpine Makers : numpy + bibliothèque standard seulement ; seule la structure publique des formats est
implémentée. Coordonnées en millimètres (float64), faces en indices int64.
"""
from __future__ import annotations

import io
import math
import posixpath
import re
import struct
import unicodedata
import zipfile
import zlib
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

import numpy as np

try:
    import lzma
    _LZMA_ERRORS = (lzma.LZMAError,)
except ImportError:                      # Python construit sans liblzma (certains Workers Linux)
    _LZMA_ERRORS = ()

VOLUME_TYPES = ("normal", "modifier", "support_blocker", "support_enforcer")
MAX_EXTRUDERS = 16
MAX_FILE_BYTES = 512 * 1024 * 1024       # fichier d'entrée (STL, OBJ, archive 3MF)
MAX_ZIP_BYTES = 512 * 1024 * 1024        # total décompressé d'un 3MF
MAX_XML_BYTES = 256 * 1024 * 1024        # une pièce XML d'un 3MF
MAX_COMPONENT_DEPTH = 8
MAX_PARTS = 4096                         # instances de maillage résolues par 3MF (composants en éventail bornés)
MAX_TRIANGLES = 20_000_000               # triangles cumulés des instances d'un 3MF
STL_HEADER = b"Alpine Model Engine binary STL"

NS_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
NS_PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
NS_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
REL_MODEL = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
UNIT_TO_MM = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4, "foot": 304.8, "meter": 1000.0}
ROOT_MODEL = "3D/3dmodel.model"
SETTINGS_ENTRY = "Metadata/model_settings.config"
_PART_SUBTYPES = {"normal_part": "normal", "modifier_part": "modifier", "support_blocker": "support_blocker",
                  "support_enforcer": "support_enforcer", "normal": "normal", "modifier": "modifier"}

_STL_RECORD = np.dtype([("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("attr", "<u2")])
_IDENTITY = np.eye(4)


class MeshError(ValueError):
    """Maillage ou fichier de maillage invalide (code stable ``MESH_INVALID``)."""

    code = "MESH_INVALID"


# ---------------------------------------------------------------------------------------------- matrices

def translation_matrix(dx, dy=0.0, dz=0.0):
    """Matrice 4×4 de translation (``dx`` peut être un vecteur de 3)."""
    if np.ndim(dx):
        dx, dy, dz = (float(c) for c in np.asarray(dx, dtype=np.float64).reshape(3))
    m = np.eye(4)
    m[:3, 3] = (dx, dy, dz)
    return m


def scale_matrix(factor):
    """Matrice 4×4 d'homothétie (scalaire ou facteurs par axe)."""
    f = np.asarray(factor, dtype=np.float64)
    m = np.eye(4)
    m[:3, :3] = np.diag(np.full(3, float(f)) if f.ndim == 0 else f.reshape(3))
    return m


def rotation_matrix(axis, angle_rad):
    """Matrice 4×4 de rotation (Rodrigues) autour de ``axis`` : ``"x"``/``"y"``/``"z"`` ou vecteur quelconque."""
    if isinstance(axis, str):
        try:
            axis = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}[axis.lower()]
        except KeyError:
            raise MeshError(f"axe de rotation inconnu : {axis!r} (x, y, z ou vecteur)") from None
    u = np.asarray(axis, dtype=np.float64).reshape(3)
    norm = float(np.linalg.norm(u))
    if norm == 0.0:
        raise MeshError("axe de rotation nul")
    x, y, z = u / norm
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    t = 1.0 - c
    m = np.eye(4)
    m[:3, :3] = [[t * x * x + c, t * x * y - s * z, t * x * z + s * y],
                 [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
                 [t * x * z - s * y, t * y * z + s * x, t * z * z + c]]
    return m


def quaternion_matrix(q):
    """Matrice 4×4 de la rotation d'un quaternion ``[x, y, z, w]`` (convention Three.js / model_studio_orient.js)."""
    x, y, z, w = (float(c) for c in np.asarray(q, dtype=np.float64).reshape(4))
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    m = np.eye(4)
    m[:3, :3] = [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                 [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                 [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]
    return m


# ---------------------------------------------------------------------------------------------- Mesh

class Mesh:
    """Maillage triangulé indexé : ``vertices`` float64 (n, 3) mm, ``faces`` int64 (m, 3).

    ``extruder`` 1..16, ``paint`` int8 par triangle (0 = aucune peinture, sinon index d'extrudeur peint),
    ``volume_type`` parmi ``VOLUME_TYPES``, ``object_id``/``part_id`` = identifiants du 3MF d'origine (0 sinon).
    Les transformations modifient le maillage en place et renvoient ``self``.
    """

    __slots__ = ("vertices", "faces", "name", "extruder", "paint", "volume_type", "object_id", "part_id")

    def __init__(self, vertices, faces, name="", extruder=1, paint=None, volume_type="normal", object_id=0, part_id=0):
        v = np.array(vertices, dtype=np.float64, copy=True)
        f = np.array(faces, dtype=np.int64, copy=True)
        if v.size == 0:
            v = np.zeros((0, 3), dtype=np.float64)
        if f.size == 0:
            f = np.zeros((0, 3), dtype=np.int64)
        if v.ndim != 2 or v.shape[1] != 3:
            raise MeshError("sommets attendus sous la forme (n, 3)")
        if f.ndim != 2 or f.shape[1] != 3:
            raise MeshError("faces attendues sous la forme (m, 3)")
        if not np.isfinite(v).all():
            raise MeshError("coordonnées de sommet non finies")
        if f.size and (int(f.min()) < 0 or int(f.max()) >= len(v)):
            raise MeshError("indice de face hors des sommets")
        self.vertices, self.faces = v, f
        self.name = str(name or "")
        self.extruder = _check_extruder(extruder)
        self.volume_type = _check_volume_type(volume_type)
        self.object_id, self.part_id = int(object_id), int(part_id)
        self.paint = None
        self.set_paint(paint)

    # ------------------------------------------------------------------ construction, copie
    @classmethod
    def from_triangles(cls, triangles, name="", **kwargs):
        """Maillage à partir de triangles bruts (m, 3, 3) : sommets fusionnés, faces dégénérées retirées."""
        tri = np.asarray(triangles, dtype=np.float64).reshape(-1, 3, 3)
        mesh = cls(tri.reshape(-1, 3), np.arange(tri.shape[0] * 3, dtype=np.int64).reshape(-1, 3), name=name, **kwargs)
        return mesh.merge_vertices()

    def copy(self):
        return Mesh(self.vertices, self.faces, self.name, self.extruder, self.paint, self.volume_type, self.object_id, self.part_id)

    def set_paint(self, paint):
        """Peinture par triangle (int8, 0 = aucune) ; ``None`` = aucune."""
        if paint is None:
            self.paint = np.zeros(len(self.faces), dtype=np.int8)
        else:
            p = np.asarray(paint).astype(np.int8).reshape(-1)
            if len(p) != len(self.faces):
                raise MeshError("peinture attendue avec une valeur par triangle")
            self.paint = p.copy()
        return self

    def __repr__(self):
        return f"Mesh({self.name!r}, {len(self.vertices)} sommets, {len(self.faces)} faces, extrudeur {self.extruder}, {self.volume_type})"

    def __len__(self):
        return len(self.faces)

    # ------------------------------------------------------------------ géométrie
    def triangles(self):
        """Triangles (m, 3, 3) en mm."""
        return self.vertices[self.faces]

    def face_normals(self, normalized=True):
        """Normales des faces (m, 3) ; nulles pour un triangle dégénéré."""
        tri = self.triangles()
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        if normalized:
            length = np.linalg.norm(n, axis=1)
            n = np.divide(n, length[:, None], out=np.zeros_like(n), where=length[:, None] > 0)
        return n

    def face_areas(self):
        return 0.5 * np.linalg.norm(self.face_normals(normalized=False), axis=1)

    def surface_mm2(self):
        return float(self.face_areas().sum())

    def bounds(self):
        """``(min, max)`` : deux tableaux (3,) en mm ; zéros pour un maillage vide."""
        if len(self.vertices) == 0:
            return np.zeros(3), np.zeros(3)
        return self.vertices.min(axis=0), self.vertices.max(axis=0)

    def size(self):
        lo, hi = self.bounds()
        return hi - lo

    def center(self):
        lo, hi = self.bounds()
        return (lo + hi) / 2.0

    def volume_mm3(self):
        """Volume signé (positif si les faces sont orientées vers l'extérieur)."""
        if len(self.faces) == 0:
            return 0.0
        tri = self.triangles()
        return float(np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum() / 6.0)

    # ------------------------------------------------------------------ transformations (en place)
    def transform(self, matrix):
        """Applique une matrice 4×4 (ou 3×3 linéaire). Un miroir (déterminant < 0) retourne les faces."""
        m = np.asarray(matrix, dtype=np.float64)
        if m.shape == (3, 3):
            linear, offset = m, np.zeros(3)
        elif m.shape == (4, 4):
            linear, offset = m[:3, :3], m[:3, 3]
        else:
            raise MeshError("matrice 4×4 attendue")
        self.vertices = self.vertices @ linear.T + offset
        if np.linalg.det(linear) < 0:
            self.faces = np.ascontiguousarray(self.faces[:, [0, 2, 1]])
        return self

    def translate(self, dx, dy=0.0, dz=0.0):
        if np.ndim(dx):
            dx, dy, dz = (float(c) for c in np.asarray(dx, dtype=np.float64).reshape(3))
        self.vertices = self.vertices + np.array((dx, dy, dz), dtype=np.float64)
        return self

    def scale(self, factor, center=None):
        """Homothétie (scalaire ou par axe) autour de ``center`` (origine par défaut)."""
        c = np.zeros(3) if center is None else np.asarray(center, dtype=np.float64).reshape(3)
        f = np.asarray(factor, dtype=np.float64)
        if f.ndim == 0:
            f = np.full(3, float(f))
        if np.any(f == 0):
            raise MeshError("facteur d'échelle nul")
        self.vertices = (self.vertices - c) * f + c
        if np.prod(f) < 0:
            self.faces = np.ascontiguousarray(self.faces[:, [0, 2, 1]])
        return self

    def rotate(self, axis, angle_rad, center=None):
        """Rotation autour de ``axis`` (``"x"``/``"y"``/``"z"`` ou vecteur) passant par ``center`` (origine par défaut)."""
        c = np.zeros(3) if center is None else np.asarray(center, dtype=np.float64).reshape(3)
        m = rotation_matrix(axis, angle_rad)
        self.vertices = (self.vertices - c) @ m[:3, :3].T + c
        return self

    def rotate_quaternion(self, q, center=None):
        """Rotation par quaternion ``[x, y, z, w]`` (résultat de ``scene.auto_orient``)."""
        c = np.zeros(3) if center is None else np.asarray(center, dtype=np.float64).reshape(3)
        m = quaternion_matrix(q)
        self.vertices = (self.vertices - c) @ m[:3, :3].T + c
        return self

    def drop_to_bed(self):
        """Pose le maillage sur le plan Z = 0."""
        if len(self.vertices):
            self.vertices[:, 2] -= float(self.vertices[:, 2].min())
        return self

    # ------------------------------------------------------------------ topologie
    def edge_stats(self):
        """Arêtes : ``{"edges", "boundary", "non_manifold", "inconsistent"}`` (tri numpy des arêtes)."""
        f = self.faces
        if len(f) == 0:
            return {"edges": 0, "boundary": 0, "non_manifold": 0, "inconsistent": 0}
        n = max(len(self.vertices), 1)
        a = np.concatenate((f[:, 0], f[:, 1], f[:, 2]))
        b = np.concatenate((f[:, 1], f[:, 2], f[:, 0]))
        lo, hi = np.minimum(a, b), np.maximum(a, b)
        _, counts = np.unique(lo * n + hi, return_counts=True)
        # Arête dirigée vue deux fois dans le même sens = deux faces voisines d'orientation contraire.
        _, directed = np.unique(a * n + b, return_counts=True)
        return {"edges": int(len(counts)), "boundary": int((counts == 1).sum()), "non_manifold": int((counts > 2).sum()),
                "inconsistent": int((directed > 1).sum())}

    def is_watertight(self):
        """Fermé et 2-variété : chaque arête partagée par exactement deux faces."""
        if len(self.faces) == 0:
            return False
        stats = self.edge_stats()
        return stats["boundary"] == 0 and stats["non_manifold"] == 0

    def is_consistently_oriented(self):
        return len(self.faces) > 0 and self.edge_stats()["inconsistent"] == 0

    def remove_degenerate(self, min_area_mm2=1e-9):
        """Retire les faces à indices répétés ou d'aire < ``min_area_mm2``. Renvoie le nombre retiré."""
        f = self.faces
        if len(f) == 0:
            return 0
        keep = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])
        keep &= self.face_areas() >= min_area_mm2
        removed = int((~keep).sum())
        if removed:
            self.faces = np.ascontiguousarray(f[keep])
            self.paint = self.paint[keep]
        return removed

    def merge_vertices(self, decimals=6):
        """Fusionne les sommets confondus (arrondi à ``decimals``), retire les faces dégénérées et les sommets inutilisés."""
        if len(self.vertices) == 0:
            return self
        r = np.round(self.vertices, decimals) + 0.0      # +0.0 : -0.0 et 0.0 confondus
        order = np.lexsort((r[:, 2], r[:, 1], r[:, 0]))
        s = r[order]
        change = np.empty(len(s), dtype=bool)
        change[0] = True
        change[1:] = np.any(s[1:] != s[:-1], axis=1)
        group = np.cumsum(change) - 1
        inverse = np.empty(len(r), dtype=np.int64)
        inverse[order] = group
        self.vertices = np.ascontiguousarray(self.vertices[order[change]])
        self.faces = inverse[self.faces]
        self.remove_degenerate(min_area_mm2=0.0)
        return self.remove_unreferenced()

    def remove_unreferenced(self):
        """Retire les sommets qu'aucune face n'utilise."""
        if len(self.faces) == 0:
            self.vertices = np.zeros((0, 3), dtype=np.float64)
            return self
        used, inverse = np.unique(self.faces, return_inverse=True)
        if len(used) != len(self.vertices):
            self.vertices = np.ascontiguousarray(self.vertices[used])
            self.faces = inverse.reshape(-1, 3).astype(np.int64)
        return self

    def fix_winding(self):
        """Rend l'orientation des faces cohérente par propagation d'arête en arête (parcours en largeur vectorisé),
        puis oriente chaque composante vers l'extérieur (volume signé > 0). Renvoie le nombre de faces retournées."""
        f = self.faces
        m = len(f)
        if m == 0:
            return 0
        n = max(len(self.vertices), 1)
        a = np.concatenate((f[:, 0], f[:, 1], f[:, 2]))
        b = np.concatenate((f[:, 1], f[:, 2], f[:, 0]))
        face_id = np.tile(np.arange(m, dtype=np.int64), 3)
        forward = a < b
        key = np.minimum(a, b) * n + np.maximum(a, b)
        order = np.argsort(key, kind="stable")
        key_s, face_s, fwd_s = key[order], face_id[order], forward[order]
        starts = np.flatnonzero(np.concatenate(([True], key_s[1:] != key_s[:-1])))
        counts = np.diff(np.concatenate((starts, [len(key_s)])))
        pair = starts[counts == 2]                      # arêtes partagées par exactement deux faces
        fa, fb = face_s[pair], face_s[pair + 1]
        same_dir = fwd_s[pair] == fwd_s[pair + 1]       # même sens de parcours = orientations contraires
        src = np.concatenate((fa, fb))
        dst = np.concatenate((fb, fa))
        rel = np.concatenate((same_dir, same_dir)).astype(np.int8)
        o = np.argsort(src, kind="stable")
        src, dst, rel = src[o], dst[o], rel[o]
        indptr = np.zeros(m + 1, dtype=np.int64)
        indptr[1:] = np.cumsum(np.bincount(src, minlength=m))
        state = np.full(m, -1, dtype=np.int8)            # -1 inconnu, 0 garder, 1 retourner
        flip = np.zeros(m, dtype=bool)
        tri = self.triangles()
        seed_from = 0
        while True:
            rest = np.flatnonzero(state[seed_from:] < 0)
            if rest.size == 0:
                break
            seed = seed_from + int(rest[0])
            seed_from = seed + 1
            state[seed] = 0
            component = [np.array([seed], dtype=np.int64)]
            frontier = component[0]
            while frontier.size:
                start, end = indptr[frontier], indptr[frontier + 1]
                deg = end - start
                total = int(deg.sum())
                if total == 0:
                    break
                idx = np.repeat(start, deg) + (np.arange(total) - np.repeat(np.cumsum(deg) - deg, deg))
                nb = dst[idx]
                nstate = state[np.repeat(frontier, deg)] ^ rel[idx]
                new = state[nb] < 0
                nb, nstate = nb[new], nstate[new]
                uniq, first = np.unique(nb, return_index=True)
                state[uniq] = nstate[first]
                frontier = uniq
                component.append(uniq)
            faces_c = np.concatenate(component)
            flip_c = state[faces_c] == 1
            t = tri[faces_c].copy()
            t[flip_c] = t[flip_c][:, [0, 2, 1]]
            volume = float(np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])).sum())
            if abs(volume) < 1e-9:
                # Surface ouverte : normales majoritairement vers l'extérieur du centre de la composante.
                normals = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
                centers = t.mean(axis=1)
                volume = float(np.einsum("ij,ij->i", normals, centers - centers.mean(axis=0)).sum())
            if volume < 0:
                flip_c = ~flip_c
            flip[faces_c] = flip_c
        flipped = int(flip.sum())
        if flipped:
            f = f.copy()
            f[flip] = f[flip][:, [0, 2, 1]]
            self.faces = f
        return flipped


def _check_extruder(value):
    try:
        e = int(value)
    except (TypeError, ValueError):
        raise MeshError(f"extrudeur invalide : {value!r}") from None
    if not 1 <= e <= MAX_EXTRUDERS:
        raise MeshError(f"extrudeur hors de 1..{MAX_EXTRUDERS} : {e}")
    return e


def _check_volume_type(value):
    v = str(value or "normal")
    if v not in VOLUME_TYPES:
        raise MeshError(f"type de volume inconnu : {v!r}")
    return v


# ---------------------------------------------------------------------------------------------- peinture Bambu

def decode_paint_state(token):
    """Index d'extrudeur dominant d'un ``paint_color`` (arbre de quartets lu de droite à gauche) ; 0 = aucun."""
    token = str(token or "").strip().lower()
    if not token or any(ch not in "0123456789abcdef" for ch in token):
        return 0
    nibbles = [int(ch, 16) for ch in reversed(token)]
    counts = {}
    position = 0

    def walk():
        nonlocal position
        if position >= len(nibbles):
            return
        code = nibbles[position]
        position += 1
        split, upper = code & 3, (code >> 2) & 3
        if split:
            for _ in range(split + 1):
                walk()
            return
        state = upper
        if upper == 3:
            offset = 0
            while position < len(nibbles) and nibbles[position] == 15:
                offset += 15
                position += 1
            if position >= len(nibbles):
                return
            state = nibbles[position] + offset + 3
            position += 1
        if state > 0:
            counts[state] = counts.get(state, 0) + 1

    walk()
    if not counts:
        return 0
    top = max(counts.values())
    return min(s for s, c in counts.items() if c == top)


def encode_paint_state(state):
    """``paint_color`` d'un triangle entièrement peint avec l'extrudeur ``state`` (≥ 1) ; ``""`` pour 0."""
    state = int(state)
    if state <= 0:
        return ""
    if state <= 2:
        nibbles = [state << 2]
    else:
        rest = state - 3
        nibbles = [3 << 2] + [15] * (rest // 15) + [rest % 15]
    return "".join(f"{n:x}" for n in reversed(nibbles))


# ---------------------------------------------------------------------------------------------- lecture

def _read_bytes(path):
    path = Path(path)
    if not path.is_file():
        raise MeshError(f"fichier introuvable : {path.name}")
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise MeshError(f"fichier trop volumineux ({size // 1048576} Mio > {MAX_FILE_BYTES // 1048576} Mio)")
    return path.read_bytes()


def read_stl(path, name=None):
    """STL binaire ou ASCII, détection tolérante (en-tête « solid » d'un binaire, compte faux, octets en trop)."""
    path = Path(path)
    return parse_stl(_read_bytes(path), name if name is not None else path.stem)


def parse_stl(data, name=""):
    """Analyse le contenu d'un STL (bytes)."""
    size = len(data)
    declared = struct.unpack("<I", data[80:84])[0] if size >= 84 else -1
    head = data[:1024].lstrip(b"\xef\xbb\xbf \t\r\n")     # BOM UTF-8 et blancs tolérés devant « solid »
    text_like = head[:5].lower() == b"solid" and re.search(rb"facet|endsolid|vertex", data[:8192], re.I) is not None
    if size >= 84 and 84 + 50 * declared == size:
        tri = _stl_binary_triangles(data, declared)
    elif text_like:
        tri = _stl_ascii_triangles(data)
    elif size >= 134:
        count = (size - 84) // 50
        tri = _stl_binary_triangles(data, min(declared, count) if declared > 0 else count)
    else:
        tri = _stl_ascii_triangles(data)
    if tri.shape[0] == 0:
        raise MeshError("STL sans triangle")
    return Mesh.from_triangles(tri, name=name)


def _stl_binary_triangles(data, count):
    records = np.frombuffer(data, dtype=_STL_RECORD, count=int(count), offset=84)
    tri = records["vertices"].astype(np.float64)
    if not np.isfinite(tri).all():
        raise MeshError("STL binaire : coordonnées non finies")
    return tri


def _stl_ascii_triangles(data):
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    text = data.decode("latin-1")
    text = re.sub(r"(?im)^[ \t]*(?:end)?solid\b[^\n]*", " ", text)
    text = re.sub(r"(?im)^[ \t]*facet[ \t]+normal\b[^\n]*", " ", text)   # normales ignorées : recalculées
    text = re.sub(r"(?<![0-9.eE+\-])[A-Za-z_]+", " ", text)              # mots-clés ; exposants préservés
    tokens = text.split()
    try:
        values = np.array(tokens, dtype=np.float64)
    except ValueError:
        raise MeshError("STL ASCII : nombre illisible") from None
    if values.size == 0 or values.size % 9:
        raise MeshError(f"STL ASCII mal formé : {values.size} nombres lus, multiple de 9 attendu")
    if not np.isfinite(values).all():
        raise MeshError("STL ASCII : coordonnées non finies")
    return values.reshape(-1, 3, 3)


def read_obj(path, name=None):
    """OBJ Wavefront : ``v`` et ``f`` (polygones triangulés en éventail, indices négatifs relatifs, ``a/b/c`` toléré)."""
    path = Path(path)
    text = _read_bytes(path).decode("utf-8", errors="replace")
    vertices, faces = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] == "#":
            continue
        parts = line.split()
        tag = parts[0]
        if tag == "v":
            if len(parts) < 4:
                raise MeshError(f"OBJ : sommet incomplet « {raw.strip()[:60]} »")   # un saut silencieux décalerait les indices
            try:
                vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
            except ValueError:
                raise MeshError(f"OBJ : sommet illisible « {raw.strip()[:60]} »") from None
        elif tag == "f" and len(parts) >= 4:
            idx = []
            for token in parts[1:]:
                head = token.split("/", 1)[0]
                try:
                    i = int(head)
                except ValueError:
                    raise MeshError(f"OBJ : face illisible « {raw.strip()[:60]} »") from None
                i = len(vertices) + i if i < 0 else i - 1
                if i < 0 or i >= len(vertices):
                    raise MeshError("OBJ : indice de face hors des sommets")
                idx.append(i)
            for k in range(1, len(idx) - 1):
                faces.append((idx[0], idx[k], idx[k + 1]))
    if not faces:
        raise MeshError("OBJ sans face")
    mesh = Mesh(np.array(vertices, dtype=np.float64), np.array(faces, dtype=np.int64), name=name if name is not None else path.stem)
    return mesh.merge_vertices()


def read_any(path):
    """Lit STL, OBJ ou 3MF selon l'extension ; renvoie toujours une liste de maillages."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".stl":
        return [read_stl(path)]
    if ext == ".obj":
        return [read_obj(path)]
    if ext == ".3mf":
        return read_3mf(path)
    raise MeshError(f"format non pris en charge : {ext or 'sans extension'}")


# ---------------------------------------------------------------------------------------------- 3MF : lecture

def parse_xml_secure(data, what="XML"):
    """``xml.etree`` sans DTD : tout ``<!DOCTYPE`` ou ``<!ENTITY`` est refusé, taille bornée."""
    if len(data) > MAX_XML_BYTES:
        raise MeshError(f"{what} : pièce XML trop volumineuse")
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", data, re.I):
        raise MeshError(f"{what} : déclaration DOCTYPE/ENTITY refusée")
    try:
        return ET.fromstring(data)
    except ET.ParseError as error:
        raise MeshError(f"{what} : XML invalide ({error})") from None


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_transform(text):
    """``transform`` 3MF (12 flottants, lignes de la matrice 3×4 ; les trois derniers = translation) → 4×4."""
    if text is None or not str(text).strip():
        return np.eye(4)
    try:
        v = [float(t) for t in str(text).replace(",", " ").split()]
    except ValueError:
        raise MeshError("3MF : transform illisible") from None
    if len(v) != 12 or not all(math.isfinite(x) for x in v):
        raise MeshError("3MF : transform attendu avec 12 flottants")
    m = np.eye(4)
    m[:3, :3] = np.array(v[:9]).reshape(3, 3).T
    m[:3, 3] = v[9:]
    return m


def format_transform(matrix):
    """Matrice 4×4 → attribut ``transform`` 3MF (12 flottants)."""
    m = np.asarray(matrix, dtype=np.float64)
    if m.shape != (4, 4):
        raise MeshError("matrice 4×4 attendue")
    values = list(m[:3, :3].T.reshape(9)) + list(m[:3, 3])
    return " ".join(_fmt(x) for x in values)


class _ModelDoc:
    __slots__ = ("name", "unit", "objects", "build")

    def __init__(self, name, root):
        self.name = name
        unit = (root.get("unit") or "millimeter").lower()
        if unit not in UNIT_TO_MM:
            raise MeshError(f"3MF : unité inconnue « {unit} »")
        self.unit = UNIT_TO_MM[unit]
        self.build = root.find(f"{{{NS_CORE}}}build")
        self.objects = {}
        resources = root.find(f"{{{NS_CORE}}}resources")
        if resources is not None:
            for obj in resources.findall(f"{{{NS_CORE}}}object"):
                try:
                    self.objects[int(obj.get("id"))] = obj
                except (TypeError, ValueError):
                    raise MeshError("3MF : objet sans id entier") from None


class _ThreeMFReader:
    def __init__(self, archive):
        self.archive = archive
        self.lookup = {}
        total = 0
        for info in archive.infolist():
            total += info.file_size
            self.lookup[_norm_entry(info.filename).lower()] = info.filename
        if total > MAX_ZIP_BYTES:
            raise MeshError("3MF : contenu décompressé > 512 Mio")
        self.docs = {}
        self.triangles_total = 0
        self.settings = self._read_settings()

    def entry(self, name):
        real = self.lookup.get(_norm_entry(name).lower())
        if real is None:
            raise MeshError(f"3MF : pièce « {name} » absente")
        info = self.archive.getinfo(real)
        if info.file_size > MAX_XML_BYTES:
            raise MeshError(f"3MF : pièce « {name} » trop volumineuse")
        try:
            with self.archive.open(info) as stream:
                data = stream.read(MAX_XML_BYTES + 1)
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, OSError, ValueError, zlib.error,
                *_LZMA_ERRORS) as error:
            # CRC faux, membre tronqué, chiffré ou compression inconnue : le fichier est invalide, pas le moteur.
            raise MeshError(f"3MF : pièce « {name} » illisible ({error})") from None
        if len(data) > MAX_XML_BYTES:
            raise MeshError(f"3MF : pièce « {name} » trop volumineuse")
        return data

    def root_model_name(self):
        if "_rels/.rels" in self.lookup:
            rels = parse_xml_secure(self.entry("_rels/.rels"), "_rels/.rels")
            for rel in rels.iter():
                if _local(rel.tag) == "Relationship" and (rel.get("Type") or "").rstrip("/").lower().endswith("3dmodel"):
                    target = _norm_entry(rel.get("Target") or "")
                    if target.lower() in self.lookup:
                        return target
        if ROOT_MODEL.lower() in self.lookup:
            return ROOT_MODEL
        for key, real in sorted(self.lookup.items()):
            if key.endswith(".model"):
                return real
        raise MeshError("3MF : aucun fichier .model")

    def doc(self, name):
        key = _norm_entry(name).lower()
        if key not in self.docs:
            root = parse_xml_secure(self.entry(name), name)
            if _local(root.tag) != "model":
                raise MeshError(f"3MF : « {name} » n'est pas un modèle")
            self.docs[key] = _ModelDoc(_norm_entry(name), root)
        return self.docs[key]

    def _read_settings(self):
        info = {"objects": {}, "parts": {}}
        if SETTINGS_ENTRY.lower() not in self.lookup:
            return info
        root = parse_xml_secure(self.entry(SETTINGS_ENTRY), SETTINGS_ENTRY)
        for obj in root.iter():
            if _local(obj.tag) != "object":
                continue
            oid = _int_or_none(obj.get("id"))
            if oid is None:
                continue
            meta = _metadata_map(obj)
            info["objects"][oid] = {"name": meta.get("name", ""), "extruder": _int_or_none(meta.get("extruder"))}
            for part in obj:
                if _local(part.tag) != "part":
                    continue
                pid = _int_or_none(part.get("id"))
                if pid is None:
                    continue
                pmeta = _metadata_map(part)
                subtype = (part.get("subtype") or pmeta.get("volume_type") or pmeta.get("subtype") or pmeta.get("type") or "").strip().lower()
                info["parts"][pid] = {"name": pmeta.get("name", ""), "extruder": _int_or_none(pmeta.get("extruder")),
                                      "volume_type": _PART_SUBTYPES.get(subtype), "object": oid}
        return info

    def meshes(self):
        root_name = self.root_model_name()
        root = self.doc(root_name)
        items = [] if root.build is None else [n for n in root.build if _local(n.tag) == "item"]
        parts = []
        for item in items:
            oid = _int_or_none(item.get("objectid"))
            if oid is None:
                raise MeshError("3MF : build/item sans objectid")
            matrix = parse_transform(item.get("transform"))
            doc = self._target_doc(root, item)
            self._resolve(doc, oid, matrix, 0, [], oid, parts)
        if not items:
            # Sans build (fichier hors spécification) : seuls les objets de tête, pas ceux déjà pris comme composants.
            referenced = set()
            for obj in root.objects.values():
                components = obj.find(f"{{{NS_CORE}}}components")
                if components is not None:
                    for comp in components.findall(f"{{{NS_CORE}}}component"):
                        if not comp.get(f"{{{NS_PROD}}}path"):
                            referenced.add(_int_or_none(comp.get("objectid")))
            for oid in root.objects:
                if oid not in referenced:
                    self._resolve(root, oid, np.eye(4), 0, [], oid, parts)
        meshes = [self._build_mesh(*p) for p in parts]
        meshes = [m for m in meshes if m is not None]
        if not meshes:
            raise MeshError("3MF : aucun maillage")
        return meshes

    def _target_doc(self, doc, node):
        path = node.get(f"{{{NS_PROD}}}path")
        if not path:
            return doc
        path = _norm_entry(path) if path.startswith("/") else _norm_entry(posixpath.join(posixpath.dirname(doc.name), path))
        return self.doc(path)

    def _resolve(self, doc, oid, matrix, depth, chain, build_id, out):
        if depth > MAX_COMPONENT_DEPTH:
            raise MeshError(f"3MF : composants imbriqués au-delà de {MAX_COMPONENT_DEPTH} niveaux")
        key = (doc.name.lower(), oid)
        if key in chain:
            raise MeshError("3MF : composants en boucle")
        obj = doc.objects.get(oid)
        if obj is None:
            raise MeshError(f"3MF : objet {oid} introuvable dans « {doc.name} »")
        mesh = obj.find(f"{{{NS_CORE}}}mesh")
        if mesh is not None:
            if len(out) >= MAX_PARTS:
                raise MeshError(f"3MF : plus de {MAX_PARTS} instances de maillage (composants en éventail)")
            triangles = mesh.find(f"{{{NS_CORE}}}triangles")
            self.triangles_total += 0 if triangles is None else len(triangles)
            if self.triangles_total > MAX_TRIANGLES:
                raise MeshError(f"3MF : plus de {MAX_TRIANGLES} triangles cumulés")
            out.append((doc, obj, mesh, matrix, build_id))
        components = obj.find(f"{{{NS_CORE}}}components")
        if components is not None:
            for comp in components.findall(f"{{{NS_CORE}}}component"):
                cid = _int_or_none(comp.get("objectid"))
                if cid is None:
                    raise MeshError("3MF : component sans objectid")
                sub = self._target_doc(doc, comp)
                self._resolve(sub, cid, matrix @ parse_transform(comp.get("transform")), depth + 1, chain + [key], build_id, out)

    def _build_mesh(self, doc, obj, mesh_el, matrix, build_id):
        vertices_el = mesh_el.find(f"{{{NS_CORE}}}vertices")
        triangles_el = mesh_el.find(f"{{{NS_CORE}}}triangles")
        if vertices_el is None or triangles_el is None:
            return None
        try:
            vertices = np.array([(v.get("x"), v.get("y"), v.get("z")) for v in vertices_el], dtype=np.float64)
            tri_nodes = list(triangles_el)
            faces = np.array([(t.get("v1"), t.get("v2"), t.get("v3")) for t in tri_nodes], dtype=np.int64)
        except (TypeError, ValueError):
            raise MeshError("3MF : sommet ou triangle illisible") from None
        if len(faces) == 0 or len(vertices) == 0:
            return None
        if vertices.ndim != 2 or vertices.shape[1] != 3 or faces.ndim != 2 or faces.shape[1] != 3:
            raise MeshError("3MF : sommets ou triangles incomplets")
        paint = np.zeros(len(faces), dtype=np.int8)
        for i, t in enumerate(tri_nodes):
            token = t.get("paint_color")
            if token:
                paint[i] = min(decode_paint_state(token), 127)
        oid = int(obj.get("id"))
        part_meta = self.settings["parts"].get(oid, {})
        object_meta = self.settings["objects"].get(part_meta.get("object", build_id)) or self.settings["objects"].get(build_id) or {}
        name = part_meta.get("name") or obj.get("name") or object_meta.get("name") or f"objet {build_id}"
        extruder = part_meta.get("extruder") or object_meta.get("extruder") or 1
        volume_type = part_meta.get("volume_type")
        if volume_type is None:
            volume_type = "support_enforcer" if (obj.get("type") or "").lower() in ("support", "solidsupport") else "normal"
        extruder = min(max(int(extruder), 1), MAX_EXTRUDERS)
        mesh = Mesh(vertices * doc.unit, faces, name=name, extruder=extruder, paint=paint, volume_type=volume_type,
                    object_id=build_id, part_id=oid)
        if not np.allclose(matrix, _IDENTITY):
            m = np.array(matrix, dtype=np.float64)
            m[:3, 3] *= doc.unit        # la translation d'un transform est exprimée dans l'unité du modèle
            mesh.transform(m)
        return mesh


def _norm_entry(name):
    return posixpath.normpath(str(name).replace("\\", "/")).lstrip("/")


def _int_or_none(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _metadata_map(node):
    out = {}
    for child in node:
        if _local(child.tag) == "metadata" and child.get("key"):
            out[child.get("key")] = child.get("value") or (child.text or "").strip()
    return out


def read_3mf(path):
    """3MF → liste de maillages (une entrée par part, transformations appliquées, métadonnées Bambu lues)."""
    path = Path(path)
    data = _read_bytes(path)
    return parse_3mf(data)


def parse_3mf(data):
    """Analyse un conteneur 3MF (bytes)."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise MeshError("3MF : archive ZIP invalide") from None
    with archive:
        return _ThreeMFReader(archive).meshes()


# ---------------------------------------------------------------------------------------------- écriture

def write_stl(mesh, path):
    """STL binaire (normales recalculées)."""
    Path(path).write_bytes(stl_bytes(mesh))
    return Path(path)


def stl_bytes(mesh):
    tri = mesh.triangles()
    records = np.zeros(len(tri), dtype=_STL_RECORD)
    records["normal"] = mesh.face_normals()
    records["vertices"] = tri
    return STL_HEADER.ljust(80, b"\0") + struct.pack("<I", len(tri)) + records.tobytes()


def write_stl_ascii(mesh, path):
    """STL ASCII (pour l'échange ; le binaire reste le format par défaut)."""
    tri = mesh.triangles()
    normals = mesh.face_normals()
    label = _ascii_label(mesh.name)
    lines = [f"solid {label}"]
    for t, nrm in zip(tri, normals):
        lines.append(f"  facet normal {_fmt(nrm[0])} {_fmt(nrm[1])} {_fmt(nrm[2])}")
        lines.append("    outer loop")
        for p in t:
            lines.append(f"      vertex {_fmt(p[0])} {_fmt(p[1])} {_fmt(p[2])}")
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append(f"endsolid {label}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="ascii")
    return Path(path)


def _ascii_label(name):
    """Nom de solide STL ASCII : accents retirés, un seul mot par espace, « alpine » si rien ne reste."""
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii")
    return " ".join(text.split()) or "alpine"


def _fmt(x):
    s = f"{float(x):.9g}"
    return "0" if s in ("-0", "-0.0") else s


_CONTENT_TYPES = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                  f'<Types xmlns="{NS_TYPES}">\n'
                  ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
                  ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
                  ' <Default Extension="config" ContentType="text/xml"/>\n'
                  '</Types>\n')
_ROOT_RELS = ('<?xml version="1.0" encoding="UTF-8"?>\n'
              f'<Relationships xmlns="{NS_RELS}">\n'
              f' <Relationship Target="/{ROOT_MODEL}" Id="rel-1" Type="{REL_MODEL}"/>\n'
              '</Relationships>\n')


def mesh_object_xml(mesh, object_id, name=None):
    """Fragment ``<object id type="model"><mesh>…</mesh></object>`` (3MF Core, ``paint_color`` si peint)."""
    v = mesh.vertices
    f = mesh.faces
    out = [f'  <object id="{int(object_id)}" name="{_attr(name if name is not None else mesh.name)}" type="model">',
           "   <mesh>", "    <vertices>"]
    out.extend(f'     <vertex x="{_fmt(x)}" y="{_fmt(y)}" z="{_fmt(z)}"/>' for x, y, z in v)
    out.append("    </vertices>")
    out.append("    <triangles>")
    paint = mesh.paint
    if paint is not None and np.any(paint):
        for (a, b, c), p in zip(f, paint):
            token = encode_paint_state(int(p))
            extra = f' paint_color="{token}"' if token else ""
            out.append(f'     <triangle v1="{a}" v2="{b}" v3="{c}"{extra}/>')
    else:
        out.extend(f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in f)
    out.append("    </triangles>")
    out.append("   </mesh>")
    out.append("  </object>")
    return "\n".join(out)


def model_document(objects_xml, build_items_xml="", unit="millimeter", production=False):
    """Document ``.model`` complet (3MF Core + ``BambuStudio:3mfVersion``)."""
    ns = f' xmlns:p="{NS_PROD}"' if production else ""
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<model unit="{unit}" xml:lang="en-US" xmlns="{NS_CORE}"{ns}>\n'
            ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
            ' <metadata name="Application">Alpine Model Engine</metadata>\n'
            ' <resources>\n' + objects_xml + '\n </resources>\n'
            ' <build>\n' + build_items_xml + ('\n' if build_items_xml else '') + ' </build>\n'
            '</model>\n')


def model_settings_xml(entries):
    """``Metadata/model_settings.config`` : ``entries`` = ``[(object_id, name, extruder, [(part_id, name, extruder, volume_type)])]``."""
    out = ['<?xml version="1.0" encoding="UTF-8"?>', "<config>"]
    for object_id, name, extruder, parts in entries:
        out.append(f'  <object id="{int(object_id)}">')
        out.append(f'    <metadata key="name" value="{_attr(name)}"/>')
        out.append(f'    <metadata key="extruder" value="{int(extruder)}"/>')
        for part_id, pname, pextruder, volume_type in parts:
            subtype = {"normal": "normal_part", "modifier": "modifier_part"}.get(volume_type, volume_type)
            out.append(f'    <part id="{int(part_id)}" subtype="{subtype}">')
            out.append(f'      <metadata key="name" value="{_attr(pname)}"/>')
            out.append(f'      <metadata key="extruder" value="{int(pextruder)}"/>')
            out.append("    </part>")
        out.append("  </object>")
    out.append("</config>")
    return "\n".join(out) + "\n"


def write_3mf_objects(meshes, transforms=None, with_components=True):
    """Conteneur 3MF complet (bytes) : un objet par maillage, ``Metadata/model_settings.config`` (extrudeur, type de
    volume), ``paint_color`` par triangle peint, ``BambuStudio:3mfVersion``. Avec ``with_components`` : chaque
    maillage va dans ``3D/Objects/object_<id>.model`` (extension production, ``p:path``) et l'objet de construction
    du modèle racine le référence par un ``component`` ; sinon tout est dans ``3D/3dmodel.model``.
    ``transforms`` : matrices 4×4 (ou ``None``) appliquées aux ``build/item``."""
    meshes = list(meshes)
    if not meshes:
        raise MeshError("aucun maillage à écrire")
    transforms = list(transforms) if transforms is not None else [None] * len(meshes)
    if len(transforms) != len(meshes):
        raise MeshError("une transformation par maillage attendue")
    files = {}
    root_objects, build_items, settings, object_rels = [], [], [], []
    for index, (mesh, matrix) in enumerate(zip(meshes, transforms)):
        if len(mesh.faces) == 0:
            raise MeshError(f"maillage « {mesh.name} » sans face")
        mesh_id, build_id = 2 * index + 1, 2 * index + 2
        name = mesh.name or f"objet {index + 1}"
        transform = "" if matrix is None else f' transform="{format_transform(matrix)}"'
        if with_components:
            part = f"3D/Objects/object_{mesh_id}.model"
            files[part] = model_document(mesh_object_xml(mesh, mesh_id, name))
            root_objects.append(f'  <object id="{build_id}" name="{_attr(name)}" type="model">\n'
                                f'   <components>\n    <component p:path="/{part}" objectid="{mesh_id}"/>\n   </components>\n  </object>')
            object_rels.append(f' <Relationship Target="/{part}" Id="rel-obj-{mesh_id}" Type="{REL_MODEL}"/>')
        else:
            root_objects.append(mesh_object_xml(mesh, mesh_id, name))
            root_objects.append(f'  <object id="{build_id}" name="{_attr(name)}" type="model">\n'
                                f'   <components>\n    <component objectid="{mesh_id}"/>\n   </components>\n  </object>')
        build_items.append(f'  <item objectid="{build_id}"{transform}/>')
        settings.append((build_id, name, mesh.extruder, [(mesh_id, name, mesh.extruder, mesh.volume_type)]))
    files[ROOT_MODEL] = model_document("\n".join(root_objects), "\n".join(build_items), production=with_components)
    if with_components:
        files["3D/_rels/3dmodel.model.rels"] = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                                               f'<Relationships xmlns="{NS_RELS}">\n' + "\n".join(object_rels) + "\n</Relationships>\n")
    files[SETTINGS_ENTRY] = model_settings_xml(settings)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        for name in sorted(files):
            archive.writestr(name, files[name])
    return buffer.getvalue()


def write_3mf(meshes, path, transforms=None, with_components=True):
    """Écrit ``write_3mf_objects`` dans un fichier."""
    Path(path).write_bytes(write_3mf_objects(meshes, transforms, with_components))
    return Path(path)


def _attr(value):
    return _xml_escape(str(value or ""), {'"': "&quot;"})
