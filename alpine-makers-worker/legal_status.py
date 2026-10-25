"""Licences et droits de ce Worker, lus sur le site pour worker.exe et le menu console (capacité legal_status_v1).

Chargé à la demande par ``local_control`` (``LAZY_ACTIONS`` : « legal-status »). La session du compte connecté passe par
un fichier d'arguments ``runtime/local-control/<32 hex>.args.json`` {"session_token"}, écrit par worker.exe ou le menu,
lu puis effacé ici : jamais par la ligne de commande. L'agent signe la demande (route self/legal-status) ; le site juge
seul (propriétaire, co-propriétaire avec le droit « status ») et rend, pour CE compte, l'état juridique de chaque moteur,
module et modèle du Worker avec les états de la page /legal.

Lecture seule : rien n'est accepté, déclaré, installé ni gardé ici. Appel synchrone (au plus ``TIMEOUT`` secondes), une
lecture à la fois ; la réponse est réduite à ses champs connus, bornés, avant d'être rendue à l'outil qui l'a demandée.
"""
import re
import threading

if __package__:
    from .local_control import v_hex
    from .self_service import SESSION_ARGS_MAX_BYTES, args_from_file, check_session_args, classify
else:
    from local_control import v_hex
    from self_service import SESSION_ARGS_MAX_BYTES, args_from_file, check_session_args, classify

CAPABILITY = "legal_status_v1"
PATH = "/api/worker-protocol/self/legal-status"
TIMEOUT = 20.0                  # attente maximale de la réponse du site, en secondes
ITEMS_MAX = 400
TODO_MAX = 8
STATE_CODES = ("authorization_obtained", "documented", "notices_accepted", "user_declaration", "request_pending",
               "known_restriction", "to_verify", "action")
KINDS = ("tool", "component", "model")
TODO_TYPES = ("act", "wait", "info")
# Liens rendus aux outils : pages du site seulement, chemin relatif (l'outil y joint l'adresse du Dashboard).
_LINK = re.compile(r"/(?:legal|licences-personnelles)(?:#[A-Za-z0-9._:+@()-]{1,120}|\?component=[A-Za-z0-9._~%-]{1,360})?")
_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_GUARD = threading.Lock()       # une lecture à la fois ; libéré par le fil d'appel, même après un délai dépassé


def _text(value, limit=200):
    return _CONTROL.sub(" ", value)[:limit] if isinstance(value, str) else ""


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _link(value):
    return value if isinstance(value, str) and _LINK.fullmatch(value) else ""


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100000 else 0


def _states(value):
    found = []
    for item in value if isinstance(value, list) else []:
        code = item.get("code") if isinstance(item, dict) else None
        if code in STATE_CODES and code not in [state["code"] for state in found]:
            found.append({"code": code, "label": _text(item.get("label"), 60), **({"color": item["color"]} if
                                                                                 _COLOR.fullmatch(str(item.get("color") or "")) else {})})
    return found


def _summary(value):
    value = value if isinstance(value, dict) else {}
    states = value.get("states") if isinstance(value.get("states"), dict) else {}
    return {"total": _count(value.get("total")), "to_act": _count(value.get("to_act")), "waiting": _count(value.get("waiting")),
            "states": {code: _count(states.get(code)) for code in STATE_CODES}}


def _item(value):
    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or value.get("kind") not in KINDS:
        return None
    todo = []
    for entry in (value.get("todo") if isinstance(value.get("todo"), list) else [])[:TODO_MAX]:
        if isinstance(entry, dict) and entry.get("type") in TODO_TYPES:
            todo.append({"code": _text(entry.get("code"), 40), "type": entry["type"], "label": _text(entry.get("label"), 80),
                         "detail": _text(entry.get("detail"), 300), "url": _link(entry.get("url"))})
    links = value.get("links") if isinstance(value.get("links"), dict) else {}
    declaration = value.get("declaration") if isinstance(value.get("declaration"), dict) else None
    decision = value.get("decision") if isinstance(value.get("decision"), dict) else {}
    return {"id": _text(value["id"], 120), "item_id": _text(value.get("item_id"), 120), "kind": value["kind"],
            "kind_label": _text(value.get("kind_label"), 30), "tool_id": _text(value.get("tool_id"), 40),
            "label": _text(value.get("label"), 160), "engine": _text(value.get("engine"), 40),
            "publisher": _text(value.get("publisher"), 160), "license": _text(value.get("license"), 200),
            "registered": value.get("registered") is True, "unlisted": value.get("unlisted") is True,
            "install_state": _text(value.get("install_state"), 20), "states": _states(value.get("states")),
            "acceptance_required": value.get("acceptance_required") is True, "accepted": value.get("accepted") is True,
            "renewal_required": value.get("renewal_required") is True,
            "declaration": {"status": _text(declaration.get("status"), 20), "label": _text(declaration.get("label"), 80),
                            "current": declaration.get("current") is True} if declaration else None,
            "decision": {"allowed": decision.get("allowed") is True, "kind": _text(decision.get("kind"), 20),
                         "reason": _text(decision.get("reason"), 240)},
            "todo": todo, "links": {name: _link(links.get(name)) for name in ("legal", "declare") if _link(links.get(name))}}


def clean_answer(answer):
    """Réponse du site réduite à ses champs connus et bornés (jamais un jeton, un compte ou un chemin)."""
    answer = answer if isinstance(answer, dict) else {}
    worker = answer.get("worker") if isinstance(answer.get("worker"), dict) else {}
    viewer = answer.get("viewer") if isinstance(answer.get("viewer"), dict) else {}
    summary = answer.get("summary") if isinstance(answer.get("summary"), dict) else {}
    links = answer.get("links") if isinstance(answer.get("links"), dict) else {}
    lists = {}
    for name in ("installed", "available"):
        items = [_item(item) for item in (answer.get(name) if isinstance(answer.get(name), list) else [])[:ITEMS_MAX]]
        lists[name] = [item for item in items if item is not None]
    return {"ok": answer.get("ok") is True, "code": "", "error": "" if answer.get("ok") is True else "Réponse du site illisible.",
            "http": None, "unreachable": False, "site_missing": False, "update_required": False,
            "worker": {"name": _text(worker.get("name"), 120), "online": worker.get("online") is True,
                       "inventory_known": worker.get("inventory_known") is True},
            "viewer": {"role": _text(viewer.get("role"), 20), "label": _text(viewer.get("label"), 40)},
            "policy_version": _text(answer.get("policy_version"), 40), "mode": _text(answer.get("mode"), 20),
            "enforcement": answer.get("enforcement") is True, "country_unknown": answer.get("country_unknown") is True,
            "states": _states(answer.get("states")),
            "summary": {"installed": _summary(summary.get("installed")), "available": _summary(summary.get("available"))},
            "installed": lists["installed"], "available": lists["available"], "available_models": _count(answer.get("available_models")),
            "truncated": answer.get("truncated") is True,
            "links": {name: _link(links.get(name)) for name in ("legal", "personal_licenses", "country") if _link(links.get(name))},
            "notice": _text(answer.get("notice"), 400), "generated_at": _number(answer.get("generated_at"))}


def _failure(message, code="", http=None, *, unreachable=False, site_missing=False, update_required=False):
    return {"ok": False, "code": code, "error": message, "http": http, "unreachable": unreachable, "site_missing": site_missing,
            "update_required": update_required}


def _clean(params):
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("Paramètres locaux illisibles.")
    unknown = sorted(str(name)[:40] for name in set(params) - set(LOCAL_ACTIONS["legal-status"]["params"]))
    if unknown:
        raise ValueError("Paramètre local non autorisé : " + unknown[0])
    return {name: LOCAL_ACTIONS["legal-status"]["params"][name](value) for name, value in params.items()}


def legal_status(agent, params):
    """Licences et droits de ce Worker pour le compte de la session de ``args_file`` (lecture seule, synchrone)."""
    params = _clean(params)
    if "args_file" not in params:
        raise ValueError("Connexion requise : fichier de session manquant (runtime/local-control/<32 hex>.args.json).")
    body = args_from_file(agent, params["args_file"], check_session_args, SESSION_ARGS_MAX_BYTES)
    if not _GUARD.acquire(blocking=False):
        return _failure("Lecture des licences déjà en cours : réessaie dans un instant.", "LEGAL_STATUS_BUSY")
    holder, done = {}, threading.Event()

    def run():
        try:
            holder["answer"] = agent.request(PATH, "POST", body)
        except Exception as error:  # noqa: BLE001 - rangée par classify, jamais relancée hors du fil
            holder["error"] = classify(error)
        finally:
            try:
                done.set()
            finally:
                _GUARD.release()

    try:
        threading.Thread(target=run, name="local-legal-status", daemon=True).start()
    except BaseException:
        _GUARD.release()
        raise
    if not done.wait(TIMEOUT):
        return _failure("Le site n'a pas répondu à temps : réessaie dans un instant.", "LEGAL_STATUS_TIMEOUT", unreachable=True)
    if "error" in holder:
        verdict = holder["error"]
        return _failure(verdict["message"], verdict["site_code"], verdict["http"], unreachable=verdict["unreachable"],
                        site_missing=verdict["site_missing"], update_required=verdict["update_required"])
    return clean_answer(holder.get("answer"))


LOCAL_ACTIONS = {
    # « args_file » : session du compte connecté (fichier {"session_token"} lu puis effacé), exigée par le site.
    "legal-status": {"handler": legal_status, "params": {"args_file": v_hex(32)}, "read": True},
}
