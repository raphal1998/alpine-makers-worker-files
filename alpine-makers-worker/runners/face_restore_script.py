# -*- coding: utf-8 -*-
"""Labo image — restauration des visages (GFPGAN v1.4), script autonome lancé par l'agent.

Lancé par ``runners/face_restore.py`` avec le Python de l'environnement du moteur image (ComfyUI), en sous-processus :
``python -I -B face_restore_script.py <request.json>``. Ce n'est pas un nœud de ComfyUI et rien n'est installé : le script
n'importe que torch, spandrel, kornia, numpy et Pillow, déjà présents dans cet environnement, et la bibliothèque standard.

Étapes :
1. Détection : YuNet (``kornia.models.yunet``) sur une copie réduite (côté long ≤ 1280 px) en BGR 0–255, la convention
   d'entraînement de libfacedetection (OpenCV) ; boîtes, score et cinq repères (yeux, nez, coins de la bouche).
2. Alignement : similitude (échelle + rotation + translation, moindres carrés) des cinq repères vers le gabarit FFHQ
   512 × 512 de facexlib, puis découpe par ``grid_sample``.
3. Restauration : GFPGAN v1.4 chargé par spandrel ; le réseau sous-jacent est appelé directement avec la conversion
   faite ici — entrée RGB ``x * 2 - 1``, sortie ``(y + 1) / 2`` bornée à 0–1 (l'appel de spandrel, ``model(image)[0]`` sur
   une image 0–1, perdrait la moitié sombre de la plage).
4. Recollage : transformation inverse, masque carré érodé à bord progressif (comme facexlib), mélange selon la force.

Sécurité : aucun accès réseau (sockets coupées, ``torch.hub`` neutralisé, ``HF_HUB_OFFLINE``) ; les deux fichiers de poids
sont lus en mémoire, leur SHA-256 comparé à celui que l'agent transmet (catalogue épinglé), puis désérialisés par
``torch.load(..., weights_only=True)`` depuis ces octets mêmes : le fichier vérifié est celui qui est chargé.

Demande (JSON) : workdir, input, output, report (chemins absolus, les trois derniers dans workdir), gfpgan, gfpgan_sha256,
detector, detector_sha256, strength (0–1), max_faces (1–8), device (« auto » ou « cpu »).
Compte rendu (JSON, fichier « report ») : {"ok": true, "faces_detected", "faces_restored", "faces_too_small",
"faces_over_limit", "boxes", "device", "cpu_fallback", "width", "height", "seconds"} ou {"ok": false, "code", "error"}.
Aucun visage : ok, ``faces_restored`` = 0 et aucune image écrite.

Les fonctions de géométrie et de masque (``similarity_transform``, ``invert_similarity``, ``apply_affine``,
``mask_weight``, ``paste_box``, ``select_faces``, ``validate_request``) n'utilisent que la bibliothèque standard : elles
sont testées sans torch.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import sys
import time
from pathlib import Path

FACE_SIZE = 512
# Gabarit des cinq repères des visages FFHQ en 512 × 512 utilisé par GFPGAN pour aligner les visages : œil gauche, œil
# droit, nez, coin gauche puis coin droit de la bouche (gauche / droite vus par le spectateur). Source : facexlib,
# facexlib/utils/face_restoration_helper.py (FaceRestoreHelper.__init__, « standard 5 landmarks for FFHQ faces with
# 512 x 512 »), https://github.com/xinntao/facexlib — les cinq points de référence d'ArcFace mis à l'échelle du cadrage FFHQ.
FFHQ_TEMPLATE_512 = (
    (192.98138, 239.94708), (318.90277, 240.1936), (256.63416, 314.01935),
    (201.26117, 371.41043), (313.08905, 371.15118),
)
MAX_FACES = 8
MIN_FACE_PIXELS = 24          # côté le plus court de la boîte d'un visage, en pixels de l'image d'origine
MAX_PIXELS = 16_000_000       # bornes du Labo image (image_lab.SOURCE_MAX_PIXELS, SOURCE_MAX_SIDE)
MAX_SIDE = 8192
DETECT_MAX_SIDE = 1280        # la détection travaille sur une copie réduite ; les repères sont remis à l'échelle
DETECT_STRIDE = 64            # YuNet réduit l'image jusqu'à 1/64 : la copie est complétée à un multiple de 64
DETECT_SCORE = 0.6
DETECT_NMS_IOU = 0.3
DETECT_TOP_K = 5000
# Masque de recollage dans le cadre 512 × 512 du visage : bord retiré puis fondu progressif (facexlib érode d'environ
# 1/20 du côté puis floute sur la même largeur).
MASK_INSET = 24.0
MASK_FEATHER = 56.0
GFPGAN_MAX_BYTES = 400 * 1024 * 1024
DETECTOR_MAX_BYTES = 4 * 1024 * 1024
REQUEST_KEYS = frozenset({"workdir", "input", "output", "report", "gfpgan", "gfpgan_sha256", "detector", "detector_sha256",
                          "strength", "max_faces", "device"})


class FaceRestoreError(Exception):
    """Refus ou échec explicable : code stable lu par le lanceur, message en français sans chemin."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ géométrie (bibliothèque standard seulement)
def similarity_transform(source, target):
    """Similitude (échelle, rotation, translation) qui envoie au mieux ``source`` sur ``target`` (moindres carrés).

    Rend (a, b, tx, ty) : x' = a·x − b·y + tx ; y' = b·x + a·y + ty. Aucune symétrie possible (déterminant a² + b² > 0).
    """
    points = list(zip(source, target))
    if len(points) < 2:
        raise FaceRestoreError("LANDMARKS_INVALID", "Repères du visage insuffisants.")
    count = float(len(points))
    sx = sum(p[0][0] for p in points) / count
    sy = sum(p[0][1] for p in points) / count
    tx_mean = sum(p[1][0] for p in points) / count
    ty_mean = sum(p[1][1] for p in points) / count
    norm = dot = cross = 0.0
    for (x, y), (u, v) in points:
        x, y, u, v = x - sx, y - sy, u - tx_mean, v - ty_mean
        norm += x * x + y * y
        dot += x * u + y * v
        cross += x * v - y * u
    if not math.isfinite(norm) or norm < 1e-6:
        raise FaceRestoreError("LANDMARKS_INVALID", "Repères du visage dégénérés.")
    a, b = dot / norm, cross / norm
    if not (math.isfinite(a) and math.isfinite(b)) or a * a + b * b < 1e-12:
        raise FaceRestoreError("LANDMARKS_INVALID", "Repères du visage dégénérés.")
    return a, b, tx_mean - (a * sx - b * sy), ty_mean - (b * sx + a * sy)


def affine_matrix(params):
    a, b, tx, ty = params
    return ((a, -b, tx), (b, a, ty))


def invert_similarity(matrix):
    """Inverse exacte d'une similitude 2 × 3."""
    (a, minus_b, tx), (b, _a, ty) = matrix
    scale = a * a + b * b
    if scale < 1e-12:
        raise FaceRestoreError("LANDMARKS_INVALID", "Transformation du visage non inversible.")
    ia, ib = a / scale, b / scale
    # Inverse de [[a, -b], [b, a]] : [[a, b], [-b, a]] / (a² + b²).
    return ((ia, ib, -(ia * tx + ib * ty)), (-ib, ia, -(-ib * tx + ia * ty)))


def apply_affine(matrix, x, y):
    return (matrix[0][0] * x + matrix[0][1] * y + matrix[0][2], matrix[1][0] * x + matrix[1][1] * y + matrix[1][2])


def mask_weight(u, v, size=FACE_SIZE, inset=MASK_INSET, feather=MASK_FEATHER):
    """Poids du recollage (0–1) au point (u, v) du cadre du visage : 0 sur le bord, 1 au cœur, transition douce."""
    distance = min(u, v, size - 1 - u, size - 1 - v)
    ramp = min(1.0, max(0.0, (distance - inset) / feather))
    return ramp * ramp * (3.0 - 2.0 * ramp)


def paste_box(inverse, width, height, size=FACE_SIZE, margin=2):
    """Rectangle (x0, y0, x1, y1) de l'image couvert par le cadre du visage, borné à l'image ; None s'il est vide."""
    corners = [apply_affine(inverse, u, v) for u, v in ((0, 0), (size - 1, 0), (0, size - 1), (size - 1, size - 1))]
    x0 = max(0, int(math.floor(min(x for x, _ in corners))) - margin)
    y0 = max(0, int(math.floor(min(y for _, y in corners))) - margin)
    x1 = min(width, int(math.ceil(max(x for x, _ in corners))) + margin + 1)
    y1 = min(height, int(math.ceil(max(y for _, y in corners))) + margin + 1)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def select_faces(detections, max_faces, min_pixels=MIN_FACE_PIXELS):
    """(visages gardés, trop petits, au-delà de la limite). Les plus grands visages passent en premier.

    ``detections`` : [{"box": (x0, y0, x1, y1), "landmarks": ((x, y) × 5), "score": s}] en pixels de l'image d'origine.
    """
    sized, small = [], 0
    for item in detections:
        x0, y0, x1, y1 = item["box"]
        if min(x1 - x0, y1 - y0) < min_pixels:
            small += 1
            continue
        sized.append(item)
    sized.sort(key=lambda item: (item["box"][2] - item["box"][0]) * (item["box"][3] - item["box"][1]), reverse=True)
    return sized[:max_faces], small, max(0, len(sized) - max_faces)


def _inside(path, folder):
    try:
        return Path(path).resolve().is_relative_to(Path(folder).resolve())
    except (OSError, ValueError):
        return False


def validate_request(raw):
    """Demande du lanceur, revérifiée champ par champ : clés exactes, bornes, chemins de travail dans le dossier du job."""
    if not isinstance(raw, dict) or set(raw) != REQUEST_KEYS:
        raise FaceRestoreError("REQUEST_INVALID", "Demande de restauration invalide.")
    strength, faces, device = raw["strength"], raw["max_faces"], raw["device"]
    if type(strength) not in (int, float) or not math.isfinite(strength) or not 0 <= strength <= 1:
        raise FaceRestoreError("REQUEST_INVALID", "Force de restauration hors limites.")
    if type(faces) is not int or not 1 <= faces <= MAX_FACES:
        raise FaceRestoreError("REQUEST_INVALID", "Nombre de visages hors limites.")
    if device not in ("auto", "cpu"):
        raise FaceRestoreError("REQUEST_INVALID", "Appareil de calcul non autorisé.")
    for key in ("gfpgan_sha256", "detector_sha256"):
        value = raw[key]
        if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise FaceRestoreError("REQUEST_INVALID", "Empreinte attendue invalide.")
    paths = {}
    for key in ("workdir", "input", "output", "report", "gfpgan", "detector"):
        value = raw[key]
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise FaceRestoreError("REQUEST_INVALID", "Chemin de la demande invalide.")
        paths[key] = Path(value)
    for key in ("input", "output", "report"):
        if not _inside(paths[key], paths["workdir"]):
            raise FaceRestoreError("REQUEST_INVALID", "Fichier hors du dossier du job.")
    if paths["output"].suffix.lower() != ".png" or paths["report"].suffix.lower() != ".json":
        raise FaceRestoreError("REQUEST_INVALID", "Nom de sortie non autorisé.")
    return {**paths, "strength": float(strength), "max_faces": faces, "device": device,
            "gfpgan_sha256": raw["gfpgan_sha256"], "detector_sha256": raw["detector_sha256"]}


# ------------------------------------------------------------------ isolement
def block_network():
    """Coupe le réseau du processus : toute tentative (téléchargement de poids, télémétrie) lève une erreur."""
    import socket

    def refused(*_args, **_kwargs):
        raise OSError("Accès réseau interdit au calcul de restauration des visages.")

    socket.socket.connect = refused
    socket.socket.connect_ex = refused
    socket.create_connection = refused
    socket.getaddrinfo = refused
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"


def _verified_bytes(path, expected, maximum, label):
    """Octets du fichier de poids, lus une fois ; leur SHA-256 doit être celui du catalogue épinglé."""
    try:
        size = path.stat().st_size
    except OSError:
        raise FaceRestoreError("WEIGHTS_MISSING", f"Fichier {label} absent : installe-le dans Mes Workers → Installations.") from None
    if size <= 0 or size > maximum:
        raise FaceRestoreError("WEIGHTS_INVALID", f"Fichier {label} de taille inattendue : répare-le dans Mes Workers → Installations.")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise FaceRestoreError("WEIGHTS_INVALID", f"Empreinte du fichier {label} incorrecte : répare-le dans Mes Workers → Installations.")
    return data


def _load_state(torch, data):
    """État de poids désérialisé sans exécution de code : ``weights_only=True`` depuis les octets vérifiés."""
    try:
        return torch.load(io.BytesIO(data), map_location="cpu", weights_only=True)
    except Exception:  # noqa: BLE001 - fichier refusé par le chargeur restreint de torch
        raise FaceRestoreError("WEIGHTS_INVALID", "Fichier de poids refusé par le chargeur sécurisé.") from None


# ------------------------------------------------------------------ calcul (torch, spandrel, kornia, numpy, Pillow)
def _load_detector(torch, data):
    from kornia.models.yunet import YuNet
    model = YuNet("test", pretrained=False)    # jamais les poids « préentraînés » de kornia : ils viendraient de torch.hub
    try:
        model.load_state_dict(_load_state(torch, data), strict=True)
    except FaceRestoreError:
        raise
    except Exception:  # noqa: BLE001
        raise FaceRestoreError("WEIGHTS_INVALID", "Poids du détecteur de visages incompatibles.") from None
    return model.eval()


def _load_gfpgan(torch, data):
    from spandrel import ModelLoader, canonicalize_state_dict
    try:
        descriptor = ModelLoader(device="cpu").load_from_state_dict(canonicalize_state_dict(_load_state(torch, data)))
    except FaceRestoreError:
        raise
    except Exception:  # noqa: BLE001
        raise FaceRestoreError("WEIGHTS_INVALID", "Poids GFPGAN non reconnus.") from None
    if getattr(getattr(descriptor, "architecture", None), "id", "") != "GFPGAN":
        raise FaceRestoreError("WEIGHTS_INVALID", "Le fichier chargé n’est pas un modèle GFPGAN.")
    network = descriptor.model.eval()
    for parameter in network.parameters():
        parameter.requires_grad_(False)
    return network


def detect_faces(torch, detector, rgb):
    """Visages de l'image Pillow RGB ``rgb`` : boîtes, repères et score en pixels de l'image d'origine."""
    import numpy as np
    from kornia.geometry.bbox import nms
    from kornia.models.yunet.processors import PriorBox, decode
    from PIL import Image

    width, height = rgb.size
    scale = min(1.0, DETECT_MAX_SIDE / float(max(width, height)))
    small = rgb if scale >= 1.0 else rgb.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.BILINEAR)
    array = np.asarray(small, dtype=np.float32)[:, :, ::-1]     # BGR 0–255 : convention d'entraînement (OpenCV)
    h, w = array.shape[:2]
    padded_h, padded_w = -(-h // DETECT_STRIDE) * DETECT_STRIDE, -(-w // DETECT_STRIDE) * DETECT_STRIDE
    canvas = np.zeros((padded_h, padded_w, 3), dtype=np.float32)
    canvas[:h, :w] = array
    tensor = torch.from_numpy(canvas).permute(2, 0, 1).unsqueeze(0).contiguous()
    with torch.no_grad():
        output = detector(tensor)
    loc, conf, iou = output["loc"][0], output["conf"][0], output["iou"][0]
    priors = PriorBox([[10, 16, 24], [32, 48], [64, 96], [128, 192, 256]], [8, 16, 32, 64], False, (padded_h, padded_w))()
    if priors.shape[0] != loc.shape[0]:
        raise FaceRestoreError("DETECTION_FAILED", "Détection des visages impossible sur cette image.")
    boxes = decode(loc, priors, [0.1, 0.2]) * torch.tensor([padded_w, padded_h] * 7, dtype=torch.float32)
    scores = (conf[:, 1] * iou[:, 0].clamp(0.0, 1.0)).sqrt()
    keep = scores > DETECT_SCORE
    boxes, scores = boxes[keep], scores[keep]
    order = scores.sort(descending=True)[1][:DETECT_TOP_K]
    boxes, scores = boxes[order], scores[order]
    if boxes.shape[0]:
        kept = nms(boxes[:, :4], scores, DETECT_NMS_IOU)
        boxes, scores = boxes[kept], scores[kept]
    faces = []
    for row, score in zip((boxes / scale).tolist(), scores.tolist()):
        x0, y0, x1, y1 = (min(max(value, 0.0), limit) for value, limit in zip(row[:4], (width, height, width, height)))
        landmarks = tuple((row[4 + 2 * index], row[5 + 2 * index]) for index in range(5))
        if x1 - x0 <= 1 or y1 - y0 <= 1 or not all(math.isfinite(value) for point in landmarks for value in point):
            continue
        faces.append({"box": (x0, y0, x1, y1), "landmarks": landmarks, "score": float(score)})
    return faces


def _face_mask(torch):
    index = torch.arange(FACE_SIZE, dtype=torch.float32)
    v, u = torch.meshgrid(index, index, indexing="ij")
    distance = torch.minimum(torch.minimum(u, v), torch.minimum(FACE_SIZE - 1 - u, FACE_SIZE - 1 - v))
    ramp = ((distance - MASK_INSET) / MASK_FEATHER).clamp(0.0, 1.0)
    return (ramp * ramp * (3.0 - 2.0 * ramp))[None, None]


def _sampling_grid(torch, matrix, xs, ys, source_width, source_height):
    """Grille normalisée de ``grid_sample`` (align_corners=False) : point (x, y) → matrice → pixel de la source."""
    gx = matrix[0][0] * xs + matrix[0][1] * ys + matrix[0][2]
    gy = matrix[1][0] * xs + matrix[1][1] * ys + matrix[1][2]
    return torch.stack(((2.0 * gx + 1.0) / source_width - 1.0, (2.0 * gy + 1.0) / source_height - 1.0), dim=-1)[None]


def _restore(torch, network, crop, device):
    x = (crop * 2.0 - 1.0).to(device)
    with torch.no_grad():
        y = network(x, return_rgb=False, randomize_noise=False)[0]
    return ((y.float().cpu() + 1.0) / 2.0).clamp(0.0, 1.0)


def restore(request):
    """Calcul complet ; rend le compte rendu (dict). Lève FaceRestoreError pour un refus explicable."""
    started = time.monotonic()
    import numpy as np
    import torch
    import torch.nn.functional as functional
    from PIL import Image, UnidentifiedImageError

    def no_download(*_args, **_kwargs):
        raise OSError("Téléchargement interdit au calcul de restauration des visages.")

    torch.hub.load_state_dict_from_url = no_download
    torch.hub.download_url_to_file = no_download
    torch.manual_seed(0)
    gfpgan_bytes = _verified_bytes(request["gfpgan"], request["gfpgan_sha256"], GFPGAN_MAX_BYTES, "GFPGAN v1.4")
    detector_bytes = _verified_bytes(request["detector"], request["detector_sha256"], DETECTOR_MAX_BYTES, "du détecteur de visages")

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(request["input"]) as opened:
            width, height = opened.size
            if width < 1 or height < 1 or width > MAX_SIDE or height > MAX_SIDE or width * height > MAX_PIXELS:
                raise FaceRestoreError("IMAGE_TOO_LARGE", "Image trop grande : au plus 16 mégapixels et 8192 px de côté.")
            opened.load()
            alpha = opened.getchannel("A") if opened.mode in ("RGBA", "LA") else None
            rgb = opened.convert("RGB")
    except FaceRestoreError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise FaceRestoreError("IMAGE_INVALID", "Image source illisible.") from None

    detector = _load_detector(torch, detector_bytes)
    del detector_bytes
    found = detect_faces(torch, detector, rgb)
    faces, too_small, over_limit = select_faces(found, request["max_faces"])
    report = {"ok": True, "faces_detected": len(found), "faces_restored": 0, "faces_too_small": too_small,
              "faces_over_limit": over_limit, "boxes": [], "device": "cpu", "cpu_fallback": False,
              "width": width, "height": height}
    if not faces:
        report["seconds"] = round(time.monotonic() - started, 2)
        return report

    network = _load_gfpgan(torch, gfpgan_bytes)
    del gfpgan_bytes
    device = "cpu"
    if request["device"] == "auto" and torch.cuda.is_available():
        try:
            network.to("cuda")
            device = "cuda"
        except Exception:  # noqa: BLE001 - carte indisponible ou pleine : le calcul reste sur le processeur
            network.to("cpu")
            report["cpu_fallback"] = True

    image = torch.from_numpy(np.asarray(rgb, dtype=np.uint8).copy()).permute(2, 0, 1).unsqueeze(0).float().div_(255.0)
    output = image.clone()
    mask = _face_mask(torch)
    index = torch.arange(FACE_SIZE, dtype=torch.float32)
    tv, tu = torch.meshgrid(index, index, indexing="ij")
    strength = request["strength"]
    for face in faces:
        to_template = affine_matrix(similarity_transform(face["landmarks"], FFHQ_TEMPLATE_512))
        to_image = invert_similarity(to_template)
        grid = _sampling_grid(torch, to_image, tu, tv, width, height)
        crop = functional.grid_sample(image, grid, mode="bilinear", padding_mode="reflection", align_corners=False)
        try:
            restored = _restore(torch, network, crop, device)
        except Exception as error:  # noqa: BLE001 - mémoire graphique insuffisante : repli sur le processeur
            if device == "cpu" or "out of memory" not in str(error).lower() and not isinstance(error, getattr(torch.cuda, "OutOfMemoryError", ())):
                raise
            network.to("cpu")
            torch.cuda.empty_cache()
            device, report["cpu_fallback"] = "cpu", True
            restored = _restore(torch, network, crop, device)
        box = paste_box(to_image, width, height)
        if box is None:
            continue
        x0, y0, x1, y1 = box
        ys, xs = torch.meshgrid(torch.arange(y0, y1, dtype=torch.float32), torch.arange(x0, x1, dtype=torch.float32), indexing="ij")
        back = _sampling_grid(torch, to_template, xs, ys, FACE_SIZE, FACE_SIZE)
        face_pixels = functional.grid_sample(restored, back, mode="bilinear", padding_mode="zeros", align_corners=False)
        weight = functional.grid_sample(mask, back, mode="bilinear", padding_mode="zeros", align_corners=False) * strength
        region = output[:, :, y0:y1, x0:x1]
        output[:, :, y0:y1, x0:x1] = region * (1.0 - weight) + face_pixels * weight
        report["faces_restored"] += 1
        report["boxes"].append([int(round(value)) for value in face["box"]])
    report["device"] = device
    pixels = output[0].mul(255.0).round_().clamp_(0, 255).to(torch.uint8).permute(1, 2, 0).contiguous().numpy()
    result = Image.fromarray(pixels, "RGB")
    if alpha is not None:
        result.putalpha(alpha)
    temporary = request["output"].with_name(request["output"].name + ".part")
    result.save(temporary, format="PNG", compress_level=4)
    os.replace(temporary, request["output"])
    report["seconds"] = round(time.monotonic() - started, 2)
    return report


def _write_report(path, value):
    temporary = Path(str(path) + ".part")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def main(argv):
    if len(argv) != 2:
        print("Usage : face_restore_script.py <request.json>", file=sys.stderr)
        return 2
    block_network()
    try:
        raw = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("Demande de restauration illisible.", file=sys.stderr)
        return 2
    try:
        request = validate_request(raw)
    except FaceRestoreError as error:
        print(str(error), file=sys.stderr)
        return 2
    try:
        report = restore(request)
    except FaceRestoreError as error:
        report = {"ok": False, "code": error.code, "error": str(error)}
    except MemoryError:
        report = {"ok": False, "code": "OUT_OF_MEMORY", "error": "Mémoire insuffisante pour restaurer les visages de cette image."}
    except Exception as error:  # noqa: BLE001 - seule la nature de l'erreur est rapportée (jamais un chemin)
        report = {"ok": False, "code": "INTERNAL", "error": f"Échec du calcul de restauration ({type(error).__name__})."}
    _write_report(request["report"], report)
    return 0 if report.get("ok") else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
