"""Gestion complète du Worker depuis worker.exe (agent 1.50.0).

Chargé à la demande par ``local_control`` (table ``LOCAL_ACTIONS``). Une seule action locale, ``manage``, relaie une
opération de Mes Workers vers la route signée ``/api/worker-protocol/self/manage`` du site. Rien n'est décidé ici :
le site exige que le propriétaire ait activé « Gestion complète depuis worker.exe » pour ce Worker, vérifie
l'opération, ses champs, les outils permis au compte, et applique les mêmes contrôles que la page Mes Workers.

Comme les relais de ``self_service`` : l'action rend aussitôt un identifiant de suivi, l'appel au site part dans un
fil démon, et ``relay-status`` en rend le résultat. Les arguments d'une opération dépassent parfois les 2048 octets
des paramètres locaux (sélection d'installations) : ils passent alors par un fichier
``runtime/local-control/<32 hex>.args.json`` écrit par l'appelant, lu une fois puis effacé.
"""
import json
import secrets
import threading
import time

if __package__:
    from . import self_service
    from .local_control import checked, v_choice, v_hex
else:
    import self_service
    from local_control import checked, v_choice, v_hex

MANAGE_PATH = "/api/worker-protocol/self/manage"
# Mêmes opérations que worker_full_control.OPS côté site (une opération inconnue y est de toute façon refusée).
OPS = ("worker", "installations", "installations-plan", "installations-batch", "installations-cancel",
       "commands-status", "command", "settings", "primary", "associations", "tools-control", "repair",
       "cancel-jobs", "cancel-job", "console", "job-requests", "job-request-decide", "sharing-user",
       "sharing-user-remove", "team-sharing", "revoke", "forget", "delete")
READ_OPS = frozenset({"worker", "installations", "installations-plan", "commands-status", "console", "job-requests"})
ARGS_MAX_BYTES = 65536
ANSWER_MAX_BYTES = 8 * 1024 * 1024  # inventaire des installations compris
ARGS_DEPTH_MAX = 5


def _check_args(value, depth=0):
    """Arguments : objet JSON simple (chaînes, nombres, booléens, null, listes, objets), profondeur bornée."""
    if depth > ARGS_DEPTH_MAX:
        raise ValueError("Arguments de gestion trop imbriqués.")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 80:
                raise ValueError("Arguments de gestion invalides.")
            _check_args(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_args(item, depth + 1)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError("Arguments de gestion invalides.")
    return value


def v_args(value):
    """Validateur local : un objet d'arguments (petit : les gros passent par ``args_file``)."""
    if type(value) is not dict:
        raise ValueError("Paramètre local invalide : un objet d'arguments est attendu.")
    return _check_args(value)


def _args_from_file(agent, token):
    """Lit puis efface ``runtime/local-control/<token>.args.json`` (écrit par worker.exe juste avant la demande)."""
    path = checked(agent.root, f"runtime/local-control/{token}.args.json")
    try:
        if not path.is_file():
            raise ValueError("Fichier d'arguments introuvable : relance l'action.")
        if path.stat().st_size > ARGS_MAX_BYTES:
            raise ValueError("Fichier d'arguments trop volumineux.")
        raw = path.read_text(encoding="utf-8-sig")
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise ValueError("Fichier d'arguments illisible.") from error
    return v_args(value)


def _call(agent, body):
    """Un appel à la route de gestion, rendu sous la forme des champs de « relay-status ». Ne lève jamais."""
    try:
        answer = agent.request(MANAGE_PATH, "POST", body)
        if not isinstance(answer, dict):
            raise ValueError("reponse du site illisible")
        if len(json.dumps(answer, ensure_ascii=False).encode("utf-8")) > ANSWER_MAX_BYTES:
            raise ValueError("reponse du site trop volumineuse")
        return {"state": "done", "response": answer}
    except Exception as error:
        verdict = self_service.classify(error)
        return {"state": "failed", "http": verdict["http"], "site_code": verdict["site_code"],
                "site_missing": verdict["site_missing"], "unreachable": verdict["unreachable"],
                "error": verdict["message"]}


def _worker(agent, runtime, operation, body, read):
    outcome = {"state": "failed", "error": "Demande au site interrompue."}
    try:
        outcome = _call(agent, body)
    except Exception as error:  # filet : aucune exception ne sort d'un fil
        outcome = {"state": "failed", "error": f"Demande au site interrompue ({type(error).__name__})."}
    finally:
        try:
            with runtime.guard:
                if operation in agent.self_ops:
                    agent.self_ops[operation] = {**agent.self_ops[operation], **outcome}
        finally:
            agent.self_relay_lock.release()
    if not read and not agent.stop_requested.is_set():
        agent.reconnect_requested.set()  # une commande mise en file arrive au prochain heartbeat, sans attendre
        self_service._refresh(agent, fresh=True)


def manage(agent, params):
    """Relaie une opération de Mes Workers. Rend aussitôt {"operation", "state": "running"} (suivi : relay-status)."""
    params = _clean(params)
    op = params["op"]
    if "args" in params and "args_file" in params:
        raise ValueError("Arguments en double : « args » ou « args_file », pas les deux.")
    args = _args_from_file(agent, params["args_file"]) if "args_file" in params else params.get("args", {})
    body = {"op": op, "args": args}
    if not agent.self_relay_lock.acquire(blocking=False):
        raise ValueError("Une demande au site est déjà en cours ; attends sa fin.")
    runtime = self_service._runtime(agent)
    operation = secrets.token_hex(16)
    read = op in READ_OPS
    try:
        with runtime.guard:
            now = time.monotonic()
            self_service._expire(agent.self_ops, now)
            finished = sorted((key for key, item in agent.self_ops.items() if item["state"] != "running"),
                              key=lambda key: agent.self_ops[key]["at"])
            while len(agent.self_ops) >= self_service.OPERATIONS_MAX and finished:
                del agent.self_ops[finished.pop(0)]
            agent.self_ops[operation] = {"state": "running", "action": "manage:" + op, "http": None, "site_code": "",
                                         "site_missing": False, "unreachable": False, "error": "", "response": None,
                                         "local": None, "at": now}
        threading.Thread(target=_worker, args=(agent, runtime, operation, body, read),
                         name="local-manage-" + op, daemon=True).start()
    except BaseException:
        with runtime.guard:
            agent.self_ops.pop(operation, None)
        agent.self_relay_lock.release()
        raise
    return {"operation": operation, "state": "running"}


def _clean(params):
    """Seconde barrière : ``local_control`` a déjà validé la demande, rien ne part au site sans repasser ici."""
    if params is None or not isinstance(params, dict):
        raise ValueError("Paramètres locaux illisibles.")
    validators = LOCAL_ACTIONS["manage"]["params"]
    cleaned = {}
    for name, value in params.items():
        if name not in validators:
            raise ValueError("Paramètre local non autorisé : " + str(name)[:40])
        cleaned[name] = validators[name](value)
    if "op" not in cleaned:
        raise ValueError("Opération de gestion manquante.")
    return cleaned


LOCAL_ACTIONS = {
    "manage": {"handler": manage, "read": False,
               "params": {"op": v_choice(*OPS), "args": v_args, "args_file": v_hex(32)}},
}
