"""Mesure des performances du moteur flamme/fumée sur ce Worker (décodage, prétraitement, inférence, post-traitement).

    python -m fire_watch_engine.bench --model id=chemin.onnx [--model ...] --provider cpu|gpu|auto --count 60
        [--image fichier.jpg] [--threads N]

Sans ``--image``, une image JPEG 640×480 synthétique et déterministe est générée (numpy + OpenCV). Chaque mesure
passe par exactement le chemin du service (``Detector.infer_jpeg``) après le préchauffage du moteur. Le processus se
place en priorité basse (le moteur en service, lui, tourne en priorité normale depuis l'agent 1.35.8). Une ligne JSON par modèle sur la sortie standard :
``{"id", "provider", "requested", "fallback_reason", "warmup_ms", "check", "threads", "count", "image",
"detections", "timing_ms": {"decode" | "preprocess" | "inference" | "postprocess" | "total": {p50, p95, max}}}``.
"""
import argparse
import json
import sys
import time

from . import engine

COUNT_RANGE = (1, 2000)
MAX_IMAGE_BYTES = 8 * 1024 * 1024
STEPS = ("decode", "preprocess", "inference", "postprocess", "total")


def load_image(path):
    if not path:
        data = engine.synthetic_jpeg(640, 480)
        synthetic = True
    else:
        try:
            with open(path, "rb") as handle:
                data = handle.read(MAX_IMAGE_BYTES + 1)
        except OSError as exc:
            raise engine.EngineError(f"Image illisible : {engine.short_error(exc)}") from None
        if len(data) > MAX_IMAGE_BYTES:
            raise engine.EngineError("Image trop volumineuse (8 Mio au plus).")
        synthetic = False
    height, width = engine.decode_jpeg(data).shape[:2]
    return data, {"width": int(width), "height": int(height), "bytes": len(data), "synthetic": synthetic}


def bench_model(model_id, path, data, image, provider, count, threads=None, conf=engine.DEFAULT_CONF):
    started = time.monotonic()
    detector = engine.Detector(path, provider=provider, threads=threads)
    load_s = time.monotonic() - started
    timings = {step: [] for step in STEPS}
    detections = 0
    for _ in range(count):
        result = detector.infer_jpeg(data, conf=conf)
        for step in STEPS:
            timings[step].append(result["timing_ms"][step])
        detections = len(result["detections"])
    info = detector.describe()
    return {"id": model_id, "provider": detector.provider, "requested": provider,
            "fallback_reason": detector.fallback_reason, "warmup_ms": info["warmup_ms"], "check": info["check"],
            "threads": detector.threads, "load_s": round(load_s, 2), "count": count, "image": image,
            "detections": detections, "timing_ms": {step: engine.timing_summary(values) for step, values in timings.items()},
            "versions": info["versions"]}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="fire_watch_engine.bench", description="Mesures du moteur flamme/fumée.")
    parser.add_argument("--model", action="append", required=True, help="id=chemin.onnx (répétable)")
    parser.add_argument("--provider", choices=engine.PROVIDERS, default="auto")
    parser.add_argument("--count", type=int, default=60)
    parser.add_argument("--image", default=None)
    parser.add_argument("--threads", type=int, default=None)
    args = parser.parse_args(argv)
    if not COUNT_RANGE[0] <= args.count <= COUNT_RANGE[1]:
        parser.error(f"--count : {COUNT_RANGE[0]} à {COUNT_RANGE[1]}")
    engine.utf8_stderr()
    engine.lower_priority()
    try:
        engine.require_runtime()
        data, image = load_image(args.image)
        for text in args.model:
            model_id, path = engine.parse_model_option(text)
            report = bench_model(model_id, path, data, image, args.provider, args.count, threads=args.threads)
            print(json.dumps(report, ensure_ascii=False), flush=True)
    except engine.EngineError as exc:
        print(f"[fire-watch-bench] {exc}", file=sys.stderr, flush=True)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
