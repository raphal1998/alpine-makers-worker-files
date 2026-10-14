"""Relais vers le site, demandés depuis la machine du Worker (agent 1.48.0).

Chargé à la demande par ``local_control`` (table ``LOCAL_ACTIONS``). Chaque action rend aussitôt : tout appel au
site part dans un fil démon, jamais dans la boucle du contrôle local (le bandeau des outils n'attend ``status`` que
quelques secondes). Un seul relais à la fois, suivi par ``relay-status``. Seule exception : ``login`` (agent 1.50.12)
attend la réponse du site, au plus quinze secondes, car worker.exe a besoin du verdict tout de suite.

Rien n'est décidé ici à la place du site : compte propriétaire, admission, équipe et confirmation sont vérifiés par
les routes signées ``/api/worker-protocol/self/…``. Ce module ne crée aucune commande serveur et ne pilote aucun
équipement : « Arrêter tous les jobs » ne touche ni graveuse, ni imprimante, ni caméra, ni moteur ; les gravures en
cours sont seulement nommées.
"""
import importlib
import json
import secrets
import threading
import time

if __package__:
    from .local_control import checked, v_bool, v_hex, v_list_of
else:
    from local_control import checked, v_bool, v_hex, v_list_of

STATE_PATH = "/api/worker-protocol/self/state"
# Agent 1.50.12 : connexion du compte depuis worker.exe (route signée self/login). Le mot de passe passe par un fichier
# d'arguments lu une fois puis effacé, jamais par la ligne de commande ni par un journal.
LOGIN_PATH = "/api/worker-protocol/self/login"
LOGIN_TIMEOUT = 15.0            # attente maximale de la réponse du site, en secondes (appel synchrone)
LOGIN_LOCK_RETRIES = 6          # « déjà en cours » : jusqu'à six essais espacés d'un demi-seconde
LOGIN_LOCK_RETRY_DELAY = 0.5
LOGIN_ARGS_MAX_BYTES = 4096
LOGIN_FIELDS = ("ok", "code", "error", "totp", "username")  # seuls champs de la réponse du site recopiés
USERNAME_MAX, PASSWORD_MAX, TOTP_MAX = 120, 512, 16
RELAY_PATHS = {
    "site-update": "/api/worker-protocol/self/update",
    "site-repair": "/api/worker-protocol/self/repair",
    "jobs-stop": "/api/worker-protocol/self/cancel-jobs",
    "sharing-set": "/api/worker-protocol/self/sharing",
}
SHARING_AUDIENCES = ("team", "contributors", "everyone")
SHARING_TOOLS = ("image_generation", "ai3d", "converter")

STATE_FIRST_DELAY = 30       # première lecture de l'état, après le démarrage de l'agent
STATE_PERIOD = 300           # puis toutes les cinq minutes
STATE_PERIOD_MISSING = 1800  # le site répond 404 : dashboard pas encore redémarré dans sa nouvelle version
STATE_FRESH_SECONDS = 60     # au-delà, « site-state » relance une lecture même sans « refresh »
OPERATION_LIFETIME = 900     # un relais reste consultable quinze minutes
OPERATIONS_MAX = 16
ANSWER_MAX_BYTES = 65536     # une réponse du site plus grosse est tenue pour illisible


class _Runtime:
    """Ce que ce module garde par agent, en plus des attributs déclarés par ``Agent.__init__``."""

    def __init__(self):
        self.guard = threading.Lock()  # protège self_ops, self_state et les champs ci-dessous
        self.refreshing = False        # une lecture de l'état est en cours
        self.again = False             # une lecture fraîche a été demandée pendant celle en cours
        self.attempted = None          # time.monotonic() de la dernière lecture tentée, réussie ou non
        self.period = STATE_PERIOD
        self.thread = None


_RUNTIME_GUARD = threading.Lock()


def _runtime(agent):
    runtime = getattr(agent, "_self_service", None)
    if not isinstance(runtime, _Runtime):
        with _RUNTIME_GUARD:
            runtime = getattr(agent, "_self_service", None)
            if not isinstance(runtime, _Runtime):
                runtime = agent._self_service = _Runtime()
    return runtime


def classify(error):
    """Range une erreur d'appel au site. Règles figées, lues par les outils du Worker :

    - ``site_missing`` : 404 sans code du site, la route n'existe pas (dashboard pas redémarré) ;
    - ``unreachable`` : aucune réponse, ou réponse d'un intermédiaire (502, 503, 504, 52x sans code du site) ;
    - ``update_required`` : 409 ``WORKER_UPDATE_REQUIRED``, le site juge l'agent trop ancien ;
    - un 5xx qui porte un code du site (503 ``SITE_MAINTENANCE``) est un refus du site, pas une panne.
    """
    http = getattr(error, "code", None)
    if isinstance(http, bool) or not isinstance(http, int):
        http = None
    site_code = getattr(error, "site_code", "")
    site_code = site_code[:60] if isinstance(site_code, str) else ""
    unreachable = http is None or (site_code == "" and (http in {502, 503, 504} or 520 <= http <= 530 or http >= 500))
    if http is None:
        # Le texte d'une erreur réseau peut nommer une adresse ou un chemin : seul son type est gardé.
        message = f"Site injoignable ({type(error).__name__})."
    else:
        message = str(error)[:300] or f"Erreur {http}."
    return {"http": http, "site_code": site_code, "site_missing": http == 404 and site_code == "",
            "unreachable": unreachable, "update_required": http == 409 and site_code == "WORKER_UPDATE_REQUIRED",
            "message": message}


def _answer(value):
    """Réponse du site : un objet JSON de taille raisonnable, sinon elle est tenue pour illisible."""
    if not isinstance(value, dict):
        raise ValueError("reponse du site illisible")
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > ANSWER_MAX_BYTES:
        raise ValueError("reponse du site trop volumineuse")
    return value


def _clean(action, params):
    """Seconde barrière : ``local_control`` a déjà validé la demande, mais rien ne part au site sans repasser ici."""
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("Paramètres locaux illisibles.")
    validators = LOCAL_ACTIONS[action]["params"]
    cleaned = {}
    for name, value in params.items():
        if name not in validators:
            raise ValueError("Paramètre local non autorisé : " + str(name)[:40])
        cleaned[name] = validators[name](value)
    return cleaned


def args_from_file(agent, token, check, limit):
    """Lit puis efface ``runtime/local-control/<token>.args.json`` (écrit par worker.exe juste avant la demande).

    ``token`` a déjà passé ``v_hex(32)`` ; ``checked`` refuse de toute façon un chemin hors du dossier du Worker. Le
    fichier est effacé avant toute lecture de son contenu, donc aussi quand il est trop gros ou illisible. ``check``
    valide la valeur JSON et rend la valeur nettoyée."""
    path = checked(agent.root, f"runtime/local-control/{token}.args.json")
    try:
        if not path.is_file():
            raise ValueError("Fichier d'arguments introuvable : relance l'action.")
        if path.stat().st_size > limit:
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
    return check(value)


# --- État de ce Worker lu sur le site -----------------------------------------------------------------------------

def _read_site(agent, runtime):
    previous = agent.self_state if isinstance(agent.self_state, dict) else {}
    try:
        site = _answer(agent.request(STATE_PATH))
        state = {"known": True, "fetched_at": time.time(), "missing": False, "unreachable": False, "error": None,
                 "site": site}
        period = STATE_PERIOD
    except Exception as error:
        verdict = classify(error)
        # Une lecture ratée garde le dernier état connu, avec sa date ; un site sans la fonction n'en a aucun.
        site = None if verdict["site_missing"] else previous.get("site")
        site = site if isinstance(site, dict) else None
        state = {"known": site is not None, "fetched_at": previous.get("fetched_at") if site is not None else None,
                 "missing": verdict["site_missing"], "unreachable": verdict["unreachable"],
                 "error": {"http": verdict["http"], "site_code": verdict["site_code"], "message": verdict["message"]},
                 "site": site}
        period = STATE_PERIOD_MISSING if verdict["site_missing"] else STATE_PERIOD
    with runtime.guard:
        # Même forme que la réponse de « site-state » ; l'âge et « refreshing » sont recalculés à chaque lecture.
        agent.self_state = {**state, "age_seconds": 0 if state["known"] else None, "refreshing": False}
        runtime.period = period


def _fetch_state(agent, runtime):
    """Lit l'état sur le site et le range dans ``agent.self_state``. Appelé avec ``runtime.refreshing`` déjà posé.
    Ne lève jamais."""
    while True:
        try:
            _read_site(agent, runtime)
        except Exception as error:
            print(f"Etat du Worker sur le site non enregistre ({type(error).__name__}).", flush=True)
        with runtime.guard:
            runtime.attempted = time.monotonic()
            if not runtime.again:
                runtime.refreshing = False
                return
            runtime.again = False  # demandée pendant la lecture : on relit, pour un état postérieur à la demande


def _refresh(agent, fresh=False):
    """Lecture de l'état dans le fil appelant. ``fresh`` : exige un état lu après cet appel, même si une lecture
    est déjà en cours (elle sera suivie d'une seconde)."""
    runtime = _runtime(agent)
    with runtime.guard:
        if runtime.refreshing:
            runtime.again = runtime.again or fresh
            return
        runtime.refreshing = True
    _fetch_state(agent, runtime)


def _view(agent, runtime):
    with runtime.guard:
        state, refreshing = agent.self_state, runtime.refreshing
    state = state if isinstance(state, dict) else {}
    site = state.get("site") if isinstance(state.get("site"), dict) else None
    fetched_at = state.get("fetched_at")
    if site is None or isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float)):
        fetched_at = None
    return {"known": site is not None, "fetched_at": fetched_at,
            "age_seconds": max(0, int(time.time() - fetched_at)) if fetched_at is not None else None,
            "refreshing": refreshing, "missing": state.get("missing") is True,
            "unreachable": state.get("unreachable") is True,
            "error": state.get("error") if isinstance(state.get("error"), dict) else None, "site": site}


def site_state(agent, params):
    """Dernier état connu, rendu tout de suite. La lecture sur le site, si elle est due, part dans un fil."""
    params = _clean("site-state", params)
    runtime = _runtime(agent)
    with runtime.guard:
        # « attempted » compte aussi les lectures ratées : un site en panne n'est pas rappelé à chaque seconde.
        stale = runtime.attempted is None or time.monotonic() - runtime.attempted > STATE_FRESH_SECONDS
        launch = False
        if runtime.refreshing:
            runtime.again = runtime.again or params.get("refresh", False)
        elif params.get("refresh", False) or stale:
            launch = runtime.refreshing = True
    if launch:
        try:
            threading.Thread(target=_fetch_state, args=(agent, runtime), name="local-site-state", daemon=True).start()
        except BaseException:
            with runtime.guard:
                runtime.refreshing = False
            raise
    return _view(agent, runtime)


def _state_loop(agent):
    delay = STATE_FIRST_DELAY
    while True:
        # Toute attente passe par stop_requested : à l'arrêt, le fil sort avant tout appel réseau.
        if agent.stop_requested.wait(delay):
            return
        try:
            if not getattr(agent, "disconnecting", False):  # déconnexion en cours : plus aucun appel au site
                _refresh(agent)
            delay = _runtime(agent).period
        except Exception as error:
            print(f"Lecture de l'etat du Worker sur le site interrompue ({type(error).__name__}).", flush=True)
            delay = STATE_PERIOD


def start(agent):
    """Lance le fil permanent « worker-self-state ». Rien n'est lancé pour un Worker non associé."""
    config = agent.config if isinstance(agent.config, dict) else {}
    if not config.get("worker_id") or not config.get("token"):
        return None
    runtime = _runtime(agent)
    with runtime.guard:
        if runtime.thread is None or not runtime.thread.is_alive():
            runtime.thread = threading.Thread(target=_state_loop, args=(agent,), name="worker-self-state", daemon=True)
            runtime.thread.start()
        return runtime.thread


# --- Relais : une demande au site, suivie par « relay-status » ----------------------------------------------------

def _call(agent, path, payload):
    """Un appel au site, rendu sous la forme des champs de « relay-status ». Ne lève jamais."""
    try:
        return {"state": "done", "response": _answer(agent.request(path, "POST", payload))}
    except Exception as error:
        verdict = classify(error)
        return {"state": "failed", "http": verdict["http"], "site_code": verdict["site_code"],
                "site_missing": verdict["site_missing"], "unreachable": verdict["unreachable"],
                "error": verdict["message"]}


def _relay_worker(agent, runtime, operation, work):
    outcome = {"state": "failed", "error": "Demande au site interrompue."}
    try:
        outcome = work()
    except Exception as error:  # filet : aucune exception ne sort d'un fil
        outcome = {"state": "failed", "error": f"Demande au site interrompue ({type(error).__name__})."}
        print(f"Relais vers le site interrompu ({type(error).__name__}).", flush=True)
    finally:
        try:
            with runtime.guard:
                if operation in agent.self_ops:
                    agent.self_ops[operation] = {**agent.self_ops[operation], **outcome}
        finally:
            agent.self_relay_lock.release()
    if not agent.stop_requested.is_set():
        _refresh(agent, fresh=True)  # l'état est relu tout de suite après chaque relais terminé


def _relay(agent, action, work):
    """Lance ``work`` dans un fil démon et rend aussitôt l'identifiant de suivi."""
    if not agent.self_relay_lock.acquire(blocking=False):
        raise ValueError("Une demande au site est déjà en cours ; attends sa fin.")
    runtime = _runtime(agent)
    operation = secrets.token_hex(16)
    try:
        with runtime.guard:
            now = time.monotonic()
            _expire(agent.self_ops, now)
            finished = sorted((key for key, item in agent.self_ops.items() if item["state"] != "running"),
                              key=lambda key: agent.self_ops[key]["at"])
            while len(agent.self_ops) >= OPERATIONS_MAX and finished:
                del agent.self_ops[finished.pop(0)]
            agent.self_ops[operation] = {"state": "running", "action": action, "http": None, "site_code": "",
                                         "site_missing": False, "unreachable": False, "error": "", "response": None,
                                         "local": None, "at": now}
        threading.Thread(target=_relay_worker, args=(agent, runtime, operation, work),
                         name="local-relay-" + action, daemon=True).start()
    except BaseException:
        with runtime.guard:
            agent.self_ops.pop(operation, None)
        agent.self_relay_lock.release()
        raise
    return {"operation": operation, "state": "running"}


def _expire(operations, now):
    for key in [key for key, item in operations.items()
                if item["state"] != "running" and now - item["at"] > OPERATION_LIFETIME]:
        del operations[key]


def relay_status(agent, params):
    params = _clean("relay-status", params)
    runtime = _runtime(agent)
    with runtime.guard:
        _expire(agent.self_ops, time.monotonic())
        record = agent.self_ops.get(params.get("operation"))
    if record is None:
        raise ValueError("Opération inconnue ou expirée.")
    return {key: value for key, value in record.items() if key != "at"}


def site_update(agent, params):
    """Demande au site la commande de mise à jour : les gardes de l'agent s'appliquent à son arrivée."""
    _clean("site-update", params)
    return _relay(agent, "site-update", lambda: _call(agent, RELAY_PATHS["site-update"], {}))


def site_repair(agent, params):
    params = _clean("site-repair", params)
    body = {"include_heavy": params.get("include_heavy", False)}
    return _relay(agent, "site-repair", lambda: _call(agent, RELAY_PATHS["site-repair"], body))


def sharing_set(agent, params):
    """Règle le partage par la route signée. Le corps est un dictionnaire neuf, jamais les paramètres reçus."""
    params = _clean("sharing-set", params)
    if "enabled" not in params and "auto_accept_jobs" not in params:
        raise ValueError("Réglage de partage vide : rien n'est demandé au site.")
    if "enabled" in params and "expected" not in params:
        raise ValueError("Réglage de partage refusé : relis d'abord l'état du partage sur le site.")
    audiences, tools = params.get("audiences"), params.get("tools")
    if (audiences is not None or tools is not None) and params.get("enabled") is not True:
        raise ValueError("Réglage de partage refusé : publics et outils ne se règlent qu'avec un partage activé.")
    if audiences and not tools:
        raise ValueError("Réglage de partage refusé : choisis au moins un outil à ouvrir à ces publics.")
    body = {}
    if "enabled" in params:
        sharing = {"enabled": params["enabled"]}
        if audiences is not None:
            sharing["audiences"] = list(dict.fromkeys(audiences))
        if tools is not None:
            sharing["tools"] = list(dict.fromkeys(tools))
        body["sharing"] = sharing
        body["expected"] = params["expected"]
    for name in ("auto_accept_jobs", "confirm_everyone_auto"):
        if name in params:
            body[name] = params[name]
    return _relay(agent, "sharing-set", lambda: _call(agent, RELAY_PATHS["sharing-set"], body))


# --- Arrêter tous les jobs ----------------------------------------------------------------------------------------

def _cancel_local_audit(agent):
    """Annulation coopérative du contrôle local des doublons, s'il tourne (module absent : rien à annuler)."""
    try:
        module = importlib.import_module((__package__ + "." if __package__ else "") + "duplicates")
        result = module.LOCAL_ACTIONS["duplicates-cancel"]["handler"](agent, {})
        return isinstance(result, dict) and result.get("cancel_requested") is True
    except Exception:
        return False


def _grbl_jobs(agent):
    """Machines dont une gravure ou un cadrage est en cours : elles sont nommées, jamais arrêtées d'ici."""
    try:
        names = agent.active_grbl_jobs()
    except Exception:
        return []
    return [str(name)[:80] for name in names][:20] if isinstance(names, list) else []


def _cancel_local_jobs(agent):
    """Repli quand le site ne peut pas répondre : arrêt demandé à chaque job actif de l'association courante.

    Les installations en cours ne sont pas annulées ici : le site refuserait ensuite leur résultat.
    """
    count = 0
    try:
        for job_id, item in agent.journal.records("jobs").items():
            if not agent._job_active(item) or not agent._job_identity_matches(item):
                continue
            try:
                agent.cancel_job(str(job_id))
                count += 1
            except Exception as error:
                print(f"Arret local d'un job impossible ({type(error).__name__}).", flush=True)
    except Exception as error:
        print(f"Journal des jobs illisible pour l'arret local ({type(error).__name__}).", flush=True)
    return count


def _stop_jobs(agent):
    local = {"fallback": False, "fallback_reason": "", "jobs_cancel_requested": 0,
             "audit_cancelled": _cancel_local_audit(agent), "grbl_jobs": _grbl_jobs(agent)}
    outcome = _call(agent, RELAY_PATHS["jobs-stop"], {})
    if outcome["state"] == "done":
        agent.reconnect_requested.set()  # les arrêts reviennent par le heartbeat : ne pas attendre vingt secondes
    else:
        # Seuls deux cas mènent au repli : le site ne répond pas, ou il n'a pas encore la fonction.
        reason = "missing" if outcome["site_missing"] else "unreachable" if outcome["unreachable"] else ""
        if reason:
            local.update(fallback=True, fallback_reason=reason, jobs_cancel_requested=_cancel_local_jobs(agent))
    return {**outcome, "local": local}


def jobs_stop(agent, params):
    """Arrête calculs et installations de ce Worker par le site ; aucun équipement n'est commandé."""
    _clean("jobs-stop", params)
    return _relay(agent, "jobs-stop", lambda: _stop_jobs(agent))


# --- Connexion du compte depuis worker.exe (agent 1.50.12) --------------------------------------------------------

def _check_login_args(value):
    """Contenu du fichier d'arguments : {"username", "password", "totp_code"?}, chaînes bornées, rien d'autre."""
    if not isinstance(value, dict):
        raise ValueError("Fichier d'arguments illisible : un objet est attendu.")
    unknown = sorted(str(key)[:40] for key in set(value) - {"username", "password", "totp_code"})
    if unknown:
        raise ValueError("Argument de connexion non autorisé : " + unknown[0])
    username, password, totp = value.get("username"), value.get("password"), value.get("totp_code", "")
    if not isinstance(username, str) or not username.strip() or len(username) > USERNAME_MAX:
        raise ValueError("Identifiant de connexion manquant ou trop long.")
    if not isinstance(password, str) or not password or len(password) > PASSWORD_MAX:
        raise ValueError("Mot de passe manquant ou trop long.")
    if totp is None:
        totp = ""
    if not isinstance(totp, str) or len(totp) > TOTP_MAX:
        raise ValueError("Code de validation illisible.")
    return {"username": username.strip(), "password": password, "totp_code": totp.strip()}


def _login_failure(message, code="", http=None, site_missing=False, unreachable=False):
    return {"ok": False, "code": code, "error": message, "totp": "", "username": "", "http": http,
            "site_missing": site_missing, "unreachable": unreachable}


def _login_outcome(outcome):
    """Réponse de « login » à partir des champs de ``_call`` : seuls les champs connus du site sont recopiés."""
    if outcome["state"] != "done":
        return _login_failure(outcome["error"], outcome["site_code"], outcome["http"], outcome["site_missing"],
                              outcome["unreachable"])
    answer = outcome["response"]
    result = {"ok": answer.get("ok") is True, "code": "", "error": "", "totp": "", "username": "", "http": None,
              "site_missing": False, "unreachable": False}
    for name in LOGIN_FIELDS:
        value = answer.get(name)
        if name != "ok" and isinstance(value, str):
            result[name] = value[:300]
    if not result["ok"] and not result["error"]:
        result["error"] = "Connexion refusée par le site."
    return result


def _login_worker(agent, body, holder, done):
    try:
        holder["outcome"] = _call(agent, LOGIN_PATH, body)
    except Exception as error:  # filet : aucune exception (ni son texte) ne sort du fil
        holder["outcome"] = {"state": "failed", "http": None, "site_code": "", "site_missing": False,
                             "unreachable": False, "error": f"Connexion interrompue ({type(error).__name__})."}
    finally:
        try:
            done.set()
        finally:
            agent.self_relay_lock.release()


def login(agent, params):
    """Connecte le compte par la route signée self/login, de façon synchrone (au plus LOGIN_TIMEOUT secondes).

    Le fichier d'arguments est lu puis effacé avant tout appel ; le mot de passe ne va que dans le corps de la
    requête signée. Rien n'est inscrit dans ``self_ops`` ni dans l'état du site : la réponse est rendue à l'appelant
    et oubliée. Une demande déjà en cours est attendue un court instant avant d'être refusée."""
    params = _clean("login", params)
    if "args_file" not in params:
        raise ValueError("Fichier d'arguments manquant : écris runtime/local-control/<32 hex>.args.json.")
    body = args_from_file(agent, params["args_file"], _check_login_args, LOGIN_ARGS_MAX_BYTES)
    acquired = False
    for attempt in range(LOGIN_LOCK_RETRIES):
        if agent.self_relay_lock.acquire(blocking=False):
            acquired = True
            break
        if attempt + 1 < LOGIN_LOCK_RETRIES:
            agent.stop_requested.wait(LOGIN_LOCK_RETRY_DELAY)
    if not acquired:
        raise ValueError("Une demande au site est déjà en cours ; attends sa fin.")
    holder, done = {}, threading.Event()
    try:
        threading.Thread(target=_login_worker, args=(agent, body, holder, done), name="local-relay-login",
                         daemon=True).start()
    except BaseException:
        agent.self_relay_lock.release()
        raise
    finally:
        body = None
    if not done.wait(LOGIN_TIMEOUT):
        # L'appel continue dans son fil (il rendra le verrou tout seul) ; sa réponse tardive est oubliée.
        return _login_failure("Le site n'a pas répondu à temps : réessaie dans un instant.", "LOGIN_TIMEOUT",
                              unreachable=True)
    result = _login_outcome(holder["outcome"])
    if not agent.stop_requested.is_set():
        try:
            site_state(agent, {"refresh": True})  # l'état (verrou, compteur d'échecs) est relu en arrière-plan
        except Exception:
            pass
    return result


def login_state(agent, params):
    """État du verrou de connexion de worker.exe, lu dans le dernier état connu du site (``exe_login``)."""
    view = site_state(agent, params)
    site = view["site"] if isinstance(view.get("site"), dict) else {}
    exe_login = site.get("exe_login") if isinstance(site.get("exe_login"), dict) else {}
    failed = exe_login.get("failed_count")
    return {"locked": exe_login.get("locked") is True,
            "failed_count": failed if isinstance(failed, int) and not isinstance(failed, bool) and failed >= 0 else 0,
            "known": view["known"], "refreshing": view["refreshing"], "age_seconds": view["age_seconds"],
            "missing": view["missing"], "unreachable": view["unreachable"], "error": view["error"]}


LOCAL_ACTIONS = {
    "site-state": {"handler": site_state, "params": {"refresh": v_bool}, "read": True},
    "site-update": {"handler": site_update, "params": {}, "read": False},
    "site-repair": {"handler": site_repair, "params": {"include_heavy": v_bool}, "read": False},
    "jobs-stop": {"handler": jobs_stop, "params": {}, "read": False},
    "sharing-set": {"handler": sharing_set, "read": False,
                    "params": {"enabled": v_bool, "audiences": v_list_of(SHARING_AUDIENCES, 3),
                               "tools": v_list_of(SHARING_TOOLS, 3), "expected": v_hex(16),
                               "auto_accept_jobs": v_bool, "confirm_everyone_auto": v_bool}},
    "relay-status": {"handler": relay_status, "params": {"operation": v_hex(32)}, "read": True},
    # Agent 1.50.12 : connexion depuis worker.exe ; les identifiants passent par le fichier d'arguments, jamais ici.
    "login": {"handler": login, "params": {"args_file": v_hex(32)}, "read": False},
    "login-state": {"handler": login_state, "params": {"refresh": v_bool}, "read": True},
}
