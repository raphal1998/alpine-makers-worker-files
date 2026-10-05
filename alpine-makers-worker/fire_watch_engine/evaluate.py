"""Évaluation de modèles flamme/fumée sur une vidéo enregistrée (les mêmes images pour tous les modèles).

    python -m fire_watch_engine.evaluate --video V --model id=chemin.onnx [--model ...] --sample-fps 5
        [--max-seconds 600] [--config config.json] [--provider auto|gpu|cpu] [--threads N] --json resultat.json

Lancé par le service Worker (requête ``evaluate`` du site, processus BELOW_NORMAL) ou à la main ; l'agent n'est
pas nécessaire (cwd ou PYTHONPATH = dossier de l'agent pour ``fire_watch_rules``). La vidéo est lue avec OpenCV
et échantillonnée au temps vidéo ; chaque image retenue est encodée une fois en JPEG (comme une image de caméra)
puis analysée par chaque modèle. Les règles du service s'appliquent avec la configuration donnée, le temps vidéo
servant d'horloge : ``filter_detections`` (score, surface, zone de travail) puis ``TemporalValidator`` (k sur n).

Résultat JSON : ``{"ok", "video": {frames, fps, duration_s, width, height}, "sample_fps", "config", "models": [{"id",
"provider", "frames", "timing_ms": {p50, p95, max}, "classes": {"fire": {positive_frames, first_positive_s,
max_score, glow_rejected, incidents: [{start_s, confirmed_s, latency_s, hits, end_s}]}, "smoke": {...}}, "timeline":
[[t, flamme, fumée], ... ≤ 600 points]}], "elapsed_s"}``. ``max_score`` et la frise donnent le meilleur score brut
de la classe (avant les règles) pour aider au réglage ; ``positive_frames`` et les incidents comptent ce que les
règles retiennent ; ``glow_rejected`` compte les détections qui auraient compté sans le filtre de lueur laser
(``reject_laser_glow``, flamme seulement). Une évaluation ne valide pas un réglage pour la vraie machine : elle aide
à comparer et à régler.
En cas d'erreur : ``{"ok": false, "error"}`` et code de sortie 2.
"""
import argparse
import json
import math
import os
import sys
import time

from . import engine

try:
    from .. import fire_watch_rules
except (ImportError, ValueError):  # lancé en « fire_watch_engine.evaluate » depuis le dossier de l'agent
    import fire_watch_rules

CLASSES = fire_watch_rules.CLASSES
MAX_TIMELINE = 600
MAX_INCIDENTS = 200
MAX_MODELS = 4
SAMPLE_FPS_RANGE = (0.2, 30.0)
MAX_SECONDS_RANGE = (1.0, 7200.0)
JPEG_QUALITY = 95
PROGRESS_EVERY_S = 10.0
EXIT_OK = 0
EXIT_ERROR = 2


class EvaluationError(Exception):
    """Erreur d'évaluation, message en français."""


def log(message):
    print(f"[fire-watch-evaluate] {message}", file=sys.stderr, flush=True)


def load_config(path):
    """Configuration de surveillance nettoyée par les règles du service (mode et caméra sans objet ici)."""
    raw = {}
    if path:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError) as exc:
            raise EvaluationError(f"Configuration illisible : {engine.short_error(exc)}") from None
        if isinstance(raw, dict) and isinstance(raw.get("config"), dict):
            raw = raw["config"]
        if not isinstance(raw, dict):
            raise EvaluationError("Configuration invalide (objet JSON attendu).")
    raw = {key: value for key, value in raw.items() if key not in ("mode", "camera_id", "laser_id")}
    try:
        config = fire_watch_rules.clean_config(raw)
    except ValueError as exc:
        raise EvaluationError(str(exc)) from None
    for key in ("mode", "camera_id", "laser_id"):
        config.pop(key, None)
    return config


def compact_timeline(points, limit=MAX_TIMELINE):
    """Au plus ``limit`` points ; chaque point regroupé garde le maximum de chaque score (aucun pic perdu)."""
    if len(points) <= limit:
        return points
    result, total = [], len(points)
    for bucket in range(limit):
        chunk = points[bucket * total // limit:(bucket + 1) * total // limit]
        if chunk:
            result.append([chunk[0][0], max(point[1] for point in chunk), max(point[2] for point in chunk)])
    return result


class ModelRun:
    """Analyse d'une séquence par un modèle, avec les règles du service (une instance par modèle)."""

    def __init__(self, model_id, detector, config, conf):
        self.model_id = model_id
        self.detector = detector
        self.config = config
        self.conf = conf
        self.validators = {cls: fire_watch_rules.TemporalValidator(config[cls]) for cls in CLASSES}
        self.classes = {cls: {"positive_frames": 0, "first_positive_s": None, "max_score": 0.0, "incidents": [],
                              "incidents_total": 0, "glow_rejected": 0} for cls in CLASSES}
        self.timings = []
        self.timeline = []
        self.errors = 0
        self.last_error = None

    def analyse(self, at, jpeg):
        try:
            result = self.detector.infer_jpeg(jpeg, conf=self.conf, iou=engine.DEFAULT_IOU)
        except engine.EngineError as exc:
            self.errors += 1
            self.last_error = str(exc)
            return
        self.timings.append(result["timing_ms"]["total"])
        detections = result["detections"]
        maxima = {}
        for cls in CLASSES:
            stats = self.classes[cls]
            maxima[cls] = max((item["score"] for item in detections if item["label"] == cls), default=0.0)
            stats["max_score"] = max(stats["max_score"], maxima[cls])
            rule = self.config[cls]
            kept = fire_watch_rules.filter_detections(detections, rule, self.config["roi"], cls)
            if rule.get("reject_laser_glow"):
                # Détections qui compteraient sans le filtre de lueur laser (score, surface, zone respectés).
                loose = fire_watch_rules.filter_detections(detections, {**rule, "reject_laser_glow": False},
                                                           self.config["roi"], cls)
                stats["glow_rejected"] += len(loose) - len(kept)
            if kept:
                stats["positive_frames"] += 1
                if stats["first_positive_s"] is None:
                    stats["first_positive_s"] = round(at, 3)
            state = self.validators[cls].update(at, kept)
            if state["new"]:
                stats["incidents_total"] += 1
                if len(stats["incidents"]) < MAX_INCIDENTS:
                    start = state["incident_started_at"] if state["incident_started_at"] is not None else at
                    stats["incidents"].append({"start_s": round(start, 3), "confirmed_s": round(at, 3),
                                               "latency_s": round(at - start, 3), "hits": state["hits"],
                                               "max_score": round(state["score"], 4), "end_s": None})
            elif state["cleared"] and stats["incidents"] and stats["incidents"][-1]["end_s"] is None:
                stats["incidents"][-1]["end_s"] = round(at, 3)
        self.timeline.append([round(at, 3), maxima["fire"], maxima["smoke"]])

    def report(self):
        detector = self.detector
        return {"id": self.model_id, "provider": detector.provider, "requested": detector.requested,
                "fallback_reason": detector.fallback_reason, "model_sha256": detector.sha256,
                "names": {str(key): value for key, value in detector.names.items()},
                "frames": len(self.timings), "errors": self.errors, "last_error": self.last_error,
                "timing_ms": engine.timing_summary(self.timings),
                "classes": {cls: {**stats, "max_score": round(stats["max_score"], 4)} for cls, stats in self.classes.items()},
                "timeline": compact_timeline(self.timeline)}


def _video_info(capture):
    cv2 = engine.cv2
    fps = capture.get(cv2.CAP_PROP_FPS)
    frames = capture.get(cv2.CAP_PROP_FRAME_COUNT)
    fps = float(fps) if math.isfinite(fps) and 0.5 <= fps <= 1000 else None
    frames = int(frames) if math.isfinite(frames) and frames > 0 else None
    return {"frames": frames, "fps": round(fps, 3) if fps else None,
            "duration_s": round(frames / fps, 3) if fps and frames else None,
            "width": int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0), "height": int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)}


def _short_path(path):
    """Chemin court 8.3 sous Windows (dossiers accentués), sinon le chemin tel quel."""
    if os.name != "nt":
        return path
    try:
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
        return buffer.value if 0 < length < len(buffer) else path
    except (OSError, AttributeError, ValueError):
        return path


def open_capture(path):
    """``cv2.VideoCapture`` ouverte (FFmpeg d'abord, puis les autres lecteurs, puis le chemin court) ou ``None``."""
    cv2 = engine.cv2
    candidates = [path]
    short = _short_path(os.path.abspath(path))
    if short != path:
        candidates.append(short)
    for candidate in candidates:
        for backend in (cv2.CAP_FFMPEG, cv2.CAP_ANY):
            capture = cv2.VideoCapture(candidate, backend)
            if capture.isOpened():
                return capture
            capture.release()
    return None


def sample_frames(capture, fps, sample_fps, max_seconds):
    """Images échantillonnées ``(temps vidéo en s, image BGR)`` : lecture séquentielle, décodage des seules retenues."""
    cv2 = engine.cv2
    period = 1.0 / sample_fps
    next_at, index, last_at = 0.0, 0, None
    while True:
        if not capture.grab():
            return
        # Horodatage du conteneur d'abord : certaines vidéos (WebM à cadence variable) annoncent une cadence
        # fausse (1000 images/s constaté), qui comprimerait toute la séquence en quelques dixièmes de seconde.
        msec = capture.get(cv2.CAP_PROP_POS_MSEC)
        stamp = msec / 1000.0 if math.isfinite(msec) and msec >= 0 else None
        if stamp is not None and (last_at is None or stamp > last_at):
            at = stamp
        elif fps and (last_at is None or index / fps > last_at):
            at = index / fps
        else:  # ni horodatage ni cadence exploitables : 30 images/s supposées
            at = index / 30.0 if last_at is None else last_at + 1.0 / 30.0
        index += 1
        last_at = at
        if at > max_seconds:
            return
        if at + 1e-9 < next_at:
            continue
        ok, frame = capture.retrieve()
        if not ok or frame is None:
            continue
        while next_at <= at + 1e-9:
            next_at += period
        yield at, frame


def evaluate(video, models, sample_fps=5.0, max_seconds=600.0, config=None, provider=None, threads=None):
    """Évalue les ``models`` (liste de ``(id, chemin)``) sur ``video`` ; renvoie le dict de résultat."""
    engine.require_runtime()
    cv2 = engine.cv2
    started = time.monotonic()
    if not models:
        raise EvaluationError("Indique au moins un modèle (--model id=chemin.onnx).")
    if len(models) > MAX_MODELS:
        raise EvaluationError(f"{MAX_MODELS} modèles au plus par évaluation.")
    if len({model_id for model_id, _ in models}) != len(models):
        raise EvaluationError("Identifiants de modèles en double.")
    if not SAMPLE_FPS_RANGE[0] <= sample_fps <= SAMPLE_FPS_RANGE[1]:
        raise EvaluationError("Cadence d'échantillonnage hors limites (0,2 à 30 images/s).")
    if not MAX_SECONDS_RANGE[0] <= max_seconds <= MAX_SECONDS_RANGE[1]:
        raise EvaluationError("Durée maximale hors limites (1 à 7200 s).")
    if not os.path.isfile(video):
        raise EvaluationError("Vidéo introuvable.")
    config = config if config is not None else load_config(None)
    provider = provider or config.get("provider") or "auto"
    # Seuil plancher du moteur : jamais au-dessus du score minimal d'une règle (le filtrage métier suit).
    conf = max(0.01, min([engine.DEFAULT_CONF] + [config[cls]["min_score"] for cls in CLASSES]))
    runs = []
    for model_id, path in models:
        detector = engine.Detector(path, provider=provider, threads=threads)
        log(f"modèle {model_id} : {detector.provider}" + (f" (repli : {detector.fallback_reason})" if detector.fallback_reason else ""))
        runs.append(ModelRun(model_id, detector, config, conf))
    capture = open_capture(video)
    if capture is None:
        raise EvaluationError("Vidéo illisible (format ou codec non pris en charge par OpenCV).")
    try:
        info = _video_info(capture)
        sampled, last_log, last_at = 0, time.monotonic(), None
        for at, frame in sample_frames(capture, info["fps"], sample_fps, max_seconds):
            ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if not ok:
                continue
            if not info["width"]:
                info["height"], info["width"] = frame.shape[:2]
            jpeg = encoded.tobytes()
            for run in runs:
                run.analyse(at, jpeg)
            sampled += 1
            last_at = at
            if time.monotonic() - last_log >= PROGRESS_EVERY_S:
                last_log = time.monotonic()
                log(f"{at:.1f} s de vidéo analysées ({sampled} images)")
    finally:
        capture.release()
    if not sampled:
        raise EvaluationError("Aucune image lisible dans la vidéo.")
    return {"ok": True, "video": info, "sample_fps": sample_fps, "max_seconds": max_seconds,
            "analysed_frames": sampled, "analysed_s": round(last_at or 0.0, 3), "config": config,
            "models": [run.report() for run in runs], "elapsed_s": round(time.monotonic() - started, 2)}


def write_json(path, data):
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    os.replace(temporary, path)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="fire_watch_engine.evaluate",
                                     description="Comparaison de modèles flamme/fumée sur une vidéo enregistrée.")
    parser.add_argument("--video", required=True)
    parser.add_argument("--model", action="append", required=True, help="id=chemin.onnx (répétable)")
    parser.add_argument("--sample-fps", type=float, default=5.0)
    parser.add_argument("--max-seconds", type=float, default=600.0)
    parser.add_argument("--config", default=None, help="configuration de surveillance (JSON)")
    parser.add_argument("--provider", choices=engine.PROVIDERS, default=None)
    parser.add_argument("--threads", type=int, default=None)
    parser.add_argument("--json", required=True, help="fichier de résultat")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    engine.utf8_stderr()
    engine.lower_priority()
    try:
        models = [engine.parse_model_option(text) for text in args.model]
        result = evaluate(args.video, models, sample_fps=args.sample_fps, max_seconds=args.max_seconds,
                          config=load_config(args.config), provider=args.provider, threads=args.threads)
    except (EvaluationError, engine.EngineError) as exc:
        log(f"échec : {exc}")
        write_json(args.json, {"ok": False, "error": str(exc)})
        return EXIT_ERROR
    write_json(args.json, result)
    log(f"terminé : {result['analysed_frames']} images, {len(result['models'])} modèle(s), {result['elapsed_s']} s")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
