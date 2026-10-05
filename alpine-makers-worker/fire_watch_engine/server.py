"""Processus d'analyse flamme/fumée du Worker, piloté par le service ``fire_watch.py`` par tubes.

    <components/fire-watch/.venv python> -u -m fire_watch_engine.server --model model.onnx --provider auto [--threads N]

(cwd et PYTHONPATH = dossier de l'agent ; sous Windows le lanceur pose CREATE_NO_WINDOW | NORMAL depuis l'agent
1.35.8 et le processus garde cette classe. Le fil d'envoi GRBL passe devant par sa propre priorité de fil.)

Trame : une requête = une ligne JSON UTF-8 terminée par ``\\n`` (4096 octets au plus), suivie de ``jpeg_bytes``
octets bruts s'il y a lieu ; une réponse = une ligne JSON. La sortie standard est réservée au protocole : au
démarrage elle est détournée au niveau du descripteur, de sorte qu'un ``print`` ou une bibliothèque native
écrive sur stderr (journal ``logs/fire-watch-engine.log``) sans désynchroniser le service.

- démarrage : ``{"type": "ready", ...}`` (fournisseur réellement retenu, raison d'un repli CPU, modèle, contrôle
  de cohérence, versions) ou ``{"type": "fatal", "error"}`` puis code de sortie 1 ;
- ``{"type": "infer", "id", "jpeg_bytes", "conf", "iou"}`` + octets → ``{"type": "result", "id", "ok": true,
  "width", "height", "detections", "timing_ms", "provider"}`` ou ``{"type": "result", "id", "ok": false, "error"}`` ;
- ``{"type": "ping"}`` → ``{"type": "pong"}`` ; ``{"type": "quit"}`` ou fin de stdin → code 0 ;
- en-tête illisible ou trop long, image > 8 Mio : réponse d'erreur puis code 2 (flux désynchronisé).
"""
import argparse
import json
import os
import sys
import traceback

from . import engine

MAX_HEADER_BYTES = 4096
MAX_JPEG_BYTES = 8 * 1024 * 1024
EXIT_OK = 0
EXIT_FATAL = 1
EXIT_PROTOCOL = 2
CONF_RANGE = (0.01, 0.99)
IOU_RANGE = (0.05, 0.95)


class ProtocolError(Exception):
    """Le flux ne peut plus être suivi : réponse d'erreur, puis arrêt du moteur (le service le relance)."""

    def __init__(self, message, header=None):
        super().__init__(message)
        self.header = header if isinstance(header, dict) else {}

    def response(self):
        request_id = _request_id(self.header.get("id"))
        if self.header.get("type") == "infer":
            return {"type": "result", "id": request_id, "ok": False, "error": str(self)}
        return {"type": "error", "id": request_id, "error": str(self)}


def _request_id(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def log(message):
    print(f"[fire-watch-engine] {message}", file=sys.stderr, flush=True)


def encode(message):
    """Une réponse = une ligne JSON UTF-8 (jamais NaN/Infinity)."""
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"


def write_message(stream, message):
    stream.write(encode(message))
    stream.flush()


def read_exact(stream, count):
    """Exactement ``count`` octets, ou ``None`` si le flux se termine avant."""
    chunks, remaining = [], count
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_request(stream):
    """Prochaine requête ``(en-tête, octets)`` ; ``None`` en fin de flux ; ``ProtocolError`` si désynchronisé."""
    while True:
        line = stream.readline(MAX_HEADER_BYTES + 1)
        if not line:
            return None
        if not line.endswith(b"\n"):
            if len(line) > MAX_HEADER_BYTES:
                raise ProtocolError(f"En-tête de requête trop long ({MAX_HEADER_BYTES} octets au plus).")
            return None  # fin de flux au milieu d'une ligne : rien d'exploitable
        if line.strip():
            break
    try:
        header = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ProtocolError("Requête illisible (ligne JSON UTF-8 attendue).") from None
    if not isinstance(header, dict):
        raise ProtocolError("Requête invalide (objet JSON attendu).")
    payload = b""
    if "jpeg_bytes" in header:
        size = header["jpeg_bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ProtocolError("Taille d'image invalide (« jpeg_bytes » entier attendu).", header)
        if size > MAX_JPEG_BYTES:
            raise ProtocolError(f"Image trop volumineuse ({size} octets ; 8 Mio au plus).", header)
        if size:
            payload = read_exact(stream, size)
            if payload is None:
                return None
    return header, payload


def _bounded(value, default, bounds):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if bounds[0] <= value <= bounds[1] else None


def handle_infer(detector, header, payload):
    request_id = _request_id(header.get("id"))

    def failure(message):
        return {"type": "result", "id": request_id, "ok": False, "error": message}

    if request_id is None:
        return failure("Identifiant de requête invalide (entier attendu).")
    if not payload:
        return failure("Image absente de la requête.")
    conf = _bounded(header.get("conf"), engine.DEFAULT_CONF, CONF_RANGE)
    iou = _bounded(header.get("iou"), engine.DEFAULT_IOU, IOU_RANGE)
    if conf is None:
        return failure("Seuil « conf » invalide (0,01 à 0,99).")
    if iou is None:
        return failure("Seuil « iou » invalide (0,05 à 0,95).")
    try:
        result = detector.infer_jpeg(payload, conf=conf, iou=iou)
    except engine.EngineError as exc:
        return failure(str(exc))
    except Exception as exc:  # noqa: BLE001 - une image ne doit jamais faire tomber le moteur
        traceback.print_exc(file=sys.stderr)
        return failure(f"Erreur du moteur : {engine.short_error(exc)}")
    return {"type": "result", "id": request_id, "ok": True, **result}


def serve(detector, stdin, stdout):
    """Boucle requête → réponse (une à la fois) ; renvoie le code de sortie."""
    while True:
        try:
            request = read_request(stdin)
        except ProtocolError as exc:
            log(f"flux désynchronisé : {exc}")
            write_message(stdout, exc.response())
            return EXIT_PROTOCOL
        if request is None:
            return EXIT_OK
        header, payload = request
        kind = header.get("type")
        if kind == "quit":
            return EXIT_OK
        if kind == "ping":
            write_message(stdout, {"type": "pong"})
        elif kind == "infer":
            write_message(stdout, handle_infer(detector, header, payload))
        else:
            write_message(stdout, {"type": "error", "id": _request_id(header.get("id")),
                                   "error": "Type de requête inconnu."})


def ready_message(detector):
    return {"type": "ready", **detector.describe()}


def claim_stdout():
    """Réserve la sortie standard au protocole et renvoie le flux binaire du protocole.

    Le descripteur 1 d'origine est dupliqué pour le protocole, puis le descripteur 1 est redirigé vers stderr :
    les écritures parasites (``print``, messages des DLL CUDA ou d'ONNX Runtime) partent dans le journal.
    """
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


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise engine.EngineError(f"Arguments du moteur invalides : {message}")


def parse_args(argv=None):
    parser = _Parser(prog="fire_watch_engine.server", description="Moteur local d'analyse flamme/fumée (tubes).")
    parser.add_argument("--model", required=True)
    parser.add_argument("--provider", choices=engine.PROVIDERS, default="auto")
    parser.add_argument("--threads", type=int, default=None)
    return parser.parse_args(argv)


def main(argv=None):
    engine.utf8_stderr()
    out = claim_stdout()
    # Priorité fixée par le lanceur : normale pour la surveillance (agent 1.35.8). L'abaisser ici la ferait manquer
    # d'images dès qu'un travail lourd occupe le PC ; les outils de mesure (bench, evaluate) l'abaissent eux-mêmes.
    stdin = sys.stdin.buffer
    try:
        args = parse_args(argv)
        detector = engine.Detector(args.model, provider=args.provider, threads=args.threads)
        ready = ready_message(detector)
    except engine.EngineError as exc:
        log(f"démarrage impossible : {exc}")
        write_message(out, {"type": "fatal", "error": str(exc)})
        return EXIT_FATAL
    except Exception as exc:  # noqa: BLE001 - toujours une ligne « fatal » lisible par le service
        traceback.print_exc(file=sys.stderr)
        write_message(out, {"type": "fatal", "error": f"Démarrage du moteur impossible : {engine.short_error(exc)}"})
        return EXIT_FATAL
    log(f"prêt : {ready['provider']} (demandé : {ready['requested']}), préchauffage {ready['warmup_ms']} ms"
        + (f", repli : {ready['fallback_reason']}" if ready["fallback_reason"] else ""))
    try:
        write_message(out, ready)
        return serve(detector, stdin, out)
    except (BrokenPipeError, ConnectionResetError):
        return EXIT_OK  # le service a fermé les tubes : arrêt normal
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
