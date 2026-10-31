"""Alpine Guard côté Worker : surveillance des impressions 3D dans l'agent (agent 1.52.0, capacité ``guard_v1``).

Le service tourne DANS le processus agent, à côté d'``EquipmentRelay`` qui tient la liaison MQTT de l'imprimante et
la caméra : l'image et la télémétrie sont lues en mémoire, une pause ou un arrêt part directement par la liaison de
l'imprimante, sans file de commandes et sans dépendre du site. Bibliothèque standard seulement : l'analyse d'image
tourne dans un processus séparé (``guard_engine.server``, Python isolé du composant « guard-engine », ou celui du
module « guard-obico » quand le modèle facultatif est demandé), piloté par tubes avec le client du moteur de la
surveillance laser (même protocole, redémarrage borné).

Fils (daemon) : ``guard`` (analyse, jamais de réseau), ``guard-sync`` (échanges avec le site), un fil court par
réponse (pause / reprise / arrêt) pour ne jamais bloquer l'analyse pendant la vérification.

Règles de sûreté :
- une réponse n'est JAMAIS tenue pour exécutée sur l'écho de la commande : seul ``gcode_state`` relu sur la
  télémétrie la confirme (PAUSE, RUNNING, FINISH/IDLE/FAILED) ; le HMS 0500-0500-0001-0007 signe un refus ;
- une pause ou un arrêt automatiques ne partent que si Guard est choisi pour l'imprimante (``exclusive``) ; dans ce
  cas la pause automatique PrintGuard de ce Worker s'abstient (``claims``) : jamais deux pauses automatiques ;
- une panne de Guard (moteur absent, exception) ne touche jamais aux commandes de l'imprimante : le service ne fait
  que lire, et n'écrit sur la liaison que par ``EquipmentRelay.guard_command`` ;
- l'absence de données n'est jamais « surveillé » : sans image fraîche ni analyse récente l'état est « dégradé ».
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

try:
    from . import guard_rules as rules
except ImportError:  # Worker installé : modules à plat
    import guard_rules as rules

try:
    from .fire_watch import EngineClient, EngineError
except ImportError:
    from fire_watch import EngineClient, EngineError

try:
    from .storage_paths import ascii_path
except ImportError:
    try:
        from storage_paths import ascii_path
    except ImportError:
        def ascii_path(path):
            return str(path)

AGENT_DIR = Path(__file__).resolve().parent
SYNC_PATH = "/api/worker-protocol/guard/sync"
ENGINE_MODULE = "guard_engine.server"
MODEL_RELATIVE = Path("models") / "obico" / "model.onnx"
ANALYSE_TIMEOUT_S = 8.0
ENGINE_READY_TIMEOUT_S = 120.0
IDLE_PERIOD_S = 5.0                # hors impression : santé de la caméra et aperçu seulement
WANT_FRAMES_TTL_S = 20.0
SYNC_ACTIVE_S = 1.0
SYNC_IDLE_S = 10.0
SYNC_BACKOFF_S = (2, 5, 10, 30)
MAX_SYNC_BODY = 5 * 1024 * 1024
MAX_EVENTS_PER_SYNC = 12
MAX_IMAGES_PER_SYNC = 4
MAX_OUTBOX = 300
MAX_REQUESTS = 300
MAX_MONITORS = 8
MAX_IMAGE_BYTES = 300 * 1024
VERIFY_S = 20.0                    # attente de l'état réel après une pause / reprise / arrêt
RESEND_AFTER_S = 8.0               # une seule relance si l'état n'a pas bougé et qu'aucun refus n'est signalé
POLL_S = 0.5
FROZEN_DIFF = 0.05                 # écart moyen entre deux images (niveaux) en dessous duquel l'image est identique
CAMERA_STALE_S = 15.0
WANTED_STATES = {"pause": ("PAUSE",), "resume": ("RUNNING",), "stop": ("FINISH", "IDLE", "FAILED")}
REQUIRED_STATES = {"pause": ("RUNNING",), "resume": ("PAUSE",), "stop": ("RUNNING", "PAUSE", "PREPARE")}
ACTION_LABELS = {"pause": "pause", "resume": "reprise", "stop": "arrêt", "notify": "notification", "ignore": "ignorer"}


def _log(message):
    print(f"[guard] {message}", flush=True)


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path, default):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return value


def _round(value, digits=4):
    return round(float(value), digits) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class GuardEngineClient(EngineClient):
    """Client du moteur d'Alpine Guard : protocole et cycle de vie du moteur de la surveillance laser."""

    def _check_model(self, ready):
        return None  # la vision classique n'a pas de modèle ; le modèle facultatif rapporte son propre état

    def request(self, kind, jpeg, params, timeout=ANALYSE_TIMEOUT_S):
        with self._cond:
            self._next_id += 1
            request_id = self._next_id
        header = json.dumps({"type": kind, "id": request_id, "jpeg_bytes": len(jpeg), **params},
                            separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        if len(header) > 4000:
            raise EngineError("requête d'analyse trop longue.")
        self._exchange(header + bytes(jpeg), lambda: request_id in self._results, timeout)
        with self._cond:
            response = self._results.pop(request_id)
        if response.get("ok") is not True:
            raise EngineError(f"analyse refusée par le moteur : {response.get('error') or 'erreur inconnue'}"[:300])
        return response

    def model_report(self):
        ready = self.ready if isinstance(self.ready, dict) else {}
        model = ready.get("model") if isinstance(ready.get("model"), dict) else {}
        return {"available": bool(model.get("available")), "error": model.get("error"),
                "versions": ready.get("versions") if isinstance(ready.get("versions"), dict) else {}}


class _Monitor:
    def __init__(self, monitor_id):
        self.id = monitor_id
        self.gate = rules.Gate()
        self.decision = rules.Decision()
        self.health = rules.CameraHealth()
        self.buckets = rules.Buckets()
        self.void = rules.VoidTracker()
        self.run_id = None
        self.run_alerts = 0
        self.run_peak = 0.0
        self.next_due = 0.0
        self.last_frame_at = None
        self.frame_same_since = None
        self.diff_low_since = None
        self.last_analysis = None        # horloge monotone
        self.last_result = None
        self.last_jpeg = None
        self.last_jpeg_at = None
        self.frame_sent_at = None
        self.acting = False
        self.status = None
        self.timings = []


class Guard:
    """Service Alpine Guard créé par l'agent : ``Guard(agent)``. ``tick()`` et ``sync_once()`` s'appellent aussi
    directement (tests) sans démarrer de fil ; ``engine_factory`` et les horloges sont injectables."""

    def __init__(self, agent, *, clock=None, monotonic=None, engine_factory=None, verify_s=VERIFY_S, poll_s=POLL_S):
        self.agent = agent
        self.root = Path(getattr(agent, "root", "."))
        self.data_dir = self.root / "data" / "guard"
        self.images_dir = self.data_dir / "images"
        self.outbox_dir = self.data_dir / "outbox"
        self.log_path = self.root / "logs" / "guard-engine.log"
        self.agent_version = str(getattr(agent, "agent_version", "") or "inconnue")
        self._clock = clock or time.time
        self._mono = monotonic or time.monotonic
        self.engine_factory = engine_factory or self._make_engine
        self.verify_s = float(verify_s)
        self.poll_s = float(poll_s)
        self.lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._halt = threading.Event()
        self._sync_wake = threading.Event()
        self._threads = []
        self._engine = None
        self._engine_key = None
        self.configs = {}
        self.config_errors = {}
        self.revision = 0
        self.monitors = {}
        self._outbox = OrderedDict()
        self._requests = OrderedDict()
        self._want_frames = {}
        self._pending_buckets = {}
        self._responders = {}
        self._errors = {}
        self.sync_state = {"ok": None, "last_ok_at": None, "error": None, "failures": 0}
        self._load()

    # ------------------------------------------------------------------------------------------- persistance
    def _load(self):
        stored = _read_json(self.data_dir / "config.json", {})
        if isinstance(stored, dict):
            revision = stored.get("revision")
            self.revision = revision if isinstance(revision, int) and not isinstance(revision, bool) else 0
            for monitor_id, raw in (stored.get("configs") or {}).items() if isinstance(stored.get("configs"), dict) else ():
                try:
                    monitor_id = rules.clean_identifier(monitor_id, "Moniteur")
                    self.configs[monitor_id] = rules.clean_config(raw)
                except ValueError as error:
                    _log(f"réglage local ignoré pour {monitor_id} ({error}).")
        if self.outbox_dir.is_dir():
            for path in sorted(self.outbox_dir.glob("*.json"))[-MAX_OUTBOX:]:
                event = _read_json(path, None)
                if isinstance(event, dict) and event.get("id") == path.stem and rules.EVENT_ID.fullmatch(path.stem):
                    self._outbox[event["id"]] = event
        done = _read_json(self.data_dir / "requests.json", {})
        for entry in (done.get("requests") or []) if isinstance(done, dict) else []:
            if isinstance(entry, dict) and rules.EVENT_ID.fullmatch(str(entry.get("id") or "")):
                if entry.get("status") != "done":
                    # Interrompue par un redémarrage : jamais relancée, résultat inconnu signalé au site.
                    entry.update(status="done", ok=False, sent=False,
                                 error="Worker redémarré pendant l'action : résultat inconnu, rien n'a été renvoyé.")
                self._requests[entry["id"]] = entry
        for monitor_id in self.configs:
            self.monitors[monitor_id] = _Monitor(monitor_id)

    def _save_configs(self):
        _write_json(self.data_dir / "config.json", {"revision": self.revision, "configs": self.configs})

    def _save_requests(self):
        entries = list(self._requests.values())[-MAX_REQUESTS:]
        _write_json(self.data_dir / "requests.json", {"requests": entries})

    def _persist_event(self, event):
        try:
            _write_json(self.outbox_dir / f"{event['id']}.json", event)
        except OSError as error:
            self._log_once("outbox", f"événement non enregistré sur disque ({error}) ; il reste en mémoire.")

    def _log_once(self, key, message):
        if self._errors.get(key) != message:
            self._errors[key] = message
            _log(message)

    # ------------------------------------------------------------------------------------------------ fils
    def start(self):
        with self.lock:
            if self._threads and all(thread.is_alive() for thread in self._threads) and not self._halt.is_set():
                return
            self._halt.clear()
            self._threads = [threading.Thread(target=self._analysis_loop, name="guard", daemon=True),
                             threading.Thread(target=self._sync_loop, name="guard-sync", daemon=True)]
            for thread in self._threads:
                thread.start()

    def stop(self, timeout=5.0):
        self._halt.set()
        self._sync_wake.set()
        deadline = time.monotonic() + max(0.1, float(timeout))
        for thread in self._threads:
            thread.join(max(0.05, deadline - time.monotonic()))
        with self.lock:
            engine, self._engine, self._engine_key = self._engine, None, None
            for monitor in self.monitors.values():
                monitor.status = None
        if engine is not None:
            engine.stop(timeout=max(0.2, min(3.0, deadline - time.monotonic())))

    def suspend_engines(self, reason=None):
        """Opération sur le composant : moteur arrêté (fichiers libérés), relancé au tour suivant après ``resume``."""
        with self.lock:
            engine, self._engine, self._engine_key = self._engine, None, None
            self._maintenance = str(reason or "opération sur le moteur")[:200]
        if engine is not None:
            engine.stop(timeout=3.0)
        return True

    def resume_engines(self):
        with self.lock:
            self._maintenance = None
        return True

    _maintenance = None

    def _stopping(self):
        stop = getattr(self.agent, "stop_requested", None)
        return self._halt.is_set() or bool(stop is not None and stop.is_set())

    def _analysis_loop(self):
        while not self._stopping():
            try:
                delay = self.tick()
            except Exception as error:  # jamais de sortie silencieuse : l'état deviendra périmé et le dira
                self._log_once("tick", f"erreur de la boucle d'analyse ({type(error).__name__}: {error}).")
                delay = 1.0
            self._halt.wait(delay)
        with self.lock:
            engine, self._engine, self._engine_key = self._engine, None, None
        if engine is not None:
            engine.stop()

    def _sync_loop(self):
        while not self._stopping():
            try:
                delay = self.sync_once()
            except Exception as error:
                self._log_once("sync", f"erreur de synchronisation ({type(error).__name__}: {error}).")
                delay = 5.0
            self._sync_wake.wait(delay)
            self._sync_wake.clear()

    # -------------------------------------------------------------------------------------------- relais
    def _relay(self):
        return getattr(self.agent, "equipment_relay", None)

    def _printer(self, printer_id):
        relay = self._relay()
        try:
            value = relay.guard_printer(printer_id)
        except Exception as error:  # relais absent, imprimante retirée : jamais une exception vers l'analyse
            return {"present": False, "error": type(error).__name__}
        return value if isinstance(value, dict) else {"present": False}

    def _camera(self, camera_id):
        relay = self._relay()
        try:
            value = relay.camera_frame(camera_id)
        except Exception as error:
            return {"present": False, "error": type(error).__name__}
        return value if isinstance(value, dict) else {"present": False}

    def claims(self, equipment_id):
        """Vrai si Guard est choisi pour cette imprimante avec une réponse automatique (la pause automatique
        PrintGuard de ce Worker s'abstient alors : jamais deux pauses automatiques sur la même imprimante)."""
        with self.lock:
            config = self.configs.get(str(equipment_id or ""))
            return bool(config and config.get("enabled") and config.get("exclusive")
                        and rules.effective_response(config) != "notify")

    # --------------------------------------------------------------------------------------------- moteur
    def _component(self, name):
        root = self.root / "components" / name
        python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        return root, python, (root / ".raphal-ready").is_file() and python.is_file()

    def _make_engine(self, use_model):
        """Moteur avec le Python du module guard-obico (numpy, OpenCV, ONNX Runtime) quand le modèle est demandé et
        installé, sinon celui de guard-engine. ``None`` : composant absent."""
        model_root, model_python, model_ready = self._component(rules.MODEL_COMPONENT_ID)
        root, python, ready = self._component(rules.COMPONENT_ID)
        model_path = model_root / MODEL_RELATIVE
        if use_model and model_ready and model_path.is_file():
            command_python, model = model_python, model_path
        elif ready:
            command_python, model = python, None
        else:
            return None, None

        def command(model_id=None, model_path=None, provider=None, threads=None):
            args = [str(command_python), "-u", "-m", ENGINE_MODULE]
            if model is not None:
                args += ["--model", ascii_path(model)]
            env = {key: value for key, value in os.environ.items()
                   if key not in {"PYTHONHOME", "VIRTUAL_ENV", "PYTHONPATH", "PYTHONSTARTUP", "__PYVENV_LAUNCHER__"}}
            env["PYTHONPATH"] = str(AGENT_DIR)
            flags = 0
            if os.name == "nt":
                flags = subprocess.CREATE_NO_WINDOW | subprocess.NORMAL_PRIORITY_CLASS
            return {"args": args, "cwd": str(AGENT_DIR), "env": env, "creationflags": flags}

        key = (str(command_python), str(model) if model else None)
        engine = GuardEngineClient("guard", model or root, "cpu", command, self.log_path,
                                   ready_timeout=ENGINE_READY_TIMEOUT_S)
        return engine, key

    def _ensure_engine(self, mono):
        with self.lock:
            if self._maintenance:
                return None
            use_model = any(config.get("enabled") and config.get("use_model") for config in self.configs.values())
            current, key = self._engine, self._engine_key
        if current is None or self._engine_use_model != use_model:
            # Composant absent : relu à chaque tour (simples tests de fichiers) jusqu'à son installation.
            engine, new_key = self.engine_factory(use_model)
            self._engine_use_model = use_model
            if current is None or new_key != key:
                with self.lock:
                    old, self._engine, self._engine_key = self._engine, engine, new_key
                if old is not None and old is not engine:
                    old.stop(wait=False)
                current = engine
        if current is not None:
            current.maintain(mono)
        return current

    _engine_use_model = None

    def _engine_report(self):
        engine = self._engine
        if engine is None:
            _, _, ready = self._component(rules.COMPONENT_ID)
            return {"state": "not_installed" if not ready else "stopped", "error": None, "model": {"available": False}}
        report = engine.report()
        return {"state": report.get("state"), "error": report.get("error"), "model": engine.model_report()
                if hasattr(engine, "model_report") else {"available": False}}

    # ------------------------------------------------------------------------------------------- analyse
    def tick(self):
        """Un tour sur chaque moniteur actif ; renvoie l'attente avant le suivant (s)."""
        now, mono = self._clock(), self._mono()
        with self.lock:
            configs = {key: dict(value) for key, value in self.configs.items() if value.get("enabled")}
            for key in list(self.monitors):
                if key not in self.configs:
                    self.monitors.pop(key, None)
            for key in configs:
                self.monitors.setdefault(key, _Monitor(key))
        if not configs:
            return 2.0
        engine = self._ensure_engine(mono)
        delays = []
        for monitor_id, config in configs.items():
            try:
                delays.append(self._step(monitor_id, config, engine, now, mono))
            except Exception as error:  # un moniteur en erreur n'arrête jamais les autres
                self._log_once(f"step:{monitor_id}", f"moniteur {monitor_id} en erreur ({type(error).__name__}: {error}).")
                delays.append(1.0)
        return max(0.05, min(delays) if delays else 1.0)

    def _wanted(self, monitor_id, mono):
        return self._want_frames.get(monitor_id, 0.0) >= mono

    def _step(self, monitor_id, config, engine, now, mono):
        monitor = self.monitors[monitor_id]
        printer = self._printer(monitor_id)
        phase = rules.printer_phase(printer)
        telemetry = printer.get("telemetry") if isinstance(printer.get("telemetry"), dict) else {}
        job = str(telemetry.get("subtask_name") or "")[:120] or None
        change = monitor.gate.update(phase, now, job)
        if change == "start":
            self._open_run(monitor, config, now, job)
        elif change == "end":
            self._close_run(monitor, config, now, phase)
        armed = monitor.gate.armed
        period = 1.0 / float(config["analysis_fps"]) if armed else IDLE_PERIOD_S
        if self._wanted(monitor_id, mono):
            period = min(period, 1.0)
        if mono < monitor.next_due:
            return max(0.05, monitor.next_due - mono)
        monitor.next_due = mono + period
        reasons = []
        camera = self._camera(config.get("camera_id"))
        frame, frame_at = camera.get("frame"), camera.get("frame_at")
        flags = {name: False for name in rules.CAMERA_SIGNALS}
        if not camera.get("present") or not camera.get("connected") or not frame:
            flags["camera_lost"] = True
            reasons.append({"code": "camera_lost", "message": "Caméra absente ou déconnectée sur ce Worker."})
        elif isinstance(frame_at, (int, float)) and frame_at and now - float(frame_at) > CAMERA_STALE_S:
            flags["camera_frozen"] = True
            reasons.append({"code": "camera_stale", "message": "Image de la caméra trop ancienne."})
        result = None
        if frame and not flags["camera_lost"]:
            if frame_at != monitor.last_frame_at:
                monitor.last_frame_at, monitor.frame_same_since = frame_at, None
            elif monitor.frame_same_since is None:
                monitor.frame_same_since = now
            if monitor.frame_same_since is not None and now - monitor.frame_same_since >= float(config["frozen_s"]):
                flags["camera_frozen"] = True
            monitor.last_jpeg, monitor.last_jpeg_at = bytes(frame), now
            if engine is None:
                reasons.append({"code": "engine_missing", "message": (
                    "Moteur Alpine Guard non installé sur ce Worker (Mes Workers → Installations → Alpine Guard)."
                    if not self._maintenance else f"Moteur suspendu : {self._maintenance}.")})
            elif not engine.usable():
                reasons.append({"code": "engine_starting" if engine.state == "starting" else "engine_error",
                                "message": "Démarrage du moteur d'analyse…" if engine.state == "starting"
                                else f"Moteur d'analyse en erreur : {engine.error or 'inconnue'}."})
            else:
                zone = rules.expanded_zone(config.get("zone"), config.get("zone_height"))
                params = {"monitor": monitor_id, "ts": round(now, 3), "zone": zone, "armed": armed,
                          "in_grace": monitor.gate.in_grace(now, config["grace_s"]), "run": monitor.run_id,
                          "use_model": bool(config.get("use_model"))}
                started = time.perf_counter()
                try:
                    result = engine.request("analyse", frame, params)
                    monitor.last_analysis = mono
                    monitor.timings = (monitor.timings + [round((time.perf_counter() - started) * 1000, 1)])[-30:]
                except EngineError as error:
                    reasons.append({"code": "engine_error", "message": str(error)[:300]})
                except Exception as error:  # noqa: BLE001
                    reasons.append({"code": "engine_error", "message": f"{type(error).__name__}: {error}"[:300]})
        if result is not None:
            camera_scores = result.get("camera") or {}
            flags["camera_dark"] = rules.clamp01(camera_scores.get("camera_dark")) >= 0.5
            flags["camera_masked"] = rules.clamp01(camera_scores.get("camera_masked")) >= 0.5
            diff = (result.get("health") or {}).get("diff_prev")
            if isinstance(diff, (int, float)) and diff <= FROZEN_DIFF:
                monitor.diff_low_since = monitor.diff_low_since or now
            else:
                monitor.diff_low_since = None
            if monitor.diff_low_since is not None and now - monitor.diff_low_since >= float(config["frozen_s"]):
                flags["camera_frozen"] = True
        if config.get("camera_checks", True):
            raised, cleared = monitor.health.update(now, flags)
            if monitor.gate.printing:
                for name in raised:
                    self._event(monitor, config, "warning", name, f"Surveillance aveugle : {rules.SIGNAL_LABELS[name]}.",
                                now, level="warning", notify=True)
                for name in cleared:
                    self._event(monitor, config, "recovered", f"{name}_ok",
                                f"Caméra rétablie ({rules.SIGNAL_LABELS[name]} terminé).", now, level="info")
        blind = any(flags.values())
        signals, risk, top = {}, 0.0, None
        if result is not None:
            signals = {key: rules.clamp01(value) for key, value in (result.get("signals") or {}).items()
                       if key in rules.PRINT_SIGNALS}
            in_grace = monitor.gate.in_grace(now, config["grace_s"])
            layer = rules._int(telemetry.get("layer_num"))
            signals["void"] = monitor.void.update(now, layer, (result.get("metrics") or {}).get("bg_change_60"), config) \
                if armed and not in_grace and not blind else 0.0
            if armed and not blind:
                risk, top = rules.fuse(signals, config)
                with self.lock:   # le fil de synchro prend les agrégats sous le même verrou
                    monitor.buckets.add(now, risk, float(config["threshold"]))
                monitor.run_peak = max(monitor.run_peak, risk)
            outputs = monitor.decision.step(now, risk, top, config, armed and not blind, in_grace)
            for kind, score, signal in outputs:
                if kind == "alert":
                    self._alert(monitor, config, score, signal, signals, result, now)
                elif kind == "near":
                    self._event(monitor, config, "warning", "near_alert",
                                f"Quasi-alerte : {rules.SIGNAL_LABELS.get(signal, signal)} ({round(score * 100)} %).",
                                now, level="info", score=score, signal=signal, signals=signals, image_from=(frame, result),
                                image_kind="near")
                elif kind == "recovered":
                    self._event(monitor, config, "recovered", "risk_low", "Risque redescendu sous le seuil.", now,
                                level="info", score=score)
        monitor.last_result = {"signals": signals, "risk": risk, "top": top, "metrics": (result or {}).get("metrics"),
                               "health": (result or {}).get("health"), "boxes": (result or {}).get("boxes") or []}
        monitor.status = self._status(monitor, config, printer, phase, reasons, flags, risk, top, signals, now, mono)
        return max(0.05, monitor.next_due - self._mono())

    def _status(self, monitor, config, printer, phase, reasons, flags, risk, top, signals, now, mono):
        telemetry = printer.get("telemetry") if isinstance(printer.get("telemetry"), dict) else {}
        gate = monitor.gate
        if reasons and any(reason["code"].startswith("engine") or reason["code"] == "camera_lost" for reason in reasons):
            state = "degraded"
        elif monitor.decision.alert is not None:
            state = "alert"
        elif gate.armed and gate.in_grace(now, config["grace_s"]):
            state = "grace"
        elif gate.armed:
            state = "degraded" if any(flags.values()) else "watching"
        else:
            state = "idle"
        for name, on in flags.items():
            if on and not any(reason["code"] == name for reason in reasons):
                reasons.append({"code": name, "message": rules.SIGNAL_LABELS[name].capitalize() + "."})
        response = rules.effective_response(config)
        if config.get("response") != "notify" and response == "notify":
            reasons.append({"code": "printguard_chosen", "message": (
                "PrintGuard reste choisi pour cette imprimante : Guard notifie seulement (choisis Guard pour la pause "
                "ou l'arrêt automatiques).")})
        timings = sorted(monitor.timings)
        return {"monitor_id": monitor.id, "printer_id": monitor.id, "camera_id": config.get("camera_id"),
                "enabled": True, "state": state, "phase": phase, "armed": gate.armed,
                "in_grace": bool(gate.armed and gate.in_grace(now, config["grace_s"])),
                "risk": _round(risk), "top_signal": top, "signals": {key: _round(value) for key, value in signals.items()},
                "camera_flags": [name for name, on in flags.items() if on], "reasons": reasons[:8],
                "response": response, "exclusive": bool(config.get("exclusive")), "acting": monitor.acting,
                "alert": dict(monitor.decision.alert) if monitor.decision.alert else None,
                "cooldown_s": max(0, round(monitor.decision.cooldown_until - now)) if monitor.decision.cooldown_until > now else 0,
                "run": {"id": monitor.run_id, "job": gate.job, "started_at": gate.started_at, "alerts": monitor.run_alerts,
                        "peak": _round(monitor.run_peak)} if monitor.run_id else None,
                "printer": {"connected": bool(printer.get("connected")), "gcode_state": telemetry.get("gcode_state"),
                            "layer": rules._int(telemetry.get("layer_num")), "layers": rules._int(telemetry.get("total_layer_num")),
                            "progress": rules._int(telemetry.get("mc_percent")),
                            "remaining_min": rules._int(telemetry.get("mc_remaining_time")),
                            "hms": rules.hms_codes(telemetry)[:6]},
                "analysis_ms": timings[len(timings) // 2] if timings else None,
                "analysed_at": round(now, 3) if monitor.last_analysis == mono else None,
                "updated_at": round(now, 3)}

    # ---------------------------------------------------------------------------------------- exécutions
    def _open_run(self, monitor, config, now, job):
        monitor.run_id = uuid.uuid4().hex
        monitor.run_alerts, monitor.run_peak = 0, 0.0
        monitor.decision.reset()
        monitor.void.reset()
        self._event(monitor, config, "run_started", "run_started",
                    f"Impression suivie{f' : {job}' if job else ''}.", now, level="info")

    def _close_run(self, monitor, config, now, phase):
        if monitor.run_id is None:
            return
        self._event(monitor, config, "run_finished", phase,
                    f"Fin de l'impression suivie ({rules.PHASE_LABELS.get(phase, phase)}) : {monitor.run_alerts} alerte(s), "
                    f"risque maximal {round(monitor.run_peak * 100)} %.", now, level="info",
                    data={"alerts": monitor.run_alerts, "peak": _round(monitor.run_peak), "phase": phase})
        monitor.run_id = None
        monitor.decision.reset()

    # ------------------------------------------------------------------------------------------ événements
    def _image(self, monitor_id, jpeg, result, label, config):
        """Image annotée (zone, boîtes, libellé) par le moteur ; repli sur l'image brute si elle est assez petite."""
        engine = self._engine
        zone = rules.expanded_zone(config.get("zone"), config.get("zone_height"))
        boxes = [box[:4] for box in (result or {}).get("boxes") or []][:20]
        if engine is not None and engine.usable():
            try:
                response = engine.request("annotate", jpeg, {"zone": zone, "boxes": boxes, "label": label})
                data = base64.b64decode(response.get("annotated_b64") or "", validate=True)
                if data.startswith(b"\xff\xd8") and len(data) <= MAX_IMAGE_BYTES:
                    return data
            except Exception as error:  # noqa: BLE001
                self._log_once("annotate", f"image d'alerte non annotée ({type(error).__name__}).")
        return bytes(jpeg) if jpeg and len(jpeg) <= MAX_IMAGE_BYTES else None

    def _event(self, monitor, config, kind, code, message, now, *, level="info", score=None, signal=None, signals=None,
               image_from=None, image_kind=None, notify=False, action=None, data=None):
        event = {"id": uuid.uuid4().hex, "rev": 1, "monitor_id": monitor.id, "kind": kind, "code": str(code)[:40],
                 "level": level, "message": str(message)[:500], "at": round(now, 3), "score": _round(score),
                 "signal": signal, "signals": {key: _round(value) for key, value in (signals or {}).items()},
                 "run_id": monitor.run_id, "job": monitor.gate.job, "action": action, "image": False,
                 "image_kind": None, "notify": bool(notify and config.get("notify", True)), "data": data or {}}
        if image_from is not None:
            jpeg, result = image_from
            label = f"{rules.SIGNAL_LABELS.get(signal, signal or '')} {round((score or 0) * 100)}%"
            image = self._image(monitor.id, jpeg, result, label, config) if jpeg else None
            if image:
                try:
                    self.images_dir.mkdir(parents=True, exist_ok=True)
                    (self.images_dir / f"{event['id']}.jpg").write_bytes(image)
                    event["image"], event["image_kind"] = True, image_kind
                except OSError as error:
                    self._log_once("image", f"image d'alerte non enregistrée ({error}).")
        with self.lock:
            self._outbox[event["id"]] = event
            while len(self._outbox) > MAX_OUTBOX:
                # Arriéré plein (site injoignable longtemps) : les informations partent d'abord, jamais une alerte.
                victim = next((key for key, item in self._outbox.items() if item["kind"] not in ("alert", "action")),
                              next(iter(self._outbox)))
                dropped = self._outbox.pop(victim)
                self._drop_files(dropped)
        self._persist_event(event)
        self._sync_wake.set()
        return event

    def _update_event(self, event, **changes):
        with self.lock:
            event.update(changes)
            event["rev"] = int(event.get("rev") or 1) + 1
            self._outbox[event["id"]] = event
        self._persist_event(event)
        self._sync_wake.set()

    def _drop_files(self, event):
        for path in (self.outbox_dir / f"{event['id']}.json", self.images_dir / f"{event['id']}.jpg"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _alert(self, monitor, config, score, signal, signals, result, now):
        monitor.run_alerts += 1
        response = rules.effective_response(config)
        label = rules.SIGNAL_LABELS.get(signal, signal or "défaut")
        message = f"Défaut probable : {label} ({round(score * 100)} %)."
        action = {"type": response, "status": "requested" if response != "notify" else "skipped",
                  "message": "Notification seule." if response == "notify" else f"{ACTION_LABELS[response].capitalize()} automatique demandée.",
                  "at": round(now, 3), "gcode_state": None, "auto": True}
        if config.get("response") != "notify" and response == "notify":
            action["message"] = ("Notification seule : PrintGuard reste choisi pour cette imprimante (jamais deux pauses "
                                 "automatiques).")
        event = self._event(monitor, config, "alert", signal or "defect", message, now, level="critical", score=score,
                            signal=signal, signals=signals, image_from=(monitor.last_jpeg, result), image_kind="alert",
                            notify=True, action=action)
        if response in ("pause", "stop"):
            self._spawn_response(monitor.id, response, event)

    # --------------------------------------------------------------------------------------------- réponses
    def _spawn_response(self, monitor_id, action, event, request_id=None, by=None):
        with self.lock:
            monitor = self.monitors.get(monitor_id)
            if monitor is not None and monitor.acting:
                busy = "Une action est déjà en cours sur cette imprimante : rien n'a été renvoyé."
                if event is not None:
                    self._update_event(event, action=dict(event.get("action") or {}, status="skipped", message=busy))
                if request_id:
                    self._finish_request(request_id, False, None, busy)
                return None
            if monitor is not None:
                monitor.acting = True
        thread = threading.Thread(target=self._respond, args=(monitor_id, action, event, request_id, by),
                                  name=f"guard-{action}-{monitor_id[:8]}", daemon=True)
        self._responders[monitor_id] = thread
        thread.start()
        return thread

    def respond_now(self, monitor_id, action, event=None, request_id=None, by=None):
        """Exécution synchrone d'une réponse (tests)."""
        with self.lock:
            monitor = self.monitors.get(monitor_id)
            if monitor is not None:
                monitor.acting = True
        return self._respond(monitor_id, action, event, request_id, by)

    def _respond(self, monitor_id, action, event, request_id=None, by=None):
        """Envoie pause / reprise / arrêt par la liaison de l'imprimante puis ATTEND l'état réel (``gcode_state``).

        Statuts : confirmed (état atteint), refused (HMS de refus de commande), unconfirmed (état inchangé au bout de
        ``verify_s``), skipped (rien envoyé : état incompatible), failed (envoi impossible)."""
        relay = self._relay()
        wanted, required = WANTED_STATES[action], REQUIRED_STATES[action]
        outcome = {"type": action, "status": "failed", "message": "", "at": round(self._clock(), 3), "gcode_state": None,
                   "auto": request_id is None, "by": by}
        try:
            before = self._printer(monitor_id)
            telemetry = before.get("telemetry") if isinstance(before.get("telemetry"), dict) else {}
            state = str(telemetry.get("gcode_state") or "").upper()
            hms_before = set(rules.hms_codes(telemetry))
            outcome["gcode_state"] = state or None
            if state in wanted:
                outcome.update(status="confirmed", message=f"Déjà dans l'état voulu ({state}) : rien n'a été envoyé.")
            elif not before.get("connected"):
                outcome.update(status="failed", message="Imprimante déconnectée du Worker : rien n'a été envoyé.")
            elif state not in required:
                outcome.update(status="skipped", message=f"État de l'imprimante {state or 'inconnu'} : {ACTION_LABELS[action]} non envoyée.")
            else:
                sent_at = self._mono()
                resent = False
                try:
                    relay.guard_command(monitor_id, action)
                except Exception as error:  # noqa: BLE001 - message clair de l'équipement, sans secret
                    outcome.update(status="failed", message=f"Commande non envoyée : {error}"[:400])
                    raise _Done() from None
                outcome.update(status="unconfirmed")
                while self._mono() - sent_at < self.verify_s and not self._halt.is_set():
                    self._halt.wait(self.poll_s) if self.poll_s > 0 else None
                    after = self._printer(monitor_id)
                    telemetry = after.get("telemetry") if isinstance(after.get("telemetry"), dict) else {}
                    state = str(telemetry.get("gcode_state") or "").upper()
                    outcome["gcode_state"] = state or None
                    if state in wanted:
                        outcome.update(status="confirmed", message=f"{ACTION_LABELS[action].capitalize()} confirmée par "
                                                                   f"l'imprimante (état {state}).")
                        break
                    # Refus : HMS apparu après l'envoi, ou déjà présent et toujours là 3 s plus tard sans changement d'état.
                    if rules.HMS_COMMAND_REFUSED in set(rules.hms_codes(telemetry)) - hms_before or (
                            rules.command_refused(telemetry) and state in required and self._mono() - sent_at >= 3.0):
                        outcome.update(status="refused", message=rules.PAUSE_REFUSED_HELP if action == "pause" else
                                       rules.PAUSE_REFUSED_HELP.replace("en pause", "à l’arrêt"))
                        break
                    if not resent and self._mono() - sent_at >= RESEND_AFTER_S and state in required:
                        resent = True
                        try:
                            relay.guard_command(monitor_id, action)
                        except Exception:  # noqa: BLE001
                            pass
                if outcome["status"] == "unconfirmed":
                    outcome["message"] = (f"{ACTION_LABELS[action].capitalize()} NON confirmée : l'imprimante est toujours "
                                          f"{state or 'dans un état inconnu'} après {int(self.verify_s)} s. Vérifie-la tout de suite.")
        except _Done:
            pass
        except Exception as error:  # noqa: BLE001 - jamais d'exception vers l'agent
            outcome.update(status="failed", message=f"Erreur pendant la {ACTION_LABELS[action]} ({type(error).__name__}).")
        finally:
            with self.lock:
                monitor = self.monitors.get(monitor_id)
                if monitor is not None:
                    monitor.acting = False
        outcome["finished_at"] = round(self._clock(), 3)
        if event is not None:
            self._update_event(event, action=dict(event.get("action") or {}, **outcome))
        if event is None or outcome["status"] != "confirmed":
            # Action manuelle (toujours journalisée) ou réponse automatique non obtenue : événement à part, notifié
            # quand l'imprimante n'a pas fait ce qui était demandé (elle imprime peut-être encore).
            failed = outcome["status"] in ("refused", "unconfirmed", "failed")
            level = "info" if outcome["status"] == "confirmed" else "critical" if failed else "warning"
            monitor = self.monitors.get(monitor_id)
            config = self.configs.get(monitor_id) or rules.clean_config(None)
            if monitor is not None:
                self._event(monitor, config, "action", f"{action}_{outcome['status']}", outcome["message"], self._clock(),
                            level=level, action=outcome, notify=failed)
        if request_id:
            self._finish_request(request_id, outcome["status"] == "confirmed", outcome, None if outcome["status"] == "confirmed"
                                 else outcome["message"])
        return outcome

    # ----------------------------------------------------------------------------------------- requêtes site
    def _handle_requests(self, requests):
        for item in requests[:20]:
            if not isinstance(item, dict):
                continue
            request_id = str(item.get("id") or "")
            if not rules.EVENT_ID.fullmatch(request_id):
                continue
            with self.lock:
                known = self._requests.get(request_id)
                if known is not None:
                    if known.get("status") == "done" and known.get("sent"):
                        known["sent"] = False   # rejouée : jamais réexécutée, résultat renvoyé
                    continue
                entry = {"id": request_id, "type": str(item.get("type") or "")[:20], "status": "running",
                         "at": self._clock(), "sent": False, "ok": None, "result": None, "error": None}
                self._requests[request_id] = entry
                try:
                    self._save_requests()   # AVANT l'exécution : un redémarrage ne la relance jamais
                except OSError as error:
                    self._log_once("requests", f"requête non enregistrée ({error}).")
            try:
                self._execute(request_id, item)
            except ValueError as error:
                self._finish_request(request_id, False, None, str(error))
            except Exception as error:  # noqa: BLE001
                self._finish_request(request_id, False, None, f"Erreur interne du Worker ({type(error).__name__}).")

    def _execute(self, request_id, item):
        if item.get("type") != "action":
            raise ValueError("Requête inconnue.")
        monitor_id = rules.clean_identifier(item.get("monitor_id"), "Moniteur")
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        action = params.get("action")
        by = str(item.get("by") or "")[:80] or None
        if action not in rules.MANUAL_ACTIONS:
            raise ValueError("Action inconnue.")
        with self.lock:
            config = self.configs.get(monitor_id)
            monitor = self.monitors.get(monitor_id)
        if config is None or monitor is None:
            raise ValueError("Imprimante non surveillée par Alpine Guard sur ce Worker.")
        if action == "ignore":
            monitor.decision.ignore(self._clock(), config["cooldown_s"])
            self._event(monitor, config, "action", "ignored", f"Alerte ignorée{f' par {by}' if by else ''} : "
                        f"pas de nouvelle alerte pendant {config['cooldown_s']} s.", self._clock(), level="info",
                        action={"type": "ignore", "status": "confirmed", "by": by})
            self._finish_request(request_id, True, {"type": "ignore", "status": "confirmed",
                                                    "message": "Alerte ignorée."}, None)
            return
        self._spawn_response(monitor_id, action, None, request_id=request_id, by=by)

    def _finish_request(self, request_id, ok, result, error):
        with self.lock:
            entry = self._requests.get(request_id)
            if entry is None:
                return
            entry.update(status="done", ok=bool(ok), result=result, error=str(error)[:600] if error else None,
                         sent=False, finished_at=self._clock())
            try:
                self._save_requests()
            except OSError as failure:
                self._log_once("requests", f"résultat non enregistré ({failure}).")
        self._sync_wake.set()

    # ------------------------------------------------------------------------------------------- synchro
    def sync_once(self):
        with self._sync_lock:
            return self._sync_exchange()

    def _sync_body(self):
        mono = self._mono()
        with self.lock:
            monitors = []
            for monitor_id in sorted(self.configs):
                monitor = self.monitors.get(monitor_id)
                config = self.configs[monitor_id]
                status = dict(monitor.status) if monitor is not None and monitor.status else {
                    "monitor_id": monitor_id, "printer_id": monitor_id, "camera_id": config.get("camera_id"),
                    "enabled": bool(config.get("enabled")), "state": "off" if not config.get("enabled") else "starting",
                    "reasons": [], "response": rules.effective_response(config), "exclusive": bool(config.get("exclusive"))}
                if self.config_errors.get(monitor_id):
                    status["config_error"] = self.config_errors[monitor_id]
                monitors.append(status)
                if monitor is not None:
                    taken = monitor.buckets.take()
                    pending = self._pending_buckets.setdefault(monitor_id, {})
                    for bucket in taken:
                        pending[bucket["minute"]] = bucket
            events = list(self._outbox.values())[:MAX_EVENTS_PER_SYNC]
            done = [{"id": entry["id"], "ok": bool(entry.get("ok")), "result": entry.get("result"), "error": entry.get("error")}
                    for entry in self._requests.values() if entry.get("status") == "done" and not entry.get("sent")][:32]
            buckets = {key: sorted(value.values(), key=lambda item: item["minute"])[-120:]
                       for key, value in self._pending_buckets.items() if value}
            wanted = [monitor_id for monitor_id, until in self._want_frames.items() if until >= mono]
        body = {"agent_version": self.agent_version, "config_revision": self.revision, "engine": self._engine_report(),
                "monitors": monitors, "events": [], "done": done, "buckets": buckets, "sent_at": round(self._clock(), 3)}
        size = len(json.dumps(body, ensure_ascii=False))
        sent = {"events": {}, "done": [item["id"] for item in done], "frames": {}, "buckets": {
            key: [item["minute"] for item in value if not item.get("open")] for key, value in buckets.items()}}
        images = 0
        for event in events:
            item = json.loads(json.dumps(event, ensure_ascii=False))
            included_image = False
            if event.get("image") and not event.get("image_acked") and images < MAX_IMAGES_PER_SYNC:
                try:
                    data = (self.images_dir / f"{event['id']}.jpg").read_bytes()
                    encoded = base64.b64encode(data).decode("ascii")
                    if size + len(encoded) < MAX_SYNC_BODY - 512 * 1024:
                        item["image_b64"] = encoded
                        size += len(encoded)
                        included_image = True
                        images += 1
                except OSError:
                    item["image"] = False
            item.pop("image_acked", None)
            size += len(json.dumps(item, ensure_ascii=False)) - len(item.get("image_b64", ""))
            body["events"].append(item)
            sent["events"][event["id"]] = {"rev": event["rev"], "image": included_image}
        for status in body["monitors"]:
            monitor = self.monitors.get(status["monitor_id"])
            if status["monitor_id"] not in wanted or monitor is None or monitor.last_jpeg is None \
                    or monitor.last_jpeg_at == monitor.frame_sent_at:
                continue
            preview = self._preview(monitor)
            if preview and size + len(preview) * 4 // 3 < MAX_SYNC_BODY - 256 * 1024:
                status["frame"] = base64.b64encode(preview).decode("ascii")
                status["frame_at"] = round(monitor.last_jpeg_at, 3)
                size += len(status["frame"])
                sent["frames"][monitor.id] = monitor.last_jpeg_at
        return body, sent

    def _preview(self, monitor):
        config = self.configs.get(monitor.id) or {}
        result = monitor.last_result or {}
        label = ""
        if result.get("top") and result.get("risk"):
            label = f"{rules.SIGNAL_LABELS.get(result['top'], result['top'])} {round(result['risk'] * 100)}%"
        return self._image(monitor.id, monitor.last_jpeg, result, label, config)

    def _sync_exchange(self):
        body, sent = self._sync_body()
        try:
            response = self.agent.request(SYNC_PATH, "POST", body)
            if not isinstance(response, dict) or response.get("ok") is not True:
                raise RuntimeError((response or {}).get("error") if isinstance(response, dict) else "réponse invalide")
        except Exception as error:
            failures = self.sync_state["failures"] + 1
            code = getattr(error, "code", None)
            message = ("site sans Alpine Guard (version trop ancienne)" if code == 404 else f"{type(error).__name__}: {error}")[:300]
            if self.sync_state["error"] != message:
                _log(f"synchronisation avec le site impossible ({message}) ; l'analyse locale continue.")
            self.sync_state.update(ok=False, error=message, failures=failures)
            return SYNC_BACKOFF_S[-1] if code == 404 else SYNC_BACKOFF_S[min(failures - 1, len(SYNC_BACKOFF_S) - 1)]
        self.sync_state.update(ok=True, error=None, failures=0, last_ok_at=self._clock())
        self._apply_response(response, sent)
        with self.lock:
            busy = any(config.get("enabled") for config in self.configs.values()) or bool(self._outbox)
            backlog = len(self._outbox) > MAX_EVENTS_PER_SYNC
        return 0.2 if backlog else SYNC_ACTIVE_S if busy else SYNC_IDLE_S

    def _apply_response(self, response, sent):
        self.apply_configs(response.get("revision"), response.get("configs"))
        with self.lock:
            for item in response.get("acked_events") or []:
                if not isinstance(item, dict):
                    continue
                event = self._outbox.get(item.get("id"))
                included = sent["events"].get(item.get("id"))
                rev = item.get("rev")
                if event is None or included is None or not isinstance(rev, int) or isinstance(rev, bool):
                    continue
                if included["image"] and item.get("image") is True:
                    event["image_acked"] = True
                image_pending = event.get("image") and not event.get("image_acked")
                if rev >= int(event.get("rev") or 1) and not image_pending:
                    self._outbox.pop(event["id"], None)
                    self._drop_files(event)
                else:
                    self._persist_event(event)
            changed = False
            for request_id in sent["done"]:
                entry = self._requests.get(request_id)
                if entry is not None and entry.get("status") == "done" and not entry.get("sent"):
                    entry["sent"] = changed = True
            if changed:
                try:
                    self._save_requests()
                except OSError:
                    pass
            for monitor_id, minutes in sent["buckets"].items():
                pending = self._pending_buckets.get(monitor_id) or {}
                for minute in minutes:
                    pending.pop(minute, None)
                for minute in [key for key, value in pending.items() if value.get("open")]:
                    pending.pop(minute, None)   # minute en cours : renvoyée à jour au prochain échange
            for monitor_id, frame_at in sent["frames"].items():
                monitor = self.monitors.get(monitor_id)
                if monitor is not None:
                    monitor.frame_sent_at = frame_at
            wanted = response.get("want_frames")
            until = self._mono() + WANT_FRAMES_TTL_S
            self._want_frames = {key: until for key in (wanted if isinstance(wanted, list) else [])
                                 if isinstance(key, str) and rules.IDENTIFIER.fullmatch(key)}
        requests = response.get("requests")
        if isinstance(requests, list):
            self._handle_requests(requests)

    def apply_configs(self, revision, configs):
        if not isinstance(revision, int) or isinstance(revision, bool) or not isinstance(configs, list):
            return
        if revision == self.revision:
            return
        cleaned, errors = {}, {}
        for raw in configs[:MAX_MONITORS]:
            if not isinstance(raw, dict):
                continue
            try:
                monitor_id = rules.clean_identifier(raw.get("monitor_id"), "Moniteur")
            except ValueError:
                continue
            try:
                cleaned[monitor_id] = rules.clean_config({key: value for key, value in raw.items() if key != "monitor_id"})
            except ValueError as error:
                errors[monitor_id] = str(error)
                cleaned[monitor_id] = rules.clean_config(None)   # refusé : désactivé ET signalé, jamais « surveillé »
        with self.lock:
            for monitor_id, config in cleaned.items():
                old = self.configs.get(monitor_id)
                monitor = self.monitors.get(monitor_id)
                if monitor is not None and old is not None and (old.get("camera_id") != config.get("camera_id")
                                                                or old.get("zone") != config.get("zone")):
                    monitor.health.reset()
                    monitor.decision.reset()
            self.configs, self.config_errors, self.revision = cleaned, errors, revision
            try:
                self._save_configs()
            except OSError as error:
                self._log_once("config", f"réglages non enregistrés sur disque ({error}).")
        _log(f"réglages appliqués (révision {revision}, {sum(1 for c in cleaned.values() if c.get('enabled'))} imprimante(s) surveillée(s)).")

    # -------------------------------------------------------------------------------------- état local
    def local_status(self):
        with self.lock:
            return {"revision": self.revision, "sync": dict(self.sync_state), "engine": self._engine_report(),
                    "monitors": [dict(monitor.status) for monitor in self.monitors.values() if monitor.status],
                    "outbox": len(self._outbox)}


class _Done(Exception):
    """Sortie anticipée de ``_respond`` après un échec d'envoi déjà décrit."""
