"""Bibliothèque du compte pour Alpine Makers Desktop (agent 1.50.22), outil « library » de ``desktop_actions``.

Les fichiers vivent sur le site (bibliothèques 3D, laser, CNC et image : un seul espace de travail filtré par famille).
Chaque action relaie une demande signée vers les routes ``/api/worker-protocol/self/library*`` du site, qui appellent
les MÊMES fonctions que les pages Bibliothèque (propriété, partages acceptés, corbeille, import). Rien n'est décidé
ici : le site vérifie l'identité signée du Worker, puis la session de connexion de worker.exe (« session_token »,
rendu par « login » : propriétaire seulement), puis l'onglet permis au compte.

Toutes les actions sont des tâches (``desktop_actions.start_task``) : la boucle locale n'attend jamais le site.

- ``list``      : une page d'une bibliothèque (privée, partagée par moi, reçue) avec filtres et recherche ;
- ``download``  : télécharge un fichier (ou son aperçu) dans ``runtime/desktop/cache/library`` ;
- ``thumbnails`` : les aperçus d'une page de la grille (60 au plus) en une seule tâche, même route que ``download`` ;
- ``upload``    : importe un fichier déposé par worker.exe dans ``runtime/desktop/inbox/<hex>/<nom>`` (jamais un
  chemin arbitraire : l'agent ne lit pas un fichier que l'utilisateur de la session n'a pas pu copier lui-même) ;
- ``update``    : favori, collection (= déplacer), étiquettes, notes, statut, partage ;
- ``delete``    : met en corbeille ; ``confirm: true`` obligatoire (la confirmation est faite dans l'interface) ;
- ``copy-shared`` : copie un fichier reçu dans la bibliothèque privée.

Pas de renommage : le site n'a aucune fonction de renommage de la bibliothèque (voir docs/desktop/SOCLE.md).

Projets Alpine scellés (.alsproj v2, .amsproj, .acsproj ; agent 1.53.5, docs/FORMATS_ALPINE.md). Le dépôt du Worker est
public : AUCUNE clé ici. Le site ouvre et scelle pour le Worker (routes signées ``self/alpine-formats/open`` et
``/seal``, session du propriétaire exigée) :

- ``alpine-open`` : fichier scellé déposé par worker.exe → manifeste + fichiers du projet écrits dans
  ``runtime/desktop/cache/alpine/<tâche>/`` (8 ouvertures gardées) ; ``main_path`` / ``program_path`` sont lus par le
  studio de bureau du format (Model Studio pour un .amsproj, CNC Studio pour le G-code d'un .acsproj) ;
- ``alpine-seal`` : fichier ordinaire déposé (projet Laser Studio v1, 3MF, G-code, SVG, DXF, STEP, STL) → fichier
  scellé par le site (clé 1) dans ``runtime/desktop/cache/alpine-out/<tâche>/`` ; worker.exe le copie où l'utilisateur
  l'a choisi, ou l'importe dans la bibliothèque.

Hors connexion, un fichier scellé ne s'ouvre pas : « Projet Alpine scellé : connecte ce Worker au site pour l'ouvrir. »
Les lignes scellées de la liste portent ``alpine`` = {label, format, title, error, tool} (outil de bureau qui l'ouvre).
"""
import base64
import hashlib
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request

if __package__:
    from . import desktop_actions as desk
    from .desktop_actions import TaskCancelled, TaskError, v_bool, v_choice, v_hex, v_int, v_object, v_session, v_text
    from .local_control import checked
    from .safety import is_link, protect_credentials
else:
    import desktop_actions as desk
    from desktop_actions import TaskCancelled, TaskError, v_bool, v_choice, v_hex, v_int, v_object, v_session, v_text
    from local_control import checked
    from safety import is_link, protect_credentials

TOOL = "library"
LIBRARY_PATH = "/api/worker-protocol/self/library"
DOWNLOAD_PATH = "/api/worker-protocol/self/library/download"
UPLOAD_PATH = "/api/worker-protocol/self/library/upload"
FAMILIES = ("3d", "laser", "cnc", "image")
SCOPES = ("private", "shared", "received")
SORTS = ("modified", "name", "favorite", "collection")
UPDATE_FIELDS = ("favorite", "collection", "tags", "notes", "status", "creator", "source_url", "license",
                 "relationship", "parent_model", "shared")
FILE_MAX = 256 * 1024 * 1024          # même plafond que la route d'import signée du site
ANSWER_INLINE_MAX = 256 * 1024        # au-delà, la réponse passe par runtime/desktop/out
CHUNK = 256 * 1024
_FILE_NAME = re.compile(r"[^\\/:*?\"<>|\x00-\x1f]{1,200}")
# Projets Alpine scellés : ouverts et scellés par le site (aucune clé dans le Worker).
ALPINE_OPEN_PATH = "/api/worker-protocol/self/alpine-formats/open"
ALPINE_SEAL_PATH = "/api/worker-protocol/self/alpine-formats/seal"
SEALED_MAGIC = b"ALPINE\x00"
SEALED_KINDS = ("alsproj", "amsproj", "acsproj")
SEALED_LABEL = "Projet Alpine scellé"
SEALED_OFFLINE = "Projet Alpine scellé : connecte ce Worker au site pour l'ouvrir."
SEAL_OFFLINE = ("Projet Alpine scellé : le site scelle le fichier, connecte ce Worker au site. Hors ligne, enregistre "
                "le projet en format lisible (non scellé).")
SEALED_SITE_MISSING = ("Le Dashboard n'ouvre pas encore les projets Alpine scellés pour worker.exe : il doit être "
                       "redémarré dans sa nouvelle version.")
# Outil de bureau qui ouvre chaque format (identifiants de desktop_actions.TOOL_MODULES / IDesktopTool.AgentTool).
SEALED_TOOLS = {"alsproj": "laser-studio", "amsproj": "model-studio", "acsproj": "cnc-studio"}
SEALED_OPEN_MAX = 64 * 1024 * 1024        # fichier remis au site (même borne que la route du site)
SEALED_ANSWER_MAX = 96 * 1024 * 1024      # réponse : 64 Mio de contenu au plus, en base64
SEAL_CONTENT_MAX = 48 * 1024 * 1024       # contenu remis à sceller
SEALED_FILE_MAX = 256 * 1024 * 1024       # fichier scellé rendu par le site
ALPINE_JOBS_KEPT = 8
GCODE_KINDS = ("nc", "ngc", "gcode", "tap")
# Fichier ordinaire → fichier principal du projet scellé (contrat docs/FORMATS_ALPINE.md).
SEAL_MEMBERS = {"alsproj": {"alsproj": "project.json", "json": "project.json"}, "amsproj": {"3mf": "model.3mf"},
                "acsproj": {**{kind: "program.nc" for kind in GCODE_KINDS}, "svg": "drawing.svg", "dxf": "drawing.dxf",
                            "step": "model.step", "stp": "model.step", "stl": "model.stl"}}
_TOOL_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")


def v_file_name(value):
    """Nom de fichier seul (jamais un chemin), comme le site le rend dans ses listes."""
    if type(value) is not str or not _FILE_NAME.fullmatch(value) or value in {".", ".."} or value != value.strip():
        raise ValueError("Nom de fichier invalide.")
    return value


def v_changes(value):
    value = v_object(depth=3, keys=len(UPDATE_FIELDS))(value)
    unknown = sorted(set(value) - set(UPDATE_FIELDS))
    if unknown or not value:
        raise ValueError("Modification non autorisée : " + (unknown[0][:40] if unknown else "aucun champ"))
    return value


def v_true(value):
    if value is not True:
        raise ValueError("Suppression non confirmée : confirme-la dans l'interface.")
    return value


# --- Appels au site ---------------------------------------------------------------------------------------------

def _version():
    module = __import__("sys").modules.get("agent") or __import__("sys").modules.get("worker_agent.agent")
    return str(getattr(module, "AGENT_VERSION", "") or "desktop")


def _urlopen(request, timeout):  # remplacé par les tests
    return urllib.request.urlopen(request, timeout=timeout)


def _site_error(error):
    """Erreur HTTP du site → TaskError lisible, avec le code du site (jamais l'adresse)."""
    if isinstance(error, urllib.error.HTTPError):
        code, message = "", ""
        try:
            body = json.loads(error.read(65536).decode("utf-8"))
            message, code = str(body.get("error") or ""), str(body.get("code") or "")
        except Exception:
            pass
        if error.code == 404 and not code:
            return TaskError("Le Dashboard n'a pas encore la Bibliothèque Desktop : il doit être redémarré dans sa "
                             "nouvelle version.", "SITE_MISSING")
        return TaskError(message[:300] or f"Erreur {error.code} du site.", code[:60] or f"HTTP_{error.code}")
    code = getattr(error, "code", None)
    if isinstance(code, int) and not isinstance(code, bool):  # AgentError de agent.request
        site_code = getattr(error, "site_code", "") or ""
        if code == 404 and not site_code:
            return TaskError("Le Dashboard n'a pas encore la Bibliothèque Desktop : il doit être redémarré dans sa "
                             "nouvelle version.", "SITE_MISSING")
        return TaskError(str(error)[:300] or f"Erreur {code} du site.", site_code[:60] or f"HTTP_{code}")
    return TaskError(f"Site injoignable ({type(error).__name__}) : vérifie la connexion internet du PC.", "UNREACHABLE")


def _json_call(agent, op, args, session):
    try:
        answer = agent.request(LIBRARY_PATH, "POST", {"op": op, "args": args, "session_token": session})
    except Exception as error:
        raise _site_error(error) from None
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        raise TaskError(str((answer or {}).get("error") or "Réponse du site illisible.")[:300], str((answer or {}).get("code") or ""))
    return answer.get("response") if isinstance(answer.get("response"), dict) else {}


def _signed(agent, path, data, headers):
    """Requête signée hors JSON (téléchargement, import), mêmes en-têtes que agent.request (User-Agent compris :
    Cloudflare refuse celui de Python-urllib)."""
    agent.reload_config()
    if __package__:
        from .identity import configure
    else:
        from identity import configure
    configure(agent.root, agent.config)
    server = agent.server
    parsed = urllib.parse.urlsplit(server)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}):
        raise TaskError("HTTPS est obligatoire hors connexion locale.", "HTTPS_REQUIRED")
    base = {"Authorization": "Bearer " + str(agent.config.get("token", "")), "X-Worker-ID": str(agent.config.get("worker_id") or ""),
            "User-Agent": "AlpineWorker/" + _version(), "Accept": "application/octet-stream, application/json"}
    base.update(headers)
    return protect_credentials(urllib.request.Request(server + path, data=data, headers=base, method="POST"))


def _finish(agent, value):
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > ANSWER_INLINE_MAX:
        return desk.result_file(agent, value)
    return value


# --- Actions ----------------------------------------------------------------------------------------------------

def _list(agent, params):
    session = params.pop("session_token")
    args = {key: value for key, value in params.items()}
    label = "Bibliothèque " + args.get("family", "3d")
    return desk.start_task(agent, TOOL, label, lambda task: _finish(agent, mark_sealed(_json_call(agent, "list", args, session))))


def _update(agent, params):
    session = params.pop("session_token")
    return desk.start_task(agent, TOOL, "Modifier " + params["name"],
                           lambda task: _json_call(agent, "update", {"name": params["name"], "changes": params["changes"]}, session))


def _delete(agent, params):
    session = params.pop("session_token")
    return desk.start_task(agent, TOOL, "Corbeille : " + params["name"],
                           lambda task: _json_call(agent, "delete", {"name": params["name"], "confirm": True}, session))


def _copy(agent, params):
    session = params.pop("session_token")
    return desk.start_task(agent, TOOL, "Copier " + params["name"],
                           lambda task: _json_call(agent, "copy-shared", {"name": params["name"]}, session))


def cache_folder(agent):
    return desk.runtime_folder(agent, "cache", "library")


def _download_task(agent, params):
    return desk.start_task(agent, TOOL, "Télécharger " + params["name"], _download_work(agent, dict(params)))


def _fetch(agent, task, session, name, what, report=True, limit=FILE_MAX):
    """Télécharge ``name`` (fichier ou aperçu) dans le cache Desktop ; rend la fiche du fichier en cache."""
    cache_folder(agent)
    digest_name = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
    target = checked(agent.root, f"runtime/desktop/cache/library/{digest_name}-{'apercu-' if what == 'thumbnail' else ''}{name}")
    partial = target.with_name(target.name + ".part")
    body = json.dumps({"name": name, "what": what, "session_token": session}).encode("utf-8")
    total, digest = 0, hashlib.sha256()
    try:
        request = _signed(agent, DOWNLOAD_PATH, body, {"Content-Type": "application/json"})
        with _urlopen(request, 3600) as response, open(partial, "wb") as output:
            expected = int(response.headers.get("Content-Length") or 0)
            while True:
                if task.cancelled():
                    raise TaskCancelled()
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise TaskError("Fichier trop volumineux pour le cache Desktop.", "TOO_LARGE")
                digest.update(chunk)
                output.write(chunk)
                if report:
                    task.update(total * 100 // expected if expected else None, f"{total // 1024} Kio reçus")
        if is_link(target):
            raise TaskError("Cache Desktop invalide (lien).", "CACHE_LINK")
        os.replace(partial, target)
    except (TaskError, TaskCancelled):
        raise
    except Exception as error:
        raise _site_error(error) from None
    finally:
        try:
            partial.unlink()
        except OSError:
            pass
    return {"name": name, "what": what, "path": str(target), "bytes": total, "sha256": digest.hexdigest()}


def _download_work(agent, params):
    session = params.pop("session_token")
    name, what = params["name"], params.get("what", "file")
    return lambda task: _fetch(agent, task, session, name, what)


THUMBNAILS_MAX = 60                    # une page de la grille au plus
THUMBNAIL_FILE_MAX = 16 * 1024 * 1024  # image servie comme son propre aperçu : bornée


def v_thumbnail_items(value):
    """Liste d'au plus THUMBNAILS_MAX ``{"name", "what"}`` (what = thumbnail, ou file pour une image)."""
    if type(value) is not list or not value or len(value) > THUMBNAILS_MAX:
        raise ValueError(f"Liste d'aperçus invalide (1 à {THUMBNAILS_MAX} fichiers).")
    cleaned, seen = [], set()
    for item in value:
        if type(item) is not dict or set(item) - {"name", "what"} or "name" not in item:
            raise ValueError("Aperçu demandé invalide.")
        name, what = v_file_name(item["name"]), v_choice("file", "thumbnail")(item.get("what", "thumbnail"))
        if (name, what) not in seen:
            seen.add((name, what))
            cleaned.append({"name": name, "what": what})
    return cleaned


def _thumbnails(agent, params):
    """Vignettes d'une page de la grille en UNE tâche : chaque fichier par la même route signée que ``download``.
    Un aperçu manquant (404 du site) n'arrête pas les autres ; la session expirée, si (même refus pour tous)."""
    session = params.pop("session_token")
    items = params["items"]

    def work(task):
        done = []
        for index, item in enumerate(items):
            if task.cancelled():
                raise TaskCancelled()
            task.update(index * 100 // len(items), f"Aperçu {index + 1} / {len(items)}")
            try:
                limit = THUMBNAIL_FILE_MAX if item["what"] == "file" else FILE_MAX
                found = _fetch(agent, task, session, item["name"], item["what"], report=False, limit=limit)
                done.append({"name": found["name"], "what": found["what"], "path": found["path"], "bytes": found["bytes"]})
            except TaskError as error:
                if error.code in {"WORKER_EXE_SESSION_REQUIRED", "WORKER_EXE_SESSION_INVALID", "LIBRARY_OWNER_ONLY",
                                  "UNREACHABLE", "SITE_MISSING", "HTTPS_REQUIRED"}:
                    raise
                done.append({"name": item["name"], "what": item["what"], "error": str(error)[:200], "code": error.code})
        return _finish(agent, {"items": done})
    return desk.start_task(agent, TOOL, f"Aperçus ({len(items)})", work)


def inbox_file(agent, staged, name):
    """Fichier déposé par worker.exe : ``runtime/desktop/inbox/<staged>/<name>``, fichier ordinaire, jamais un lien."""
    path = checked(agent.root, f"runtime/desktop/inbox/{staged}/{name}")
    if is_link(path.parent) or is_link(path) or not path.is_file():
        raise ValueError("Fichier à importer introuvable : dépose-le de nouveau.")
    size = path.stat().st_size
    if size <= 0 or size > FILE_MAX:
        raise ValueError(f"Fichier à importer vide ou trop gros (au plus {FILE_MAX // 1024 // 1024} Mio).")
    return path


def _upload(agent, params):
    session = params.pop("session_token")
    staged, name = params["staged"], params["name"]
    path = inbox_file(agent, staged, name)   # vérifié tout de suite : un refus n'ouvre pas de tâche

    def work(task):
        size = path.stat().st_size
        query = urllib.parse.urlencode({"name": name})
        try:
            with open(path, "rb") as source:
                request = _signed(agent, UPLOAD_PATH + "?" + query, source,
                                  {"Content-Type": "application/octet-stream", "Content-Length": str(size),
                                   "X-Worker-Exe-Session": session})
                task.update(None, f"Envoi de {size // 1024} Kio")
                with _urlopen(request, 3600) as response:
                    answer = json.loads(response.read(1024 * 1024).decode("utf-8"))
        except (TaskError, TaskCancelled):
            raise
        except Exception as error:
            raise _site_error(error) from None
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)   # dépôt à usage unique, réussi ou non
        if not isinstance(answer, dict) or answer.get("ok") is not True:
            raise TaskError(str((answer or {}).get("error") or "Import refusé par le site.")[:300], str((answer or {}).get("code") or ""))
        return answer.get("response") if isinstance(answer.get("response"), dict) else {}
    return desk.start_task(agent, TOOL, "Importer " + name, work)


def _cache(agent, params):
    """Fichiers déjà téléchargés dans le cache Desktop (lecture locale, sans le site)."""
    folder = cache_folder(agent)
    items = []
    for path in sorted(folder.iterdir(), key=lambda item: item.name):
        if path.is_file() and not is_link(path) and not path.name.endswith(".part"):
            stat = path.stat()
            items.append({"file": path.name, "path": str(path), "bytes": stat.st_size, "modified": stat.st_mtime})
    return {"folder": str(folder), "items": items[:500]}


# --- Projets Alpine scellés (le site ouvre et scelle : aucune clé ici) -----------------------------------------------

def is_sealed(data):
    return isinstance(data, (bytes, bytearray)) and bytes(data[:len(SEALED_MAGIC)]) == SEALED_MAGIC


def kind_of(name):
    lower = str(name or "").lower()
    return "3mf" if lower.endswith(".gcode.3mf") else os.path.splitext(lower)[1].lstrip(".")


def mark_sealed(data):
    """Lignes de liste scellées (champ ``sealed`` du site) → ``alpine`` : libellé, titre, erreur et outil de bureau."""
    items = data.get("items") if isinstance(data, dict) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        sealed, kind = item.get("sealed"), kind_of(item.get("name"))
        if isinstance(sealed, dict) and kind in SEALED_KINDS:
            item["alpine"] = {"label": str(sealed.get("label") or SEALED_LABEL)[:60], "format": kind,
                              "title": str(sealed.get("title") or "")[:120], "error": str(sealed.get("error") or "")[:300],
                              "tool": SEALED_TOOLS[kind]}
    return data


def _alpine_error(error, offline):
    failure = _site_error(error)
    if failure.code == "UNREACHABLE":
        return TaskError(offline, "SEALED_OFFLINE")
    if failure.code == "SITE_MISSING":
        return TaskError(SEALED_SITE_MISSING, "SITE_MISSING")
    return failure


def _safe_member(path):
    """Chemin d'un fichier du projet tel que le site l'a validé (relatif, sans « .. », sans « : »), revérifié ici."""
    if (type(path) is not str or not 0 < len(path) <= 160 or "\\" in path or ":" in path or path.startswith("/")
            or any(part in ("", ".", "..") for part in path.split("/")) or any(ord(char) < 32 for char in path)):
        raise TaskError("Chemin interdit dans le projet Alpine.", "SEALED_PATH")
    return path


def sealed_open(agent, session, data, expected="", name=""):
    """Remet un fichier scellé au site, qui l'ouvre : (manifeste public, {chemin: octets}). Lève TaskError."""
    if not is_sealed(data):
        raise TaskError("Ce n'est pas un fichier Alpine scellé.", "NOT_SEALED")
    if len(data) > SEALED_OPEN_MAX:
        raise TaskError(f"Fichier Alpine trop volumineux pour une ouverture depuis worker.exe "
                        f"({SEALED_OPEN_MAX // 1024 // 1024} Mo au plus) : ouvre-le dans le studio du site.", "TOO_LARGE")
    query = urllib.parse.urlencode([(key, value) for key, value in (("expected", expected), ("name", name)) if value])
    try:
        request = _signed(agent, ALPINE_OPEN_PATH + ("?" + query if query else ""), bytes(data),
                          {"Content-Type": "application/octet-stream", "Content-Length": str(len(data)),
                           "X-Worker-Exe-Session": session, "Accept": "application/json"})
        with _urlopen(request, 600) as response:
            raw = response.read(SEALED_ANSWER_MAX + 1)
    except (TaskError, TaskCancelled):
        raise
    except Exception as error:
        raise _alpine_error(error, SEALED_OFFLINE) from None
    if len(raw) > SEALED_ANSWER_MAX:
        raise TaskError("Réponse du site trop volumineuse.", "TOO_LARGE")
    try:
        answer = json.loads(raw.decode("utf-8"))
    except ValueError:
        answer = None
    if not isinstance(answer, dict) or answer.get("ok") is not True or not isinstance(answer.get("project"), dict):
        raise TaskError(str((answer or {}).get("error") or "Réponse du site illisible.")[:300], str((answer or {}).get("code") or ""))
    project = answer["project"]
    contents = project.pop("contents", None)
    if not isinstance(contents, dict) or str(project.get("main") or "") not in contents:
        raise TaskError("Réponse du site illisible.", "SEALED_ANSWER")
    files = {}
    for path, encoded in contents.items():
        try:
            files[_safe_member(path)] = base64.b64decode(str(encoded), validate=True)
        except (ValueError, TypeError):
            raise TaskError("Réponse du site illisible.", "SEALED_ANSWER") from None
    return project, files


def sealed_seal(agent, session, fmt, manifest, files, name=""):
    """Remet un contenu au site, qui le scelle avec sa clé : octets du fichier scellé. Lève TaskError."""
    total = sum(len(content) for content in files.values())
    if total > SEAL_CONTENT_MAX:
        raise TaskError(f"Projet trop volumineux pour un scellement depuis worker.exe ({SEAL_CONTENT_MAX // 1024 // 1024} Mo "
                        "au plus) : enregistre-le en format lisible ou scelle-le depuis le studio du site.", "TOO_LARGE")
    body = json.dumps({"format": fmt, "manifest": manifest, "name": name, "session_token": session,
                       "files": {path: base64.b64encode(content).decode("ascii") for path, content in files.items()}},
                      ensure_ascii=False).encode("utf-8")
    try:
        request = _signed(agent, ALPINE_SEAL_PATH, body, {"Content-Type": "application/json", "Content-Length": str(len(body))})
        with _urlopen(request, 600) as response:
            sealed = response.read(SEALED_FILE_MAX + 1)
    except (TaskError, TaskCancelled):
        raise
    except Exception as error:
        raise _alpine_error(error, SEAL_OFFLINE) from None
    if len(sealed) > SEALED_FILE_MAX or not is_sealed(sealed):
        raise TaskError("Réponse du site illisible : aucun fichier scellé reçu.", "SEALED_ANSWER")
    return sealed


def _alpine_job(agent, folder_name, kept=ALPINE_JOBS_KEPT):
    """Dossier neuf ``runtime/desktop/cache/<folder_name>/<hex>`` ; les plus anciens partent (``kept`` gardés)."""
    folder = desk.runtime_folder(agent, "cache", folder_name)
    jobs = sorted((path for path in folder.iterdir() if path.is_dir() and not is_link(path)), key=lambda path: path.stat().st_mtime)
    for old in jobs[:max(0, len(jobs) - kept + 1)]:
        shutil.rmtree(old, ignore_errors=True)
    job = hashlib.sha256(os.urandom(16)).hexdigest()[:32]
    path = checked(agent.root, f"runtime/desktop/cache/{folder_name}/{job}")
    path.mkdir(parents=True)
    return path


def _local_name(value, fallback):
    """Nom de fichier local sûr (accents permis) : tout autre caractère → « _ »."""
    cleaned = re.sub(r"[^\w.\- ]+", "_", str(value or ""), flags=re.UNICODE).strip(" ._")[:120]
    return cleaned or fallback


def staged_bytes(agent, staged, name, limit):
    """Lit puis efface le dépôt ``runtime/desktop/inbox/<staged>/<name>`` (fichier ordinaire, ``limit`` octets au plus)."""
    path = checked(agent.root, f"runtime/desktop/inbox/{staged}/{name}")
    try:
        if is_link(path.parent) or is_link(path) or not path.is_file():
            raise ValueError("Fichier déposé introuvable : recommence l'opération.")
        size = path.stat().st_size
        if size <= 0 or size > limit:
            raise ValueError(f"Fichier déposé vide ou trop gros ({limit // 1024 // 1024} Mio au plus).")
        return path.read_bytes()
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)


def write_opened(agent, project, files, source_name):
    """Fichiers d'un projet ouvert par le site → ``runtime/desktop/cache/alpine/<hex>/`` ; rend la fiche pour worker.exe.
    Le fichier principal prend le nom du fichier d'origine (« lithophanie.amsproj » → « lithophanie.3mf »)."""
    folder = _alpine_job(agent, "alpine")
    stem = _local_name(os.path.splitext(os.path.basename(str(source_name or "")))[0], "projet")
    main, program = str(project.get("main") or ""), str(((project.get("program") or {}) if isinstance(project.get("program"), dict) else {}).get("path") or "")
    written, used = [], set()
    for path, content in sorted(files.items()):
        extension = os.path.splitext(path)[1].lower()
        local = stem + extension if path == main else _local_name(path.replace("/", "__"), "fichier")
        while local.lower() in used:
            local = "_" + local
        used.add(local.lower())
        target = checked(agent.root, f"runtime/desktop/cache/alpine/{folder.name}/{local}")
        target.write_bytes(content)
        written.append({"path": path, "local": str(target), "size": len(content)})
    by_path = {item["path"]: item["local"] for item in written}
    fmt = str(project.get("format") or "")
    return {"format": fmt, "label": SEALED_LABEL, "format_label": str(project.get("label") or ""),
            "title": str(project.get("title") or "")[:120], "tool": str(project.get("tool") or "")[:40],
            "app": str(project.get("app") or "")[:60], "origin": str(project.get("origin") or "")[:30],
            "created": str(project.get("created") or "")[:40], "hints": project.get("hints") if isinstance(project.get("hints"), dict) else {},
            "main": main, "main_kind": kind_of(main), "main_path": by_path.get(main, ""),
            "program_path": by_path.get(program, "") if program else "", "preview_path": by_path.get("preview.png", ""),
            "studio_tool": SEALED_TOOLS.get(fmt, ""), "files": written, "folder": str(folder)}


def _alpine_open(agent, params):
    session = params.pop("session_token")
    name, expected = params["name"], params.get("expected", "")
    data = staged_bytes(agent, params["staged"], name, SEALED_OPEN_MAX)   # lu tout de suite : le dépôt n'attend pas
    if not is_sealed(data):
        raise ValueError("Ce n'est pas un fichier Alpine scellé.")

    def work(task):
        task.update(None, "Ouverture par le site")
        project, files = sealed_open(agent, session, data, expected, name)
        return _finish(agent, write_opened(agent, project, files, name))
    return desk.start_task(agent, TOOL, "Ouvrir le projet Alpine " + name, work)


def seal_files(fmt, name, data):
    """Fichier ordinaire → {chemin: octets} d'un projet ``fmt`` (le site revérifie tout)."""
    member = SEAL_MEMBERS.get(fmt, {}).get(kind_of(name))
    if member is None:
        raise ValueError({"alsproj": "Projet Alpine Laser Studio : un fichier .alsproj lisible (v1) est attendu.",
                          "amsproj": "Projet Alpine Model Studio : un modèle 3MF est attendu.",
                          "acsproj": "Un .acsproj contient un G-code (.nc), un dessin (SVG, DXF) ou un modèle (STEP, STL)."}[fmt])
    if is_sealed(data):
        raise ValueError("Ce fichier est déjà un fichier Alpine scellé.")
    return {member: data}, member


def write_sealed(agent, sealed, name, fmt):
    folder = _alpine_job(agent, "alpine-out")
    local = _local_name(os.path.splitext(os.path.basename(str(name or "")))[0], "projet-alpine") + "." + fmt
    target = checked(agent.root, f"runtime/desktop/cache/alpine-out/{folder.name}/{local}")
    target.write_bytes(sealed)
    return {"path": str(target), "name": local, "bytes": len(sealed), "format": fmt, "sealed": True, "label": SEALED_LABEL}


def _alpine_seal(agent, params):
    session = params.pop("session_token")
    fmt, name = params["format"], params["name"]
    data = staged_bytes(agent, params["staged"], name, SEAL_CONTENT_MAX)
    files, member = seal_files(fmt, name, data)
    tool = params.get("tool", "")
    if tool and not _TOOL_NAME.fullmatch(tool):
        raise ValueError("Nom d'outil invalide dans le manifeste.")
    manifest = {"main": member, "title": params.get("title") or os.path.splitext(name)[0], "tool": tool}

    def work(task):
        task.update(None, "Scellement par le site")
        sealed = sealed_seal(agent, session, fmt, manifest, files, name)
        return write_sealed(agent, sealed, name, fmt)
    return desk.start_task(agent, TOOL, "Sceller " + name, work)


_SESSION = {"session_token": v_session}
DESKTOP_ACTIONS = {
    "list": {"handler": _list, "read": True, "params": {
        **_SESSION, "family": v_choice(*FAMILIES), "scope": v_choice(*SCOPES), "page": v_int(1, 100000),
        "per_page": v_int(1, 100), "q": v_text(200), "collection": v_text(200), "status": v_text(40),
        "favorite": v_bool, "problems": v_bool, "sort": v_choice(*SORTS)}},
    "download": {"handler": _download_task, "read": True,
                 "params": {**_SESSION, "name": v_file_name, "what": v_choice("file", "thumbnail")}},
    "thumbnails": {"handler": _thumbnails, "read": True, "params": {**_SESSION, "items": v_thumbnail_items}},
    "upload": {"handler": _upload, "read": False, "params": {**_SESSION, "staged": v_hex(32), "name": v_file_name}},
    "update": {"handler": _update, "read": False, "params": {**_SESSION, "name": v_file_name, "changes": v_changes}},
    "delete": {"handler": _delete, "read": False, "params": {**_SESSION, "name": v_file_name, "confirm": v_true}},
    "copy-shared": {"handler": _copy, "read": False, "params": {**_SESSION, "name": v_file_name}},
    "cache": {"handler": _cache, "read": True, "params": {}},
    # Agent 1.53.5 : projets Alpine scellés, ouverts et scellés par le site.
    "alpine-open": {"handler": _alpine_open, "read": True,
                    "params": {**_SESSION, "staged": v_hex(32), "name": v_file_name, "expected": v_choice(*SEALED_KINDS)}},
    "alpine-seal": {"handler": _alpine_seal, "read": True,
                    "params": {**_SESSION, "staged": v_hex(32), "name": v_file_name, "format": v_choice(*SEALED_KINDS),
                               "title": v_text(120), "tool": v_text(40)}},
}
# Champs obligatoires par action (le validateur générique ne connaît que les champs permis).
REQUIRED = {"list": ("session_token", "family"), "download": ("session_token", "name"), "thumbnails": ("session_token", "items"),
            "upload": ("session_token", "staged", "name"), "update": ("session_token", "name", "changes"),
            "delete": ("session_token", "name", "confirm"), "copy-shared": ("session_token", "name"), "cache": (),
            "alpine-open": ("session_token", "staged", "name"), "alpine-seal": ("session_token", "staged", "name", "format")}


def _required(name, handler):
    def run(agent, params):
        missing = [field for field in REQUIRED[name] if field not in params]
        if missing:
            raise ValueError("Paramètre manquant : " + missing[0] + (" (connecte-toi dans worker.exe)" if missing[0] == "session_token" else ""))
        return handler(agent, dict(params))
    return run


for _name, _spec in DESKTOP_ACTIONS.items():
    _spec["handler"] = _required(_name, _spec["handler"])
del _name, _spec
