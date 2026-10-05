"""Analyse flamme/fumée d'une image : prétraitement, inférence ONNX Runtime, post-traitement.

Fonctions pures (testables sans ONNX Runtime ; numpy/OpenCV seulement pour celles qui manipulent des tableaux)
et ``Detector`` (session ONNX Runtime, choix du fournisseur, repli CPU motivé).

Modèles : exports YOLOv8 d'Ultralytics sans NMS (opset 17). Entrée ``images`` float32 [1, 3, 640, 640] RGB /255,
letterbox gris 114 centré ; sortie ``output0`` [1, 4 + classes, 8400] = cx, cy, w, h en pixels de l'image
letterboxée puis un score par classe déjà sigmoïdé (pas d'objectness). Les classes sont associées par leur NOM
(métadonnée ``names``), jamais par indice : l'ordre diffère d'un modèle du catalogue à l'autre.

Chaque détection porte aussi ``color`` (``color_stats`` : parts de pixels colorés, chauds, bleu-violet et très
lumineux dans la boîte, sur l'image d'origine) ; la règle flamme s'en sert pour écarter la lueur du point laser.

GPU : fournisseur CUDA avec ``use_tf32=0`` (TF32 décale les boîtes jusqu'à 2 px sur une RTX 5060 Ti), puis contrôle
de cohérence au démarrage contre le CPU sur des entrées déterministes ; au moindre doute (paquet sans CUDA, DLL ou
pilote absent, pas de noyau pour ce GPU, mémoire, écart hors tolérance) le moteur analyse sur le processeur et en
donne la raison. Le fournisseur annoncé est celui que rapporte la session, jamais celui qu'on a demandé.
"""
import ast
import hashlib
import math
import os
import platform
import sys
import time

# Plafond de pixels décodables (lu par OpenCV au premier décodage) : une image hostile ne réserve pas des Go.
os.environ.setdefault("OPENCV_IO_MAX_IMAGE_PIXELS", str(1 << 25))

try:
    import numpy as np
except ImportError:  # l'agent et le site n'ont pas numpy : seules les fonctions pures sont alors utilisables
    np = None
try:
    import cv2
except ImportError:
    cv2 = None

CLASSES = ("fire", "smoke")
PROVIDERS = ("auto", "gpu", "cpu")
CPU = "CPUExecutionProvider"
CUDA = "CUDAExecutionProvider"
CUDA_OPTIONS = {"device_id": 0, "use_tf32": 0}
PAD_VALUE = 114
DEFAULT_CONF = 0.10
DEFAULT_IOU = 0.45
MAX_DETECTIONS = 100
MAX_CANDIDATES = 3000          # boîtes au plus soumises à la NMS (borne le calcul si le modèle s'emballe)
CHECK_BOX_PX = 0.5             # écart GPU/CPU toléré sur les boîtes (px de l'image letterboxée)
CHECK_SCORE = 1e-3             # écart GPU/CPU toléré sur les scores
WARMUP_RUNS = 2
MAX_MODEL_BYTES = 256 * 1024 * 1024
MAX_REASON_CHARS = 400
# Couleur des boîtes (HSV d'OpenCV : H 0..180, S et V 0..255), pour écarter la lueur bleu-violet du point laser.
COLOR_MIN_S = 60               # pixel « coloré » : saturation et luminosité suffisantes
COLOR_MIN_V = 120
WARM_HUES = ((0, 35), (165, 180))   # rouge, orange, jaune
BLUE_HUES = (95, 160)               # bleu, indigo, violet
BRIGHT_V = 230
MAX_COLOR_PIXELS = 1 << 16     # pixels lus au plus par boîte (grande boîte : un pixel sur k dans chaque sens)
MISSING_RUNTIME = ("Paquets du moteur manquants ({names}) : réinstalle le composant « fire-watch » "
                   "(Surveillance IA graveuse) sur ce Worker.")


class EngineError(Exception):
    """Erreur du moteur ; le message, en français, est destiné à l'utilisateur."""


class _GpuUnavailable(Exception):
    """Le GPU n'est pas retenu ; le message devient ``fallback_reason``."""


def short_error(exc, limit=MAX_REASON_CHARS):
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text if len(text) <= limit else text[: limit - 1] + "…"


def default_threads(cpu_count=None):
    """Fils ONNX Runtime par défaut : un quart des cœurs, entre 1 et 4 (le fil d'envoi GRBL garde des cœurs libres)."""
    count = os.cpu_count() if cpu_count is None else cpu_count
    return min(4, max(1, int(count or 1) // 4))


def lower_priority():
    """Abaisse la priorité de ce processus (Windows : BELOW_NORMAL ; ailleurs : nice 5). Ne l'élève jamais.

    Réservé aux outils de mesure (``bench``, ``evaluate``) : le moteur de surveillance (``server``) garde la priorité
    normale choisie par son lanceur depuis l'agent 1.35.8.
    """
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.GetPriorityClass.argtypes = [wintypes.HANDLE]
            kernel32.GetPriorityClass.restype = wintypes.DWORD
            kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.SetPriorityClass.restype = wintypes.BOOL
            handle = kernel32.GetCurrentProcess()
            current = kernel32.GetPriorityClass(handle)
            # NORMAL, ABOVE_NORMAL, HIGH, REALTIME → BELOW_NORMAL ; IDLE et BELOW_NORMAL restent tels quels.
            if current in (0x20, 0x8000, 0x80, 0x100):
                return bool(kernel32.SetPriorityClass(handle, 0x4000))
            return True
        except (OSError, AttributeError, ValueError):
            return False
    try:
        current = os.nice(0)
        if current < 5:
            os.nice(5 - current)
        return True
    except (OSError, AttributeError):
        return False


def utf8_stderr():
    """Journal (stderr) toujours en UTF-8, quel que soit le code de page de la console ou du lanceur."""
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError, OSError):
        pass


def require_runtime(onnxruntime=True):
    """Vérifie numpy et OpenCV (et ONNX Runtime) ; ``EngineError`` claire sinon. Renvoie le module onnxruntime."""
    missing = [name for name, module in (("numpy", np), ("opencv-python-headless", cv2)) if module is None]
    ort = None
    if onnxruntime:
        try:
            import onnxruntime as ort
        except ImportError:
            missing.append("onnxruntime")
    if missing:
        raise EngineError(MISSING_RUNTIME.format(names=", ".join(missing)))
    return ort


# --- Classes du modèle -------------------------------------------------------------------------------------------

def parse_names(raw):
    """Métadonnée ``names`` d'un export Ultralytics (``"{0: 'smoke', 1: 'fire'}"``) → ``{indice: nom}``.

    Lue avec ``ast.literal_eval`` (littéraux seulement, jamais d'exécution). Accepte aussi un dict déjà décodé
    (clés entières ou textuelles, p. ex. relu d'une ligne JSON ``ready``).
    """
    if isinstance(raw, dict):
        value = raw
    else:
        if not isinstance(raw, str) or not raw.strip():
            raise EngineError("Métadonnée « names » absente du modèle ONNX : exporte-le avec fire_watch_engine.export_onnx.")
        if len(raw) > 4096:
            raise EngineError("Métadonnée « names » du modèle ONNX trop longue.")
        try:
            value = ast.literal_eval(raw.strip())
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            raise EngineError("Métadonnée « names » illisible dans le modèle ONNX.") from None
    if isinstance(value, (list, tuple)):
        value = dict(enumerate(value))
    if not isinstance(value, dict) or not value:
        raise EngineError("Métadonnée « names » invalide dans le modèle ONNX.")
    names = {}
    for key, name in value.items():
        if isinstance(key, bool) or not isinstance(key, (int, str)) or not isinstance(name, str):
            raise EngineError("Métadonnée « names » invalide dans le modèle ONNX.")
        try:
            index = int(key)
        except ValueError:
            raise EngineError("Métadonnée « names » invalide dans le modèle ONNX.") from None
        if index in names:
            raise EngineError("Métadonnée « names » invalide (indice répété).")
        names[index] = name
    return dict(sorted(names.items()))


def class_labels(names):
    """Libellé canonique (``fire``/``smoke``) de chaque sortie du modèle, dans l'ordre des indices.

    Refuse tout modèle dont les classes ne sont pas exactement {fire, smoke} (casse ignorée).
    """
    if sorted(names) != list(range(len(names))):
        raise EngineError("Classes du modèle mal numérotées (indices 0 à n−1 attendus).")
    labels = tuple(str(names[index]).strip().lower() for index in range(len(names)))
    if len(labels) != len(CLASSES) or set(labels) != set(CLASSES):
        shown = ", ".join(str(names[index]) for index in range(len(names)))
        raise EngineError(f"Classes du modèle inattendues ({shown}) : un modèle de surveillance doit détecter "
                          "exactement « fire » et « smoke ».")
    return labels


def parse_model_option(text):
    """Option ``--model id=chemin.onnx`` des outils (ou un chemin seul : l'identifiant est le nom du dossier)."""
    text = str(text or "").strip()
    model_id, separator, path = text.partition("=")
    if not separator:
        model_id, path = "", text
    model_id, path = model_id.strip(), path.strip()
    if not path:
        raise EngineError("Option --model : chemin du modèle ONNX manquant (id=chemin.onnx).")
    if not model_id:
        model_id = os.path.basename(os.path.dirname(os.path.abspath(path))) or "modele"
    if len(model_id) > 64 or not all(char.isalnum() or char in "-_." for char in model_id):
        raise EngineError("Option --model : identifiant invalide (lettres, chiffres, - _ . ; 64 au plus).")
    return model_id, path


def timing_summary(values):
    """``{"p50", "p95", "max"}`` (ms) d'une série de durées ; ``None`` si la série est vide."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2.0
    p95 = ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
    return {"p50": round(median, 2), "p95": round(p95, 2), "max": round(ordered[-1], 2)}


def names_match(names, expected):
    """Vrai si deux tables de classes associent les mêmes noms aux mêmes indices (casse ignorée)."""
    try:
        return class_labels(parse_names(names)) == class_labels(parse_names(expected))
    except EngineError:
        return False


# --- Géométrie ---------------------------------------------------------------------------------------------------

def letterbox_geometry(width, height, size=(640, 640)):
    """Redimensionnement « letterbox » d'Ultralytics (centré, agrandissement permis, arrondis identiques).

    ``size`` = (hauteur, largeur) de l'entrée du modèle. Renvoie ``(ratio, (largeur, hauteur) redimensionnées,
    (gauche, haut, droite, bas))`` en pixels.
    """
    target_h, target_w = int(size[0]), int(size[1])
    if width <= 0 or height <= 0:
        raise EngineError("Image vide.")
    ratio = min(target_h / height, target_w / width)
    new_w, new_h = int(round(width * ratio)), int(round(height * ratio))
    dw, dh = (target_w - new_w) / 2.0, (target_h - new_h) / 2.0
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    return ratio, (new_w, new_h), (left, top, right, bottom)


def unletterbox_box(box, ratio, pad, width, height):
    """Boîte xyxy de l'image letterboxée → boîte normalisée [0, 1] de l'image d'origine (bornée au cadre)."""
    left, top = pad
    x1, y1, x2, y2 = ((box[0] - left) / ratio, (box[1] - top) / ratio, (box[2] - left) / ratio, (box[3] - top) / ratio)
    x1, x2 = (min(float(width), max(0.0, value)) for value in (x1, x2))
    y1, y2 = (min(float(height), max(0.0, value)) for value in (y1, y2))
    return [x1 / width, y1 / height, x2 / width, y2 / height]


def letterbox(image, size=(640, 640)):
    """Image BGR → image letterboxée ``size`` (bordures 114), ratio, (gauche, haut)."""
    height, width = image.shape[:2]
    ratio, (new_w, new_h), (left, top, right, bottom) = letterbox_geometry(width, height, size)
    if (new_w, new_h) != (width, height):
        image = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    image = cv2.copyMakeBorder(image, top, bottom, left, right, cv2.BORDER_CONSTANT, value=(PAD_VALUE,) * 3)
    return image, ratio, (left, top)


def preprocess(image, size=(640, 640)):
    """Image BGR uint8 → tenseur float32 [1, 3, H, W] RGB /255, ratio, (gauche, haut)."""
    boxed, ratio, pad = letterbox(image, size)
    rgb = cv2.cvtColor(boxed, cv2.COLOR_BGR2RGB)
    tensor = rgb.transpose(2, 0, 1).astype(np.float32, order="C")[None]
    tensor /= np.float32(255.0)
    return tensor, ratio, pad


def nms(boxes, scores, iou_threshold):
    """NMS gloutonne (numpy) : indices conservés, par score décroissant ; supprime IoU > seuil."""
    order = np.argsort(-scores, kind="stable")
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(x2 - x1, 0.0) * np.maximum(y2 - y1, 0.0)
    keep = []
    while order.size:
        index = order[0]
        keep.append(int(index))
        rest = order[1:]
        if not rest.size:
            break
        width = np.maximum(0.0, np.minimum(x2[index], x2[rest]) - np.maximum(x1[index], x1[rest]))
        height = np.maximum(0.0, np.minimum(y2[index], y2[rest]) - np.maximum(y1[index], y1[rest]))
        inter = width * height
        union = areas[index] + areas[rest] - inter
        overlap = np.where(union > 0, inter / np.maximum(union, 1e-12), 0.0)
        order = rest[overlap <= iou_threshold]
    return np.asarray(keep, dtype=np.int64)


def nms_per_class(boxes, scores, class_ids, iou_threshold):
    """NMS séparée par classe (une flamme ne supprime jamais une fumée) ; indices par score décroissant."""
    keep = []
    for class_id in np.unique(class_ids):
        members = np.flatnonzero(class_ids == class_id)
        keep.extend(members[nms(boxes[members], scores[members], iou_threshold)].tolist())
    keep = np.asarray(keep, dtype=np.int64)
    return keep[np.argsort(-scores[keep], kind="stable")] if keep.size else keep


def postprocess(output, labels, ratio, pad, image_size, conf=DEFAULT_CONF, iou=DEFAULT_IOU, max_det=MAX_DETECTIONS):
    """Sortie brute [1, 4 + classes, ancres] → détections ``{"label", "score", "box"}`` normalisées, triées par score.

    Score = classe la plus probable de chaque ancre (> ``conf``), boîtes cx,cy,w,h → xyxy, NMS par classe dans
    l'espace letterboxé (comme Ultralytics), puis retrait des bordures, division par le ratio, bornage au cadre et
    normalisation par la taille de l'image d'origine.
    """
    width, height = image_size
    prediction = np.asarray(output)
    if prediction.ndim == 3:
        prediction = prediction[0]
    if prediction.ndim != 2 or prediction.shape[0] != 4 + len(labels):
        raise EngineError("Sortie du modèle inattendue.")
    prediction = prediction.T.astype(np.float32, copy=False)
    scores = prediction[:, 4:]
    class_ids = scores.argmax(axis=1)
    best = scores[np.arange(scores.shape[0]), class_ids]
    finite = np.isfinite(prediction).all(axis=1)
    mask = finite & (best > conf)
    if not mask.any():
        return []
    xywh, best, class_ids = prediction[mask, :4], best[mask], class_ids[mask]
    if best.size > MAX_CANDIDATES:
        top = np.argsort(-best, kind="stable")[:MAX_CANDIDATES]
        xywh, best, class_ids = xywh[top], best[top], class_ids[top]
    boxes = np.empty_like(xywh)
    boxes[:, 0] = xywh[:, 0] - xywh[:, 2] / 2.0
    boxes[:, 1] = xywh[:, 1] - xywh[:, 3] / 2.0
    boxes[:, 2] = xywh[:, 0] + xywh[:, 2] / 2.0
    boxes[:, 3] = xywh[:, 1] + xywh[:, 3] / 2.0
    detections = []
    for index in nms_per_class(boxes, best, class_ids, iou):
        box = unletterbox_box([float(value) for value in boxes[index]], ratio, pad, width, height)
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        detections.append({"label": labels[int(class_ids[index])], "score": round(float(best[index]), 4),
                           "box": [round(value, 5) for value in box]})
        if len(detections) >= max_det:
            break
    return detections


def color_stats(image, box):
    """Couleur des pixels d'une boîte normalisée d'une image BGR : ``{"colored", "warm", "blue", "bright"}``.

    ``colored`` = pixels S ≥ 60 et V ≥ 120 / pixels de la boîte ; ``warm`` (H ≤ 35 ou H ≥ 165) et ``blue``
    (95 ≤ H ≤ 160) = parts des pixels colorés ; ``bright`` = pixels V ≥ 230 / pixels de la boîte. Fractions à 4
    décimales. Une grande boîte est lue un pixel sur k dans chaque sens (``MAX_COLOR_PIXELS`` au plus) : le coût
    reste borné quelle que soit la taille de l'image. ``None`` si l'image n'est pas BGR ou la boîte vide.
    """
    if image is None or image.ndim != 3 or image.shape[2] != 3:
        return None
    height, width = image.shape[:2]
    x1, y1 = int(box[0] * width), int(box[1] * height)
    x2, y2 = max(x1 + 1, int(box[2] * width)), max(y1 + 1, int(box[3] * height))
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(width, x2), min(height, y2)
    if x2 <= x1 or y2 <= y1:
        return None
    step = max(1, math.ceil(math.sqrt((x2 - x1) * (y2 - y1) / MAX_COLOR_PIXELS)))
    crop = image[y1:y2:step, x1:x2:step]
    if step > 1:
        crop = np.ascontiguousarray(crop)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    total = hsv.shape[0] * hsv.shape[1]

    def count(h_low, h_high, s_low, v_low):
        return cv2.countNonZero(cv2.inRange(hsv, (h_low, s_low, v_low), (h_high, 255, 255)))

    colored = count(0, 180, COLOR_MIN_S, COLOR_MIN_V)
    warm = sum(count(low, high, COLOR_MIN_S, COLOR_MIN_V) for low, high in WARM_HUES) if colored else 0
    blue = count(BLUE_HUES[0], BLUE_HUES[1], COLOR_MIN_S, COLOR_MIN_V) if colored else 0
    bright = count(0, 180, 0, BRIGHT_V)
    return {"colored": round(colored / total, 4), "warm": round(warm / max(1, colored), 4),
            "blue": round(blue / max(1, colored), 4), "bright": round(bright / total, 4)}


def add_colors(image, detections):
    """Ajoute ``color`` (``color_stats``) à chaque détection (au plus ``MAX_DETECTIONS``), sur l'image d'origine."""
    for detection in detections[:MAX_DETECTIONS]:
        color = color_stats(image, detection["box"])
        if color is not None:
            detection["color"] = color
    return detections


def compare_outputs(reference, other):
    """Écarts maximaux (boîtes en px, scores) entre deux listes de sorties brutes [1, 4 + classes, ancres]."""
    box_diff = score_diff = 0.0
    for ref, out in zip(reference, other):
        ref, out = np.asarray(ref, dtype=np.float64), np.asarray(out, dtype=np.float64)
        if ref.shape != out.shape or ref.ndim < 2 or not np.isfinite(ref).all() or not np.isfinite(out).all():
            return math.inf, math.inf
        ref = ref.reshape(ref.shape[-2], ref.shape[-1])
        out = out.reshape(out.shape[-2], out.shape[-1])
        box_diff = max(box_diff, float(np.abs(ref[:4] - out[:4]).max()))
        score_diff = max(score_diff, float(np.abs(ref[4:] - out[4:]).max()))
    return box_diff, score_diff


def synthetic_image(width=640, height=480, seed=2):
    """Image BGR déterministe : fond sombre bruité et « flamme » dessinée (couches rouge → jaune pâle, floutées).

    Sert au contrôle de cohérence GPU/CPU, au préchauffage, aux mesures et aux tests (les deux modèles du catalogue
    y voient une flamme de score modeste) : ce n'est pas une vraie flamme et elle ne valide aucun réglage.
    """
    rng = np.random.default_rng(seed)
    image = np.full((height, width, 3), (30, 30, 35), np.float64) + rng.normal(0.0, 5.0, (height, width, 3))
    image = np.clip(image, 0, 255).astype(np.uint8)
    center, base = width // 2, int(height * 0.8)
    for blue, green, red, scale in ((0, 40, 200, 1.0), (0, 110, 255, 0.8), (40, 190, 255, 0.6), (180, 240, 255, 0.35)):
        outline = []
        for side, steps in ((-1, range(24)), (1, range(23, -1, -1))):
            for step in steps:
                t = step / 23
                half = int(width * 0.12 * scale * np.sin(np.pi * t) ** 0.8 * (1 - 0.6 * t))
                outline.append((center + side * half + int(rng.normal(0.0, 6.0 * scale)),
                                int(base - t * height * 0.6 * scale)))
        cv2.fillPoly(image, [np.array(outline, np.int32)], (blue, green, red))
    return cv2.GaussianBlur(image, (0, 0), 3)


def synthetic_jpeg(width=640, height=480, seed=2, quality=90):
    ok, encoded = cv2.imencode(".jpg", synthetic_image(width, height, seed), [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise EngineError("Encodage JPEG impossible.")
    return encoded.tobytes()


def decode_jpeg(data):
    """Octets JPEG → image BGR ; ``EngineError`` si illisible ou trop grande."""
    if not data:
        raise EngineError("Image absente.")
    try:
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    except cv2.error:
        image = None
    if image is None or image.ndim != 3 or image.shape[0] < 1 or image.shape[1] < 1:
        raise EngineError("Image JPEG illisible ou trop grande.")
    return image


def versions(ort=None):
    result = {"onnxruntime": getattr(ort, "__version__", None) if ort is not None else None,
              "numpy": getattr(np, "__version__", None), "opencv": getattr(cv2, "__version__", None),
              "python": platform.python_version()}
    if ort is None:
        try:
            import onnxruntime
            result["onnxruntime"] = onnxruntime.__version__
        except ImportError:
            pass
    return result


# --- Session ONNX Runtime ----------------------------------------------------------------------------------------

class Detector:
    """Détecteur flamme/fumée sur une session ONNX Runtime (un appel à la fois ; pas de fil interne).

    ``provider`` : ``cpu`` (CPU seulement), ``auto`` ou ``gpu`` (CUDA si disponible ET cohérent avec le CPU, sinon
    repli CPU avec ``fallback_reason``). ``runtime`` permet d'injecter un faux module onnxruntime dans les tests.
    """

    def __init__(self, model_path, provider="auto", threads=None, runtime=None):
        if provider not in PROVIDERS:
            raise EngineError("Fournisseur d'analyse inconnu (auto, gpu ou cpu).")
        self.ort = runtime if runtime is not None else require_runtime()
        if runtime is not None:
            require_runtime(onnxruntime=False)
        self.requested = provider
        self.threads = int(threads) if threads else default_threads()
        if not 1 <= self.threads <= 64:
            raise EngineError("Nombre de fils invalide (1 à 64).")
        self.model_path = os.path.abspath(str(model_path))
        self.model_bytes = self._read_model(self.model_path)
        self.sha256 = hashlib.sha256(self.model_bytes).hexdigest()
        self.fallback_reason = None
        self.check = {"compared": False, "max_box_diff_px": None, "max_score_diff": None,
                      "box_limit_px": CHECK_BOX_PX, "score_limit": CHECK_SCORE}
        self.warmup_ms = None
        self.names = None
        self.labels = None
        self.input_name = None
        self.input_shape = None
        self.output_shape = None
        self.size = None
        self._cpu_session = None
        self._tensors = None
        self.runtime_fallback = False
        cv2.setNumThreads(1)
        severity = getattr(self.ort, "set_default_logger_severity", None)
        if callable(severity):
            severity(3)  # erreurs seulement dans le journal (les avertissements CUDA y arrivent en UTF-16)
        # Les octets du modèle restent en mémoire (≈ 12 Mo) : ils servent à ouvrir la session CPU de secours.
        self.session = self._open()

    @staticmethod
    def _read_model(path):
        # Lu en Python puis passé en octets : aucun souci de chemin accentué côté ONNX Runtime.
        try:
            size = os.path.getsize(path)
            if size > MAX_MODEL_BYTES:
                raise EngineError("Modèle ONNX trop volumineux.")
            with open(path, "rb") as handle:
                return handle.read()
        except FileNotFoundError:
            raise EngineError(f"Modèle ONNX introuvable : {path}") from None
        except OSError as exc:
            raise EngineError(f"Modèle ONNX illisible : {short_error(exc)}") from None

    @property
    def provider(self):
        providers = self.session.get_providers() if self.session is not None else []
        return providers[0] if providers else None

    def _options(self):
        options = self.ort.SessionOptions()
        options.intra_op_num_threads = self.threads
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        try:
            options.execution_mode = self.ort.ExecutionMode.ORT_SEQUENTIAL
            options.graph_optimization_level = self.ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        except AttributeError:
            pass
        # Pas d'attente active des fils entre deux images : le processeur reste au fil d'envoi GRBL.
        for key in ("session.intra_op.allow_spinning", "session.inter_op.allow_spinning"):
            try:
                options.add_session_config_entry(key, "0")
            except (AttributeError, RuntimeError):
                pass
        return options

    def _session(self, providers):
        return self.ort.InferenceSession(self.model_bytes, sess_options=self._options(), providers=providers)

    def _cpu(self):
        if self._cpu_session is None:
            self._cpu_session = self._session([CPU])
        return self._cpu_session

    def _inspect(self, session):
        inputs, outputs = session.get_inputs(), session.get_outputs()
        if len(inputs) != 1 or not outputs:
            raise EngineError("Modèle ONNX inattendu (une entrée image attendue).")
        shape = list(inputs[0].shape)
        if len(shape) != 4 or not all(isinstance(dim, int) and dim > 0 for dim in shape) or shape[:2] != [1, 3]:
            raise EngineError("Entrée du modèle inattendue (image [1, 3, H, W] de taille fixe attendue).")
        metadata = session.get_modelmeta().custom_metadata_map or {}
        names = parse_names(metadata.get("names"))
        labels = class_labels(names)
        out_shape = list(outputs[0].shape)
        if len(out_shape) != 3 or (isinstance(out_shape[1], int) and out_shape[1] != 4 + len(labels)):
            raise EngineError("Sortie du modèle inattendue (export YOLOv8 sans NMS attendu).")
        self.names, self.labels = names, labels
        self.input_name, self.input_shape, self.output_shape = inputs[0].name, shape, out_shape
        self.size = (shape[2], shape[3])

    def _check_tensors(self):
        """Entrées déterministes : bruit uniforme (graine 0) et image synthétique prétraitée."""
        if self._tensors is None:
            height, width = self.size
            noise = np.random.default_rng(0).random((1, 3, height, width), dtype=np.float32)
            image = synthetic_image(max(32, width), max(24, height * 3 // 4))
            self._tensors = (noise, preprocess(image, self.size)[0])
        return self._tensors

    def _run_session(self, session, tensor):
        return session.run(None, {self.input_name: tensor})[0]

    def _warm(self, session, runs=WARMUP_RUNS):
        tensor = self._check_tensors()[1]
        started = time.perf_counter()
        self._run_session(session, tensor)
        first = (time.perf_counter() - started) * 1000.0
        for _ in range(max(0, runs - 1)):
            self._run_session(session, tensor)
        return round(first, 1)

    def _open(self):
        if self.requested == "cpu":
            session = self._cpu()
            self._inspect(session)
            self.warmup_ms = self._warm(session)
            return session
        try:
            return self._open_gpu()
        except _GpuUnavailable as exc:
            self.fallback_reason = str(exc)
        session = self._cpu()
        if self.labels is None:
            self._inspect(session)
        self.warmup_ms = self._warm(session)
        return session

    def _open_gpu(self):
        ort = self.ort
        try:
            available = list(ort.get_available_providers())
        except Exception as exc:  # noqa: BLE001 - tout échec du runtime GPU mène au CPU, motivé
            raise _GpuUnavailable(f"Fournisseurs ONNX Runtime illisibles ({short_error(exc)}) : analyse sur le processeur.")
        if CUDA not in available:
            raise _GpuUnavailable("Le runtime ONNX Runtime installé ne propose pas CUDA : analyse sur le processeur "
                                  "(runtime GPU non installé ou incompatible).")
        preload = getattr(ort, "preload_dlls", None)
        if callable(preload):
            try:
                preload()
            except Exception as exc:  # noqa: BLE001 - la création de session dira si CUDA manque vraiment
                print(f"[fire-watch-engine] préchargement des DLL CUDA : {short_error(exc)}", file=sys.stderr, flush=True)
        try:
            session = self._session([(CUDA, dict(CUDA_OPTIONS)), CPU])
        except Exception as exc:  # noqa: BLE001
            raise _GpuUnavailable(f"Initialisation CUDA impossible ({short_error(exc)}) : analyse sur le processeur.")
        chosen = (session.get_providers() or [None])[0]
        if chosen != CUDA:
            raise _GpuUnavailable("CUDA indisponible (pilote NVIDIA ≥ 580, DLL CUDA/cuDNN ou GPU absents) : "
                                  "analyse sur le processeur.")
        self._inspect(session)
        tensors = self._check_tensors()
        try:
            started = time.perf_counter()
            gpu_outputs = [self._run_session(session, tensors[0])]
            self.warmup_ms = round((time.perf_counter() - started) * 1000.0, 1)
            gpu_outputs.append(self._run_session(session, tensors[1]))
        except Exception as exc:  # noqa: BLE001 - noyau absent pour ce GPU, mémoire, pilote…
            raise _GpuUnavailable(f"Exécution sur le GPU impossible ({short_error(exc)}) : analyse sur le processeur.")
        cpu = self._cpu()
        cpu_outputs = [self._run_session(cpu, tensor) for tensor in tensors]
        box_diff, score_diff = compare_outputs(cpu_outputs, gpu_outputs)
        finite = math.isfinite(box_diff) and math.isfinite(score_diff)
        self.check.update({"compared": True, "max_box_diff_px": round(box_diff, 5) if finite else None,
                           "max_score_diff": round(score_diff, 7) if finite else None})
        if not finite:
            raise _GpuUnavailable("Sortie GPU invalide au contrôle de cohérence : analyse sur le processeur.")
        if box_diff > CHECK_BOX_PX or score_diff > CHECK_SCORE:
            raise _GpuUnavailable(f"Résultats GPU incohérents avec le processeur (écart boîtes {box_diff:.3f} px, "
                                  f"scores {score_diff:.5f}) : analyse sur le processeur.")
        for _ in range(max(0, WARMUP_RUNS - 1)):
            self._run_session(session, tensors[1])
        self._cpu_session = None  # référence CPU libérée ; recréée seulement en cas de panne GPU en service
        return session

    def _infer(self, tensor):
        try:
            return self._run_session(self.session, tensor)
        except Exception as exc:  # noqa: BLE001
            if self.provider != CUDA:
                raise EngineError(f"Inférence impossible : {short_error(exc)}") from None
            # Panne GPU en service (mémoire prise par un autre moteur, pilote réinitialisé…) : la surveillance
            # continue sur le processeur, raison annoncée dans chaque résultat.
            self.fallback_reason = (f"Erreur GPU pendant l'analyse ({short_error(exc, 200)}) : analyse poursuivie "
                                    "sur le processeur.")
            self.runtime_fallback = True
            print(f"[fire-watch-engine] {self.fallback_reason}", file=sys.stderr, flush=True)
            try:
                self.session = self._cpu()
                return self._run_session(self.session, tensor)
            except Exception as retry:  # noqa: BLE001
                raise EngineError(f"Inférence impossible : {short_error(retry)}") from None

    def infer_image(self, image, conf=DEFAULT_CONF, iou=DEFAULT_IOU, decode_ms=0.0):
        """Analyse d'une image BGR ; boîtes normalisées dans l'image d'origine, durées par étape (ms)."""
        started = time.perf_counter()
        height, width = image.shape[:2]
        tensor, ratio, pad = preprocess(image, self.size)
        prepared = time.perf_counter()
        output = self._infer(tensor)
        inferred = time.perf_counter()
        detections = add_colors(image, postprocess(output, self.labels, ratio, pad, (width, height), conf=conf, iou=iou))
        finished = time.perf_counter()
        result = {"width": int(width), "height": int(height), "detections": detections,
                  "timing_ms": {"decode": round(decode_ms, 2), "preprocess": round((prepared - started) * 1000.0, 2),
                                "inference": round((inferred - prepared) * 1000.0, 2),
                                "postprocess": round((finished - inferred) * 1000.0, 2),
                                "total": round(decode_ms + (finished - started) * 1000.0, 2)},
                  "provider": self.provider}
        if self.runtime_fallback:  # repli survenu après la ligne « ready » : le service doit le voir
            result["fallback_reason"] = self.fallback_reason
        return result

    def infer_jpeg(self, data, conf=DEFAULT_CONF, iou=DEFAULT_IOU):
        started = time.perf_counter()
        image = decode_jpeg(data)
        return self.infer_image(image, conf=conf, iou=iou, decode_ms=(time.perf_counter() - started) * 1000.0)

    def describe(self):
        """Description du moteur prêt (ligne ``ready`` du protocole, sans le champ ``type``)."""
        return {"provider": self.provider, "requested": self.requested, "fallback_reason": self.fallback_reason,
                "model": {"path": self.model_path, "sha256": self.sha256,
                          "names": {str(key): value for key, value in self.names.items()},
                          "input": list(self.input_shape), "output": list(self.output_shape)},
                "warmup_ms": self.warmup_ms, "check": dict(self.check), "threads": self.threads,
                "versions": versions(self.ort)}
