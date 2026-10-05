"""Règles de la surveillance IA des graveuses (flammes, fumée) : réglages, filtrage, validation temporelle.

Module pur (bibliothèque standard seulement) partagé par l'agent Worker (``fire_watch.py``), l'outil
d'évaluation vidéo (``fire_watch_engine/evaluate.py``) et le site (``fire_watch.py`` à la racine du dashboard).

Les valeurs par défaut sont des points de départ prudents, pas des réglages validés : les modèles n'ont pas été
entraînés pour une graveuse (fumée normale de gravure, reflets, point laser bleu). Elles se règlent sur la
machine, caméra en place, et se vérifient avec des vidéos enregistrées. Aucun réglage n'est universellement
fiable, et la surveillance ne remplace jamais la présence d'un opérateur.

Validation temporelle : une classe est « confirmée » quand au moins ``hits`` des ``window`` dernières images
analysées la contiennent (score, surface et zone de travail respectés), toutes prises dans une fenêtre de
``max_window_s`` secondes d'horodatage de capture. Une image déjà analysée n'est jamais recomptée et un trou
plus long que la fenêtre efface l'historique : des images anciennes ne s'additionnent pas à des récentes.

Lueur du point laser : les modèles prennent souvent le point bleu-violet d'un laser à diode pour une flamme. Le
moteur joint à chaque détection la couleur des pixels de sa boîte (``color`` : fractions ``colored``, ``warm``,
``blue``, ``bright``) ; la règle flamme écarte par défaut (``reject_laser_glow``) une boîte colorée surtout bleue et
presque pas chaude (``is_laser_glow``). Mesuré sur 13 vidéos : lueur laser 100 % bleue et 0 % chaude, vraies
flammes chaudes à 96 % au moins. Une flamme bleue (gaz, alcool) serait écartée aussi : l'option se décoche.
"""
import math
import re
from collections import deque

MODES = ("off", "observe", "auto_stop")
CLASSES = ("fire", "smoke")
CLASS_LABELS = {"fire": "flamme", "smoke": "fumée"}
ACTIONS = ("ignore", "alert", "stop")
PROVIDERS = ("auto", "gpu", "cpu")
LOSS_ACTIONS = ("stop", "alert")
MODEL_IDS = ("rabahdev-yolov8n", "luminous0219-yolov8n")
DEFAULT_MODEL = "rabahdev-yolov8n"
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MAX_DETECTIONS = 100
COLOR_KEYS = ("colored", "warm", "blue", "bright")
# Lueur laser : part de pixels colorés dans la boîte, part de bleu-violet et part de chaud parmi les colorés.
GLOW_MIN_COLORED = 0.02
GLOW_MIN_BLUE = 0.5
GLOW_MAX_WARM = 0.2

DEFAULT_RULES = {
    # Flamme : peu d'images exigées pour rester réactif ; la surface minimale écarte le point laser seul et le
    # filtre de couleur sa lueur bleu-violet (qui permet un score minimal plus bas).
    "fire": {"enabled": True, "action": "stop", "min_score": 0.30, "min_area_pct": 0.02, "hits": 3, "window": 5,
             "max_window_s": 2.5, "clear_after_s": 10.0, "reject_laser_glow": True},
    # Fumée : la gravure en produit normalement ; alerte seulement, sur une présence large et persistante.
    "smoke": {"enabled": True, "action": "alert", "min_score": 0.55, "min_area_pct": 1.0, "hits": 8, "window": 10,
              "max_window_s": 4.0, "clear_after_s": 20.0},
}
DEFAULT_CONFIG = {
    "mode": "off",
    "camera_id": None,
    "model": DEFAULT_MODEL,
    "provider": "auto",
    "analysis_fps": 4.0,
    "roi": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
    "fire": dict(DEFAULT_RULES["fire"]),
    "smoke": dict(DEFAULT_RULES["smoke"]),
    "stale_after_s": 3.0,
    "frozen_after_s": 8.0,
    "loss_action": "stop",
    "loss_grace_s": 3.0,
    "require_healthy_to_start": True,
    "capture_after_stop": True,
    "retention_days": 30,
    "max_storage_mb": 500,
}
# (minimum, maximum) inclus ; les entiers sont signalés par des bornes entières.
LIMITS = {
    "analysis_fps": (1.0, 10.0),
    "stale_after_s": (1.0, 30.0),
    "frozen_after_s": (2.0, 120.0),
    "loss_grace_s": (0.0, 30.0),
    "retention_days": (1, 365),
    "max_storage_mb": (50, 20000),
    "min_score": (0.05, 0.99),
    "min_area_pct": (0.0, 50.0),
    "hits": (1, 30),
    "window": (1, 60),
    "max_window_s": (0.5, 30.0),
    "clear_after_s": (2.0, 300.0),
    "roi_min": (0.05, 1.0),
}
LABELS = {
    "analysis_fps": "Cadence d’analyse (images/s)",
    "stale_after_s": "Image trop ancienne après (s)",
    "frozen_after_s": "Image figée après (s)",
    "loss_grace_s": "Délai avant réaction à une perte (s)",
    "retention_days": "Conservation des captures (jours)",
    "max_storage_mb": "Place maximale des captures (Mo)",
    "min_score": "Score minimal",
    "min_area_pct": "Surface minimale (% de l’image)",
    "hits": "Détections exigées (k)",
    "window": "Images observées (n)",
    "max_window_s": "Fenêtre maximale (s)",
    "clear_after_s": "Fin d’incident après (s sans détection)",
}


def _number(value, key, integer=False, label=None):
    low, high = LIMITS[key]
    name = label or LABELS.get(key, key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} : nombre attendu.")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} : nombre attendu.")
    if integer:
        if number != int(number):
            raise ValueError(f"{name} : nombre entier attendu.")
        number = int(number)
    if not low <= number <= high:
        raise ValueError(f"{name} hors limites ({_format(low)} à {_format(high)}).")
    return number


def _format(value):
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).replace(".", ",")


def _bool(value, name):
    if not isinstance(value, bool):
        raise ValueError(f"{name} : vrai ou faux attendu.")
    return value


def _choice(value, choices, name):
    if value not in choices:
        raise ValueError(f"{name} invalide.")
    return value


def _rule(raw, cls, fps):
    base = dict(DEFAULT_RULES[cls])
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"Règle {CLASS_LABELS[cls]} invalide.")
    merged = {**base, **{key: raw[key] for key in base if key in raw}}
    label = CLASS_LABELS[cls].capitalize()
    rule = {
        "enabled": _bool(merged["enabled"], f"{label} : activation"),
        "action": _choice(merged["action"], ACTIONS, f"{label} : action"),
        "min_score": _number(merged["min_score"], "min_score", label=f"{label} : score minimal"),
        "min_area_pct": _number(merged["min_area_pct"], "min_area_pct", label=f"{label} : surface minimale (%)"),
        "hits": _number(merged["hits"], "hits", integer=True, label=f"{label} : détections exigées (k)"),
        "window": _number(merged["window"], "window", integer=True, label=f"{label} : images observées (n)"),
        "max_window_s": _number(merged["max_window_s"], "max_window_s", label=f"{label} : fenêtre maximale (s)"),
        "clear_after_s": _number(merged["clear_after_s"], "clear_after_s", label=f"{label} : fin d’incident (s)"),
    }
    if "reject_laser_glow" in base:
        rule["reject_laser_glow"] = _bool(merged["reject_laser_glow"], f"{label} : ignorer la lueur du point laser")
    if rule["hits"] > rule["window"]:
        raise ValueError(f"{label} : k ({rule['hits']}) ne peut pas dépasser n ({rule['window']}).")
    reachable = math.floor(rule["max_window_s"] * fps + 1e-9) + 1
    if rule["enabled"] and rule["hits"] > reachable:
        raise ValueError(f"{label} : {rule['hits']} détections exigées en {_format(rule['max_window_s'])} s à "
                         f"{_format(fps)} images/s sont impossibles ({reachable} images au plus) ; allonge la fenêtre, "
                         "augmente la cadence ou baisse k.")
    return rule


def _roi(raw):
    if raw is None:
        return dict(DEFAULT_CONFIG["roi"])
    if not isinstance(raw, dict):
        raise ValueError("Zone de travail invalide.")
    roi = {}
    for key in ("x", "y", "w", "h"):
        value = raw.get(key, DEFAULT_CONFIG["roi"][key])
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError("Zone de travail : nombres attendus (fractions de l’image).")
        roi[key] = float(value)
    low = LIMITS["roi_min"][0]
    if not (0.0 <= roi["x"] < 1.0 and 0.0 <= roi["y"] < 1.0 and low <= roi["w"] <= 1.0 and low <= roi["h"] <= 1.0):
        raise ValueError("Zone de travail hors de l’image ou trop petite (5 % au moins par côté).")
    if roi["x"] + roi["w"] > 1.0 + 1e-6 or roi["y"] + roi["h"] > 1.0 + 1e-6:
        raise ValueError("Zone de travail : elle dépasse le bord de l’image.")
    return roi


def clean_identifier(value, name="Identifiant"):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} invalide.")
    return value


def clean_config(raw, *, laser_id=None):
    """Configuration complète et bornée (``ValueError`` en français si une valeur est invalide)."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("Configuration de surveillance invalide.")
    merged = {**DEFAULT_CONFIG, **{key: raw[key] for key in DEFAULT_CONFIG if key in raw}}
    fps = _number(merged["analysis_fps"], "analysis_fps")
    config = {
        "mode": _choice(merged["mode"], MODES, "Mode de surveillance"),
        "camera_id": clean_identifier(merged["camera_id"], "Caméra"),
        "model": _choice(merged["model"], MODEL_IDS, "Modèle"),
        "provider": _choice(merged["provider"], PROVIDERS, "Accélération"),
        "analysis_fps": fps,
        "roi": _roi(merged["roi"]),
        "fire": _rule(merged["fire"], "fire", fps),
        "smoke": _rule(merged["smoke"], "smoke", fps),
        "stale_after_s": _number(merged["stale_after_s"], "stale_after_s"),
        "frozen_after_s": _number(merged["frozen_after_s"], "frozen_after_s"),
        "loss_action": _choice(merged["loss_action"], LOSS_ACTIONS, "Réaction à une perte de surveillance"),
        "loss_grace_s": _number(merged["loss_grace_s"], "loss_grace_s"),
        "require_healthy_to_start": _bool(merged["require_healthy_to_start"], "Départ exigeant une surveillance opérationnelle"),
        "capture_after_stop": _bool(merged["capture_after_stop"], "Capture après arrêt"),
        "retention_days": _number(merged["retention_days"], "retention_days", integer=True),
        "max_storage_mb": _number(merged["max_storage_mb"], "max_storage_mb", integer=True),
    }
    if config["mode"] != "off" and not config["camera_id"]:
        raise ValueError("Choisis la caméra qui filme la zone de travail avant d’activer la surveillance.")
    if laser_id is not None:
        config["laser_id"] = clean_identifier(laser_id, "Machine")
    return config


def effective_action(config, cls):
    """Action réellement appliquée : ``none``, ``alert`` ou ``stop`` (arrêt seulement en mode arrêt automatique)."""
    rule = config.get(cls) or {}
    if config.get("mode") not in {"observe", "auto_stop"} or not rule.get("enabled") or rule.get("action") == "ignore":
        return "none"
    if rule.get("action") == "stop":
        return "stop" if config.get("mode") == "auto_stop" else "alert"
    return "alert"


def _unit(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def clean_color(value):
    """Couleur d'une boîte ``{"colored", "warm", "blue", "bright"}`` (fractions 0..1, 4 décimales) ou ``None``.

    Les quatre fractions sont exigées et doivent être comprises entre 0 et 1 : sinon la couleur est ignorée (la
    détection compte alors comme une flamme ordinaire, jamais comme une lueur écartée).
    """
    if not isinstance(value, dict):
        return None
    color = {}
    for key in COLOR_KEYS:
        number = _unit(value.get(key))
        if number is None or not 0.0 <= number <= 1.0:
            return None
        color[key] = round(number, 4)
    return color


def clean_detection(item):
    """Détection normalisée ``{"label", "score", "box": [x1, y1, x2, y2]}`` (fractions de l'image) ou ``None``.

    ``color`` (facultatif, voir ``clean_color``) est conservé s'il est valide, retiré sinon.
    """
    if not isinstance(item, dict):
        return None
    label = item.get("label")
    score = _unit(item.get("score"))
    box = item.get("box")
    if label not in CLASSES or score is None or not 0.0 <= score <= 1.0 or not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    coords = [_unit(value) for value in box]
    if any(value is None for value in coords):
        return None
    x1, y1, x2, y2 = (min(1.0, max(0.0, value)) for value in coords)
    if x2 <= x1 or y2 <= y1:
        return None
    detection = {"label": label, "score": round(score, 4), "box": [round(x1, 5), round(y1, 5), round(x2, 5), round(y2, 5)]}
    color = clean_color(item.get("color"))
    if color is not None:
        detection["color"] = color
    return detection


def clean_detections(items, limit=MAX_DETECTIONS):
    if not isinstance(items, (list, tuple)):
        return []
    result = []
    for item in items:
        detection = clean_detection(item)
        if detection is not None:
            result.append(detection)
            if len(result) >= limit:
                break
    return result


def is_laser_glow(detection):
    """Vrai si la boîte ressemble à la lueur bleu-violet du point laser et pas à une flamme.

    Exige la couleur mesurée par le moteur (``color``) : au moins 2 % de pixels colorés, dont la moitié au moins
    bleu-violet et 20 % au plus chauds (rouge, orange, jaune). Sans couleur (ancien moteur, simulation) : faux.
    """
    if not isinstance(detection, dict):
        return False
    color = clean_color(detection.get("color"))
    return (color is not None and color["colored"] >= GLOW_MIN_COLORED and color["blue"] >= GLOW_MIN_BLUE
            and color["warm"] <= GLOW_MAX_WARM)


def filter_detections(detections, rule, roi, cls):
    """Détections de ``cls`` qui comptent pour la règle : score, surface et centre dans la zone de travail.

    Pour la flamme, la lueur du point laser (``is_laser_glow``) est écartée si la règle l'active
    (``reject_laser_glow``, vrai par défaut). Aucun effet sur la fumée.
    """
    if not rule or not rule.get("enabled"):
        return []
    roi = roi or DEFAULT_CONFIG["roi"]
    reject_glow = cls == "fire" and bool(rule.get("reject_laser_glow", DEFAULT_RULES["fire"]["reject_laser_glow"]))
    kept = []
    for item in clean_detections(detections):
        if item["label"] != cls or item["score"] < rule["min_score"]:
            continue
        if reject_glow and is_laser_glow(item):
            continue
        x1, y1, x2, y2 = item["box"]
        if (x2 - x1) * (y2 - y1) * 100.0 < rule["min_area_pct"]:
            continue
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        if not (roi["x"] <= cx <= roi["x"] + roi["w"] and roi["y"] <= cy <= roi["y"] + roi["h"]):
            continue
        kept.append(item)
    kept.sort(key=lambda item: item["score"], reverse=True)
    return kept


def latency_bound(config, cls):
    """Latence nominale de confirmation (s) quand la classe est détectée sur chaque image analysée.

    = (k − 1) intervalles entre analyses + un intervalle d'échantillonnage. Le temps de capture et d'inférence,
    mesuré en service, s'y ajoute. Des détections intermittentes allongent la confirmation ; au-delà de
    ``max_window_s`` les indices anciens sont oubliés : la règle ne confirme jamais sur des images périmées.
    """
    rule = config[cls]
    period = 1.0 / float(config["analysis_fps"])
    return round((rule["hits"] - 1) * period + period, 3)


def loss_threshold_s(config):
    """Âge au-delà duquel la dernière analyse réussie rend la surveillance « arrêtée » (s)."""
    return max(3.0 / float(config["analysis_fps"]), 2.0) + 2.0


class TemporalValidator:
    """Validation temporelle d'une classe : k détections sur les n dernières images, dans ``max_window_s``."""

    def __init__(self, rule):
        self.rule = dict(rule)
        self.samples = deque()          # (horodatage de capture, positive, score)
        self.active = False             # incident en cours (confirmé, pas encore retombé)
        self.last_frame_at = None
        self.last_positive_at = None
        self.incident_started_at = None

    def reset(self):
        self.samples.clear()
        self.active = False
        self.last_frame_at = None
        self.last_positive_at = None
        self.incident_started_at = None

    def state(self, new=False, cleared=False):
        positives = [(at, score) for at, positive, score in self.samples if positive]
        return {"new": new, "cleared": cleared, "active": self.active, "confirmed": len(positives) >= self.rule["hits"],
                "hits": len(positives), "window": len(self.samples), "required": self.rule["hits"],
                "first_positive_at": min(at for at, _ in positives) if positives else None,
                "score": max((score for _, score in positives), default=0.0),
                "incident_started_at": self.incident_started_at}

    def update(self, frame_at, positives):
        """Ajoute une image analysée (``frame_at`` = heure de capture, s) et ses détections retenues.

        Renvoie l'état ; ``new`` est vrai une seule fois, à la confirmation d'un nouvel incident, et ``cleared``
        quand l'incident retombe après ``clear_after_s`` sans aucune détection.
        """
        rule = self.rule
        if not rule.get("enabled"):
            self.reset()
            return self.state()
        frame_at = float(frame_at)
        if self.last_frame_at is not None and frame_at <= self.last_frame_at:
            return self.state()  # image déjà analysée ou plus ancienne : jamais recomptée
        if self.last_frame_at is not None and frame_at - self.last_frame_at > rule["max_window_s"]:
            self.samples.clear()  # trou (caméra, analyse) : les indices d'avant ne se cumulent pas
        self.last_frame_at = frame_at
        score = max((float(item.get("score") or 0.0) for item in positives or []), default=0.0)
        positive = bool(positives)
        self.samples.append((frame_at, positive, score))
        while self.samples and (len(self.samples) > rule["window"] or frame_at - self.samples[0][0] > rule["max_window_s"]):
            self.samples.popleft()
        if positive:
            self.last_positive_at = frame_at
        current = self.state()
        new = cleared = False
        if current["confirmed"] and not self.active:
            self.active = True
            self.incident_started_at = current["first_positive_at"]
            new = True
        elif (self.active and not current["confirmed"] and self.last_positive_at is not None
              and frame_at - self.last_positive_at >= rule["clear_after_s"]):
            self.active = False
            self.incident_started_at = None
            cleared = True
        return self.state(new=new, cleared=cleared)
