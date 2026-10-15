"""Salle de contrôle native de worker.exe (agent 1.50.19) : ce que l'agent sait, SANS le site, des équipements de ce Worker.

Chargé à la demande par ``local_control`` (table ``LOCAL_ACTIONS``). Une seule action, ``control-room``, en lecture
seule et sans paramètre : aucune écriture sur disque, aucun appel au site, aucune commande vers une machine, aucun
relais démarré ou arrêté. Elle lit la mémoire de l'agent (``agent.equipment_relay``, ``agent.fire_watch``,
``agent.heartbeat_state``) et rend en moins d'une seconde :

* les caméras : type (USB, MJPEG, webcam par lien, P1S), état réel de la capture (âge de la dernière image, cadence
  et taille mesurées), relais MJPEG local (adresse ``http://127.0.0.1:<port>/api/stream.mjpeg?src=…`` seulement
  quand il tourne), machines reliées CONNUES DE L'AGENT (surveillance incendie, PrintGuard) ;
* les machines (imprimantes, graveuses, CNC) : connecteur, état de la liaison, dernière télémétrie lue par l'agent ;
* le Worker : version, nom local, dernier heartbeat et, d'après lui, en ligne ou non côté site (même fenêtre de 75 s
  que le site).

Ce qui n'existe que sur le site (nom donné à l'équipement, liaison caméra ↔ machine de Mes équipements) n'est pas
fabriqué : le champ vaut ``None`` et ``absent`` le dit. Jamais de secret : ni code d'accès, ni clé, ni adresse de flux
externe, ni hôte, ni jeton ; seuls des identifiants, des états, des nombres et les messages déjà rédigés pour
l'affichage par l'agent.
"""
import time

ACTION = "control-room"
SCHEMA = 1
ONLINE_WINDOW_SECONDS = 75   # même fenêtre que le site (distributed_workers.ONLINE_WINDOW_SECONDS)
LIVE_MAX_AGE = 5.0           # image de moins de 5 s : « en direct » (même seuil que static/control_room.js)
RELAY_MAX_AGE = 8.0          # le relais MJPEG refuse une image de 8 s ou plus (equipment_video_relay.py)
LOCK_WAIT = 0.5              # attente maximale d'un verrou : la réponse ne bloque jamais la boucle locale
DEVICES_MAX = 64
TEXT_MAX = 300
CAMERA_KIND = "camera"
MACHINE_KINDS = ("printer", "bambu", "laser", "cnc")
GRBL_KINDS = ("laser", "cnc")
# Télémétrie d'imprimante rendue telle quelle (valeurs simples seulement) ; « hms » et « ams » sont résumés.
PRINTER_KEYS = ("gcode_state", "mc_percent", "mc_remaining_time", "layer_num", "total_layer_num", "subtask_name",
                "nozzle_temper", "nozzle_target_temper", "bed_temper", "bed_target_temper", "chamber_temper",
                "print_error", "mc_print_error_code", "wifi_signal", "spd_lvl", "ota_version", "sdcard")
CONNECTOR_LABELS = {"usb_camera": "Caméra USB", "mjpeg_http": "Flux MJPEG", "p1s_tls": "Caméra P1S",
                    "bambu_mqtt_tls": "Bambu LAN (MQTT)", "bambu_cloud": "Bambu Cloud", "moonraker_http": "Moonraker",
                    "octoprint_http": "OctoPrint", "grbl_serial": "GRBL série", "grbl_tcp": "GRBL TCP"}
ABSENT = {
    "name": "Nom de l'équipement : donné sur le site, inconnu de l'agent.",
    "site_link": "Liaison caméra ↔ machine de Mes équipements : enregistrée sur le site, inconnue de l'agent. "
                 "Seules les liaisons utilisées sur ce Worker (surveillance incendie, PrintGuard) sont connues.",
}


def _text(value, limit=TEXT_MAX):
    if not isinstance(value, str):
        return ""
    cleaned = "".join(char if char >= " " else " " for char in value).strip()
    return cleaned[:limit]


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _scalar(value):
    if isinstance(value, bool) or _number(value) is not None:
        return value
    if isinstance(value, str):
        return _text(value, 120)
    return None


def _round(value, digits=2):
    number = _number(value)
    return round(float(number), digits) if number is not None else None


def _acquire(lock):
    """Verrou pris avec une attente bornée ; rend False s'il est tenu trop longtemps (la vue se dit alors incomplète)."""
    if lock is None:
        return False
    try:
        return lock.acquire(timeout=LOCK_WAIT)
    except TypeError:  # verrou sans délai (doublure) : prise directe
        return lock.acquire()


def _release(lock, taken):
    if taken:
        lock.release()


def _own(obj, name):
    """Attribut d'instance seulement : jamais de __getattr__ (doublures espionnes), jamais de propriété calculée."""
    try:
        return vars(obj).get(name)
    except TypeError:
        return None


def _devices(relay):
    """Copie des fiches du relais : sous son verrou si possible, sinon copie directe (lecture d'un dict)."""
    devices = _own(relay, "devices")
    if not isinstance(devices, dict):
        return [], False
    lock = _own(relay, "lock")
    taken = _acquire(lock)
    try:
        for _attempt in range(3):
            try:
                return list(devices.items())[:DEVICES_MAX], taken
            except RuntimeError:  # dictionnaire modifié pendant la copie (sans verrou) : nouvel essai
                continue
        return [], taken
    finally:
        _release(lock, taken)


def _relay_pairs(relay):
    pairs = _own(relay, "video_sources")
    try:
        return dict(pairs) if isinstance(pairs, dict) else {}
    except RuntimeError:
        return {}


def _component_installed(root):
    try:
        return bool(root) and (root / "components" / "printer-link" / "equipment_video_relay.py").is_file()
    except (OSError, TypeError):
        return None


def _camera_type(settings):
    connector = settings.get("connector")
    if connector == "usb_camera":
        return "usb"
    if connector == "mjpeg_http":
        return "webcam_link" if settings.get("source_mode") == "webcam" else "mjpeg"
    if connector == "p1s_tls" or (not connector and not settings.get("stream_url")):
        return "p1s"
    return "other"


def _requested(settings):
    """Format demandé sur la fiche (caméra USB et webcam par lien) : nombres seulement."""
    result = {}
    for key in ("width", "height", "fps"):
        value = settings.get(key)
        try:
            number = int(value) if value not in (None, "", 0) and not isinstance(value, bool) else 0
        except (TypeError, ValueError):
            number = 0
        result[key] = number if 0 < number <= 4096 else None
    return result


def _relay_options(settings):
    result = {}
    for key, name in (("relay_fps", "fps"), ("relay_width", "width"), ("relay_height", "height")):
        try:
            value = float(settings.get(key) or 0)
        except (TypeError, ValueError):
            value = 0.0
        result[name] = (round(value, 2) if name == "fps" else int(value)) if 0 < value <= 4096 else None
    return result


def relay_urls(hub, port, source):
    """SEULE fabrique des adresses du relais MJPEG local rendues à worker.exe : {"stream_url", "frame_url"}, ou None.

    Relais 1.3.0 (agent 1.50.20, docs/desktop/ARCHITECTURE.md « Relais caméra local : jeton ») : l'adresse porte le
    jeton de lecture de la source, lu en mémoire par ``VideoRelay.url`` (rien n'est créé ni écrit ici) ; None si le
    relais ne sert pas cette source. Composant plus ancien, sans jeton : adresse simple. worker.exe utilise ces
    adresses telles quelles, sans les recomposer."""
    reader = getattr(hub, "url", None) if hub is not None else None
    if callable(reader):
        stream, frame = reader(int(port), source), reader(int(port), source, frame=True)
        if not isinstance(stream, str) or not stream:
            return None
        return {"stream_url": stream, "frame_url": frame if isinstance(frame, str) and frame else None}
    base = f"http://127.0.0.1:{int(port)}/api/"
    return {"stream_url": base + "stream.mjpeg?src=" + source, "frame_url": base + "frame.jpeg?src=" + source}


def _relay_configured(spec, settings):
    if settings.get("camera_mode") == "relay":
        return True
    return settings.get("relay_enabled") is True


def _frame_state(spec, connected, age):
    if not spec.get("enabled"):
        return "disabled"
    if age is None:
        return "waiting" if connected is not False else "disconnected"
    if age <= LIVE_MAX_AGE:
        return "live"
    return "frozen"


def _camera(identifier, device, pairs, now, hub=None):
    spec = device.spec if isinstance(getattr(device, "spec", None), dict) else {}
    settings = spec.get("settings") if isinstance(spec.get("settings"), dict) else {}
    lock = getattr(device, "lock", None)
    taken = _acquire(lock)
    try:
        snapshot = dict(getattr(device, "snapshot", {}) or {})
        relay_state = dict(getattr(device, "relay_state", {}) or {})
        frame, frame_at = getattr(device, "frame", None), getattr(device, "frame_at", 0)
        capture_fps, capture_size = getattr(device, "capture_fps", None), getattr(device, "capture_size", None)
        link_fps = getattr(device, "link_fps", None)
    finally:
        _release(lock, taken)
    kind = _camera_type(settings)
    webcam = getattr(device, "webcam", None) if kind == "webcam_link" else None
    if webcam is not None:
        # Webcam par lien : la capture vit dans la fiche fille ; c'est elle qui dit l'âge et la taille réels.
        inner = getattr(webcam, "lock", None)
        held = _acquire(inner)
        try:
            source_frame, source_at = getattr(webcam, "frame", None), getattr(webcam, "frame_at", 0)
            capture_size = getattr(webcam, "capture_size", None) or capture_size
            capture_fps = getattr(webcam, "capture_fps", None) or capture_fps
        finally:
            _release(inner, held)
        if source_frame:
            frame, frame_at = source_frame, source_at
    age = round(max(0.0, now - float(frame_at)), 2) if frame and _number(frame_at) else None
    connected = snapshot.get("connected") is True
    pair = pairs.get(identifier)
    urls = None
    if pair and relay_state.get("relay_running") is True:
        try:
            urls = relay_urls(hub, int(pair[0]), str(pair[1]))
        except Exception:
            urls = None
    running = urls is not None
    relay = {"configured": _relay_configured(spec, settings), "running": running,
             "verified": running and relay_state.get("relay_verified") is True,
             "port": None, "source_id": None, "stream_url": None, "frame_url": None,
             "options": _relay_options(settings), "message": _text(relay_state.get("relay_message"))}
    if running:
        port, source = pair
        relay.update(port=int(port), source_id=str(source), **urls)
    size = list(capture_size)[:2] if isinstance(capture_size, (list, tuple)) and len(capture_size) >= 2 \
        and all(_number(value) for value in capture_size[:2]) else None
    thread = getattr(device, "thread", None)
    capture = {"state": _frame_state(spec, connected, age), "connected": connected, "frame_age_s": age,
               "frame_bytes": len(frame) if frame else None,
               "relay_servable": age is not None and age < RELAY_MAX_AGE,
               "fps_measured": _round(capture_fps), "link_fps": _round(link_fps), "size": size,
               "requested": _requested(settings) if kind in ("usb", "webcam_link") else None,
               "reader_running": bool(thread is not None and thread.is_alive()),
               "progress": _number(snapshot.get("progress")), "message": _text(snapshot.get("message")),
               "complete": taken}
    device_name = _text(settings.get("device_name"), 80) if kind in ("usb", "webcam_link") else ""
    return {"id": identifier, "kind": CAMERA_KIND, "type": kind, "connector": _text(settings.get("connector"), 40) or None,
            "connector_label": CONNECTOR_LABELS.get(settings.get("connector") or ("p1s_tls" if kind == "p1s" else ""), "Caméra"),
            "name": None, "device_name": device_name or None,
            "device_index": settings.get("device_index") if kind in ("usb", "webcam_link") and _number(settings.get("device_index")) is not None else None,
            "enabled": spec.get("enabled") is True, "needs_update": spec.get("needs_update") is True,
            "revision": _number(spec.get("revision")), "capture": capture, "relay": relay,
            "linked_machines": [], "printguard": False}


def _grbl(device):
    controller = getattr(device, "grbl", None)
    job = dict(getattr(device, "grbl_job", {}) or {})
    block = {"linked": False, "state": None, "substate": None, "mpos": None, "wpos": None, "feed": None, "spindle": None,
             "alarm": None, "laser_mode": None, "version": None, "last_status_age_s": None,
             "job": {"active": job.get("active") is True, "name": _text(job.get("name"), 100), "percent": _round(job.get("percent"), 1),
                     "sent_lines": _number(job.get("done")), "total_lines": _number(job.get("total")),
                     "passes": _number(job.get("passes")), "loop": job.get("loop") is True}}
    if controller is None:
        return block
    status = getattr(controller, "last_status", None) or {}
    received = status.get("received_at") if isinstance(status, dict) else None
    settings = getattr(controller, "settings", None)
    alarm = getattr(controller, "alarm", None)
    block.update(linked=bool(getattr(controller, "connected", False)),
                 state=_scalar(status.get("state")), substate=_scalar(status.get("substate")),
                 mpos=[_round(value, 3) for value in status.get("mpos")][:6] if isinstance(status.get("mpos"), (list, tuple)) else None,
                 wpos=[_round(value, 3) for value in status.get("wpos")][:6] if isinstance(status.get("wpos"), (list, tuple)) else None,
                 feed=_round(status.get("feed")), spindle=_round(status.get("spindle")),
                 alarm=_scalar(alarm) if alarm is not None else None,
                 laser_mode=(settings.get(32) == 1) if isinstance(settings, dict) else None,
                 version=_scalar(getattr(controller, "version", None)),
                 last_status_age_s=round(max(0.0, time.monotonic() - received), 2) if _number(received) is not None else None)
    streaming = getattr(controller, "_streaming", None)
    if streaming is not None and hasattr(streaming, "is_set") and streaming.is_set():
        block["job"]["active"] = True
    return block


def _printer(telemetry):
    values = {key: _scalar(telemetry.get(key)) for key in PRINTER_KEYS if key in telemetry}
    hms = telemetry.get("hms")
    values["hms_count"] = len(hms) if isinstance(hms, list) else None
    return values


def _machine(identifier, device):
    spec = device.spec if isinstance(getattr(device, "spec", None), dict) else {}
    settings = spec.get("settings") if isinstance(spec.get("settings"), dict) else {}
    kind = spec.get("kind")
    lock = getattr(device, "lock", None)
    taken = _acquire(lock)
    try:
        snapshot = dict(getattr(device, "snapshot", {}) or {})
        telemetry = dict(getattr(device, "telemetry", {}) or {})
        grbl = _grbl(device) if kind in GRBL_KINDS else None
    finally:
        _release(lock, taken)
    connector = settings.get("connector") or {"printer": "bambu_mqtt_tls", "bambu": "bambu_cloud"}.get(kind) \
        or ("grbl_serial" if kind in GRBL_KINDS else "")
    thread = getattr(device, "thread", None)
    return {"id": identifier, "kind": kind, "connector": _text(connector, 40) or None,
            "connector_label": CONNECTOR_LABELS.get(connector, "Machine"), "name": None,
            "enabled": spec.get("enabled") is True, "needs_update": spec.get("needs_update") is True,
            "revision": _number(spec.get("revision")),
            "link": {"connected": snapshot.get("connected") is True, "progress": _number(snapshot.get("progress")),
                     "message": _text(snapshot.get("message")), "reader_running": bool(thread is not None and thread.is_alive())},
            # La télémétrie d'imprimante n'est pas datée par l'agent : seule la liaison GRBL l'est (last_status_age_s).
            "telemetry": _printer(telemetry) if kind in ("printer", "bambu") else None,
            "grbl": grbl, "cameras": [], "fire_watch": None, "complete": taken}


def _fire_watch(agent):
    """Réglages de la surveillance incendie : machine -> (mode, caméra). Rien de plus, aucun verrou long."""
    watch = getattr(agent, "fire_watch", None)
    configs = getattr(watch, "configs", None) if watch is not None else None
    latches = getattr(watch, "latches", None) if watch is not None else None
    result = {}
    try:
        items = list(configs.items()) if isinstance(configs, dict) else []
        latched = set(latches) if isinstance(latches, dict) else set()
    except RuntimeError:
        return result
    for laser_id, config in items:
        if not isinstance(config, dict):
            continue
        camera = config.get("camera_id")
        result[str(laser_id)] = {"mode": _text(config.get("mode"), 20) or None,
                                 "camera_id": str(camera) if isinstance(camera, str) and camera else None,
                                 "latched": str(laser_id) in latched or "*" in latched}
    return result


def _printguard_camera(relay):
    reader = getattr(type(relay), "_printguard_camera_id", None)
    if reader is None:
        return ""
    try:
        value = reader(relay)  # lecture d'un petit fichier JSON local (data/printguard/alpine_camera.json)
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def _worker(agent, now):
    beat = getattr(agent, "heartbeat_state", None)
    beat = beat if isinstance(beat, dict) else {}
    ok, at = beat.get("ok"), _number(beat.get("at"))
    age = round(max(0.0, now - at), 1) if at is not None else None
    if ok is None:
        online = None  # aucun heartbeat n'a encore abouti ni échoué depuis le démarrage de l'agent
    else:
        online = ok is True and age is not None and age <= ONLINE_WINDOW_SECONDS
    config = getattr(agent, "config", None)
    config = config if isinstance(config, dict) else {}
    version = None
    try:
        import sys
        version = getattr(sys.modules.get(type(agent).__module__), "AGENT_VERSION", None)
    except Exception:
        version = None
    http = beat.get("http")
    return {"agent_version": version if isinstance(version, str) else None,
            "local_name": _text(config.get("name"), 120) or None,
            "worker_id_prefix": str(config.get("worker_id") or "")[:8] or None,
            "site": {"online": online, "basis": "heartbeat", "window_s": ONLINE_WINDOW_SECONDS,
                     "last_heartbeat_ok": ok if isinstance(ok, bool) else None, "last_heartbeat_at": at,
                     "last_heartbeat_age_s": age, "failures": _number(beat.get("failures")) or 0,
                     "http": http if isinstance(http, int) and not isinstance(http, bool) else None},
            "disconnecting": bool(getattr(agent, "disconnecting", False))}


def control_room(agent, params):
    """Vue de la Salle de contrôle, en lecture seule. ``params`` est vide (validé par local_control)."""
    if params:
        raise ValueError("Cette action locale n'accepte aucun paramètre.")
    now = time.time()
    relay = getattr(agent, "equipment_relay", None)
    entries, complete = _devices(relay) if relay is not None else ([], False)
    pairs = _relay_pairs(relay) if relay is not None else {}
    hub = _own(relay, "video") if relay is not None else None
    cameras, machines, others = [], [], 0
    for identifier, device in entries:
        spec = getattr(device, "spec", None)
        kind = spec.get("kind") if isinstance(spec, dict) else None
        try:
            if kind == CAMERA_KIND:
                cameras.append(_camera(str(identifier), device, pairs, now, hub))
            elif kind in MACHINE_KINDS:
                machines.append(_machine(str(identifier), device))
            else:
                others += 1
        except Exception:  # une fiche illisible n'empêche jamais la vue des autres
            others += 1
    fire = _fire_watch(agent)
    printguard = _printguard_camera(relay) if relay is not None else ""
    by_camera = {camera["id"]: camera for camera in cameras}
    for machine in machines:
        config = fire.get(machine["id"])
        if config is None:
            continue
        machine["fire_watch"] = {"mode": config["mode"], "latched": config["latched"]}
        camera = config["camera_id"]
        if camera and config["mode"] not in (None, "off"):
            machine["cameras"].append({"id": camera, "via": "fire_watch", "known": camera in by_camera})
            if camera in by_camera:
                by_camera[camera]["linked_machines"].append({"id": machine["id"], "kind": machine["kind"], "via": "fire_watch"})
    if printguard in by_camera:
        by_camera[printguard]["printguard"] = True
    fetched = _number(_own(relay, "config_fetched_at")) if relay is not None else None
    cameras.sort(key=lambda item: (not item["enabled"], item["type"], item["id"]))
    machines.sort(key=lambda item: (not item["enabled"], MACHINE_KINDS.index(item["kind"]) if item["kind"] in MACHINE_KINDS else 9, item["id"]))
    return {"schema": SCHEMA, "checked_at": now, "worker": _worker(agent, now),
            "equipment": {"available": relay is not None, "complete": complete,
                          "fetched_at": fetched, "fetched_age_s": round(max(0.0, now - fetched), 1) if fetched else None,
                          "relay_component_installed": _component_installed(getattr(agent, "root", None)) if relay is not None else None,
                          "relay_loaded": _own(relay, "video") is not None if relay is not None else False,
                          "other_count": others},
            "cameras": cameras, "machines": machines, "absent": dict(ABSENT)}


LOCAL_ACTIONS = {
    ACTION: {"handler": control_room, "params": {}, "read": True},
}
