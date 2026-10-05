"""Surveillance IA des graveuses laser (flammes, fumée) côté Worker : analyse locale, arrêt, synchronisation.

Le service tourne DANS le processus agent, à côté de ``EquipmentRelay`` qui tient le port série GRBL : un arrêt
passe directement par ``relay.fire_stop`` (soft reset 0x18), sans file de commandes, sans seconde connexion série
et sans dépendre du site. Bibliothèque standard seulement : l'inférence tourne dans un processus séparé
(``fire_watch_engine.server``, Python isolé du composant « fire-watch ») piloté par tubes (ligne JSON + octets
JPEG), et ce module n'importe jamais numpy, OpenCV ni ONNX Runtime.

Fils (daemon) :
- ``fire-watch`` : boucle d'analyse, jamais de réseau ;
- ``fire-watch-sync`` : échanges avec le site (état, événements, requêtes) ; une panne réseau ne ralentit jamais
  l'analyse ;
- ``fire-watch-engine-read`` / ``fire-watch-engine-write`` : tubes du moteur (redémarrage borné 1, 2, 5, 10, 30 s) ;
- ``fire-stop-<id8>`` : un fil court par arrêt, lancé AVANT toute écriture disque (verrou d'abord en mémoire),
  pour ne pas bloquer l'analyse pendant la confirmation ;
- ``fire-watch-guard`` : chien de garde (0,5 s) ; si l'analyse d'une machine armée en travail ne tourne plus,
  il déclenche l'arrêt de perte, même quand le verrou du service reste pris ;
- ``fire-watch-retention`` : passe de rétention des captures, jamais dans le chemin d'un arrêt.

Protection SUPPLÉMENTAIRE : les modèles n'ont pas été validés pour une graveuse, leurs scores ne sont pas des
garanties et rien ici ne permet de laisser la machine sans surveillance humaine. L'absence de données n'est
jamais une situation sûre : sans image fraîche et non figée, analyse récente et moteur prêt, l'état n'est jamais
« active ». Un arrêt pose un verrou persistant (``latches.json``) que seule une requête ``acknowledge`` du site,
envoyée par un opérateur, lève : aucune reprise automatique.
"""
import base64
import hashlib
import importlib
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict, deque
from pathlib import Path

try:
    from .fire_watch_rules import (CLASS_LABELS, CLASSES, DEFAULT_CONFIG, MODEL_IDS, PROVIDERS, TemporalValidator,
                                   clean_config, clean_detections, clean_identifier, effective_action,
                                   filter_detections, latency_bound, loss_threshold_s)
except ImportError:  # Sur un Worker installé les modules sont à plat.
    from fire_watch_rules import (CLASS_LABELS, CLASSES, DEFAULT_CONFIG, MODEL_IDS, PROVIDERS, TemporalValidator,
                                  clean_config, clean_detections, clean_identifier, effective_action,
                                  filter_detections, latency_bound, loss_threshold_s)

try:
    from .safety import protect_credentials
except ImportError:
    try:
        from safety import protect_credentials
    except ImportError:  # sans elle, aucun téléchargement authentifié (évaluation vidéo refusée)
        protect_credentials = None

try:
    from .storage_paths import ascii_path
except ImportError:
    try:
        from storage_paths import ascii_path
    except ImportError:
        def ascii_path(path):
            return str(path)


def _load_catalog():
    """Catalogue du moteur (``fire_watch_engine.catalog``), importé sans numpy ; ``None`` s'il manque."""
    names = [f"{__package__}.fire_watch_engine.catalog"] if __package__ else []
    names.append("fire_watch_engine.catalog")
    for name in names:
        try:
            return importlib.import_module(name)
        except Exception:  # module absent ou abîmé : catalogue de repli, la surveillance reste utilisable
            continue
    return None


_CATALOG = _load_catalog()
_FALLBACK_MODELS = (
    {"id": "rabahdev-yolov8n", "label": "YOLOv8n flamme/fumée (rabahdev)", "expected_names": {0: "smoke", 1: "fire"}},
    {"id": "luminous0219-yolov8n", "label": "YOLOv8n flamme/fumée (luminous0219)", "expected_names": {0: "fire", 1: "smoke"}},
)


def _catalog_models():
    raw = getattr(_CATALOG, "MODELS", None) or _FALLBACK_MODELS
    items = raw.values() if isinstance(raw, dict) else raw
    models = {}
    for item in items:
        if isinstance(item, dict) and item.get("id") in MODEL_IDS:
            models[item["id"]] = item
    for item in _FALLBACK_MODELS:
        models.setdefault(item["id"], item)
    return models


MODELS = _catalog_models()
DEFAULT_MODEL = getattr(_CATALOG, "DEFAULT_MODEL", None) or "rabahdev-yolov8n"
COMPONENT_ID = getattr(_CATALOG, "COMPONENT_ID", None) or "fire-watch"
ENGINE_MODULE = getattr(_CATALOG, "ENGINE_MODULE", None) or "fire_watch_engine.server"
EVALUATE_MODULE = getattr(_CATALOG, "EVALUATE_MODULE", None) or "fire_watch_engine.evaluate"
MODEL_FILENAME = getattr(_CATALOG, "MODEL_FILENAME", None) or "model.onnx"
PROVENANCE_FILENAME = getattr(_CATALOG, "PROVENANCE_FILENAME", None) or "provenance.json"
READY_MARKER = getattr(_CATALOG, "READY_MARKER", None) or ".raphal-ready"

AGENT_DIR = Path(__file__).resolve().parent
SYNC_PATH = "/api/worker-protocol/fire-watch/sync"
STOP_STATUS_LABELS = {"requested": "demandé", "confirmed": "confirmé", "failed": "NON CONFIRMÉ", "impossible": "impossible"}
VIDEO_PATH = "/api/worker-protocol/fire-watch/videos/"

INFER_TIMEOUT_S = 2.0                 # au-delà : moteur tué puis relancé
ENGINE_READY_TIMEOUT_S = 180.0        # chargement du modèle + contrôle GPU/CPU
ENGINE_BACKOFF_S = (1, 2, 5, 10, 30)  # recul borné entre deux relances du moteur
ENGINE_STABLE_S = 60.0                # moteur resté prêt si longtemps : le recul repart de 1 s
ENGINE_EVENT_EVERY_S = 600.0          # au plus un événement « moteur » par machine sur cette durée
MAX_ENGINE_LINE = 1024 * 1024
MAX_JPEG_BYTES = 8 * 1024 * 1024
NMS_IOU = 0.45
CONF_FLOOR = 0.10
SYNC_ACTIVE_S = 1.0
SYNC_IDLE_S = 15.0
SYNC_BACKOFF_S = (2, 5, 10, 30)
MAX_SYNC_BODY = 5 * 1024 * 1024
MAX_CAPTURE_EVENTS = 4
MAX_EVENTS_PER_SYNC = 8               # limite lue par le site (laser_fire_watch.MAX_EVENTS_PER_SYNC)
MAX_REQUESTS_PER_SYNC = 20
WANT_FRAMES_TTL_S = 20.0
STATE_EVERY_S = 2.0
STATUS_MAX_AGE_S = 3.0                # garde de départ : état plus ancien = boucle d'analyse bloquée
GUARD_EVERY_S = 0.5                   # tour du chien de garde
GUARD_LOCK_TIMEOUT_S = 1.0            # au-delà, le chien de garde arrête sans le verrou du service
STOP_START_WAIT_S = 0.2               # attente bornée du départ du fil d'arrêt avant les écritures disque
RETENTION_EVERY_S = 3600.0
STORAGE_UNIT = 1024 * 1024            # Mo des réglages (remplaçable par les tests)
EVENTS_LOG_LINES = 5000
REQUESTS_KEPT = 500
RECENT_EVENTS = 200
CAPTURE_AFTER_DELAY_S = 3.0
EVALUATE_TIMEOUT_S = 20 * 60
EVALUATE_MAX_VIDEO = 512 * 1024 * 1024
EVALUATE_MAX_RESULT = 3 * 1024 * 1024
LOG_ROTATE_BYTES = 5 * 1024 * 1024
STOP_STATUSES = ("requested", "confirmed", "failed", "impossible")
BUSY_STATES = {"run", "jog", "hold", "home"}
# Motifs BLOQUANTS qui valent perte de surveillance pendant un travail armé. ``analysis_too_slow`` n'est bloquant
# que si la règle doit arrêter la machine en arrêt automatique : la règle ne peut plus confirmer de flamme.
LOSS_CODES = {"camera_missing", "camera_no_frame", "camera_stale", "camera_frozen", "engine_missing",
              "engine_starting", "engine_error", "model_mismatch", "analysis_stalled", "maintenance",
              "analysis_too_slow"}
CRITICAL_KINDS = ("incident", "loss", "machine_lost")   # envoyés au site avant l'arriéré
GRBL_KINDS = ("laser", "cnc")
REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
VIDEO_TOKEN = re.compile(r"^[A-Za-z0-9_-]{8,200}$")
STATE_LABELS = {"off": "désactivée", "starting": "démarrage", "active": "active", "degraded": "dégradée",
                "tripped": "arrêt incendie"}


def _log(message):
    """Journal de l'agent ; une sortie cassée (encodage strict, tube fermé) ne tue jamais un fil."""
    try:
        print(f"Surveillance IA : {message}", flush=True)
    except Exception:  # noqa: BLE001
        pass


def _fr(value, digits=1):
    text = f"{float(value):.{digits}f}"
    return text.replace(".", ",")


def _write_json(path, value, durable=True):
    """Écriture atomique (fichier temporaire puis ``os.replace``), toujours en UTF-8 et LF."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            if durable:
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_json(path, default):
    """Contenu JSON ou ``default`` ; un fichier abîmé est mis de côté (``.corrupt``) et signalé, jamais bloquant."""
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as error:
        _log(f"fichier {path.name} illisible ({type(error).__name__}) ; mis de côté.")
        try:
            os.replace(path, path.with_name(path.name + ".corrupt"))
        except OSError:
            pass
        return _CORRUPT


_CORRUPT = object()


def _percentile(values, fraction):
    values = sorted(value for value in values if value is not None)
    if not values:
        return None
    index = min(len(values) - 1, max(0, math.ceil(fraction * len(values)) - 1))
    return round(values[index], 1)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _epoch(value):
    """Horodatage epoch plausible, sinon ``None`` (une valeur monotone n'est pas une date)."""
    number = _number(value)
    return number if number is not None and number > 1e9 else None


def _valid_jpeg(frame):
    return isinstance(frame, (bytes, bytearray)) and 4 <= len(frame) <= MAX_JPEG_BYTES and frame[:2] == b"\xff\xd8"


def _plain(value, limit=20000):
    """Copie JSON d'une donnée externe (preuves d'arrêt), bornée ; jamais d'objet non sérialisable."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {}
    if len(text) > limit:
        return {"truncated": True}
    return json.loads(text)


def _stop_record(result, requested_at):
    """Résultat d'arrêt du relais ramené au contrat de l'événement (``confirmed`` | ``failed`` | ``impossible``)."""
    if not isinstance(result, dict):
        result = {"status": "failed", "message": "Réponse d'arrêt invalide du relais d'équipement."}
    status = result.get("status")
    if status not in ("confirmed", "failed", "impossible"):
        status = "failed"
    defaults = {"confirmed": "Arrêt confirmé par la machine.", "failed": "Arrêt non confirmé par la machine.",
                "impossible": "Arrêt impossible depuis ce Worker."}
    attempts = result.get("attempts")
    record = {
        "status": status,
        "requested_at": _epoch(result.get("requested_at")) or requested_at,
        "written_at": _epoch(result.get("written_at")),
        "confirmed_at": _epoch(result.get("confirmed_at")),
        "elapsed_ms": _number(result.get("elapsed_ms")),
        "attempts": int(attempts) if isinstance(attempts, int) and not isinstance(attempts, bool) else 0,
        "message": str(result.get("message") or defaults[status])[:500],
        "evidence": _plain(result.get("evidence")) if isinstance(result.get("evidence"), dict) else {},
    }
    if result.get("coalesced") is True:
        record["coalesced"] = True
    return record


def _requested_stop(now, message="Arrêt demandé : soft reset GRBL (0x18)."):
    return {"status": "requested", "requested_at": now, "written_at": None, "confirmed_at": None, "elapsed_ms": None,
            "attempts": 0, "message": message, "evidence": {}}


def _open_log(path):
    """Journal d'erreurs du moteur (stderr), tourné au-delà de 5 Mio ; ``None`` si le dossier est inaccessible."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > LOG_ROTATE_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
        return path.open("ab")
    except OSError:
        return None


class EngineError(RuntimeError):
    """Analyse impossible : moteur absent, arrêté, trop lent ou en erreur."""


class EngineClient:
    """Moteur d'analyse dans un sous-processus : une requête à la fois, délai borné, redémarrage borné.

    ``command(model_id=, model_path=, provider=, threads=)`` renvoie ``{"args", "cwd", "env", "creationflags"}`` :
    injectable pour les tests (faux moteur), il lance sinon le Python isolé du composant.
    """

    def __init__(self, model_id, model_path, provider, command, log_path, expected_sha256=None,
                 ready_timeout=ENGINE_READY_TIMEOUT_S):
        self.model_id = model_id
        self.model_path = Path(model_path)
        self.provider = provider
        self.command = command
        self.log_path = Path(log_path)
        self.expected_sha256 = (expected_sha256 or "").lower() or None
        self.ready_timeout = float(ready_timeout)
        self.state = "stopped"          # stopped | starting | ready | error
        self.error = None
        self.ready = None               # message « ready » du moteur
        self.model_problem = None
        self.failures = 0
        self.retry_at = None            # horloge monotone du service
        self.started_at = None
        self.ready_seen_at = None
        self.ever_ready = False
        self.closed = False             # arrêté pour de bon : jamais relancé (moteur retiré ou maintenance)
        self.process = None
        self._generation = 0
        self._cond = threading.Condition()
        self._results = {}
        self._pongs = 0
        self._next_id = 0
        self._outgoing = None

    # -- cycle de vie -------------------------------------------------------------------------------------------
    def maintain(self, now):
        """Démarre, surveille le démarrage et relance après une erreur avec un recul borné."""
        state = self.state
        if state == "stopped":
            self.start(now)
        elif state == "starting":
            if self.started_at is not None and now - self.started_at > self.ready_timeout:
                self._fail(self._generation, f"le moteur n'a pas démarré en {int(self.ready_timeout)} s.")
        elif state == "ready":
            if self.ready_seen_at is None:
                self.ready_seen_at = now
                self.ever_ready = True
        elif state == "error":
            if self.retry_at is None:
                if self.ready_seen_at is not None and now - self.ready_seen_at >= ENGINE_STABLE_S:
                    self.failures = 0
                delay = ENGINE_BACKOFF_S[min(self.failures, len(ENGINE_BACKOFF_S) - 1)]
                self.failures += 1
                self.retry_at = now + delay
                _log(f"moteur d'analyse en erreur ({self.error}) ; relance dans {delay} s.")
            elif now >= self.retry_at:
                self.start(now)

    def start(self, now):
        with self._cond:
            if self.closed or self.state in ("starting", "ready"):
                return
            self._generation += 1
            generation = self._generation
            self.state, self.error, self.ready, self.model_problem = "starting", None, None, None
            self.retry_at, self.started_at, self.ready_seen_at = None, now, None
            self._results.clear()
        try:
            spec = self.command(model_id=self.model_id, model_path=self.model_path, provider=self.provider, threads=None)
            log = _open_log(self.log_path)
            try:
                process = subprocess.Popen(spec["args"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=log if log is not None else subprocess.DEVNULL,
                                           cwd=spec.get("cwd"), env=spec.get("env"),
                                           creationflags=int(spec.get("creationflags") or 0))
            finally:
                if log is not None:
                    log.close()
        except Exception as error:  # Python du composant absent, droits, commande invalide
            with self._cond:
                if generation == self._generation:
                    self.state, self.error = "error", f"lancement du moteur impossible ({error})"[:300]
            return
        outgoing = queue.Queue()
        with self._cond:
            current = generation == self._generation
            if current:
                self.process, self._outgoing = process, outgoing
        if not current:  # arrêté pendant le lancement
            self._kill(process)
            for stream in (process.stdin, process.stdout):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            return
        threading.Thread(target=self._read, args=(process, generation), name="fire-watch-engine-read", daemon=True).start()
        threading.Thread(target=self._write, args=(process, generation, outgoing), name="fire-watch-engine-write",
                         daemon=True).start()

    def stop(self, timeout=3.0, wait=True):
        """Arrêt propre (``quit``) puis arrêt forcé borné ; le moteur n'est plus jamais relancé.

        ``wait=False`` : l'état passe tout de suite à ``stopped`` et l'attente de fin du processus (jusqu'à
        ``timeout`` puis 5 s après l'arrêt forcé) se fait dans un fil court, jamais dans la boucle d'analyse.
        """
        with self._cond:
            self._generation += 1
            self.state = "stopped"
            self.closed = True
            process, outgoing = self.process, self._outgoing
            self.process = self._outgoing = None
            self._cond.notify_all()
        if outgoing is not None:
            outgoing.put(b'{"type":"quit"}\n')
            outgoing.put(None)
        if process is None:
            return
        if not wait:
            threading.Thread(target=self._reap, args=(process, timeout), name="fire-watch-engine-stop", daemon=True).start()
            return
        self._reap(process, timeout)

    @classmethod
    def _reap(cls, process, timeout):
        try:
            process.wait(timeout=max(0.1, timeout))
        except subprocess.TimeoutExpired:
            cls._kill(process)

    @staticmethod
    def _kill(process):
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass

    def _fail(self, generation, message):
        with self._cond:
            if generation != self._generation or self.state == "stopped":
                return
            if self.state != "error":
                self.state, self.error = "error", str(message)[:300]
            process, outgoing = self.process, self._outgoing
            self._cond.notify_all()
        if outgoing is not None:
            outgoing.put(None)
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    # -- tubes --------------------------------------------------------------------------------------------------
    def _read(self, process, generation):
        reason = None
        try:
            while True:
                line = process.stdout.readline(MAX_ENGINE_LINE)
                if not line:
                    break
                if not line.endswith(b"\n"):
                    reason = "réponse du moteur trop longue ou tronquée."
                    break
                try:
                    message = json.loads(line.decode("utf-8"))
                except ValueError:
                    reason = "réponse illisible du moteur (sortie standard polluée ?)."
                    break
                if isinstance(message, dict):
                    self._receive(message, generation)
        except (OSError, ValueError) as error:
            reason = f"lecture du moteur impossible ({type(error).__name__})."
        if reason is not None:
            self._fail(generation, reason)  # tue le processus : flux désynchronisé
        try:
            code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            code = None
        self._fail(generation, f"le moteur s'est arrêté (code {code}) ; détails dans logs/{self.log_path.name}.")
        try:
            process.stdout.close()
        except OSError:
            pass

    def _receive(self, message, generation):
        with self._cond:
            if generation != self._generation:
                return
            kind = message.get("type")
            if kind == "ready" and self.state == "starting":
                self.state, self.ready = "ready", message
                self.model_problem = self._check_model(message)
            elif kind == "fatal":
                self.state, self.error = "error", f"démarrage refusé : {message.get('error')}"[:300]
            elif kind == "result" and isinstance(message.get("id"), int):
                self._results[message["id"]] = message
                while len(self._results) > 16:
                    self._results.pop(next(iter(self._results)))
            elif kind == "pong":
                self._pongs += 1
            self._cond.notify_all()

    def _write(self, process, generation, outgoing):
        try:
            while True:
                data = outgoing.get()
                if data is None:
                    return
                try:
                    process.stdin.write(data)
                    process.stdin.flush()
                except (OSError, ValueError):
                    self._fail(generation, "envoi au moteur impossible (processus arrêté).")
                    return
        finally:
            try:
                process.stdin.close()  # fin de flux : le moteur sort de lui-même
            except (OSError, ValueError):
                pass

    def _check_model(self, ready):
        model = ready.get("model") if isinstance(ready.get("model"), dict) else {}
        names = model.get("names")
        values = list(names.values()) if isinstance(names, dict) else list(names) if isinstance(names, list) else []
        lowered = sorted(str(value).strip().lower() for value in values)
        if lowered != ["fire", "smoke"]:
            return (f"classes du modèle inattendues ({', '.join(map(str, values)) or 'aucune'}) : "
                    "fire et smoke exigées ; modèle refusé.")
        actual = str(model.get("sha256") or "").lower()
        if self.expected_sha256 and actual and actual != self.expected_sha256:
            return "empreinte du modèle différente de celle notée à l'installation : réinstalle le moteur local."
        return None

    # -- requêtes -----------------------------------------------------------------------------------------------
    def usable(self):
        return self.state == "ready" and not self.model_problem

    def _exchange(self, data, key, timeout):
        with self._cond:
            if not self.usable():
                raise EngineError(self.model_problem or self.error or "moteur d'analyse non prêt.")
            generation, outgoing = self._generation, self._outgoing
        outgoing.put(data)
        deadline = time.monotonic() + timeout
        with self._cond:
            while not key() and generation == self._generation and self.state == "ready":
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            answered = key()
            alive = generation == self._generation and self.state == "ready"
        if not answered:
            if alive:
                self._fail(generation, f"analyse trop lente : pas de réponse en {_fr(timeout)} s ; moteur arrêté puis relancé.")
            raise EngineError(self.error or "moteur d'analyse arrêté.")

    def infer(self, jpeg, conf=CONF_FLOOR, iou=NMS_IOU, timeout=INFER_TIMEOUT_S):
        with self._cond:
            self._next_id += 1
            request_id = self._next_id
        header = json.dumps({"type": "infer", "id": request_id, "jpeg_bytes": len(jpeg), "conf": round(float(conf), 4),
                             "iou": float(iou)}, separators=(",", ":")).encode("utf-8") + b"\n"
        self._exchange(header + bytes(jpeg), lambda: request_id in self._results, timeout)
        with self._cond:
            response = self._results.pop(request_id)
            self._note_provider(response)
        if response.get("ok") is not True:
            raise EngineError(f"analyse refusée par le moteur : {response.get('error') or 'erreur inconnue'}"[:300])
        return response

    def _note_provider(self, response):
        """Repli GPU → CPU survenu en service (``_cond`` tenu) : le rapport et l'événement « moteur » le montrent."""
        ready = self.ready
        if not isinstance(ready, dict):
            return
        provider = response.get("provider") if isinstance(response.get("provider"), str) else None
        reason = response.get("fallback_reason") if isinstance(response.get("fallback_reason"), str) else None
        changed_provider = bool(provider) and provider != ready.get("provider")
        if not changed_provider and not (reason and reason != ready.get("fallback_reason")):
            return
        update = {}
        if changed_provider:
            update["provider"] = provider[:80]
            if not reason and provider == "CPUExecutionProvider" and not ready.get("fallback_reason"):
                reason = "Le moteur est passé sur le processeur en cours de service."
        if reason:
            update["fallback_reason"] = reason[:300]
        self.ready = {**ready, **update}

    def ping(self, timeout=INFER_TIMEOUT_S):
        with self._cond:
            before = self._pongs
        self._exchange(b'{"type":"ping"}\n', lambda: self._pongs > before, timeout)
        return True

    def report(self):
        ready = self.ready or {}
        model = ready.get("model") if isinstance(ready.get("model"), dict) else {}
        return {"state": self.state, "provider": ready.get("provider"), "requested": ready.get("requested") or self.provider,
                "fallback_reason": ready.get("fallback_reason"), "model_id": self.model_id,
                "model_sha256": model.get("sha256"), "versions": ready.get("versions") if isinstance(ready.get("versions"), dict) else {},
                "warmup_ms": ready.get("warmup_ms"), "check": ready.get("check"),
                "error": self.model_problem or self.error}


# Dates des dernières analyses : la cadence affichée les compte sur 10 s. 64 entrées la plafonnaient à 6,4 images/s
# (constaté le 2 octobre 2026 : 6,4 affichées pour 8,7 réelles et 10 réglées) ; 10 s à 10 images/s, avec marge.
ANALYSIS_HISTORY = 128


class _Watch:
    """État d'exécution de la surveillance d'une machine (jamais persisté, sauf le verrou)."""

    def __init__(self, laser_id, config, mono):
        self.laser_id = laser_id
        self.config = config
        self.enabled_at = mono
        self.validators = {cls: TemporalValidator(config[cls]) for cls in CLASSES}
        self.suspect = {}
        self.frame = self.frame_at = self.digest = self.digest_since = None
        self.analysed_frame_at = self.analysed_digest = None
        self.last_success = None            # monotone
        self.analysed_wall = None
        self.analysed = 0
        self.analysis_times = deque(maxlen=ANALYSIS_HISTORY)
        self.timings = deque(maxlen=50)     # (inférence ms, total ms)
        self.analysis_error = None
        self.last_detections = []
        self.last_size = None
        self.last_jpeg = None
        self.last_jpeg_at = None
        self.frame_sent_at = None
        self.next_due = 0.0
        self.incident = None
        self.loss_since = None
        self.loss_event = None
        self.job_seen = False
        self.sim = None
        self.sim_until = None
        self.engine_event_at = None
        self.engine_event_open = False
        self.status = None
        self.status_at = None

    def reset(self, mono, camera=True):
        for validator in self.validators.values():
            validator.reset()
        self.suspect = {}
        self.incident = None
        self.loss_since = self.loss_event = None
        self.sim = self.sim_until = None
        self.enabled_at = mono
        self.next_due = 0.0
        if camera:
            self.frame = self.frame_at = self.digest = self.digest_since = None
            self.analysed_frame_at = self.analysed_digest = None
            self.last_success = None
            self.analysis_times.clear()  # cadence d'avant une coupure ou un changement de mode : plus comptée
            self.last_detections, self.last_size, self.last_jpeg, self.last_jpeg_at = [], None, None, None


class FireWatch:
    """Service de surveillance IA des graveuses, créé par l'agent : ``FireWatch(agent)``.

    ``clock``/``monotonic``/``sleep``, ``engine_command``, ``evaluate_command`` et ``downloader`` sont injectables
    pour les tests ; ``tick()`` (un tour d'analyse) et ``sync_once()`` (un échange avec le site) s'appellent
    directement sans démarrer de fil.
    """

    def __init__(self, agent, *, clock=None, monotonic=None, sleep=None, engine_command=None, evaluate_command=None,
                 downloader=None):
        self.agent = agent
        self.root = Path(getattr(agent, "root", "."))
        self.data_dir = self.root / "data" / "fire_watch"
        self.component_root = self.root / "components" / COMPONENT_ID
        self.log_path = self.root / "logs" / "fire-watch-engine.log"
        self.agent_version = self._agent_version(agent)
        self._clock = clock or time.time
        self._mono = monotonic or time.monotonic
        self._sleep = sleep
        self.engine_command = engine_command or self._engine_command
        self.evaluate_command = evaluate_command or self._evaluate_command
        self._download = downloader or self._download_from_site
        self.infer_timeout = INFER_TIMEOUT_S
        self.engine_ready_timeout = ENGINE_READY_TIMEOUT_S
        self.lock = threading.RLock()
        self._halt = threading.Event()
        self._sync_wake = threading.Event()   # remplacé à chaque démarrage : l'ancien réveille le fil remplacé
        self._sync_lock = threading.Lock()
        self._threads_lock = threading.Lock()  # liste des fils courts : jamais le verrou du service (chien de garde)
        self._engine_lock = threading.Lock()   # création/arrêt des moteurs contre suspend_engines
        self._threads = []
        self._generation = 0
        self._started_at = None
        self._stop_threads = []
        self._engines = {}
        self._maintenance = None               # motif d'une opération sur le composant (moteurs suspendus)
        self._latches_dirty = False            # verrous en mémoire pas encore écrits sur disque
        self._retention_wanted = False
        self._retention_thread = None
        self._guard_active = set()
        self._component_cache = None
        self._component_checked = None
        self._fallback_reported = set()
        self.watches = {}
        self.configs = {}
        self.config_errors = {}
        self.revision = 0
        self.latches = {}
        self._requests = OrderedDict()
        self._outbox = {}
        self._recent = OrderedDict()
        self._acked_captures = OrderedDict()
        self._want_frames = {}
        self._evaluating = None
        self._log_lines = 0
        self._state_written = None
        self._retention_done = None
        self._tick_errors = {}
        self.sync_state = {"ok": None, "last_ok_at": None, "error": None, "failures": 0}
        self._load()

    @staticmethod
    def _agent_version(agent):
        version = getattr(agent, "agent_version", None)
        if not version:
            module = sys.modules.get("agent") or sys.modules.get("worker_agent.agent")
            version = getattr(module, "AGENT_VERSION", None)
        return str(version or "inconnue")

    # -- chargement et persistance ------------------------------------------------------------------------------
    def _load(self):
        stored = _read_json(self.data_dir / "config.json", {})
        if isinstance(stored, dict):
            revision = stored.get("revision")
            self.revision = revision if isinstance(revision, int) and not isinstance(revision, bool) else 0
            for laser_id, raw in (stored.get("configs") or {}).items() if isinstance(stored.get("configs"), dict) else ():
                try:
                    laser_id = clean_identifier(laser_id, "Machine")
                    if laser_id:
                        self.configs[laser_id] = clean_config(raw, laser_id=laser_id)
                except ValueError as error:
                    _log(f"réglage local ignoré pour {laser_id} ({error}).")
        latches = _read_json(self.data_dir / "latches.json", {})
        if latches is _CORRUPT:
            # Verrous illisibles : on ne sait plus quelle machine a été arrêtée, donc toutes le restent.
            self.latches = {"*": {"at": self._clock(), "event_id": None, "kind": "unknown", "reason": "verrous illisibles",
                                  "message": "État des arrêts incendie illisible après un redémarrage : vérifie les "
                                             "machines puis acquitte l'arrêt."}}
            self._persist_latches()
        elif isinstance(latches, dict):
            self.latches = {key: value for key, value in latches.items()
                            if isinstance(value, dict) and (key == "*" or self._identifier(key))}
        requests = _read_json(self.data_dir / "requests_done.json", {})
        for entry in (requests.get("requests") or []) if isinstance(requests, dict) else []:
            if not isinstance(entry, dict) or not REQUEST_ID.fullmatch(str(entry.get("id") or "")):
                continue
            if entry.get("status") != "done":
                # Exécution interrompue par un redémarrage : jamais relancée, résultat inconnu signalé au site.
                entry.update(status="done", ok=False, result=None, sent=False,
                             error="Worker redémarré pendant l'exécution : résultat inconnu, rien n'a été relancé.")
            self._requests[entry["id"]] = entry
        outbox = self.data_dir / "outbox"
        if outbox.is_dir():
            for path in sorted(outbox.glob("*.json")):
                entry = _read_json(path, None)
                event = entry.get("event") if isinstance(entry, dict) else None
                if (isinstance(event, dict) and event.get("id") == path.stem and self._identifier(event.get("laser_id"))
                        and isinstance(event.get("rev"), int) and not isinstance(event.get("rev"), bool)):
                    entry.setdefault("capture_acked", False)
                    entry.setdefault("capture_after_acked", False)
                    self._outbox[event["id"]] = entry
                    self._recent[event["id"]] = event
        try:
            with (self.data_dir / "events.jsonl").open("rb") as handle:
                self._log_lines = sum(1 for _ in handle)
        except OSError:
            self._log_lines = 0
        mono = self._mono()
        for laser_id, config in self.configs.items():
            self.watches[laser_id] = _Watch(laser_id, config, mono)

    @staticmethod
    def _identifier(value):
        try:
            return bool(clean_identifier(value))
        except ValueError:
            return False

    def _save_latches(self):
        _write_json(self.data_dir / "latches.json", self.latches)

    def _persist_latches(self):
        """Écrit les verrous (verrou du service tenu) ; un échec est retenté à chaque tour et signalé, jamais levé."""
        try:
            self._save_latches()
        except Exception as error:  # noqa: BLE001  (disque plein, antivirus, dictionnaire modifié en parallèle)
            if not self._latches_dirty:
                _log(f"verrou incendie non enregistré sur disque ({type(error).__name__}: {error}) ; il reste actif en "
                     "mémoire et l'écriture est retentée à chaque tour.")
            self._latches_dirty = True
            return False
        if self._latches_dirty:
            _log("verrous incendie enregistrés sur disque après un échec d'écriture.")
        self._latches_dirty = False
        return True

    def _save_configs(self):
        _write_json(self.data_dir / "config.json", {"revision": self.revision, "configs": self.configs})

    def _save_requests(self):
        entries = list(self._requests.values())
        if len(entries) > REQUESTS_KEPT:
            removable = [entry["id"] for entry in entries if entry.get("status") == "done" and entry.get("sent")]
            for request_id in removable[:len(entries) - REQUESTS_KEPT]:
                self._requests.pop(request_id, None)
            entries = list(self._requests.values())
        _write_json(self.data_dir / "requests_done.json", {"requests": entries})

    # -- fils ---------------------------------------------------------------------------------------------------
    def start(self):
        """Démarre (ou redémarre après ``stop``) les fils d'analyse et de synchronisation.

        Un fil d'un démarrage précédent peut survivre au délai de ``stop`` (requête réseau de 45 s au plus) :
        il porte l'ancienne génération et s'arrête de lui-même à son prochain tour, sans toucher aux moteurs
        de la nouvelle. La surveillance ne reste donc jamais éteinte après une installation du composant.
        """
        with self.lock:
            if not self._halt.is_set() and self._threads and all(thread.is_alive() for thread in self._threads):
                return
            self._generation += 1
            generation = self._generation
            previous, self._sync_wake = self._sync_wake, threading.Event()
            previous.set()  # le fil de synchro remplacé sort de son attente et constate sa génération périmée
            self._halt.clear()
            self._started_at = self._mono()
            self._threads = [threading.Thread(target=self._analysis_loop, args=(generation,), name="fire-watch", daemon=True),
                             threading.Thread(target=self._sync_loop, args=(generation,), name="fire-watch-sync", daemon=True),
                             threading.Thread(target=self._guard_loop, args=(generation,), name="fire-watch-guard", daemon=True)]
            for thread in self._threads:
                thread.start()

    def stop(self, timeout=5.0):
        self._halt.set()
        self._sync_wake.set()
        deadline = time.monotonic() + max(0.1, float(timeout))
        for thread in self._threads:
            thread.join(max(0.05, deadline - time.monotonic()))
        with self.lock:
            engines = list(self._engines.values())
            self._engines.clear()
            # Plus aucune analyse : l'état d'avant l'arrêt ne doit jamais être renvoyé comme frais.
            for watch in self.watches.values():
                watch.status = watch.status_at = None
        for engine in engines:
            engine.stop(timeout=max(0.2, min(3.0, deadline - time.monotonic())))
        try:
            self._write_state()
        except Exception:  # noqa: BLE001
            pass

    def suspend_engines(self, reason=None):
        """Opération sur le composant « fire-watch » : moteurs arrêtés (fichiers libérés) et jamais relancés.

        La boucle d'analyse continue : motif bloquant ``maintenance``, réaction de perte d'un travail armé et garde
        de départ restent appliquées. Appelé par l'agent hors de tout verrou ; attend la fin des processus.
        """
        text = " ".join(str(reason or "opération sur le moteur local").split())[:200]
        with self._engine_lock:
            with self.lock:
                self._maintenance = text
                engines = list(self._engines.values())
                self._engines.clear()
        for engine in engines:
            try:
                engine.stop(timeout=3.0)
            except Exception as error:  # noqa: BLE001  (moteur déjà retiré : jamais d'exception vers l'agent)
                _log(f"arrêt d'un moteur d'analyse non confirmé ({type(error).__name__}: {error}).")
        _log(f"moteurs d'analyse suspendus ({text}).")
        return True

    def resume_engines(self):
        """Fin de l'opération sur le composant : le composant est relu et les moteurs relancés au tour suivant."""
        with self._engine_lock:
            with self.lock:
                was = self._maintenance
                self._maintenance = None
                self._component_cache = self._component_checked = None
        if was is not None:
            _log("moteurs d'analyse de nouveau autorisés.")
        return True

    def _stopping(self):
        stop = getattr(self.agent, "stop_requested", None)
        return self._halt.is_set() or bool(stop is not None and stop.is_set())

    def _pause(self, seconds):
        """Attente interruptible ; vrai si le service s'arrête."""
        if self._sleep is not None:
            self._sleep(seconds)
            return self._halt.is_set()
        return self._halt.wait(seconds)

    def _current(self, generation):
        return generation is None or generation == self._generation

    def _analysis_loop(self, generation=None):
        while not self._stopping() and self._current(generation):
            try:
                delay = self.tick()
            except Exception as error:  # jamais de sortie silencieuse de la boucle : l'état deviendra périmé
                self._log_once("tick", f"erreur de la boucle d'analyse ({type(error).__name__}: {error}).")
                delay = 0.5
            self._halt.wait(delay)
        if not self._current(generation):
            return  # remplacé par un nouveau démarrage : ses moteurs ne sont pas les nôtres à arrêter
        with self.lock:
            engines = list(self._engines.values())
            self._engines.clear()
        for engine in engines:
            engine.stop()

    def _sync_loop(self, generation=None):
        wake = self._sync_wake  # propre à cette génération
        while not self._stopping() and self._current(generation):
            if getattr(self.agent, "disconnecting", False):
                self._halt.wait(1.0)
                continue
            wake.clear()  # avant l'échange : une publication pendant l'envoi réveille le suivant
            try:
                delay = self.sync_once()
            except Exception as error:
                self._log_once("sync", f"erreur de synchronisation ({type(error).__name__}: {error}).")
                delay = SYNC_BACKOFF_S[-1]
            if not self._current(generation):
                return
            if self.sync_state["failures"]:
                # En erreur : recul complet, sans réveil anticipé par une publication, mais interrompu par un arrêt
                # ou un redémarrage du service.
                deadline = time.monotonic() + delay
                while not self._stopping() and self._current(generation) and time.monotonic() < deadline:
                    self._halt.wait(min(0.5, max(0.0, deadline - time.monotonic())))
            else:
                wake.wait(delay)

    def _guard_loop(self, generation=None):
        while not self._stopping() and self._current(generation):
            try:
                self.guard_once()
            except Exception as error:  # noqa: BLE001  (jamais de sortie silencieuse du chien de garde)
                self._log_once("guard", f"erreur du chien de garde ({type(error).__name__}: {error}).")
            self._halt.wait(GUARD_EVERY_S)

    def _stalled_for(self, watch, config, mono):
        """Durée sans état frais pour cette machine si elle dépasse la limite du chien de garde, sinon ``None``."""
        limit = max(float(config.get("loss_grace_s") or 0.0), STATUS_MAX_AGE_S) + 1.0
        last = watch.status_at if watch is not None else None
        if last is None:
            starts = [value for value in (watch.enabled_at if watch is not None else None, self._started_at)
                      if value is not None]
            if not starts:
                return None
            last = max(starts)
        age = mono - last
        return age if age > limit else None

    @staticmethod
    def _guard_armed(config):
        """Fiche que le chien de garde doit arrêter : mode ``auto_stop`` et réaction de perte ``stop``."""
        return bool(config) and config.get("mode") == "auto_stop" and config.get("loss_action") == "stop"

    def guard_once(self):
        """Chien de garde : arrête une machine armée EN TRAVAIL dont l'analyse ne tourne plus ; arrêts lancés.

        Armée = mode ``auto_stop`` et réaction de perte ``stop`` ; rien en observation. Lit la configuration sans
        le verrou du service (une boucle d'analyse bloquée peut le tenir) et n'interroge le relais que hors verrou.
        Chaque arrêt part dans son propre fil court : les écritures disque qui suivent l'arrêt d'une machine ne
        retardent jamais l'examen de la suivante (repli dans ce fil si le fil court ne peut pas être créé).
        """
        mono = self._mono()
        fired = []
        for laser_id, config in list(self.configs.items()):
            if not self._guard_armed(config):
                continue
            if laser_id in self._guard_active or self._latch(laser_id):
                continue
            watch = self.watches.get(laser_id)
            stalled = self._stalled_for(watch, config, mono)
            if stalled is None or not self._busy(self._machine(laser_id)):
                continue
            self._guard_active.add(laser_id)
            launched = True
            try:
                self._spawn(f"fire-guard-{laser_id}", self._guard_stop_safe, laser_id, config, stalled)
            except Exception as error:  # noqa: BLE001  (fil impossible : arrêt depuis le chien de garde lui-même)
                try:
                    launched = self._guard_stop(laser_id, config, stalled)
                except BaseException:
                    self._guard_active.discard(laser_id)
                    raise
                self._log_once("guard-thread", f"fil du chien de garde impossible ({type(error).__name__}) : arrêt "
                                               "traité depuis le chien de garde.")
            if launched:
                fired.append(laser_id)
        return fired

    def _guard_stop_safe(self, laser_id, config, stalled):
        """Corps du fil court d'un arrêt du chien de garde : une erreur libère la machine pour le tour suivant."""
        try:
            self._guard_stop(laser_id, config, stalled)
        except BaseException as error:
            self._guard_active.discard(laser_id)
            if not isinstance(error, Exception):
                raise
            self._log_once("guard", f"erreur du chien de garde ({type(error).__name__}: {error}).")

    @staticmethod
    def _guard_reason(stalled):
        message = (f"Analyse arrêtée ou bloquée depuis {_fr(stalled)} s sur le Worker pendant un travail armé "
                   "(boucle d'analyse figée).")
        return [{"code": "analysis_loop_stalled", "message": message, "blocking": True}]

    def _guard_stop(self, laser_id, config, stalled):
        """Arrêt de perte par le chien de garde ; vrai si un arrêt a été lancé.

        ``config`` et ``stalled`` ont été lus AVANT l'attente du verrou : la fiche est relue ensuite (réglages
        appliqués, boucle repartie pendant l'attente) et seule la configuration courante, encore armée, est utilisée.
        """
        if self.lock.acquire(timeout=GUARD_LOCK_TIMEOUT_S):
            fired = False
            try:
                current = self.configs.get(laser_id)
                watch = self.watches.get(laser_id)
                if self._guard_armed(current) and watch is not None and not self._latch(laser_id):
                    stalled = self._stalled_for(watch, current, self._mono())
                    if stalled is not None:  # la boucle a pu repartir pendant l'attente du verrou
                        watch.loss_since = watch.loss_since or self._mono()
                        watch.loss_event = self._loss(watch, current, self._guard_reason(stalled), self._clock())
                        fired = True
            finally:
                self.lock.release()
                self._guard_active.discard(laser_id)
            return fired
        # Verrou du service indisponible (boucle bloquée en le tenant) : arrêt direct, verrou en mémoire tout de
        # suite ; l'événement est enregistré dès que le verrou du service redevient disponible. Lectures sans le
        # verrou (dictionnaires et attributs : lectures atomiques) : réglages appliqués ou boucle repartie pendant
        # l'attente → rien.
        current = self.configs.get(laser_id)
        watch = self.watches.get(laser_id)
        stalled = self._stalled_for(watch, current, self._mono()) if self._guard_armed(current) else None
        if stalled is None:
            self._guard_active.discard(laser_id)
            return False
        now = self._clock()
        message = self._guard_reason(stalled)[0]["message"]
        event = self._template(laser_id, current.get("camera_id"), current["mode"], "loss", now)
        event.update(action="stop", reasons=["analysis_loop_stalled"],
                     message=f"Surveillance perdue pendant un travail ({message}) : arrêt demandé.")
        event["stop"] = _requested_stop(now)
        latch = self._latch_record(event, "surveillance IA perdue pendant un travail", now)
        # ``setdefault`` : atomique sans le verrou du service, et jamais par-dessus un verrou posé pendant l'attente
        # (flamme confirmée par la boucle) : cet arrêt-là est déjà en cours et garde son motif.
        if self.latches.setdefault(laser_id, latch) is not latch:
            self._guard_active.discard(laser_id)
            return False
        if watch is not None:
            # Période de perte déjà traitée : la boucle qui repart ne crée ni second verrou ni second événement.
            watch.loss_since = watch.loss_since or self._mono()
            watch.loss_event = event["id"]
        args = (laser_id, event, latch, "surveillance IA perdue pendant un travail")
        try:
            self._spawn(f"fire-stop-{event['id'][:8]}", self._run_guard_stop, *args)
        except Exception as error:  # noqa: BLE001  (fil impossible : arrêt depuis ce fil, AVANT tout journal)
            self._run_guard_stop(*args)
            _log(f"{event['message']} (machine {laser_id}, verrou du service indisponible : arrêt direct ; fil d'arrêt "
                 f"impossible ({type(error).__name__}), arrêt envoyé depuis le chien de garde).")
        else:
            # Journal APRÈS le départ du fil d'arrêt : une sortie bloquée ne retient jamais le 0x18.
            _log(f"{event['message']} (machine {laser_id}, verrou du service indisponible : arrêt direct).")
        return True

    def _persist_direct_stop(self, laser_id, event, latch, stop):
        """Arrêt direct, verrou du service encore pris : verrou et événement écrits au mieux, hors de ce verrou.

        Un Worker redémarré parce qu'un fil reste bloqué en tenant le verrou ne perd ainsi ni le verrou incendie
        (que seul un acquittement lève) ni la trace de l'arrêt. Copies prises sans le verrou, donc peut-être
        périmées : ``_latches_dirty`` est posé APRÈS l'écriture, et le tour suivant réécrit l'état courant.
        """
        try:
            latches = dict(self.latches)
            if latches.get(laser_id) is latch:
                latches[laser_id] = {**latch, "stop_status": stop["status"], "stop_message": stop["message"],
                                     "confirmed_at": stop["confirmed_at"]}
            _write_json(self.data_dir / "latches.json", latches)
        except Exception as error:  # noqa: BLE001  (disque plein, dictionnaire modifié en parallèle)
            self._log_once("direct-latch", f"verrou de l'arrêt direct non enregistré ({type(error).__name__}: {error}).")
        try:
            snapshot = json.loads(json.dumps({**event, "stop": stop}, ensure_ascii=False))
            _write_json(self.data_dir / "outbox" / f"{event['id']}.json",
                        {"event": snapshot, "capture_acked": False, "capture_after_acked": False})
        except Exception as error:  # noqa: BLE001
            self._log_once("direct-event", f"événement de l'arrêt direct non enregistré ({type(error).__name__}: "
                                           f"{error}).")
        self._latches_dirty = True

    def _run_guard_stop(self, laser_id, event, latch, reason):
        try:
            relay = self._relay()
            try:
                if relay is None or not hasattr(relay, "fire_stop"):
                    raise RuntimeError("relais d'équipement indisponible")
                result = relay.fire_stop(laser_id, reason, event["id"])
            except Exception as error:  # noqa: BLE001
                result = {"status": "failed", "message": f"Arrêt non exécuté : {error}"}
            stop = _stop_record(result, event["stop"]["requested_at"])
            if self.lock.acquire(blocking=False):
                self.lock.release()   # verrou libre : enregistrement normal ci-dessous
            else:
                self._persist_direct_stop(laser_id, event, latch, stop)
            _log(f"arrêt incendie {STOP_STATUS_LABELS.get(stop['status'], stop['status'])} sur {laser_id} : {stop['message']}")
            with self.lock:  # attend que la boucle bloquée rende le verrou
                event["stop"] = stop
                current = self.latches.get(laser_id)
                if current is None or current.get("event_id") == event["id"]:
                    latch.update(stop_status=stop["status"], stop_message=stop["message"], confirmed_at=stop["confirmed_at"])
                    self.latches[laser_id] = latch
                self._persist_latches()
                watch = self.watches.get(laser_id)
                if watch is not None:
                    watch.loss_since = watch.loss_since or self._mono()
                    watch.loss_event = event["id"]
                    jpeg = watch.frame if watch.frame is not None else watch.last_jpeg
                    if jpeg is not None:
                        self._store_capture(laser_id, event, jpeg)
                self._publish(event, new=True)
        finally:
            self._guard_active.discard(laser_id)

    def _log_once(self, key, message):
        now = self._mono()
        last = self._tick_errors.get(key)
        if last is None or last[0] != message or now - last[1] > 300:
            self._tick_errors[key] = (message, now)
            _log(message)

    def _spawn(self, name, target, *args):
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        with self._threads_lock:
            self._stop_threads = [item for item in self._stop_threads if item.is_alive()]
            self._stop_threads.append(thread)
        thread.start()
        return thread

    def wait_idle(self, timeout=10.0):
        """Attend la fin des fils courts : arrêts, chien de garde, rétention, évaluation (tests, arrêt du service)."""
        deadline = time.monotonic() + timeout
        while True:
            with self._threads_lock:
                threads = [thread for thread in self._stop_threads if thread.is_alive()]
            if not threads:
                return True
            if time.monotonic() >= deadline:
                return False
            threads[0].join(max(0.01, deadline - time.monotonic()))

    # -- relais d'équipement --------------------------------------------------------------------------------------
    def _relay(self):
        return getattr(self.agent, "equipment_relay", None)

    def _camera(self, camera_id):
        if not camera_id:
            return {"present": False}
        relay = self._relay()
        try:
            value = relay.camera_frame(camera_id)
        except Exception as error:  # relais absent, équipement retiré : caméra manquante, jamais une exception
            return {"present": False, "error": type(error).__name__}
        return value if isinstance(value, dict) else {"present": False}

    def _machine(self, laser_id):
        relay = self._relay()
        try:
            value = relay.fire_snapshot(laser_id)
        except Exception as error:
            return {"present": False, "error": type(error).__name__}
        return value if isinstance(value, dict) else {"present": False}

    @staticmethod
    def _linked(machine):
        return (machine.get("present") is True and machine.get("kind", "laser") in ("laser", "cnc")
                and machine.get("linked") is True)

    @staticmethod
    def _busy(machine):
        if machine.get("present") is not True:
            return False
        state = str(machine.get("state") or "").split(":", 1)[0].strip().lower()
        spindle = _number(machine.get("spindle"))
        return machine.get("job_active") is True or state in BUSY_STATES or bool(spindle and spindle > 0)

    # -- composant et moteurs -------------------------------------------------------------------------------------
    def _component(self):
        now = self._mono()
        if self._component_cache is not None and self._component_checked is not None and now - self._component_checked < 5.0:
            return self._component_cache
        python = self.component_root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        ready = (self.component_root / READY_MARKER).is_file() and python.is_file()
        models = {}
        for model_id in MODELS:
            folder = self.component_root / "models" / model_id
            path = folder / MODEL_FILENAME
            if not path.is_file():
                continue
            sha256 = None
            provenance = _read_json(folder / PROVENANCE_FILENAME, {})
            if isinstance(provenance, dict):
                conversion = provenance.get("conversion") if isinstance(provenance.get("conversion"), dict) else {}
                value = str(conversion.get("onnx_sha256") or provenance.get("onnx_sha256") or "").lower()
                sha256 = value if re.fullmatch(r"[0-9a-f]{64}", value) else None
            models[model_id] = {"path": path, "sha256": sha256}
        self._component_cache = {"ready": ready, "python": python, "models": models}
        self._component_checked = now
        return self._component_cache

    def _python_spec(self, args, low_priority=True):
        """Processus Python du composant. ``low_priority`` : travail de fond (évaluation d'une vidéo) en priorité basse.

        Le moteur d'analyse, lui, tourne en priorité NORMALE (agent 1.35.8) : en priorité basse, un travail lourd sur
        le même PC (rendu vidéo sur tous les cœurs, mesuré le 2 octobre 2026) le privait de processeur au point de
        dépasser ``INFER_TIMEOUT_S`` ; le moteur était relancé, et pendant un travail armé cette perte arrête la
        machine. Le fil d'envoi GRBL garde la main par sa propre priorité de fil (``equipment_runtime``)."""
        agent_dir = str(AGENT_DIR)
        env = {key: value for key, value in os.environ.items()
               if key not in {"PYTHONHOME", "VIRTUAL_ENV", "PYTHONPATH", "PYTHONSTARTUP", "__PYVENV_LAUNCHER__"}}
        env["PYTHONPATH"] = agent_dir
        flags = 0
        if os.name == "nt":
            # Classe explicite : sans elle, un enfant hérite BELOW_NORMAL d'un agent lancé en BELOW_NORMAL.
            flags = subprocess.CREATE_NO_WINDOW | (subprocess.BELOW_NORMAL_PRIORITY_CLASS if low_priority
                                                   else subprocess.NORMAL_PRIORITY_CLASS)
        elif low_priority and shutil.which("nice"):
            args = ["nice", "-n", "10", *args]
        return {"args": args, "cwd": agent_dir, "env": env, "creationflags": flags}

    def _engine_command(self, model_id, model_path, provider, threads=None):
        python = self._component()["python"]
        args = [str(python), "-u", "-m", ENGINE_MODULE, "--model", ascii_path(model_path), "--provider", provider]
        if threads:
            args += ["--threads", str(int(threads))]
        return self._python_spec(args, low_priority=False)

    def _evaluate_command(self, video, models, sample_fps, max_seconds, config_path, provider, output):
        python = self._component()["python"]
        args = [str(python), "-u", "-m", EVALUATE_MODULE, "--video", ascii_path(video)]
        for model_id, path in models.items():
            args += ["--model", f"{model_id}={ascii_path(path)}"]
        args += ["--sample-fps", str(sample_fps), "--max-seconds", str(max_seconds), "--config", ascii_path(config_path),
                 "--provider", provider, "--json", ascii_path(output)]
        return self._python_spec(args)

    def _manage_engines(self, configs, mono):
        with self._engine_lock:
            maintenance = self._maintenance is not None
            info = None if maintenance else self._component()
            wanted = {}
            for config in configs.values() if not maintenance else ():
                if config["mode"] != "off" and info["ready"] and config["model"] in info["models"]:
                    wanted[(config["model"], config["provider"])] = info["models"][config["model"]]
            with self.lock:
                # Moteur inutile, ou modèle réinstallé (autre empreinte) : arrêté puis recréé si besoin.
                unused = [self._engines.pop(key) for key in list(self._engines)
                          if key not in wanted or self._engines[key].expected_sha256 != (wanted[key]["sha256"] or None)]
                for key, model in wanted.items():
                    if key not in self._engines:
                        self._engines[key] = EngineClient(key[0], model["path"], key[1], self.engine_command, self.log_path,
                                                          expected_sha256=model["sha256"],
                                                          ready_timeout=self.engine_ready_timeout)
                engines = [self._engines[key] for key in wanted]
            for engine in unused:
                engine.stop(wait=False)  # attente de fin du processus hors de la boucle d'analyse
            for engine in engines:
                engine.maintain(mono)

    def _maintenance_message(self):
        return (f"Moteur d'analyse suspendu pendant une opération sur le composant ({self._maintenance}) : aucune "
                "analyse jusqu'à la fin de l'opération.")

    def _engine_problem(self, config, mono):
        if self._maintenance is not None:
            return None, ("maintenance", self._maintenance_message())
        info = self._component()
        if not info["ready"]:
            return None, ("engine_missing", "Moteur local de surveillance non installé sur ce Worker : installe-le depuis "
                                            "la page Surveillance IA (graveuse).")
        if config["model"] not in info["models"]:
            return None, ("model_mismatch", f"Modèle « {config['model']} » absent du moteur local : relance "
                                            "l'installation du moteur.")
        engine = self._engines.get((config["model"], config["provider"]))
        if engine is None or engine.state in ("stopped", "starting"):
            return engine, ("engine_starting", "Démarrage du moteur d'analyse…")
        if engine.state == "error":
            wait = f" Nouvelle tentative dans {max(0, math.ceil(engine.retry_at - mono))} s." if engine.retry_at else ""
            return engine, ("engine_error", f"Moteur d'analyse en erreur : {engine.error}{wait}")
        if engine.model_problem:
            return engine, ("model_mismatch", f"Modèle refusé : {engine.model_problem}")
        return engine, None

    def _engine_report(self):
        """Moteur de la première fiche active (bloc global, pour les sites qui ne lisent pas ``watches[].engine``)."""
        configs = [self.configs[key] for key in sorted(self.configs) if self.configs[key]["mode"] != "off"]
        return self._engine_block(configs[0] if configs else None)

    def _engine_block(self, config):
        """Moteur ``(model, provider)`` d'UNE fiche : état, fournisseur réel, repli CPU (verrou du service tenu)."""
        info = self._component()
        report = {"state": "not_installed" if not info["ready"] else "stopped", "provider": None, "requested": None,
                  "fallback_reason": None, "model_id": None, "model_sha256": None, "versions": {}, "error": None,
                  "installed_models": sorted(info["models"]), "component_ready": info["ready"]}
        if config is None or config["mode"] == "off" or not info["ready"]:
            return report
        if self._maintenance is not None:
            report.update(model_id=config["model"], requested=config["provider"], maintenance=self._maintenance,
                          error=self._maintenance_message())
            return report
        engine = self._engines.get((config["model"], config["provider"]))
        if engine is None:
            if config["model"] not in info["models"]:
                report.update(state="error", model_id=config["model"], requested=config["provider"],
                              error=f"Modèle « {config['model']} » non installé.")
            else:
                report.update(state="starting", model_id=config["model"], requested=config["provider"])
            return report
        report.update(engine.report())
        if engine.model_problem:
            report["state"] = "error"
        return report

    # -- boucle d'analyse -----------------------------------------------------------------------------------------
    def tick(self):
        """Un tour d'analyse pour toutes les machines surveillées ; renvoie l'attente conseillée (s)."""
        mono = self._mono()
        with self.lock:
            configs = dict(self.configs)
            for laser_id, config in configs.items():
                if laser_id not in self.watches:
                    self.watches[laser_id] = _Watch(laser_id, config, mono)
            for laser_id in [key for key in self.watches if key not in configs]:
                self.watches.pop(laser_id)
            if self._latches_dirty:
                self._persist_latches()  # verrou pas encore sur disque : nouvel essai à chaque tour
        if getattr(self.agent, "disconnecting", False):
            configs = {key: {**value, "mode": "off"} for key, value in configs.items()}
        self._manage_engines(configs, mono)
        wait = 0.25
        for laser_id in sorted(configs):
            try:
                wait = min(wait, self._step(laser_id, configs[laser_id]))
            except Exception as error:
                self._log_once(f"step:{laser_id}", f"analyse de {laser_id} en erreur ({type(error).__name__}: {error}).")
        mono = self._mono()
        if self._state_written is None or mono - self._state_written >= STATE_EVERY_S:
            self._state_written = mono
            try:
                self._write_state()
            except Exception as error:  # noqa: BLE001
                self._log_once("state", f"état local non écrit ({type(error).__name__}: {error}).")
        if self._retention_done is None or mono - self._retention_done >= RETENTION_EVERY_S:
            self._retention_done = mono
            self._retention_wanted = True
        if self._retention_wanted:
            self._start_retention()
        return max(0.02, wait)

    def _start_retention(self):
        """Passe de rétention dans un fil à part : jamais dans la boucle d'analyse ni dans le chemin d'un arrêt."""
        thread = self._retention_thread
        if thread is not None and thread.is_alive():
            return  # demande conservée : relancée au tour suivant la fin de la passe en cours
        self._retention_wanted = False
        try:
            self._retention_thread = self._spawn("fire-watch-retention", self._run_retention)
        except Exception as error:  # noqa: BLE001  (fil impossible : nouvel essai au tour suivant)
            self._retention_wanted = True
            self._log_once("retention", f"passe de rétention non lancée ({type(error).__name__}).")

    def _run_retention(self):
        try:
            self.enforce_retention()
        except Exception as error:  # noqa: BLE001
            self._log_once("retention", f"rétention des captures en erreur ({type(error).__name__}: {error}).")

    def _step(self, laser_id, config):
        watch = self.watches.get(laser_id)
        if watch is None:
            return 0.25
        if config["mode"] == "off":
            with self.lock:
                if watch.status is None or watch.status.get("mode") != "off" or watch.last_success is not None:
                    watch.reset(self._mono())
                self._set_status(watch, config, [], {}, self._clock(), self._mono())
            return 0.25
        camera = self._camera(config.get("camera_id"))
        machine = self._machine(laser_id)
        now, mono = self._clock(), self._mono()
        with self.lock:
            self._track_frame(watch, camera)
        if mono >= watch.next_due:
            self._analyse(watch, config, now, mono)
        with self.lock:
            self._assess(watch, config, camera, machine, self._clock(), self._mono())
        return max(0.0, watch.next_due - self._mono())

    @staticmethod
    def _track_frame(watch, camera):
        frame = camera.get("frame") if camera.get("present") is True and camera.get("connected", True) is not False else None
        frame_at = _number(camera.get("frame_at"))
        if not _valid_jpeg(frame) or frame_at is None:
            watch.frame = watch.frame_at = None
            return
        if frame is not watch.frame or frame_at != watch.frame_at:
            digest = hashlib.blake2b(frame, digest_size=16).digest()
            if digest != watch.digest:
                watch.digest, watch.digest_since = digest, frame_at
            watch.frame, watch.frame_at = frame, frame_at

    def _analyse(self, watch, config, now, mono):
        period = 1.0 / float(config["analysis_fps"])
        frame, frame_at = watch.frame, watch.frame_at
        fresh = frame is not None and now - frame_at <= config["stale_after_s"]
        new = fresh and (watch.analysed_frame_at is None or frame_at > watch.analysed_frame_at)
        simulating = bool(watch.sim)
        if new and not simulating and watch.digest == watch.analysed_digest:
            # Même image renvoyée (octets identiques) : verdict connu, aucun nouvel indice pour la validation
            # temporelle ; la règle « image figée » prend le relais après ``frozen_after_s``.
            with self.lock:
                watch.analysed_frame_at = frame_at
                watch.last_success = mono
            watch.next_due = mono + period
            return
        engine = self._engines.get((config["model"], config["provider"]))
        failed = False
        if new and engine is not None and engine.usable():
            if self._infer(watch, config, engine, frame, frame_at, now, mono, period):
                return
            failed = True
        if simulating:
            # Simulation « même sans moteur » : image synthétique datée maintenant.
            with self.lock:
                synthetic = self._take_synthetic(watch, config, now)
                if synthetic:
                    latest = max([validator.last_frame_at or 0.0 for validator in watch.validators.values()])
                    jpeg = frame if frame is not None else watch.last_jpeg
                    self._feed(watch, config, max(now, latest + 0.001), synthetic, jpeg, watch.last_size, True, now)
            watch.next_due = mono + period
            return
        # Échec : pas d'acharnement avant la période suivante. Sinon, attente d'une nouvelle image ou du moteur,
        # sans jamais accumuler de file.
        watch.next_due = mono + (period if failed else min(period, 0.1))

    def _infer(self, watch, config, engine, frame, frame_at, now, mono, period):
        """Analyse réelle d'une image ; vrai si elle a réussi (état et décisions mis à jour)."""
        started = time.monotonic()
        conf = min([CONF_FLOOR] + [config[cls]["min_score"] for cls in CLASSES if config[cls]["enabled"]])
        try:
            response = engine.infer(frame, conf=conf, iou=NMS_IOU, timeout=self.infer_timeout)
        except EngineError as error:
            with self.lock:
                # Image non analysable (ou moteur relancé) : on attend la suivante, jamais la même en boucle.
                watch.analysis_error = str(error)
                watch.analysed_frame_at = frame_at
            return False
        total_ms = (time.monotonic() - started) * 1000.0
        timing = response.get("timing_ms") if isinstance(response.get("timing_ms"), dict) else {}
        width, height = _number(response.get("width")), _number(response.get("height"))
        size = [int(width), int(height)] if width and height else None
        detections = clean_detections(response.get("detections"))
        done = self._mono()
        with self.lock:
            synthetic = self._take_synthetic(watch, config, now)
            watch.analysed_frame_at, watch.analysed_digest = frame_at, watch.digest
            watch.last_success, watch.analysed_wall = done, self._clock()
            watch.analysed += 1
            watch.analysis_times.append(done)
            watch.timings.append((_number(timing.get("inference")), total_ms))
            watch.analysis_error = None
            watch.last_detections = detections + synthetic
            watch.last_size, watch.last_jpeg, watch.last_jpeg_at = size, frame, frame_at
            self._feed(watch, config, frame_at, detections + synthetic, frame, size, bool(synthetic), now)
        # Échéance ancrée sur la précédente : le retard de réveil d'un tour (minuterie Windows, environ 16 ms) est repris
        # au tour suivant au lieu de s'ajouter à chaque période (8,7 analyses/s pour 10 réglées). Un retard d'une
        # période entière repart de maintenant : jamais de rafale ; moteur plus lent : cadence plafonnée, pas de rattrapage.
        late = mono - watch.next_due
        watch.next_due = max((watch.next_due if 0.0 <= late < period else mono) + period, done)
        return True

    @staticmethod
    def _take_synthetic(watch, config, now):
        sim = watch.sim
        if not sim or sim["remaining"] <= 0:
            return []
        sim["remaining"] -= 1
        if sim["remaining"] <= 0:
            watch.sim = None
        cls = sim["class"]
        rule, roi = config[cls], config["roi"]
        side = max(0.2, math.sqrt(rule["min_area_pct"] / 100.0) * 1.05)
        width, height = min(roi["w"], side), min(roi["h"], side)
        cx, cy = roi["x"] + roi["w"] / 2.0, roi["y"] + roi["h"] / 2.0
        box = [round(max(0.0, cx - width / 2), 5), round(max(0.0, cy - height / 2), 5),
               round(min(1.0, cx + width / 2), 5), round(min(1.0, cy + height / 2), 5)]
        watch.sim_until = now + max(config[item]["max_window_s"] for item in CLASSES) + 1.0
        return [{"label": cls, "score": max(0.95, rule["min_score"]), "box": box, "simulated": True}]

    def _feed(self, watch, config, frame_at, detections, jpeg, size, simulated, now):
        """Validation temporelle d'une image analysée (verrou tenu) puis décisions."""
        confirmed = []
        for cls in CLASSES:
            positives = filter_detections(detections, config[cls], config["roi"], cls)
            state = watch.validators[cls].update(frame_at, positives)
            watch.suspect[cls] = state
            if state["new"]:
                confirmed.append((cls, state))
        if confirmed:
            self._confirm(watch, config, confirmed, frame_at, detections, jpeg, size, now)
        if watch.incident is not None and not any(validator.active for validator in watch.validators.values()):
            watch.incident = None  # fin d'incident : un nouvel incident pourra naître

    def _confirm(self, watch, config, confirmed, frame_at, detections, jpeg, size, now):
        simulated = watch.sim_until is not None and now <= watch.sim_until
        incident = watch.incident
        stop_class = None
        changed = False
        for cls, state in confirmed:
            action = effective_action(config, cls)
            if action == "none":
                continue
            changed = True
            score = round(float(state["score"]), 4)
            if incident is None:
                event = self._template(watch.laser_id, config.get("camera_id"), config["mode"],
                                       "simulation" if simulated else "incident", now)
                first = state.get("first_positive_at")
                # Image, heure, taille et boîtes de la MÊME image : celle qui confirme l'incident.
                event.update(simulated=simulated, classes=[cls], max_score={cls: score},
                             frame_at=frame_at, frame_size=size, action=action,
                             detections=self._event_detections(detections),
                             latency={"first_positive_at": first, "confirmed_at": now,
                                      "detection_s": round(frame_at - first, 3) if first is not None else None,
                                      "analysis_delay_s": round(max(0.0, now - frame_at), 3)})
                incident = watch.incident = {"event": event, "stop_started": False, "published": False, "origin": cls}
            else:
                event = incident["event"]
                if cls not in event["classes"]:
                    event["classes"].append(cls)
                event["max_score"][cls] = max(score, float(event["max_score"].get(cls) or 0.0))
                if action == "stop" or event["action"] == "none":
                    event["action"] = "stop" if "stop" in (action, event["action"]) else "alert"
            if action == "stop" and not incident["stop_started"] and stop_class is None:
                stop_class = cls
        if incident is None or not changed:
            return
        event = incident["event"]
        event["message"] = self._incident_message(event, config)
        new = not incident["published"]
        incident["published"] = True
        if stop_class is not None:
            incident["stop_started"] = True
            label = CLASS_LABELS[stop_class]
            # Arrêt d'abord (verrou en mémoire + fil d'arrêt), capture et publication ensuite.
            self._begin_stop(watch.laser_id, config.get("camera_id"), event, f"{label} détectée par la surveillance IA",
                             config["capture_after_stop"])
        replaced = False
        if new:
            if jpeg is not None:
                self._store_capture(watch.laser_id, event, jpeg)
        elif stop_class is not None and incident.get("origin") != stop_class and jpeg is not None:
            # La classe qui déclenche l'arrêt n'a pas créé l'incident : son image devient la preuve. Capture, heure,
            # taille et boîtes sont remplacées ENSEMBLE, et seulement si la nouvelle capture est bien écrite.
            replaced = self._store_capture(watch.laser_id, event, jpeg)
            if replaced:
                event.update(frame_at=frame_at, frame_size=size, detections=self._event_detections(detections))
                incident["origin"] = stop_class
        self._publish(event, new=new, capture_changed=replaced)
        _log(f"{event['message']} (machine {watch.laser_id}, événement {event['id'][:8]}).")

    @staticmethod
    def _event_detections(detections):
        kept = []
        for item in detections[:100]:
            clean = clean_detections([item])
            if clean:
                if item.get("simulated"):
                    clean[0]["simulated"] = True
                kept.append(clean[0])
        return kept

    @staticmethod
    def _incident_message(event, config):
        found = ", ".join(f"{CLASS_LABELS[cls]} (score {_fr(event['max_score'].get(cls, 0.0), 2)})"
                          for cls in event["classes"])
        text = f"{'Simulation : ' if event.get('simulated') else ''}{found[0].upper() + found[1:]} détectée"
        if len(event["classes"]) > 1:
            text += "s"
        if event["action"] == "stop":
            return text + " : arrêt automatique demandé."
        if config["mode"] == "observe":
            return text + " : alerte seulement (mode observation, aucune commande envoyée à la machine)."
        return text + " : alerte seulement (action réglée sur « alerte »)."

    def _template(self, laser_id, camera_id, mode, kind, now):
        return {"id": str(uuid.uuid4()), "rev": 1, "laser_id": laser_id, "camera_id": camera_id, "kind": kind,
                "at": round(float(now), 3), "mode": mode, "simulated": False, "classes": [], "max_score": {},
                "detections": [], "frame_at": None, "frame_size": None, "latency": None, "action": "none",
                "stop": None, "message": "", "by": None, "capture": False, "capture_after": False}

    # -- santé et décisions ----------------------------------------------------------------------------------------
    def _assess(self, watch, config, camera, machine, now, mono):
        """Motifs, état et décisions de perte (verrou tenu), évalués à chaque tour."""
        reasons = []

        def add(code, message, blocking=True):
            reasons.append({"code": code, "message": message, "blocking": bool(blocking)})

        if camera.get("present") is not True or camera.get("connected") is False:
            add("camera_missing", "Caméra de la zone de travail absente ou déconnectée du Worker.")
        elif watch.frame is None:
            add("camera_no_frame", "La caméra ne fournit aucune image.")
        else:
            age = now - watch.frame_at
            if age > config["stale_after_s"]:
                add("camera_stale", f"Dernière image vieille de {_fr(age)} s (limite {_fr(config['stale_after_s'])} s) : "
                                    "la caméra ne suit plus.")
            frozen = watch.frame_at - watch.digest_since
            if frozen > config["frozen_after_s"]:
                add("camera_frozen", f"Image figée depuis {_fr(frozen, 0)} s (octets identiques) : caméra bloquée ?")
        camera_ok = not reasons
        engine, problem = self._engine_problem(config, mono)
        if problem:
            add(*problem)
        threshold = loss_threshold_s(config)
        if camera_ok and not problem:
            if watch.last_success is None:
                since = max(watch.enabled_at, engine.ready_seen_at if engine and engine.ready_seen_at else 0.0)
                if mono - since > threshold:
                    add("analysis_stalled", "Aucune analyse réussie depuis l'activation"
                                            + (f" ({watch.analysis_error})" if watch.analysis_error else "") + ".")
                else:
                    add("engine_starting", "Première analyse en cours…")
            elif mono - watch.last_success > threshold:
                add("analysis_stalled", f"Analyse arrêtée : dernière analyse réussie il y a {_fr(mono - watch.last_success)} s "
                                        f"(limite {_fr(threshold)} s)"
                                        + (f" ; {watch.analysis_error}" if watch.analysis_error else "") + ".")
        p95 = _percentile([total for _, total in watch.timings], 0.95)
        if p95 is not None and p95 > 1000.0:
            add("analysis_slow", f"Analyse lente : 95 % des analyses prennent jusqu'à {int(p95)} ms (plus d'une seconde).",
                blocking=False)
        auto = config["mode"] == "auto_stop"
        interval = self._measured_interval(watch)
        if interval is not None:
            for cls in CLASSES:
                action = effective_action(config, cls)
                rule = config[cls]
                if action == "none" or (rule["hits"] - 1) * interval <= rule["max_window_s"] + 1e-9:
                    continue
                # Cadence réelle (caméra lente, inférence lente) trop basse : k détections n'entrent plus dans la
                # fenêtre, la règle ne confirmera jamais. Bloquant quand cette règle doit arrêter la machine, et
                # alors perte de surveillance pendant un travail armé (LOSS_CODES) : aucune donnée n'est sûre.
                camera_fps = _number(camera.get("fps")) if isinstance(camera, dict) else None
                cause = (f" La caméra ne livre que {_fr(camera_fps)} image/s : vérifie sa cadence, sa résolution et son "
                         "format dans Mes équipements." if camera_fps is not None
                         and camera_fps < float(config["analysis_fps"]) * 0.8 else "")
                add("analysis_too_slow",
                    f"Cadence d'analyse réelle trop basse ({_fr(1.0 / interval)} image/s) : la règle {CLASS_LABELS[cls]} "
                    f"({rule['hits']} détections en {_fr(rule['max_window_s'])} s) ne peut plus confirmer.{cause}",
                    blocking=action == "stop" and auto)
        if machine.get("present") is not True or machine.get("kind", "laser") not in ("laser", "cnc"):
            add("machine_missing", "Graveuse non connectée au Worker : arrêt automatique impossible." if auto
                else "Graveuse non connectée au Worker (observation seulement).", blocking=auto)
        elif machine.get("linked") is not True:
            add("machine_unlinked", "Le Worker ne tient pas la liaison série de la graveuse (LightBurn ou autre logiciel, "
                                    "câble) : arrêt automatique impossible.", blocking=auto)
        busy = self._busy(machine)
        linked = self._linked(machine)
        # Machine perdue pendant un job surveillé : alerte, l'arrêt n'est plus possible depuis le Worker.
        if linked and machine.get("job_active") is True:
            watch.job_seen = True
        elif linked:
            watch.job_seen = False
        elif watch.job_seen:
            watch.job_seen = False
            event = self._template(watch.laser_id, config.get("camera_id"), config["mode"], "machine_lost", now)
            event.update(action="alert", message="Graveuse perdue pendant un travail surveillé (déconnexion) : arrêt "
                                                 "impossible depuis le Worker, vérifie la machine immédiatement.")
            self._publish(event, new=True)
            _log(f"{event['message']} (machine {watch.laser_id}).")
        # Perte de surveillance pendant un travail armé.
        blocking = [reason for reason in reasons if reason["blocking"]]
        lost = [reason for reason in blocking if reason["code"] in LOSS_CODES]
        if lost and auto and busy:
            if watch.loss_since is None:
                watch.loss_since = mono
            if watch.loss_event is None and mono - watch.loss_since >= config["loss_grace_s"]:
                watch.loss_event = self._loss(watch, config, lost, now)
        elif not lost:
            watch.loss_since = watch.loss_event = None
        else:
            watch.loss_since = None  # perte hors armement : période conservée (un seul événement par période)
        self._engine_events(watch, config, engine, now, mono)
        self._set_status(watch, config, reasons, machine, now, mono, busy=busy, linked=linked)

    def _loss(self, watch, config, lost, now):
        stop = config["loss_action"] == "stop"
        text = " ; ".join(reason["message"] for reason in lost)
        event = self._template(watch.laser_id, config.get("camera_id"), config["mode"], "loss", now)
        event.update(action="stop" if stop else "alert", reasons=[reason["code"] for reason in lost],
                     message=f"Surveillance perdue pendant un travail ({text}) : "
                             + ("arrêt demandé." if stop else "alerte seulement (réaction réglée sur « alerte »)."))
        if stop:  # arrêt d'abord, capture et publication ensuite
            self._begin_stop(watch.laser_id, config.get("camera_id"), event, "surveillance IA perdue pendant un travail",
                             config["capture_after_stop"])
        jpeg = watch.frame if watch.frame is not None else watch.last_jpeg
        if jpeg is not None:
            self._store_capture(watch.laser_id, event, jpeg)
        self._publish(event, new=True)
        _log(f"{event['message']} (machine {watch.laser_id}).")
        return event["id"]

    @staticmethod
    def _measured_interval(watch):
        """Intervalle réel entre analyses : médiane des écarts des 6 dernières (au moins 2), sinon ``None``.

        Aucun filtre d'âge : à une analyse toutes les 4 s, une fenêtre de 10 s perdrait sa plus ancienne analyse
        entre deux nouvelles, et « analyse trop lente » s'effacerait par intermittence alors qu'aucune flamme ne
        peut être confirmée. L'historique est vidé par ``_Watch.reset`` (coupure, caméra ou mode changés).
        """
        recent = list(watch.analysis_times)[-6:]
        gaps = sorted(later - earlier for earlier, later in zip(recent, recent[1:]) if later > earlier)
        if not gaps:
            return None
        middle = len(gaps) // 2
        return gaps[middle] if len(gaps) % 2 else (gaps[middle - 1] + gaps[middle]) / 2.0

    def _engine_events(self, watch, config, engine, now, mono):
        if engine is None:
            return
        if engine.state == "ready" and not engine.model_problem:
            watch.engine_event_open = False
            ready = engine.ready or {}
            key = (engine.model_id, engine.provider, watch.laser_id)
            if ready.get("fallback_reason") and engine.provider != "cpu" and key not in self._fallback_reported:
                self._fallback_reported.add(key)
                event = self._template(watch.laser_id, config.get("camera_id"), config["mode"], "engine", now)
                event["message"] = f"Analyse repliée sur le processeur : {ready.get('fallback_reason')}"[:500]
                self._publish(event, new=True)
            return
        if (engine.state == "error" and engine.ever_ready and not watch.engine_event_open
                and (watch.engine_event_at is None or mono - watch.engine_event_at >= ENGINE_EVENT_EVERY_S)):
            watch.engine_event_open, watch.engine_event_at = True, mono
            event = self._template(watch.laser_id, config.get("camera_id"), config["mode"], "engine", now)
            event["message"] = f"Moteur d'analyse tombé en erreur : {engine.error} Relance automatique en cours."[:500]
            self._publish(event, new=True)

    @staticmethod
    def _state_for(latch, mode, reasons):
        blocking = [reason for reason in reasons if reason["blocking"]]
        if latch:
            return "tripped"
        if mode == "off":
            return "off"
        if not blocking:
            return "active"
        if all(reason["code"] == "engine_starting" for reason in blocking):
            return "starting"
        return "degraded"

    def _latch_reason(self):
        return {"code": "latch_not_persisted", "blocking": True,
                "message": "Verrou incendie non enregistré sur le disque du Worker (disque plein ou fichier bloqué) : il "
                           "serait perdu à un redémarrage ; nouvel essai à chaque tour."}

    def _set_status(self, watch, config, reasons, machine, now, mono, busy=False, linked=False):
        latch = self._latch(watch.laser_id)
        if self._latches_dirty:
            reasons = reasons + [self._latch_reason()]
        state = self._state_for(latch, config["mode"], reasons)
        if self.config_errors.get(watch.laser_id):
            reasons = reasons + [{"code": "config_invalid", "message": f"Réglage reçu invalide, surveillance désactivée : "
                                                                       f"{self.config_errors[watch.laser_id]}",
                                  "blocking": True}]
        recent = [at for at in watch.analysis_times if mono - at <= 10.0]
        span = min(10.0, max(1.0, mono - watch.enabled_at))
        infer = [value for value, _ in watch.timings]
        total = [value for _, value in watch.timings]
        interval = self._measured_interval(watch)
        bounds = {cls: (round(config[cls]["hits"] * interval, 3) if interval is not None else latency_bound(config, cls))
                  for cls in CLASSES}
        watch.status = {
            "laser_id": watch.laser_id, "camera_id": config.get("camera_id"), "mode": config["mode"], "state": state,
            "label": STATE_LABELS[state], "reasons": reasons, "armed": config["mode"] == "auto_stop", "busy": bool(busy),
            "machine_linked": bool(linked), "latched": dict(latch) if latch else None,
            "metrics": {"analysis_fps": round(len(recent) / span, 2) if recent else 0.0,
                        "infer_ms_p50": _percentile(infer, 0.5), "infer_ms_p95": _percentile(infer, 0.95),
                        "total_ms_p95": _percentile(total, 0.95),
                        "frame_age_s": round(now - watch.frame_at, 2) if watch.frame_at is not None else None,
                        "last_analysis_age_s": round(mono - watch.last_success, 2) if watch.last_success is not None else None,
                        "analysed_frames": watch.analysed,
                        "latency_bound_s": bounds},
            "last": {"frame_at": watch.last_jpeg_at, "analysed_at": watch.analysed_wall, "size": watch.last_size,
                     "detections": list(watch.last_detections)},
            "suspect": {cls: {"hits": state_.get("hits", 0), "window": state_.get("window", 0),
                              "required": state_.get("required", config[cls]["hits"]), "active": bool(state_.get("active"))}
                        for cls, state_ in ((cls, watch.suspect.get(cls) or {}) for cls in CLASSES)},
            "simulation": {"class": watch.sim["class"], "remaining": watch.sim["remaining"]} if watch.sim else None,
        }
        watch.status_at = mono

    # -- verrous et arrêts ------------------------------------------------------------------------------------------
    def _latch(self, laser_id):
        return self.latches.get(laser_id) or self.latches.get("*")

    @staticmethod
    def _latch_record(event, reason, now):
        return {"at": now, "event_id": event["id"], "kind": event["kind"], "reason": reason,
                "message": event.get("message") or reason, "stop_status": "requested",
                "stop_message": "Arrêt demandé : confirmation de la machine en attente.", "confirmed_at": None}

    def _begin_stop(self, laser_id, camera_id, event, reason, capture_after, on_done=None):
        """Arrêt prioritaire (verrou du service tenu) : verrou EN MÉMOIRE, fil d'arrêt lancé, puis verrou écrit.

        Rien d'écrit sur disque (verrou, capture, outbox, journal) ne précède le départ du fil d'arrêt, et aucune
        erreur d'écriture ne peut l'empêcher. L'appelant enregistre ensuite la capture puis publie l'événement :
        ``_run_stop`` ne met à jour l'événement qu'après avoir repris le verrou du service, donc après eux.
        """
        now = self._clock()
        self.latches[laser_id] = self._latch_record(event, reason, now)
        event["action"] = "stop"
        event["stop"] = _requested_stop(now)
        entered = threading.Event()
        args = (laser_id, camera_id, event, reason, now, bool(capture_after), on_done, entered)
        try:
            self._spawn(f"fire-stop-{event['id'][:8]}", self._run_stop, *args)
        except Exception as error:  # noqa: BLE001  (fil impossible : l'arrêt part tout de même, ici)
            # Arrêt AVANT le journal : une sortie bloquée (console figée) ne retient jamais le 0x18.
            self._run_stop(laser_id, camera_id, event, reason, now, False, on_done, entered)
            _log(f"fil d'arrêt impossible ({type(error).__name__}) : arrêt envoyé depuis la boucle d'analyse.")
        else:
            entered.wait(STOP_START_WAIT_S)  # l'appel au relais part avant les écritures disque
        self._persist_latches()

    def _run_stop(self, laser_id, camera_id, event, reason, requested_at, capture_after, on_done, entered=None):
        event_id = event["id"]
        relay = self._relay()
        try:
            if relay is None or not hasattr(relay, "fire_stop"):
                raise RuntimeError("relais d'équipement indisponible")
            if entered is not None:
                entered.set()
            result = relay.fire_stop(laser_id, reason, event_id)
        except Exception as error:  # noqa: BLE001
            result = {"status": "failed", "message": f"Arrêt non exécuté : {error}"}
        finally:
            if entered is not None:
                entered.set()
        stop = _stop_record(result, requested_at)
        with self.lock:
            event["stop"] = stop
            latch = self.latches.get(laser_id)
            if latch is not None and latch.get("event_id") == event_id:
                # Le verrou dit si la machine est vraiment arrêtée : demandé, confirmé, échoué ou impossible.
                latch.update(stop_status=stop["status"], stop_message=stop["message"], confirmed_at=stop["confirmed_at"])
                self._persist_latches()
            self._publish(event)
        _log(f"arrêt incendie {STOP_STATUS_LABELS.get(stop['status'], stop['status'])} sur {laser_id} : {stop['message']}")
        if on_done is not None:
            on_done(stop, event_id)
        if not capture_after or not camera_id or self._pause(CAPTURE_AFTER_DELAY_S):
            return
        camera = self._camera(camera_id)
        frame = camera.get("frame") if camera.get("present") is True else None
        if not _valid_jpeg(frame):
            return
        with self.lock:
            if self._save_capture(laser_id, event_id, frame, after=True):
                event["capture_after"] = True
                self._publish(event)

    # -- garde de départ (appelée par le relais) --------------------------------------------------------------------
    def start_refusal(self, equipment_id, command, laser_on):
        """``None`` si le départ est permis, sinon le motif du refus en français."""
        laser_id = str(equipment_id or "")
        with self.lock:
            latch = self._latch(laser_id)
            if latch:
                return (f"Arrêt incendie en cours sur cette machine ({latch.get('message') or latch.get('reason')}) : aucun "
                        "départ tant qu'un opérateur n'a pas vérifié la machine et acquitté l'arrêt dans « Surveillance IA "
                        "(graveuse) ».")
            config = self.configs.get(laser_id)
            if not config or config["mode"] != "auto_stop" or not config["require_healthy_to_start"]:
                return None
            if command != "stream" and not laser_on:
                return None
            watch = self.watches.get(laser_id)
            status = watch.status if watch else None
            if status is None or watch.status_at is None or self._mono() - watch.status_at > STATUS_MAX_AGE_S:
                return ("Surveillance IA arrêtée ou figée sur ce Worker : départ refusé, l'arrêt automatique exige une "
                        "surveillance opérationnelle avant tout départ.")
            if status["state"] != "active":
                motifs = "; ".join(reason["message"] for reason in status["reasons"] if reason["blocking"])
                return (f"Surveillance IA non opérationnelle ({motifs or STATE_LABELS.get(status['state'], status['state'])}) : "
                        "départ refusé. Rétablis la surveillance, ou désactive « départ exigeant une surveillance "
                        "opérationnelle » dans ses réglages.")
        return None

    def is_latched(self, equipment_id):
        with self.lock:
            return bool(self._latch(str(equipment_id or "")))

    # -- événements, captures, rétention ------------------------------------------------------------------------------
    def _publish(self, event, new=False, capture_changed=False):
        """Enregistre une version d'événement (verrou tenu) : journal local, outbox, réveil de la synchro.

        Ne lève jamais : un disque plein ou un journal abîmé laisse l'événement en mémoire, prêt à l'envoi.
        ``capture_changed`` : capture remplacée, à renvoyer au site même si l'ancienne était acquittée.
        """
        event_id = event["id"]
        if not new or event_id in self._outbox or event_id in self._recent:
            event["rev"] = int(event.get("rev") or 1) + 1
        self._recent[event_id] = event
        self._recent.move_to_end(event_id)
        for key in list(self._recent) if len(self._recent) > RECENT_EVENTS else ():
            if len(self._recent) <= RECENT_EVENTS:
                break
            if key not in self._outbox:  # un événement encore à envoyer reste accessible (arrêt en cours)
                self._recent.pop(key)
        acked = self._acked_captures.get(event_id, {})
        entry = self._outbox.get(event_id) or {"capture_acked": acked.get("capture", False),
                                               "capture_after_acked": acked.get("capture_after", False)}
        if capture_changed:
            entry["capture_acked"] = False
            if event_id in self._acked_captures:
                self._acked_captures[event_id]["capture"] = False
        entry["event"] = json.loads(json.dumps(event, ensure_ascii=False))
        self._outbox[event_id] = entry
        try:
            _write_json(self.data_dir / "outbox" / f"{event_id}.json", entry)
        except Exception as error:  # noqa: BLE001
            self._log_once("outbox", f"événement non enregistré sur disque ({type(error).__name__}: {error}) ; envoi "
                                     "depuis la mémoire.")
        try:
            self._append_log(entry["event"])
        except Exception as error:  # noqa: BLE001
            self._log_once("events-log", f"journal des événements non écrit ({type(error).__name__}: {error}).")
        self._sync_wake.set()

    def _append_log(self, event):
        """Ajout au journal ``events.jsonl`` (octets, UTF-8) ; réécrit par paquets au-delà de ``EVENTS_LOG_LINES``.

        Une ligne abîmée (coupure au milieu d'un caractère) ne bloque jamais la rotation, et un échec de rotation
        n'est retenté qu'après un nouveau paquet de lignes, jamais à chaque publication.
        """
        path = self.data_dir / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8", errors="replace") + b"\n"
        with path.open("a+b") as handle:
            size = handle.seek(0, os.SEEK_END)
            if size:
                handle.seek(size - 1)
                if handle.read(1) != b"\n":  # dernière ligne coupée (arrêt brutal) : la nouvelle repart à la ligne
                    line = b"\n" + line
            handle.write(line)
        self._log_lines += 1
        if self._log_lines <= EVENTS_LOG_LINES + max(1, EVENTS_LOG_LINES // 10):  # réécrit par paquets
            return
        kept = EVENTS_LOG_LINES
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            lines = [item for item in path.read_bytes().splitlines() if item.strip()][-EVENTS_LOG_LINES:]
            kept = len(lines)
            with temporary.open("xb") as handle:
                handle.write(b"\n".join(lines) + b"\n")
            os.replace(temporary, path)
        finally:
            self._log_lines = min(self._log_lines, kept)
            try:
                temporary.unlink()
            except OSError:
                pass

    def _capture_path(self, laser_id, event_id, after=False):
        return self.data_dir / "captures" / laser_id / f"{event_id}{'-after' if after else ''}.jpg"

    def _save_capture(self, laser_id, event_id, jpeg, after=False):
        """Écrit une capture ; la rétention suit hors de ce chemin (fil dédié, lancé au tour suivant)."""
        if not _valid_jpeg(jpeg) or not self._identifier(laser_id):
            return False
        try:
            _write_bytes(self._capture_path(laser_id, event_id, after), bytes(jpeg))
        except Exception as error:  # noqa: BLE001
            self._log_once("capture", f"capture non enregistrée ({type(error).__name__}: {error}).")
            return False
        self._retention_wanted = True
        return True

    def _store_capture(self, laser_id, event, jpeg):
        """Capture principale d'un événement ; ``capture_rev`` : 1 à la première, +1 à chaque remplacement."""
        if not self._save_capture(laser_id, event["id"], jpeg):
            return False
        event["capture"] = True
        event["capture_rev"] = int(event.get("capture_rev") or 0) + 1
        return True

    def enforce_retention(self):
        """Supprime les captures au-delà de ``retention_days`` puis les plus anciennes jusqu'à ``max_storage_mb``."""
        root = self.data_dir / "captures"
        if not root.is_dir():
            return
        with self.lock:
            configs = dict(self.configs)
        default_days = max([config["retention_days"] for config in configs.values()] or [DEFAULT_CONFIG["retention_days"]])
        budget = max([config["max_storage_mb"] for config in configs.values()] or [DEFAULT_CONFIG["max_storage_mb"]]) * STORAGE_UNIT
        now = self._clock()
        kept = []
        try:
            folders = [folder for folder in root.iterdir() if folder.is_dir() and not folder.is_symlink()]
        except OSError:
            return
        for folder in folders:
            config = configs.get(folder.name)
            limit = (config["retention_days"] if config else default_days) * 86400.0
            for path in folder.glob("*.jpg"):
                try:
                    info = path.stat()
                    if now - info.st_mtime > limit:
                        path.unlink()
                    else:
                        kept.append((info.st_mtime, info.st_size, path))
                except OSError:
                    continue
        total = sum(size for _, size, _ in kept)
        for _, size, path in sorted(kept, key=lambda item: item[0]):
            if total <= budget:
                break
            try:
                path.unlink()
                total -= size
            except OSError:
                continue

    # -- synchronisation avec le site ---------------------------------------------------------------------------------
    def sync_once(self):
        """Un échange ``POST /api/worker-protocol/fire-watch/sync`` ; renvoie l'attente avant le suivant (s).

        Un seul échange à la fois : un fil d'une génération précédente qui termine sa requête et le fil courant
        ne s'entrecroisent jamais (événements et résultats envoyés, réglages appliqués).
        """
        with self._sync_lock:
            return self._sync_exchange()

    def _sync_exchange(self):
        started = self._mono()
        body, sent = self._sync_body()
        try:
            response = self.agent.request(SYNC_PATH, "POST", body)
            if not isinstance(response, dict) or response.get("ok") is not True:
                raise RuntimeError((response or {}).get("error") if isinstance(response, dict) else "réponse invalide")
        except Exception as error:
            failures = self.sync_state["failures"] + 1
            code = getattr(error, "code", None)
            message = ("site sans surveillance IA (version trop ancienne)" if code == 404
                       else f"{type(error).__name__}: {error}")[:300]
            if self.sync_state["error"] != message or self.sync_state["failures"] == 0:
                _log(f"synchronisation avec le site impossible ({message}) ; l'analyse locale continue.")
            self.sync_state.update(ok=False, error=message, failures=failures)
            return SYNC_BACKOFF_S[-1] if code == 404 else SYNC_BACKOFF_S[min(failures - 1, len(SYNC_BACKOFF_S) - 1)]
        if self.sync_state["failures"]:
            _log("synchronisation avec le site rétablie.")
        self.sync_state.update(ok=True, error=None, failures=0, last_ok_at=self._clock())
        self._apply_response(response, sent)
        with self.lock:
            # Un verrou présent compte comme activité : l'état « arrêt incendie » reste frais sur le site.
            busy = (any(config["mode"] != "off" for config in self.configs.values()) or self._outbox or self.latches
                    or any(entry.get("status") == "done" and not entry.get("sent") for entry in self._requests.values())
                    or self._wanted_frames())
            backlog = len(self._outbox) > MAX_EVENTS_PER_SYNC or len(sent["captures"]) >= MAX_CAPTURE_EVENTS
        # Échange plus long que la limite d'un état frais (corps lourd, liaison lente) : l'instantané reçu par le site
        # était déjà périmé à l'arrivée ; un état frais le remplace aussitôt.
        slow = bool(busy) and self._mono() - started > STATUS_MAX_AGE_S
        if backlog or slow:
            return 0.2
        return SYNC_ACTIVE_S if busy else SYNC_IDLE_S

    @staticmethod
    def _priority(entry):
        """Ordre d'envoi d'une entrée de l'outbox (0 d'abord), puis par date.

        0 : arrêt pas encore final, échoué ou impossible, ou machine perdue (elle peut encore fonctionner) ;
        1 : autre incident ou perte ; 2 : le reste, et toute version déjà acquittée par le site (seule sa capture
        reste à livrer).
        """
        event = entry["event"]
        acked = entry.get("acked_rev")
        if isinstance(acked, int) and not isinstance(acked, bool) and acked >= int(event.get("rev") or 1):
            return 2
        stop = event.get("stop") if isinstance(event.get("stop"), dict) else {}
        if stop.get("status") in ("requested", "failed", "impossible") or event.get("kind") == "machine_lost":
            return 0
        return 1 if event.get("kind") in CRITICAL_KINDS else 2

    def _wanted_frames(self):
        now = self._mono()
        return {laser_id for laser_id, until in self._want_frames.items() if until >= now}

    def _relay_grbl_ids(self):
        """Machines GRBL (laser, CNC) connues du relais ; JAMAIS appelé verrou du service tenu (ordre des verrous)."""
        relay = self._relay()
        devices = getattr(relay, "devices", None)
        if not isinstance(devices, dict):
            return set()
        lock = getattr(relay, "lock", None)
        acquired = False
        try:
            if lock is not None:
                acquired = lock.acquire(timeout=2.0)
            items = list(devices.items())
        except Exception:  # noqa: BLE001  (dictionnaire modifié pendant la lecture sans verrou)
            return set()
        finally:
            if acquired:
                lock.release()
        found = set()
        for key, device in items:
            spec = getattr(device, "spec", None)
            if isinstance(spec, dict) and spec.get("kind") in GRBL_KINDS and isinstance(key, str) and self._identifier(key):
                found.add(key)
        return found

    def _reported_ids(self, relay_ids=()):
        """Fiches rapportées (verrou tenu) : réglages, verrous explicites et, sous verrou global, toutes les machines."""
        ids = set(self.configs) | {key for key in self.latches if key != "*"}
        if "*" in self.latches:
            ids |= set(self.watches) | set(relay_ids)
        return sorted(ids)

    def _reported_status(self, laser_id, mono, aged=False):
        """Copie de l'état d'une fiche pour le site ou ``local_control`` (verrou tenu), jamais « active » si périmé.

        L'instantané vient du dernier tour d'analyse : ``status_age_s`` dit son âge. Au-delà de
        ``STATUS_MAX_AGE_S`` (ou sans instantané après le démarrage), la boucle d'analyse est arrêtée ou bloquée :
        état « dégradé » et motif bloquant ``analysis_loop_stalled``. ``aged`` augmente aussi les âges des
        métriques (état local) ; le site, lui, les augmente de ``status_age_s``.
        """
        watch = self.watches.get(laser_id)
        config = self.configs.get(laser_id) or clean_config(None, laser_id=laser_id)
        latch = self._latch(laser_id)
        if watch is not None and watch.status is not None and watch.status_at is not None:
            status = json.loads(json.dumps(watch.status, ensure_ascii=False))
            age = max(0.0, mono - watch.status_at)
            stalled = age > STATUS_MAX_AGE_S
        else:
            status = {"laser_id": laser_id, "camera_id": config.get("camera_id"), "mode": config["mode"], "state": "starting",
                      "reasons": [], "armed": config["mode"] == "auto_stop", "busy": False, "machine_linked": False,
                      "latched": None, "metrics": {}, "last": {}, "suspect": {}}
            age = None
            starts = [value for value in (watch.enabled_at if watch is not None else None, self._started_at)
                      if value is not None]
            stalled = bool(starts) and mono - max(starts) > STATUS_MAX_AGE_S
            if not stalled and config["mode"] != "off":
                status["reasons"] = [{"code": "engine_starting", "message": "Démarrage de la surveillance…",
                                      "blocking": True}]
        status.pop("label", None)
        mode = status.get("mode") or config["mode"]
        reasons = [reason for reason in status.get("reasons") or [] if reason.get("code") != "latch_not_persisted"]
        if self._latches_dirty:
            reasons.append(self._latch_reason())
        if stalled and mode != "off":
            waited = f" depuis {_fr(age)} s" if age is not None else ""
            reasons = [reason for reason in reasons if reason.get("code") != "engine_starting"] + [
                {"code": "analysis_loop_stalled", "blocking": True,
                 "message": f"Boucle d'analyse arrêtée ou bloquée sur le Worker : aucun état frais{waited}."}]
        status["reasons"] = reasons
        status["status_age_s"] = round(age, 2) if age is not None else None
        if aged and age is not None and isinstance(status.get("metrics"), dict):
            metrics = status["metrics"]
            for key in ("frame_age_s", "last_analysis_age_s"):
                if isinstance(metrics.get(key), (int, float)) and not isinstance(metrics.get(key), bool):
                    metrics[key] = round(metrics[key] + age, 2)
        # Verrou toujours à jour (un acquittement peut suivre le dernier tour) ; état recalculé.
        status["latched"] = dict(latch) if latch else None
        status["state"] = self._state_for(latch, mode, reasons)
        status["engine"] = self._engine_block(config)
        return status

    def _sync_body(self):
        relay_ids = self._relay_grbl_ids() if "*" in self.latches else set()
        with self.lock:
            watches = []
            mono = self._mono()
            sent_at = round(self._clock(), 3)  # heure murale de l'instantané : le site y ajoute le temps de transit
            for laser_id in self._reported_ids(relay_ids):
                watches.append((self._reported_status(laser_id, mono), self.watches.get(laser_id)))
            wanted = self._wanted_frames()
            done = [{"id": entry["id"], "ok": bool(entry.get("ok")), "result": entry.get("result"), "error": entry.get("error")}
                    for entry in self._requests.values() if entry.get("status") == "done" and not entry.get("sent")]
            # Au plus MAX_EVENTS_PER_SYNC (la limite du site) : les captures ne sont jointes qu'aux événements traités,
            # et un incident, une perte ou un arrêt pas encore final passe avant l'arriéré (alertes, moteur…).
            entries = sorted(self._outbox.values(), key=lambda entry: (self._priority(entry), entry["event"].get("at") or 0,
                                                                       entry["event"]["id"]))
            events = [(json.loads(json.dumps(entry["event"], ensure_ascii=False)), not entry["capture_acked"],
                       not entry["capture_after_acked"]) for entry in entries[:MAX_EVENTS_PER_SYNC]]
            engine = self._engine_report()
            frames = {}
            for status, watch in watches:
                if watch is not None and status["laser_id"] in wanted and watch.last_jpeg is not None \
                        and watch.last_jpeg_at != watch.frame_sent_at:
                    frames[status["laser_id"]] = (watch.last_jpeg, watch.last_jpeg_at, list(watch.last_detections))
        body = {"agent_version": self.agent_version, "config_revision": self.revision, "engine": engine,
                "watches": [status for status, _ in watches], "events": [], "done": done, "sent_at": sent_at}
        size = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        sent = {"events": {}, "captures": set(), "done": [item["id"] for item in done], "frames": {}}
        for event, need_capture, need_after in events:
            size += len(json.dumps(event, ensure_ascii=False).encode("utf-8")) + 2
            included = {"rev": event["rev"], "capture": False, "capture_after": False,
                        "capture_rev": event.get("capture_rev")}
            for key, needed in (("capture", need_capture), ("capture_after", need_after)):
                # Au plus 4 événements avec capture par envoi ; les autres suivent au prochain échange.
                if not needed or not event.get(key) or (event["id"] not in sent["captures"]
                                                        and len(sent["captures"]) >= MAX_CAPTURE_EVENTS):
                    continue
                try:
                    data = self._capture_path(event["laser_id"], event["id"], key == "capture_after").read_bytes()
                except OSError:
                    continue
                encoded = base64.b64encode(data).decode("ascii")
                if size + len(encoded) + 32 > MAX_SYNC_BODY:
                    continue
                event[f"{key}_b64"] = encoded
                size += len(encoded) + 32
                included[key] = True
                sent["captures"].add(event["id"])
            body["events"].append(event)
            sent["events"][event["id"]] = included
        for status in body["watches"]:
            frame = frames.get(status["laser_id"])
            if frame is None:
                continue
            encoded = base64.b64encode(frame[0]).decode("ascii")
            if size + len(encoded) + 64 > MAX_SYNC_BODY:
                continue
            status["frame"], status["frame_detections"] = encoded, frame[2]
            size += len(encoded) + 64
            sent["frames"][status["laser_id"]] = frame[1]
        return body, sent

    def _apply_response(self, response, sent):
        self._apply_configs(response.get("revision"), response.get("configs"))
        with self.lock:
            self._ack_events(response.get("acked_events"), sent["events"])
            changed = False
            for request_id in sent["done"]:
                entry = self._requests.get(request_id)
                if entry is not None and entry.get("status") == "done" and not entry.get("sent"):
                    entry["sent"] = changed = True
            if changed:
                self._save_requests()
            for laser_id, frame_at in sent["frames"].items():
                watch = self.watches.get(laser_id)
                if watch is not None:
                    watch.frame_sent_at = frame_at
            wanted = response.get("want_frames")
            until = self._mono() + WANT_FRAMES_TTL_S
            self._want_frames = {laser_id: until for laser_id in (wanted if isinstance(wanted, list) else [])
                                 if isinstance(laser_id, str) and self._identifier(laser_id)}
        requests = response.get("requests")
        if isinstance(requests, list):
            self._handle_requests(requests[:MAX_REQUESTS_PER_SYNC])

    def _ack_events(self, acked, sent):
        """Retire de l'outbox les événements acquittés à leur révision (verrou tenu).

        Capture : un site récent renvoie ``capture_rev`` quand il a écrit la capture ; elle n'est tenue pour livrée
        qu'à la même révision de capture que celle envoyée (et toujours courante). Un site ancien (aucun
        ``capture_rev`` dans la réponse) garde l'ancien comportement : capture livrée avec l'événement acquitté.
        """
        items = [item for item in acked if isinstance(item, dict)] if isinstance(acked, list) else []
        modern = any("capture_rev" in item for item in items)
        for item in items:
            event_id, rev = item.get("id"), item.get("rev")
            entry = self._outbox.get(event_id)
            if entry is None or not isinstance(rev, int) or isinstance(rev, bool):
                continue
            included = sent.get(event_id)
            event = entry["event"]
            if included and rev >= included["rev"]:
                if included["capture"]:
                    current = event.get("capture_rev")
                    delivered = included.get("capture_rev") == current
                    if modern:
                        received = item.get("capture_rev")
                        delivered = (delivered and isinstance(received, int) and not isinstance(received, bool)
                                     and received == current)
                    entry["capture_acked"] = entry["capture_acked"] or delivered
                entry["capture_after_acked"] = entry["capture_after_acked"] or included["capture_after"]
            previous = entry.get("acked_rev")
            if not isinstance(previous, int) or isinstance(previous, bool) or rev > previous:
                entry["acked_rev"] = rev   # version reçue par le site : plus prioritaire à l'envoi (capture seule)
            self._acked_captures[event_id] = {"capture": entry["capture_acked"], "capture_after": entry["capture_after_acked"]}
            while len(self._acked_captures) > REQUESTS_KEPT:
                self._acked_captures.popitem(last=False)
            pending = any(event.get(key) and not entry[f"{key}_acked"]
                          and self._capture_path(event["laser_id"], event_id, key == "capture_after").is_file()
                          for key in ("capture", "capture_after"))
            path = self.data_dir / "outbox" / f"{event_id}.json"
            try:
                if rev >= int(event.get("rev") or 1) and not pending:
                    self._outbox.pop(event_id, None)
                    path.unlink(missing_ok=True)
                else:
                    _write_json(path, entry)
            except Exception as error:  # noqa: BLE001
                self._log_once("outbox", f"outbox non mise à jour ({type(error).__name__}: {error}).")

    def _apply_configs(self, revision, configs):
        if not isinstance(revision, int) or isinstance(revision, bool) or not isinstance(configs, list):
            return
        if revision == self.revision:
            return
        cleaned, errors = {}, {}
        for raw in configs:
            if not isinstance(raw, dict):
                continue
            try:
                laser_id = clean_identifier(raw.get("laser_id") or raw.get("id"), "Machine")
            except ValueError:
                continue
            if not laser_id:
                continue
            try:
                cleaned[laser_id] = clean_config(raw, laser_id=laser_id)
            except ValueError as error:
                # Réglage refusé : surveillance désactivée ET signalée (le site voit le motif), jamais « active ».
                errors[laser_id] = str(error)
                cleaned[laser_id] = clean_config(None, laser_id=laser_id)
                _log(f"réglage refusé pour {laser_id} ({error}).")
        mono = self._mono()
        with self.lock:
            for laser_id, config in cleaned.items():
                old = self.configs.get(laser_id)
                watch = self.watches.get(laser_id)
                if watch is None:
                    self.watches[laser_id] = _Watch(laser_id, config, mono)
                    continue
                if old == config:
                    continue
                if config["mode"] == "off" or old is None or old["mode"] == "off" or old.get("camera_id") != config.get("camera_id"):
                    watch.reset(mono, camera=True)
                if old is None or any(old[cls] != config[cls] or effective_action(old, cls) != effective_action(config, cls)
                                      for cls in CLASSES):
                    # Règle ou action réelle changée (observation → arrêt automatique compris) : une flamme encore
                    # visible se reconfirme sur de nouvelles images et déclenche l'action désormais réglée.
                    watch.validators = {cls: TemporalValidator(config[cls]) for cls in CLASSES}
                    watch.incident = None
                if old is not None and old["analysis_fps"] != config["analysis_fps"]:
                    watch.timings.clear()
                watch.config = config
            for laser_id in [key for key in self.watches if key not in cleaned]:
                self.watches.pop(laser_id)
            self.configs, self.config_errors, self.revision = cleaned, errors, revision
            try:
                self._save_configs()
            except OSError as error:
                self._log_once("config", f"réglages non enregistrés sur disque ({error}).")
        _log(f"réglages appliqués (révision {revision}, {sum(1 for c in cleaned.values() if c['mode'] != 'off')} "
             "machine(s) surveillée(s)).")

    # -- requêtes du site ---------------------------------------------------------------------------------------------
    def _handle_requests(self, requests):
        for item in requests:
            if not isinstance(item, dict):
                continue
            request_id = str(item.get("id") or "")
            if not REQUEST_ID.fullmatch(request_id):
                continue
            with self.lock:
                known = self._requests.get(request_id)
                if known is not None:
                    # Requête rejouée : jamais réexécutée ; le résultat déjà obtenu est renvoyé tel quel.
                    if known.get("status") == "done" and known.get("sent"):
                        known["sent"] = False
                        self._save_requests()
                    continue
                entry = {"id": request_id, "type": str(item.get("type") or "")[:32], "laser_id": item.get("laser_id"),
                         "status": "running", "at": self._clock(), "sent": False, "ok": None, "result": None, "error": None}
                self._requests[request_id] = entry
                try:
                    self._save_requests()  # écrit AVANT l'exécution : un redémarrage ne relance jamais la requête
                except OSError as error:
                    self._log_once("requests", f"requête non enregistrée ({error}).")
            try:
                outcome = self._execute(request_id, item)
            except ValueError as error:
                outcome = (False, None, str(error))
            except Exception as error:
                outcome = (False, None, f"Erreur interne du Worker ({type(error).__name__}).")
                _log(f"requête {item.get('type')} en erreur ({type(error).__name__}: {error}).")
            if outcome is not None:
                self._finish_request(request_id, *outcome)

    def _finish_request(self, request_id, ok, result, error):
        with self.lock:
            entry = self._requests.get(request_id)
            if entry is None:
                return
            entry.update(status="done", ok=bool(ok), result=_plain(result, 3 * 1024 * 1024) if result is not None else None,
                         error=str(error)[:1000] if error else None, sent=False, finished_at=self._clock())
            try:
                self._save_requests()
            except OSError as failure:
                self._log_once("requests", f"résultat de requête non enregistré ({failure}).")
        self._sync_wake.set()

    def _execute(self, request_id, item):
        kind = item.get("type")
        laser_id = clean_identifier(item.get("laser_id"), "Machine")
        if not laser_id:
            raise ValueError("Machine manquante.")
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        by = str(item.get("by") or "")[:120] or None
        if kind == "acknowledge":
            return self._acknowledge(laser_id, params, by)
        if kind == "stop_test":
            return self._stop_test(request_id, laser_id, by)
        if kind == "simulate":
            return self._simulate(laser_id, params)
        if kind == "evaluate":
            return self._start_evaluate(request_id, laser_id, params)
        raise ValueError("Requête inconnue pour la surveillance IA.")

    def _acknowledge(self, laser_id, params, by):
        note = str(params.get("note") or "").strip()[:500]
        wanted = params.get("event_id")
        if wanted not in (None, ""):
            try:
                wanted = str(uuid.UUID(str(wanted)))
            except (ValueError, TypeError, AttributeError):
                raise ValueError("Identifiant de l'arrêt à acquitter invalide.") from None
        else:
            wanted = None
        relay_ids = self._relay_grbl_ids() if "*" in self.latches else set()  # hors verrou du service
        with self.lock:
            wildcard = self.latches.get("*")
            latch = self.latches.get(laser_id) or wildcard
            current_id = latch.get("event_id") if latch else None
            if wanted and current_id and current_id != wanted:
                # Acquittement envoyé pour un arrêt précédent : le nouvel arrêt n'a été vu par personne.
                return False, {"latched": True, "event_id": current_id}, ("Nouvel arrêt depuis ta vérification : vérifie la "
                                                                          "machine puis acquitte de nouveau.")
            if latch is not None:
                remaining = {key: value for key, value in self.latches.items() if key not in (laser_id, "*")}
                if wildcard is not None:
                    # Verrou global (verrous illisibles au démarrage) : chaque AUTRE machine connue garde son propre
                    # verrou, à acquitter séparément ; seule celle-ci est levée.
                    for other in (set(self.configs) | set(self.watches) | relay_ids) - {laser_id, "*"}:
                        remaining.setdefault(other, dict(wildcard))
                try:
                    _write_json(self.data_dir / "latches.json", remaining)  # disque d'abord, mémoire ensuite
                except Exception as error:  # noqa: BLE001
                    self._log_once("ack", f"acquittement non enregistré ({type(error).__name__}: {error}).")
                    return False, {"latched": True, "event_id": current_id}, (
                        f"Acquittement non enregistré sur le disque du Worker ({type(error).__name__}) : la machine reste "
                        "verrouillée ; réessaie dans un instant.")
                self.latches = remaining
                self._latches_dirty = False
        release_error = None
        relay = self._relay()
        try:
            if relay is not None and hasattr(relay, "release_halt"):
                relay.release_halt(laser_id)
        except Exception as error:
            release_error = f"déblocage de l'envoi non confirmé ({error}) : reconnecte la machine avant de relancer."
        if latch is None:
            return True, {"latched": False, "release_error": release_error,
                          "message": "Aucun arrêt incendie à acquitter sur cette machine."}, None
        with self.lock:
            config = self.configs.get(laser_id) or clean_config(None, laser_id=laser_id)
            watch = self.watches.get(laser_id)
            if watch is not None:
                # Une flamme encore visible se reconfirme sur de nouvelles images et reverrouille la machine.
                for validator in watch.validators.values():
                    validator.reset()
                watch.incident = None
                watch.loss_since = watch.loss_event = None
            event = self._template(laser_id, config.get("camera_id"), config["mode"], "acknowledged", self._clock())
            event.update(by=by, acknowledged_event_id=latch.get("event_id"),
                         message=f"Arrêt acquitté par {by or 'un opérateur'}" + (f" : {note}" if note else "") + ".")
            self._publish(event, new=True)
        _log(f"arrêt incendie acquitté sur {laser_id}.")
        return True, {"latched": True, "event_id": event["id"], "release_error": release_error}, None

    def _stop_test(self, request_id, laser_id, by):
        machine = self._machine(laser_id)
        now = self._clock()
        with self.lock:
            config = self.configs.get(laser_id) or clean_config(None, laser_id=laser_id)
            event = self._template(laser_id, config.get("camera_id"), config["mode"], "stop_test", now)
            event.update(by=by, message=f"Test d'arrêt réel demandé par {by or 'un opérateur'} (opérateur présent).")
            if not self._linked(machine):
                missing = machine.get("present") is not True or machine.get("kind", "laser") not in ("laser", "cnc")
                stop = _requested_stop(now)
                stop.update(status="impossible", message="Graveuse non connectée au Worker : test d'arrêt impossible."
                            if missing else "Le Worker ne tient pas la liaison série de la graveuse : test d'arrêt impossible.")
                event.update(action="stop", stop=stop)
                self._publish(event, new=True)
                return False, {"event_id": event["id"], "stop": stop}, stop["message"]
            watch = self.watches.get(laser_id)
            jpeg = watch.frame if watch is not None and watch.frame is not None else None
        if jpeg is None and config.get("camera_id"):
            camera = self._camera(config["camera_id"])
            jpeg = camera.get("frame") if camera.get("present") is True and _valid_jpeg(camera.get("frame")) else None

        def finished(stop, event_id):
            ok = stop["status"] == "confirmed"
            self._finish_request(request_id, ok, {"event_id": event_id, "stop": stop}, None if ok else stop["message"])

        with self.lock:  # arrêt d'abord, capture et publication ensuite
            self._begin_stop(laser_id, config.get("camera_id"), event, "test d'arrêt demandé par un opérateur",
                             config["capture_after_stop"], on_done=finished)
            if jpeg is not None:
                self._store_capture(laser_id, event, jpeg)
            self._publish(event, new=True)
        return None

    def _simulate(self, laser_id, params):
        cls = params.get("class")
        if cls not in CLASSES:
            raise ValueError("Classe à simuler invalide (flamme ou fumée).")
        frames = params.get("frames", 5)
        if isinstance(frames, bool) or not isinstance(frames, int) or not 3 <= frames <= 30:
            raise ValueError("Nombre d'images simulées invalide (3 à 30).")
        with self.lock:
            config = self.configs.get(laser_id)
            watch = self.watches.get(laser_id)
            if config is None or config["mode"] == "off" or watch is None:
                raise ValueError("Surveillance désactivée sur cette machine : passe en observation pour simuler une détection.")
            watch.sim = {"class": cls, "remaining": frames}
            action = effective_action(config, cls)
        return True, {"class": cls, "frames": frames, "mode": config["mode"], "action": action,
                      "required": config[cls]["hits"]}, None

    # -- évaluation d'une vidéo enregistrée ----------------------------------------------------------------------------
    def _start_evaluate(self, request_id, laser_id, params):
        token = str(params.get("video_token") or "")
        if not VIDEO_TOKEN.fullmatch(token):
            raise ValueError("Vidéo à évaluer invalide.")
        installed = self._component()
        if not installed["ready"]:
            raise ValueError("Moteur local de surveillance non installé sur ce Worker.")
        models = params.get("models") if isinstance(params.get("models"), list) else []
        models = [model_id for model_id in dict.fromkeys(models) if model_id in MODEL_IDS]
        if not models:
            raise ValueError("Choisis au moins un modèle à comparer.")
        missing = [model_id for model_id in models if model_id not in installed["models"]]
        if missing:
            raise ValueError(f"Modèle(s) non installé(s) sur ce Worker : {', '.join(missing)}.")
        sample_fps = _number(params.get("sample_fps", 5))
        if sample_fps is None or not 0.2 <= sample_fps <= 10:
            raise ValueError("Cadence d'échantillonnage invalide (0,2 à 10 images/s).")
        max_seconds = params.get("max_seconds", 600)
        if isinstance(max_seconds, bool) or not isinstance(max_seconds, int) or not 1 <= max_seconds <= 3600:
            raise ValueError("Durée maximale invalide (1 à 3600 s).")
        raw = params.get("config") if isinstance(params.get("config"), dict) else {}
        config = clean_config({**raw, "mode": "off", "camera_id": None})
        provider = params.get("provider") if params.get("provider") in PROVIDERS else config["provider"]
        if protect_credentials is None and self._download == self._download_from_site:
            raise ValueError("Téléchargement authentifié indisponible sur ce Worker.")
        with self.lock:
            if self._evaluating:
                raise ValueError("Une évaluation vidéo est déjà en cours sur ce Worker.")
            self._evaluating = request_id
        self._spawn("fire-watch-eval", self._run_evaluate, request_id, token,
                    {model_id: installed["models"][model_id]["path"] for model_id in models}, sample_fps, max_seconds,
                    config, provider)
        return None

    def _run_evaluate(self, request_id, token, models, sample_fps, max_seconds, config, provider):
        folder = self.root / "temp" / f"fire-watch-eval-{uuid.uuid4().hex[:12]}"
        outcome = (False, None, "Évaluation interrompue.")
        try:
            folder.mkdir(parents=True, exist_ok=True)
            video, config_path, output = folder / "video.bin", folder / "config.json", folder / "result.json"
            self._download(VIDEO_PATH + urllib.parse.quote(token, safe=""), video, EVALUATE_MAX_VIDEO)
            _write_json(config_path, config, durable=False)
            spec = self.evaluate_command(video=video, models=models, sample_fps=sample_fps, max_seconds=max_seconds,
                                         config_path=config_path, provider=provider, output=output)
            log = _open_log(self.root / "logs" / "fire-watch-evaluate.log")
            try:
                completed = subprocess.run(spec["args"], stdin=subprocess.DEVNULL, stdout=log or subprocess.DEVNULL,
                                           stderr=log or subprocess.DEVNULL, cwd=spec.get("cwd"), env=spec.get("env"),
                                           creationflags=int(spec.get("creationflags") or 0), timeout=EVALUATE_TIMEOUT_S,
                                           check=False)
            finally:
                if log is not None:
                    log.close()
            if completed.returncode:
                # evaluate.py écrit son motif en français dans le JSON de sortie ({"ok": false, "error": ...}).
                raise RuntimeError(self._evaluate_error(output) or f"l'outil d'évaluation a échoué (code "
                                   f"{completed.returncode}) ; détails dans logs/fire-watch-evaluate.log.")
            if not output.is_file() or output.stat().st_size > EVALUATE_MAX_RESULT:
                raise RuntimeError("résultat d'évaluation absent ou trop volumineux.")
            result = json.loads(output.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                raise RuntimeError("résultat d'évaluation invalide.")
            outcome = (True, result, None)
        except subprocess.TimeoutExpired:
            outcome = (False, None, "Évaluation trop longue (plus de 20 minutes) : raccourcis la vidéo ou baisse la cadence.")
        except Exception as error:
            outcome = (False, None, f"Évaluation impossible : {error}"[:1000])
        finally:
            shutil.rmtree(folder, ignore_errors=True)
            with self.lock:
                self._evaluating = None
        self._finish_request(request_id, *outcome)

    @staticmethod
    def _evaluate_error(output):
        """Motif d'échec écrit par evaluate.py, ou ``None`` (fichier absent, trop gros ou illisible)."""
        try:
            if not output.is_file() or output.stat().st_size > EVALUATE_MAX_RESULT:
                return None
            result = json.loads(output.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return None
        error = result.get("error") if isinstance(result, dict) and result.get("ok") is False else None
        return error.strip()[:500] if isinstance(error, str) and error.strip() else None

    def _download_from_site(self, path, destination, limit):
        agent = self.agent
        reload = getattr(agent, "reload_config", None)
        if callable(reload):
            reload()
        config = getattr(agent, "config", None) or {}
        headers = {"Authorization": "Bearer " + str(config.get("token", "")), "X-Worker-ID": str(config.get("worker_id", "")),
                   "User-Agent": f"AlpineWorker/{self.agent_version}"}
        request = protect_credentials(urllib.request.Request(str(agent.server) + path, headers=headers, method="GET"))
        total = 0
        with urllib.request.urlopen(request, timeout=60) as response, Path(destination).open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise RuntimeError("vidéo trop volumineuse (512 Mio au plus).")
                output.write(chunk)
        if not total:
            raise RuntimeError("vidéo téléchargée vide.")

    # -- état local ---------------------------------------------------------------------------------------------------
    def local_status(self):
        """Instantané JSON pour ``local_control`` : aucun secret, aucune image ; jamais « active » si périmé."""
        relay_ids = self._relay_grbl_ids() if "*" in self.latches else set()  # hors verrou du service
        with self.lock:
            mono = self._mono()
            watches = [self._reported_status(laser_id, mono, aged=True)
                       for laser_id in sorted(set(self._reported_ids(relay_ids)) | set(self.watches))]
            recent = [{"id": event["id"], "kind": event["kind"], "laser_id": event["laser_id"], "at": event["at"],
                       "action": event["action"], "simulated": event.get("simulated", False),
                       "stop": (event.get("stop") or {}).get("status"), "message": event.get("message")}
                      for event in list(self._recent.values())[-10:]]
            return {"feature": "fire_watch", "agent_version": self.agent_version, "updated_at": round(self._clock(), 3),
                    "running": any(thread.is_alive() for thread in self._threads), "config_revision": self.revision,
                    "engine": self._engine_report(), "watches": watches,
                    "latched": {key: dict(value) for key, value in self.latches.items()},
                    "sync": dict(self.sync_state), "pending_events": len(self._outbox),
                    "pending_requests": sum(1 for entry in self._requests.values()
                                            if entry.get("status") != "done" or not entry.get("sent")),
                    "evaluating": bool(self._evaluating), "recent_events": recent,
                    "maintenance": self._maintenance, "latches_persisted": not self._latches_dirty,
                    "notice": "Protection supplémentaire : ne remplace jamais la présence d'un opérateur."}

    def _write_state(self):
        _write_json(self.data_dir / "state.json", self.local_status(), durable=False)
