"""Modèle facultatif d'Alpine Guard : détecteur d'échecs d'Obico (1re génération, ONNX), licence AGPL-3.0.

Source officielle : dépôt TheSpaghettiDetective/obico-server (AGPL-3.0), fichier
``ml_api/model/model-weights.onnx.url`` → https://www.obico.io/static_pub/ml-models/model-weights-5a6b1be1fa.onnx
(202 223 918 octets, SHA-256 0a6ebd8e30dbf6a450c50f9c0a5406f04ba7eb1c99fd5996e888c78bb383b9aa publié par Obico dans
``model-weights.onnx.sha256``). Chaque Worker le télécharge lui-même (module « guard-obico ») ; Alpine Makers ne le
redistribue pas. Ce n'est PAS le modèle de PrintGuard.

Graphe (YOLOv2, une classe « failure », ancres déjà décodées dans l'ONNX) : entrée ``input`` float32 [1,3,416,416]
RGB / 255 étiré sans letterbox ; sorties ``boxes`` [1,N,1,4] (x1,y1,x2,y2 normalisés 0–1) et ``confs`` [1,N,1].
Post-traitement d'Obico : confiance > 0,08 puis NMS IoU 0,45 ; le score d'une image est la somme des confiances
(boîtes dans la zone d'impression seulement ici). Obico décide sur une moyenne mobile avec une image toutes les 10 s :
Alpine Guard garde sa propre décision (seuil, images consécutives) et ramène la somme à 0–1 (÷ 0,78, seuil haut d'Obico).
"""
import hashlib
from pathlib import Path

import cv2
import numpy as np

INPUT_SIZE = 416
CONF_FLOOR = 0.08
NMS_IOU = 0.45
HIGH = 0.78
MODEL_SHA256 = "0a6ebd8e30dbf6a450c50f9c0a5406f04ba7eb1c99fd5996e888c78bb383b9aa"
MODEL_SIZE = 202223918


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _nms(boxes, scores, iou):
    order = np.argsort(-scores)
    keep = []
    while order.size:
        first = int(order[0])
        keep.append(first)
        if order.size == 1:
            break
        rest = order[1:]
        x1 = np.maximum(boxes[first, 0], boxes[rest, 0])
        y1 = np.maximum(boxes[first, 1], boxes[rest, 1])
        x2 = np.minimum(boxes[first, 2], boxes[rest, 2])
        y2 = np.minimum(boxes[first, 3], boxes[rest, 3])
        inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
        area = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        union = area[first] + area[rest] - inter
        overlap = np.where(union > 0, inter / np.maximum(union, 1e-9), 0.0)
        order = rest[overlap <= iou]
    return keep


def decode_outputs(boxes, confs, conf=CONF_FLOOR, iou=NMS_IOU):
    """Boîtes [(x1,y1,x2,y2,conf)] normalisées 0–1 depuis les sorties brutes du graphe."""
    boxes = np.asarray(boxes, dtype=np.float32).reshape(-1, 4)
    confs = np.asarray(confs, dtype=np.float32).reshape(-1)
    selected = confs > conf
    boxes, confs = np.clip(boxes[selected], 0.0, 1.0), confs[selected]
    if not len(confs):
        return []
    keep = _nms(boxes, confs, iou)
    return [(float(boxes[i, 0]), float(boxes[i, 1]), float(boxes[i, 2]), float(boxes[i, 3]), float(confs[i])) for i in keep]


def preprocess(image):
    resized = cv2.resize(image, (INPUT_SIZE, INPUT_SIZE), interpolation=cv2.INTER_LINEAR)
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    return np.ascontiguousarray(rgb.transpose(2, 0, 1)[None, ...])


def score_boxes(found, mask=None):
    """Score 0–1 : somme des confiances des boîtes dont le centre est dans la zone, ramenée au seuil haut d'Obico."""
    total = 0.0
    kept = []
    for x1, y1, x2, y2, conf in found:
        if mask is not None:
            height, width = mask.shape[:2]
            cx, cy = int((x1 + x2) / 2 * (width - 1)), int((y1 + y2) / 2 * (height - 1))
            if mask[min(max(cy, 0), height - 1), min(max(cx, 0), width - 1)] == 0:
                continue
        total += conf
        kept.append([round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4), round(conf, 4)])
    return round(min(1.0, total / HIGH), 4), kept


class ObicoModel:
    """Session ONNX Runtime CPU du modèle ; ``check_sha256`` relit le fichier au chargement (≈ 200 Mo, une fois)."""

    def __init__(self, path, check_sha256=True, threads=2):
        import onnxruntime as ort  # facultatif : absent sans le module guard-obico

        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError("modèle absent")
        self.sha256 = file_sha256(path) if check_sha256 else None
        if check_sha256 and self.sha256 != MODEL_SHA256:
            raise ValueError("empreinte du modèle différente de celle publiée par Obico : réinstalle le module")
        options = ort.SessionOptions()
        options.intra_op_num_threads = int(threads)
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])
        inputs = self.session.get_inputs()
        outputs = self.session.get_outputs()
        if len(inputs) != 1 or list(inputs[0].shape)[1:] != [3, INPUT_SIZE, INPUT_SIZE] or len(outputs) < 2:
            raise ValueError("graphe inattendu : entrée [1,3,416,416] et sorties boxes / confs exigées")
        self.input_name = inputs[0].name
        names = [output.name for output in outputs]
        self.boxes_name = "boxes" if "boxes" in names else names[0]
        self.confs_name = "confs" if "confs" in names else names[1]
        self.version = ort.__version__

    def detect(self, image):
        boxes, confs = self.session.run([self.boxes_name, self.confs_name], {self.input_name: preprocess(image)})
        return decode_outputs(boxes, confs)
