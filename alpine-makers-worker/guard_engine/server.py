"""Processus d'analyse d'Alpine Guard, piloté par le service ``guard.py`` de l'agent par tubes.

    <components/guard-engine/.venv python> -u -m guard_engine.server [--model <obico.onnx>] [--threads N]

Même trame que le moteur de la surveillance laser : une requête = une ligne JSON UTF-8 (4096 octets au plus) suivie
de ``jpeg_bytes`` octets ; une réponse = une ligne JSON. La sortie standard est réservée au protocole (le descripteur
1 est redirigé vers stderr, journal ``logs/guard-engine.log``).

- démarrage : ``{"type": "ready", "versions", "model": {"available", "sha256", "names", "error"}}`` ;
- ``{"type": "analyse", "id", "jpeg_bytes", "monitor", "ts", "zone", "armed", "in_grace", "run", "use_model",
  "annotate", "label"}`` + octets → ``{"type": "result", "id", "ok": true, "width", "height", "health", "camera",
  "signals", "metrics", "boxes", "timing_ms", "annotated_b64"?}`` ;
- ``{"type": "forget", "monitor"}`` : état du moniteur oublié ; ``ping`` → ``pong`` ; ``quit`` ou fin de flux → 0.

Le modèle facultatif ne bloque jamais la vision classique : modèle absent, illisible ou en erreur → signal ``model``
à 0 et motif dans ``model.error``.
"""
import argparse
import base64
import json
import os
import sys
import time
import traceback

MAX_HEADER_BYTES = 4096
MAX_JPEG_BYTES = 8 * 1024 * 1024
MAX_MONITORS = 16


def log(message):
    print(f"[guard-engine] {message}", file=sys.stderr, flush=True)


def encode(message):
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"


def write_message(stream, message):
    stream.write(encode(message))
    stream.flush()


def read_exact(stream, count):
    chunks, remaining = [], count
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class ProtocolError(Exception):
    pass


def read_request(stream):
    while True:
        line = stream.readline(MAX_HEADER_BYTES + 1)
        if not line:
            return None
        if not line.endswith(b"\n"):
            if len(line) > MAX_HEADER_BYTES:
                raise ProtocolError("en-tête trop long")
            return None
        if line.strip():
            break
    try:
        header = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ProtocolError("requête illisible") from None
    if not isinstance(header, dict):
        raise ProtocolError("objet JSON attendu")
    payload = b""
    size = header.get("jpeg_bytes", 0)
    if isinstance(size, bool) or not isinstance(size, int) or size < 0 or size > MAX_JPEG_BYTES:
        raise ProtocolError("taille d'image invalide")
    if size:
        payload = read_exact(stream, size)
        if payload is None:
            return None
    return header, payload


def _zone(value):
    if not isinstance(value, list) or len(value) != 4:
        return None
    try:
        return [[min(1.0, max(0.0, float(x))), min(1.0, max(0.0, float(y)))] for x, y in value]
    except (TypeError, ValueError):
        return None


class Engine:
    def __init__(self, model_path=None, threads=2, check_sha256=True):
        from . import vision

        self.vision = vision
        self.monitors = {}
        self.model = None
        self.model_error = None
        if model_path:
            try:
                from .model import ObicoModel

                self.model = ObicoModel(model_path, check_sha256=check_sha256, threads=threads)
            except Exception as error:  # noqa: BLE001 - le modèle reste facultatif
                self.model_error = f"{type(error).__name__}: {error}"[:300]
                log(f"modèle facultatif indisponible : {self.model_error}")

    def describe(self):
        import cv2
        import numpy

        versions = {"numpy": numpy.__version__, "opencv": cv2.__version__}
        if self.model is not None:
            versions["onnxruntime"] = self.model.version
        return {"versions": versions, "model": {"available": self.model is not None, "names": ["failure"] if self.model else [],
                                                "sha256": getattr(self.model, "sha256", None), "error": self.model_error}}

    def analyse(self, header, payload):
        monitor = str(header.get("monitor") or "")[:64]
        if not monitor:
            raise ValueError("moniteur manquant")
        image = self.vision.decode(payload)
        state = self.monitors.get(monitor)
        if state is None:
            if len(self.monitors) >= MAX_MONITORS:
                self.monitors.pop(next(iter(self.monitors)))
            state = self.monitors[monitor] = self.vision.MonitorVision()
        started = time.perf_counter()
        zone = _zone(header.get("zone"))
        ts = float(header.get("ts") or time.time())
        result = state.analyse(image, ts, zone=zone, armed=bool(header.get("armed")),
                               in_grace=bool(header.get("in_grace")), run=header.get("run"))
        boxes = []
        model_score = None
        if header.get("use_model") and self.model is not None and header.get("armed"):
            try:
                mask = self.vision.zone_mask((image.shape[0] // 4, image.shape[1] // 4), zone) if zone else None
                found = self.model.detect(image)
                from .model import score_boxes

                model_score, boxes = score_boxes(found, mask)
            except Exception as error:  # noqa: BLE001
                self.model_error = f"{type(error).__name__}: {error}"[:300]
        result["signals"]["model"] = model_score if model_score is not None else 0.0
        result["boxes"] = boxes
        result.update(width=int(image.shape[1]), height=int(image.shape[0]),
                      timing_ms=round((time.perf_counter() - started) * 1000.0, 1), model_error=self.model_error)
        if header.get("annotate"):
            data = self.vision.annotate(image, zone=zone, boxes=boxes, label=str(header.get("label") or ""))
            result["annotated_b64"] = base64.b64encode(data).decode("ascii")
        return result


def serve(engine, stdin, stdout):
    while True:
        try:
            request = read_request(stdin)
        except ProtocolError as error:
            write_message(stdout, {"type": "error", "error": str(error)})
            return 2
        if request is None:
            return 0
        header, payload = request
        kind = header.get("type")
        request_id = header.get("id") if isinstance(header.get("id"), int) and not isinstance(header.get("id"), bool) else None
        if kind == "quit":
            return 0
        if kind == "ping":
            write_message(stdout, {"type": "pong"})
        elif kind == "forget":
            engine.monitors.pop(str(header.get("monitor") or ""), None)
        elif kind == "annotate":
            # Image d'alerte ou aperçu : dessin seul, sans toucher à l'état d'analyse du moniteur.
            try:
                image = engine.vision.decode(payload)
                boxes = [box[:4] for box in header.get("boxes") or [] if isinstance(box, list) and len(box) >= 4][:50]
                data = engine.vision.annotate(image, zone=_zone(header.get("zone")), boxes=boxes,
                                              label=str(header.get("label") or ""))
                write_message(stdout, {"type": "result", "id": request_id, "ok": True,
                                       "annotated_b64": base64.b64encode(data).decode("ascii")})
            except Exception as error:  # noqa: BLE001
                write_message(stdout, {"type": "result", "id": request_id, "ok": False,
                                       "error": f"{type(error).__name__}: {error}"[:300]})
        elif kind == "analyse":
            try:
                result = engine.analyse(header, payload)
                write_message(stdout, {"type": "result", "id": request_id, "ok": True, **result})
            except Exception as error:  # noqa: BLE001 - une image ne fait jamais tomber le moteur
                traceback.print_exc(file=sys.stderr)
                write_message(stdout, {"type": "result", "id": request_id, "ok": False,
                                       "error": f"{type(error).__name__}: {error}"[:300]})
        else:
            write_message(stdout, {"type": "error", "id": request_id, "error": "type de requête inconnu"})


def claim_stdout():
    try:
        sys.stdout.flush()
    except (OSError, ValueError, AttributeError):
        pass
    try:
        fd = os.dup(1)
        os.dup2(2, 1)
    except OSError:
        stream = sys.stdout.buffer
        sys.stdout = sys.stderr
        return stream
    if os.name == "nt":
        import msvcrt

        msvcrt.setmode(fd, os.O_BINARY)
    sys.stdout = sys.stderr
    return os.fdopen(fd, "wb", buffering=64 * 1024)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="guard_engine.server")
    parser.add_argument("--model", default="")
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args(argv)
    stdout = claim_stdout()
    stdin = sys.stdin.buffer
    try:
        engine = Engine(args.model or None, threads=max(1, min(8, args.threads)))
    except Exception as error:  # noqa: BLE001 - numpy / OpenCV absents
        write_message(stdout, {"type": "fatal", "error": f"{type(error).__name__}: {error}"[:300]})
        return 1
    write_message(stdout, {"type": "ready", **engine.describe()})
    return serve(engine, stdin, stdout)


def probe(model_path=None):
    """Sonde de l'installateur : une analyse complète sur une image synthétique, sans fichier ni réseau."""
    import cv2
    import numpy as np

    image = np.full((240, 320, 3), 90, dtype=np.uint8)
    cv2.rectangle(image, (120, 120), (200, 180), (200, 200, 200), -1)
    ok, data = cv2.imencode(".jpg", image)
    engine = Engine(model_path)
    report = engine.analyse({"monitor": "probe", "ts": 1.0, "armed": True, "use_model": bool(model_path),
                             "annotate": True}, data.tobytes())
    return {"ok": bool(ok) and "signals" in report and bool(report.get("annotated_b64")),
            "model": engine.model is not None, "model_error": engine.model_error, **engine.describe()["versions"]}


if __name__ == "__main__":
    sys.exit(main())
