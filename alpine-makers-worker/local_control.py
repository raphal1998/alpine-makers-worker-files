"""Local-only maintenance mailbox, protected by the installation directory ACL.

No registration, token rotation, shell payload or server identity changes.
Cleanup never follows links and never removes global programs or shared caches.
"""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import stat
import sys
import threading
import time
import uuid

if __package__:
    from .job_state import atomic_json
    from .safety import is_link
    from .installers.model_catalog import COMFYUI_CATALOG
else:
    from job_state import atomic_json
    from safety import is_link
    from installers.model_catalog import COMFYUI_CATALOG

ENGINES = ("comfyui", "hunyuan3d", "orca", "orca-bridge", "freecad", "cuda-toolkit", "printguard")
TOOLS = ("image_generation", "ai3d", "model-studio", "converter", "printguard")
KEEP = {"input", "output", "user", ".cache", "custom_nodes"}
CHECKPOINTS = {spec[0] for spec in COMFYUI_CATALOG.values()}

# Moteurs a processus permanent pilotes par status / engines-stop / engines-start.
# "converter" (FreeCAD) n'a pas de processus : il n'est jamais touche ici.
# Valeur = cle du verrou par moteur de l'agent (TOOL_HANDLERS, verifie par les tests).
ENGINE_LOCK_KEYS = {"image_generation": "comfyui", "ai3d": "hunyuan3d", "model-studio": "orca", "printguard": "printguard"}
# PrintGuard lit le relais camera de l'agent : arrete en premier, demarre en dernier.
ENGINE_STOP_ORDER = ("printguard", "model-studio", "ai3d", "image_generation")
ENGINE_START_ORDER = ("image_generation", "ai3d", "model-studio", "printguard")
ENGINE_ACTIONS = ("status", "engines-stop", "engines-start")
# Lectures seules sans parametre, traitees comme status (requete a echeance, aucun heartbeat force).
READ_ACTIONS = ("status", "fire-watch")
ENGINES_MEMO = "runtime/local-control/engines-memo.json"
REQUEST_MAX_LIFETIME = 660  # --timeout est borne a 600 s cote client
_ENGINES_START_GUARD = threading.Lock()


def network_info():
    addresses = set()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM):
            address = ipaddress.ip_address(item[4][0])
            if not address.is_loopback and not address.is_unspecified:
                addresses.add(str(address))
        return {"ipv4": sorted(addresses), "checked_at": time.time()}
    except OSError:
        return {"ipv4": [], "checked_at": time.time(), "error": "network_unavailable"}


def checked(root, relative):
    root = Path(root).resolve()
    path = root / relative
    if Path(relative).is_absolute() or not path.resolve().is_relative_to(root) or path.resolve() == root:
        raise ValueError("Chemin hors perimetre ; aucun nettoyage autorise.")
    cursor = path
    while cursor != root:
        if is_link(cursor):
            raise ValueError("Lien/jonction detecte ; composant conserve pour proteger sa cible.")
        cursor = cursor.parent
    return path


def cleanup_plan(root):
    root = Path(root).resolve()
    files, skipped = [], []
    checked(root, "components")
    for engine in ENGINES:
        relative = Path("components") / engine
        try:
            folder = checked(root, relative)
            if not folder.is_dir():
                continue
            # A directory name alone is not proof of an Alpine installation.
            owned = any((folder / marker).is_file() for marker in
                        (".alpine-managed.json", ".raphal-ready", "freecad-path.txt", "cuda-path.txt"))
            if not owned:
                skipped.append(str(relative) + " : origine non prouvee (installation ancienne/partielle)")
                continue
            candidates = []
            for directory, names, filenames in os.walk(folder, followlinks=False):
                directory = Path(directory)
                for name in list(names) + filenames:
                    checked(root, (directory / name).relative_to(root))
                if directory == folder:
                    names[:] = [name for name in names if name not in KEEP]
                for name in filenames:
                    if name in {".alpine-managed.json", ".disabled"}:
                        continue
                    path = directory / name
                    sub = path.relative_to(folder)
                    if sub.parts[0] == "models":
                        if engine == "comfyui" and not (len(sub.parts) == 3 and sub.parts[1] == "checkpoints" and name.removesuffix(".download") in CHECKPOINTS):
                            continue
                        if engine == "hunyuan3d" and not any((folder / "models" / sub.parts[1] / marker).is_file() for marker in (".raphal-ready", ".alpine-managed.json")):
                            continue
                    info = path.stat()
                    candidates.append({"path": path.relative_to(root).as_posix(), "bytes": info.st_size, "mtime_ns": info.st_mtime_ns})
            files.extend(candidates)
            if engine == "orca-bridge":
                bridge = checked(root, "orca_core_bridge.ps1")
                if bridge.is_file():
                    info = bridge.stat()
                    files.append({"path": "orca_core_bridge.ps1", "bytes": info.st_size, "mtime_ns": info.st_mtime_ns})
        except (OSError, ValueError) as error:
            skipped.append(str(relative) + " : " + str(error))
    files.sort(key=lambda item: item["path"])
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {"files": files, "skipped": skipped, "digest": digest, "bytes": sum(item["bytes"] for item in files)}


def cleanup(agent, digest):
    # Block launches and wait for any already-dispatched lifecycle operation.
    with agent.maintenance_lock:
        if agent.maintenance_active:
            raise ValueError("Une operation est en cours. Reessaie apres sa fin.")
        agent._assert_no_active_jobs()
        agent.maintenance_count += 1
        agent.maintenance_active = True
        agent.local_cleanup_active = True
    removed, errors = [], []
    try:
        plan = cleanup_plan(agent.root)
        if plan["digest"] != digest:
            raise ValueError("Le contenu a change depuis la confirmation. Relance le BAT pour un nouvel apercu.")
        # Persist ALL stop intents before touching any file. No auto-restart.
        for tool in TOOLS:
            agent.journal.update("runtime_intent", tool, enabled=False)
        for tool in TOOLS:
            agent.control_runtime(tool, "stop")
        # A final whole-plan check prevents deleting a changed file after stop.
        if cleanup_plan(agent.root)["digest"] != digest:
            raise ValueError("Fichiers modifies pendant l'arret : relance le BAT. Aucun fichier supprime.")
        planned_engines = {Path(item["path"]).parts[1] for item in plan["files"] if item["path"].startswith("components/")}
        for engine in planned_engines:
            marker = checked(agent.root, Path("components") / engine / ".alpine-managed.json")
            atomic_json(marker, {"owner": "Alpine Makers", "component": engine})
        for item in plan["files"]:
            try:
                path = checked(agent.root, item["path"])
                info = path.stat()
                if info.st_size != item["bytes"] or info.st_mtime_ns != item["mtime_ns"]:
                    raise ValueError("Fichier modifie, conserve")
                if not info.st_mode & stat.S_IWRITE:
                    if info.st_nlink > 1:
                        raise ValueError("Fichier en lecture seule partage par lien dur, conserve")
                    path.chmod(info.st_mode | stat.S_IWRITE)
                path.unlink()
                removed.append(item["path"])
            except (OSError, ValueError) as error:
                errors.append({"path": item["path"], "error": str(error)})
        # Keep harmless empty directories: never recursively delete a broad root.
        # Disable automatic discovery of global installations without uninstalling them.
        for engine in planned_engines:
            marker = checked(agent.root, Path("components") / engine / ".disabled")
            marker.write_text("AI removed locally; global programs preserved", encoding="utf-8")
        return {"removed_files": len(removed), "errors": errors, "skipped": plan["skipped"],
                "identity_preserved": True, "resync": "next heartbeat"}
    finally:
        with agent.maintenance_lock:
            agent.maintenance_count -= 1
            agent.local_cleanup_active = False
            agent.maintenance_active = agent.maintenance_count > 0 or bool(agent.maintenance_recovery)
        with agent.inventory_lock:
            agent.inventory_revision += 1
            agent.inventory_updated_at = 0
            agent.inventory_snapshot = {}


def _engine_intent(agent, tool_id):
    intent = agent.journal.get("runtime_intent", tool_id)
    return intent.get("enabled") if isinstance(intent, dict) else None


def _read_memo(agent):
    try:
        memo = json.loads(checked(agent.root, ENGINES_MEMO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(memo, dict):
        return None

    def names(key):
        # Le memo n'est qu'une liste d'identifiants connus : rien d'autre n'en est repris.
        values = memo.get(key) if isinstance(memo.get(key), list) else []
        return [tool for tool in ENGINE_START_ORDER if tool in values]

    at = memo.get("at")
    return {"at": at if isinstance(at, (int, float)) and not isinstance(at, bool) else None,
            "were_running": names("were_running"), "intent_enabled": names("intent_enabled")}


def _patch_inventory(agent, tool_id, started):
    # Meme correctif d'instantane que tool.start / tool.stop dans l'agent.
    with agent.inventory_lock:
        agent.inventory_revision += 1
        agent.inventory_updated_at = time.monotonic()
        for tool in agent.inventory_snapshot.get("tools", []):
            if tool.get("tool_id") == tool_id:
                tool["status"] = "online" if started else "ready"
                tool["config"] = {**(tool.get("config") or {}), "runtime_enabled": started}


def _engine_lock(agent, tool_id):
    with agent.command_lock:
        return agent.component_locks.setdefault(ENGINE_LOCK_KEYS[tool_id], threading.Lock())


def _release_maintenance(agent):
    with agent.maintenance_lock:
        agent.maintenance_count -= 1
        agent.maintenance_active = agent.maintenance_count > 0 or bool(agent.maintenance_recovery)


def status(agent):
    """Instantane en lecture seule. Jamais de jeton, d'identite complete ni de chemin."""
    jobs, previous_identity = [], 0
    for job_id, item in agent.journal.records("jobs").items():
        if not agent._job_active(item):
            continue
        if not agent._job_identity_matches(item):
            previous_identity += 1  # ignore par les gardes : identite precedente
            continue
        jobs.append({"job_id": str(job_id), "tool_id": item.get("tool_id"), "state": item.get("state"),
                     "engine_stop_confirmed": item.get("engine_stop_confirmed") is True})
    engines = []
    for tool_id in ENGINE_START_ORDER:
        pid = agent._runtime_record(tool_id).get("pid")
        engines.append({"tool_id": tool_id, "running": bool(agent._runtime_running(tool_id)),
                        "port": agent._runtime_port(tool_id),
                        "pid": pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
                        "intent": _engine_intent(agent, tool_id)})
    # "converter" (FreeCAD) n'a pas de processus permanent : il est omis.
    recovery = agent.maintenance_recovery
    # La version vient du module de l'agent deja charge : aucun import circulaire.
    version = getattr(sys.modules.get(type(agent).__module__), "AGENT_VERSION", None)
    return {"checked_at": time.time(), "agent_pid": os.getpid(),
            "agent_version": version if isinstance(version, str) else None,
            "name": agent.config.get("name"), "server_url": agent.config.get("server_url"),
            "worker_id_prefix": str(agent.config.get("worker_id") or "")[:8],
            "maintenance_active": bool(agent.maintenance_active), "maintenance_count": int(agent.maintenance_count),
            "maintenance_recovery": len(recovery) if hasattr(recovery, "__len__") else int(bool(recovery)),
            "local_cleanup_active": bool(agent.local_cleanup_active), "disconnecting": bool(agent.disconnecting),
            "jobs_active": jobs, "jobs_previous_identity": previous_identity,
            "engines": engines,
            "engines_start": {"in_progress": bool(getattr(agent, "local_engines_starting", False)),
                              "last": getattr(agent, "local_engines_last_start", None)},
            "memo": _read_memo(agent)}


def fire_watch_status(agent):
    """Instantane de la surveillance IA des graveuses, en lecture seule (aucune image, aucun secret)."""
    watch = getattr(agent, "fire_watch", None)
    if watch is None:
        error = str(getattr(agent, "fire_watch_error", "") or "")
        return {"state": "not_started", "message": "Surveillance non demarree sur ce Worker.",
                **({"error": error[:300]} if error else {})}
    try:
        return watch.local_status()
    except Exception as error:
        return {"state": "error", "message": f"Etat de la surveillance illisible ({type(error).__name__})."}


def engines_stop(agent):
    # Meme rigueur que cleanup() : on bloque les lancements, puis on arrete.
    with agent.maintenance_lock:
        if getattr(agent, "local_engines_starting", False):
            raise ValueError("Arret refuse : un demarrage des moteurs est en cours. Reessaie apres sa fin.")
        if agent.maintenance_active:
            raise ValueError("Arret refuse : une installation ou une maintenance est en cours. Reessaie apres sa fin.")
        try:
            agent._assert_no_active_jobs()
        except Exception as error:
            raise ValueError(f"Arret des moteurs refuse : {error}") from error
        agent.maintenance_count += 1
        agent.maintenance_active = True
    stopped, errors = [], []
    try:
        were_running = [tool for tool in ENGINE_START_ORDER if agent._runtime_running(tool)]
        intent_enabled = [tool for tool in ENGINE_START_ORDER if _engine_intent(agent, tool) is True]
        # Un memo non consomme est FUSIONNE, jamais ecrase : apres un arret
        # partiel (un moteur en echec), le second arret ne voit plus que ce
        # moteur-la et ferait oublier ceux deja arretes par le premier.
        previous = _read_memo(agent) or {"at": None, "were_running": [], "intent_enabled": []}
        merged = {key: [tool for tool in ENGINE_START_ORDER if tool in previous[key] or tool in current]
                  for key, current in (("were_running", were_running), ("intent_enabled", intent_enabled))}
        if previous["at"] is not None and all(merged[key] == previous[key] for key in merged):
            memo = previous  # rien de nouveau : le memo utile reste tel quel
        else:
            memo = {"at": time.time(), **merged}
            atomic_json(checked(agent.root, ENGINES_MEMO), memo)
        # Toutes les intentions a False AVANT le premier arret : ni le chien de
        # garde ni un redemarrage de l'agent ne relancent un moteur arrete ici.
        for tool in ENGINE_STOP_ORDER:
            agent.journal.update("runtime_intent", tool, enabled=False)
        for tool in ENGINE_STOP_ORDER:
            try:
                with _engine_lock(agent, tool):
                    result = agent.control_runtime(tool, "stop")
                if isinstance(result, dict) and result.get("stopped"):
                    stopped.append(tool)
                _patch_inventory(agent, tool, False)
            except Exception as error:  # un echec n'empeche pas l'arret des suivants
                errors.append({"tool_id": tool, "error": str(error)})
        return {"stopped": stopped, "were_running": were_running, "errors": errors, "memo": memo}
    finally:
        _release_maintenance(agent)


def _start_one(agent, tool_id):
    # Chemin garde de tool.start (Agent.execute puis _execute_serialized), sans
    # commande serveur : aucun recu n'est fabrique dans le journal des commandes.
    with agent.maintenance_lock:
        if agent.local_cleanup_active:
            raise ValueError("Nettoyage IA local en cours ; reessaie apres sa fin.")
        agent._assert_no_active_jobs()
        agent.maintenance_count += 1
        agent.maintenance_active = True
    try:
        with _engine_lock(agent, tool_id):
            result = agent.control_runtime(tool_id, "start")
        agent.journal.update("runtime_intent", tool_id, enabled=True)
        _patch_inventory(agent, tool_id, True)
        return result
    finally:
        _release_maintenance(agent)


def _settle_memo(agent, started):
    # Le memo n'est consomme que pour les moteurs reellement demarres : ceux
    # qui restent a relancer y demeurent pour le prochain engines-start.
    # Il est relu ici : on ne retire que ce qui a demarre, rien d'autre.
    memo = _read_memo(agent)
    if memo is None:
        return
    path = checked(agent.root, ENGINES_MEMO)
    remaining = {key: [tool for tool in memo[key] if tool not in started] for key in ("were_running", "intent_enabled")}
    if remaining == {key: memo[key] for key in remaining}:
        return
    if remaining["were_running"] or remaining["intent_enabled"]:
        atomic_json(path, {"at": memo["at"] if memo["at"] is not None else time.time(), **remaining})
    else:
        path.unlink(missing_ok=True)


def _start_worker(agent, tools, source, memo):
    started, errors = [], []
    try:
        for tool in tools:
            if agent.stop_requested.is_set() or agent.disconnecting:
                errors.append({"tool_id": tool, "error": "Agent en cours d'arret ; demarrage abandonne."})
                continue
            try:
                _start_one(agent, tool)
                started.append(tool)
            except Exception as error:
                errors.append({"tool_id": tool, "error": str(error)})
                print(f"Demarrage local du moteur {tool} impossible ({type(error).__name__}).", flush=True)
    finally:
        try:
            if memo is not None:
                try:
                    _settle_memo(agent, started)
                except (OSError, ValueError) as error:
                    errors.append({"tool_id": None, "error": f"Memo des moteurs non mis a jour ({type(error).__name__})."})
        finally:
            agent.local_engines_last_start = {"at": time.time(), "source": source, "requested": list(tools),
                                              "started": started, "errors": errors}
            # Le drapeau ne tombe qu'APRES l'ecriture du memo : un engines-stop
            # (refuse tant qu'il est leve) ne peut pas ecrire en meme temps.
            with _ENGINES_START_GUARD:
                agent.local_engines_starting = False
            agent.reconnect_requested.set()


def engines_start(agent):
    with _ENGINES_START_GUARD:
        if getattr(agent, "local_engines_starting", False):
            raise ValueError("Un demarrage des moteurs est deja en cours. Attends sa fin (action status).")
        # Memes gardes que chaque demarrage, verifiees AVANT de repondre : on
        # n'annonce pas un demarrage qui sera refuse, et le memo reste intact.
        with agent.maintenance_lock:
            if agent.local_cleanup_active:
                raise ValueError("Demarrage refuse : un nettoyage IA local est en cours. Reessaie apres sa fin.")
            if agent.maintenance_active:
                raise ValueError("Demarrage refuse : une installation ou une maintenance est en cours. Reessaie apres sa fin.")
            try:
                agent._assert_no_active_jobs()
            except Exception as error:
                raise ValueError(f"Demarrage refuse : {error}") from error
        memo = _read_memo(agent)
        if memo is not None:
            wanted, source = set(memo["were_running"]) | set(memo["intent_enabled"]), "memo"
        else:
            # Sans memo, l'etat d'avant l'arret est inconnu : on se rabat sur les
            # moteurs que l'inventaire de l'agent declare installes et prets.
            with agent.inventory_lock:
                wanted = {str(tool.get("tool_id")) for tool in agent.inventory_snapshot.get("tools", [])
                          if str(tool.get("status") or "") in {"ready", "online"}}
            source = "inventory"
        tools = [tool for tool in ENGINE_START_ORDER if tool in wanted]
        if memo is None and not tools:
            raise ValueError("Aucun memo d'arret et aucun moteur pret dans l'inventaire. "
                             "Attends la fin de l'inventaire ou demarre le moteur depuis Mes Workers.")
        agent.local_engines_starting = True
        try:
            thread = threading.Thread(target=_start_worker, args=(agent, tools, source, memo), name="local-engines-start", daemon=True)
            agent.local_engines_thread = thread
            thread.start()
        except BaseException:
            agent.local_engines_starting = False
            raise
    # Le memo est solde par le thread, une fois le resultat connu (_settle_memo).
    return {"starting": tools, "source": source}


def _check_engine_request(item):
    # Aucune surface nouvelle : ni chemin ni parametre libre pour ces actions.
    if set(item) - {"worker_id", "action", "expires_at"}:
        raise ValueError("Cette action locale n'accepte aucun parametre.")
    expires_at = item.get("expires_at")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise ValueError("Requete locale sans echeance ; action refusee.")
    now = time.time()
    if expires_at < now or expires_at > now + REQUEST_MAX_LIFETIME:
        raise ValueError("Requete locale expiree ; action non executee. Relance l'outil.")


def process_requests(agent):
    inbox = checked(agent.root, "runtime/local-control")
    inbox.mkdir(parents=True, exist_ok=True)
    for path in sorted(inbox.glob("*.request.json"))[:10]:
        result_path = path.with_name(path.name.replace(".request.json", ".result.json"))
        action = None
        try:
            checked(agent.root, path.relative_to(agent.root))
            if path.stat().st_size > 4096:
                raise ValueError("Requete locale trop volumineuse")
            item = json.loads(path.read_text(encoding="utf-8"))
            if item.get("worker_id") != agent.config.get("worker_id"):
                raise ValueError("Cette requete appartient a un autre Worker")
            if result_path.exists():
                path.unlink()
                continue
            action = item.get("action")
            if action in ENGINE_ACTIONS or action in READ_ACTIONS:
                _check_engine_request(item)
                # Requete retiree AVANT execution : un arret de l'agent en cours
                # de route ne la rejoue jamais au demarrage suivant.
                path.unlink()
                result = {"status": status, "engines-stop": engines_stop, "engines-start": engines_start,
                          "fire-watch": fire_watch_status}[action](agent)
            elif action == "cleanup":
                result = cleanup(agent, item.get("digest"))
            elif action in {"reconnect", "network"}:
                with agent.inventory_lock:
                    agent.inventory_updated_at = 0
                    agent.inventory_snapshot = {}
                result = {"network": network_info(), "identity_preserved": True,
                          "message": "Reconnexion demandee, sans changement d'identite. Envoi au prochain heartbeat."}
            else:
                raise ValueError("Action locale inconnue")
            atomic_json(result_path, {"ok": True, "result": result})
        except Exception as error:
            atomic_json(result_path, {"ok": False, "error": str(error)})
        path.unlink(missing_ok=True)
        if action not in READ_ACTIONS:  # lecture seule : aucun heartbeat force
            agent.reconnect_requested.set()


def local_loop(agent):
    while not agent.stop_requested.is_set():
        try:
            process_requests(agent)
        except (OSError, ValueError) as error:
            print(f"Maintenance locale indisponible ({type(error).__name__})", flush=True)
        agent.stop_requested.wait(1)


def main():
    parser = argparse.ArgumentParser(description="Boite aux lettres locale du Worker (aucun reseau necessaire).")
    parser.add_argument("--root", required=True)
    parser.add_argument("--action", choices=("reconnect", "network", "cleanup") + ENGINE_ACTIONS + ("fire-watch",), required=True,
                        help="status : etat de l'agent et des moteurs ; engines-stop / engines-start : moteurs IA ; "
                             "fire-watch : etat de la surveillance IA des graveuses (lecture seule) ; "
                             "reconnect / network / cleanup : maintenance")
    parser.add_argument("--json", action="store_true", help="n'imprime qu'une ligne JSON : la reponse de l'agent")
    parser.add_argument("--timeout", type=int, default=120, help="attente maximale en secondes (1 a 600)")
    args = parser.parse_args()
    if args.json and args.action == "cleanup":
        parser.error("--json est incompatible avec cleanup (confirmation interactive).")
    timeout = min(600, max(1, args.timeout))
    transient = args.action in ENGINE_ACTIONS or args.action in READ_ACTIONS

    def emit(response, code):
        print(json.dumps(response, ensure_ascii=True, separators=(",", ":")), flush=True)
        return code

    root = Path(args.root).resolve()
    try:
        config = json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        if args.json:
            return emit({"ok": False, "error": "Configuration du Worker illisible."}, 1)
        raise
    if not config.get("worker_id") or not config.get("token"):
        if args.json:
            return emit({"ok": False, "error": "Worker non associe : aucun changement effectue."}, 1)
        raise SystemExit("Worker non associe : aucun changement effectue.")
    item = {"worker_id": config["worker_id"], "action": args.action}
    if transient:
        # L'agent refuse une requete echue : jamais d'execution differee.
        item["expires_at"] = time.time() + timeout + 5
    inbox = checked(root, "runtime/local-control")
    inbox.mkdir(parents=True, exist_ok=True)
    if args.action == "cleanup":
        print("Inventaire des composants et verification des chemins, sans suppression...", flush=True)
        plan = cleanup_plan(root)
        preview = inbox / "cleanup-preview.json"
        atomic_json(preview, plan)
        print(f"Worker : {config['worker_id']}\nDossier : {root}")
        print(f"Suppression prevue : {len(plan['files'])} fichiers, {plan['bytes'] / 1024**3:.2f} Gio.")
        print("Composants : " + ", ".join(sorted({p['path'].split('/')[1] for p in plan['files'] if p['path'].startswith('components/')})))
        print(f"Liste complete : {preview}\nConserves : Worker, identite, associations, relais, travaux, caches et logiciels globaux.")
        for skipped in plan["skipped"]:
            print("CONSERVE : " + skipped)
        if not plan["files"] or input("Pour confirmer, tape SUPPRIMER IA : ").strip() != "SUPPRIMER IA":
            print("Aucune suppression demandee.")
            return 0
        item["digest"] = plan["digest"]
    request = inbox / (uuid.uuid4().hex + ".request.json")
    atomic_json(request, item)
    result = request.with_name(request.name.replace(".request.json", ".result.json"))
    if not args.json:
        print("Demande envoyee au Worker local. La connexion internet n'est pas necessaire au nettoyage.")
    deadline = time.monotonic() + timeout
    answered = False
    try:
        while time.monotonic() < deadline:
            if result.exists():
                try:
                    response = json.loads(result.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    time.sleep(.1)  # ecriture atomique : simple course de lecture
                    continue
                answered = True
                if args.json:
                    result.unlink(missing_ok=True)
                    emit(response, 0)
                else:
                    print(json.dumps(response, ensure_ascii=True, indent=2))
                failed = not response.get("ok") or (isinstance(response.get("result"), dict) and response["result"].get("errors"))
                return 1 if failed else 0
            time.sleep(.25 if transient or args.json else 1)
    finally:
        in_progress = False
        if transient and not answered:
            # Jamais d'execution differee : la requete non prise est retiree.
            # Si l'agent l'a deja retiree, l'action est en cours maintenant.
            try:
                request.unlink()
            except FileNotFoundError:
                in_progress = not result.exists()
            except OSError:
                pass
    pending = request.exists()
    if args.json:
        response = {"ok": False, "error": "timeout", "pending": pending}
        if in_progress:
            response["in_progress"] = True
        return emit(response, 2)
    if transient:
        print("Aucune reponse de l'agent dans le delai : demande retiree, rien ne sera execute plus tard."
              if not in_progress else "L'agent execute encore la demande ; consulte l'action status.")
    else:
        print(f"Demande toujours en attente, ne pas reinstaller. Resultat a consulter : {result}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
