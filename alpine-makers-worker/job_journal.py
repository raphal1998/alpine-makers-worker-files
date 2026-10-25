"""Journal local des jobs du Worker (capacité job_journal_v1).

Chaque job terminé sur CE Worker — réussi, échoué ou annulé — ajoute une ligne JSON au document
``data/journal-des-jobs.jsonl`` du dossier du Worker (``data`` est conservé par « Mettre à jour Worker » :
UPDATE_PRESERVED de agent.py, et par le nettoyage léger). Trois sources :

- jobs du site traités par l'agent (génération d'images et 3D, Labo image, convertisseur, préparation CNC, tranchage,
  Laser Studio, CNC Studio) : ``record_site_job``, appelé par ``Agent._job_status`` à l'état terminal confirmé ;
- calculs lancés depuis worker.exe (outils d'Alpine Makers Desktop) : ``record_desktop_task``, appelé par
  ``desktop_actions.start_task(..., journal=True)`` à la fin de la tâche ;
- gravures et usinages envoyés par ce Worker à une machine GRBL : ``record_machine_job`` (equipment_runtime).

Une ligne, au format EXACT de l'historique des jobs du site (job_history.py) ::

    {"job_id", "worker_id", "worker_name", "tool", "label", "account": {"id", "pseudo"},
     "status": "termine" | "echoue" | "annule",
     "created_at", "started_at", "finished_at",          # secondes epoch UTC (time.time()), ou null
     "duration_s",                                       # secondes (une décimale) ou null
     "outputs": [{"kind": "image" | "model3d" | "gcode" | "file" | "text", "name", "url", "local_path"}],
     "error"}                                            # court, sans chemin, adresse, e-mail ni jeton

``url`` est un lien RELATIF du site (« /api/workers/jobs/<job>/artifacts/<nom> »), ``local_path`` (propre au Worker)
un chemin RELATIF au dossier du Worker (barres obliques « / »), vide si le fichier n'existe pas sur ce PC.
``account`` : compte demandeur quand le site le donne dans le job (identifiant et pseudo, jamais une adresse e-mail) ;
vide pour un calcul lancé depuis worker.exe ou une gravure, que l'agent ne rattache à aucun compte. Les jours et heures
des filtres se lisent à l'heure locale du PC.

Écriture : ajout d'une ligne sous verrou (fsync), rotation au-delà de ``MAX_FILE_BYTES`` vers
``journal-des-jobs.1.jsonl`` … ``.{ARCHIVES}.jsonl`` (le plus ancien part). Une suppression (worker.exe) réécrit
chaque fichier touché par remplacement atomique ; elle n'efface rien sur le site. Le journal ne commande rien : une
écriture qui échoue écrit une ligne dans la sortie de l'agent et n'arrête jamais un job.

Actions locales (``local_control.LAZY_ACTIONS``) : ``job-journal`` (lecture filtrée et paginée) et
``job-journal-delete`` (tout, les entrées filtrées ou une sélection de job_id ; ``confirm`` exigé). Les droits
(propriétaire, ou co-propriétaire avec « jobs ») sont appliqués par worker.exe, comme pour les autres outils locaux.
"""
import datetime as _dt
import json
import os
import re
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

if __package__:
    from .local_control import checked, v_bool, v_choice, v_hex
    from .safety import is_link
else:
    from local_control import checked, v_bool, v_choice, v_hex
    from safety import is_link

CAPABILITY = "job_journal_v1"
JOURNAL = "data/journal-des-jobs.jsonl"
ARCHIVE = "data/journal-des-jobs.{}.jsonl"
ARCHIVES = 3                          # fichiers archivés gardés en plus du journal courant
MAX_FILE_BYTES = 4 * 1024 * 1024      # rotation au-delà (environ 5 000 jobs par fichier)
LINE_MAX_BYTES = 16 * 1024
OUTPUTS_MAX = 24
PAGE_SIZE_MAX = 200
IDS_MAX = 40                          # job_id par demande de suppression en paramètres (au-delà : args_file)
IDS_FILE_MAX = 5000
RESULT_FILE_MAX = 64 * 1024 * 1024
STATUSES = ("termine", "echoue", "annule")
STATE_STATUS = {"completed": "termine", "done": "termine", "failed": "echoue", "cancelled": "annule"}
KINDS = ("image", "model3d", "gcode", "file", "text")
# Familles d'outils proposées par le filtre de worker.exe ; « tool » exact reste possible. Un job du site garde l'outil
# du site (tool_id : Labo image = image_generation, préparation CNC = converter), comme son historique.
FAMILIES = {
    "images": ("image_generation", "desktop:image-lab"),
    "3d": ("ai3d",),
    "convertisseur": ("converter", "desktop:converter"),
    "tranchage": ("model-studio", "desktop:model-studio"),
    "laser": ("laser", "laser-studio", "desktop:laser-studio"),
    "cnc": ("cnc", "cnc-studio", "desktop:cnc-studio"),
    "worker.exe": (),                 # tous les « desktop:<outil> »
}
# Libellés des jobs du site : les mêmes que job_history.job_label côté site (tests/test_worker_job_journal.py compare),
# pour que le journal du Worker et l'historique du site concordent ligne à ligne.
TOOL_NAMES = {"image_generation": "Générateur image IA", "ai3d": "Générateur 3D IA", "converter": "Convertisseur 3D",
              "printguard": "Surveillance IA", "model-studio": "Alpine Model Studio", "laser-studio": "Alpine Laser Studio",
              "cnc-studio": "Alpine CNC Studio", "laser": "Gravure laser", "cnc": "Usinage CNC"}
LAB_CAPABILITIES = {"image-upscale": "agrandissement", "face-restore": "restauration de visages", "background-removal": "détourage",
                    "image-caption": "description de l’image", "image-maps": "cartes et propriétés",
                    "control-map-depth": "carte de profondeur", "control-map-pose": "carte de pose"}
LASER_OPERATIONS = {"parse": "Analyse du dessin laser", "plan": "Plan de découpe laser", "gcode": "G-code laser", "frame": "Cadrage laser",
                    "simulate": "Simulation laser", "raster": "Trame d’image laser", "script": "Script laser (assistant de code)",
                    "assistant": "Assistant de code laser", "assistant_status": "État de l’assistant de code laser"}
CNC_OPERATIONS = {"analyze": "Analyse de G-code CNC", "simulate": "Simulation CNC"}
DESKTOP_LABELS = {"converter": "Convertisseur", "image-lab": "Labo image", "model-studio": "Model Studio",
                  "cnc-studio": "CNC Studio", "laser-studio": "Laser Studio", "library": "Bibliothèque",
                  "equipment": "Équipements"}
# Type d'un fichier produit : mêmes suffixes que l'historique du site (job_history.output_kind).
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
MODEL_SUFFIXES = (".glb", ".gltf", ".obj", ".stl", ".3mf", ".ply", ".fbx", ".step", ".stp", ".iges", ".igs", ".fcstd",
                  ".brep", ".off", ".dae")
GCODE_SUFFIXES = (".gcode.3mf", ".gcode", ".gco", ".bgcode", ".nc", ".ngc", ".tap", ".cnc")
TEXT_SUFFIXES = (".json", ".txt", ".md", ".csv", ".log")
OUTPUT_KEYS = ("path", "gcode_path", "output_path")         # clés d'un résultat Desktop qui désignent un fichier produit
OUTPUT_LISTS = ("files", "outputs")

_LOCK = threading.Lock()
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_TOOL = re.compile(r"[a-z0-9][a-z0-9_:.-]{0,39}")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_HOUR = re.compile(r"(\d{2}):(\d{2})")
_EMAIL = re.compile(r"[^\s@<>\"'()]+@[^\s@<>\"'()]+\.[A-Za-z]{2,}")
_URL = re.compile(r"(?i)\b(?:https?|ftps?|file)://\S+")
_WINDOWS_PATH = re.compile(r"(?:\b[A-Za-z]:[\\/]|\\\\)[^\s\"'<>|]*")
_BACKSLASH_WORD = re.compile(r"[^\s\"'<>|]*\\[^\s\"'<>|]*")   # reste d'un chemin à espaces (« Prêt\Desktop\… »)
_POSIX_PATH = re.compile(r"(?<![\w.<])/(?:home|Users|root|tmp|var|opt|mnt|srv|etc|media|private)/\S*")
_KNOWN_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{16,}|ghp_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}"
                          r"|xox[abp]-[A-Za-z0-9\-]{10,}|hf_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16})")
_SECRET = re.compile(r"(?i)\b(bearer|token|jeton|password|passwd|mot de passe|secret|api[_ -]?key|authorization)"
                     r"(?:\s*[:=]\s*|\s+(?=[A-Za-z0-9._~+/=-]{12,}))(?:bearer\s+)?[^\s,;]+")
_ACCOUNT = re.compile(r"[\w][\w .'\-]{0,59}")
_ACCOUNT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,80}")
MAX_TIMESTAMP = 4_102_444_800          # 2100-01-01, même borne que le site
_failure_logged = False


# --- Mise en forme et nettoyage ---------------------------------------------------------------------------------------

def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def epoch(value):
    """Instant en secondes epoch UTC (à la milliseconde), comme le site, ou None. Une date ISO 8601 est relue aussi."""
    number = _number(value)
    if number is None and isinstance(value, str) and 10 <= len(value) <= 40:
        try:
            moment = _dt.datetime.fromisoformat(value)
            number = (moment if moment.tzinfo else moment.astimezone()).timestamp()   # sans décalage : heure du PC
        except (OverflowError, OSError, ValueError):
            number = None
    if number is None or number != number or not 0 < number < MAX_TIMESTAMP:
        return None
    return round(number, 3)


def local_moment(value):
    """Secondes epoch → date et heure locales du PC (aware) : c'est sur elles que portent les filtres jour / heure."""
    number = epoch(value)
    if number is None:
        return None
    try:
        return _dt.datetime.fromtimestamp(number).astimezone()
    except (OverflowError, OSError, ValueError):
        return None


def clean_text(value, limit):
    text = _CONTROL.sub(" ", str(value if value is not None else ""))
    return " ".join(text.split())[:limit]


def clean_error(value, root=None):
    """Message d'échec court : ni dossier du Worker, ni chemin absolu, ni adresse, ni e-mail, ni jeton."""
    text = clean_text(value, 2000)
    if not text:
        return ""
    if root:
        for form in {str(root), str(root).replace("\\", "/")}:
            if len(form) > 3:
                text = text.replace(form, "<Worker>")
    text = _URL.sub("<adresse>", text)
    text = _EMAIL.sub("<e-mail>", text)
    text = _KNOWN_TOKEN.sub("<masqué>", text)
    text = _SECRET.sub(lambda match: match.group(1) + " <masqué>", text)
    text = _WINDOWS_PATH.sub("<chemin>", text)
    text = _BACKSLASH_WORD.sub("<chemin>", text)
    text = _POSIX_PATH.sub("<chemin>", text)
    text = re.sub(r"<chemin>(?:\s*<chemin>)+", "<chemin>", text)
    return text[:300]


def clean_account(value):
    """Compte demandeur au format du site, {"id", "pseudo"} : jamais une adresse e-mail ; champ vide si douteux.
    Un pseudo seul (chaîne) est accepté et rangé dans « pseudo »."""
    raw_id, raw_pseudo = (value.get("id"), value.get("pseudo")) if isinstance(value, dict) else ("", value)
    account_id = str(raw_id or "") if isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool) else ""
    pseudo = clean_text(raw_pseudo, 60) if isinstance(raw_pseudo, str) else ""
    return {"id": account_id if _ACCOUNT_ID.fullmatch(account_id) else "",
            "pseudo": pseudo if pseudo and "@" not in pseudo and _ACCOUNT.fullmatch(pseudo) else ""}


def clean_job_id(value):
    text = str(value or "")
    return text if _ID.fullmatch(text) else ""


def output_kind(name):
    lower = str(name or "").lower()
    if lower.endswith(GCODE_SUFFIXES):
        return "gcode"
    if lower.endswith(IMAGE_SUFFIXES):
        return "image"
    if lower.endswith(MODEL_SUFFIXES):
        return "model3d"
    if lower.endswith(TEXT_SUFFIXES):
        return "text"
    return "file"


def _clean_url(value):
    url = str(value or "")
    if not url or len(url) > 400 or _CONTROL.search(url) or " " in url:
        return ""
    if url.startswith("/") and not url.startswith("//"):
        return url
    parsed = urllib.parse.urlsplit(url)
    return url if parsed.scheme in ("http", "https") and parsed.netloc and not parsed.username and not parsed.password else ""


def _clean_local_path(value):
    text = str(value or "").replace("\\", "/")
    if not text or len(text) > 260 or _CONTROL.search(text) or text.startswith("/") or ":" in text:
        return ""
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return ""
    return text


def clean_output(item):
    if not isinstance(item, dict):
        return None
    name = clean_text(item.get("name"), 120)
    local = _clean_local_path(item.get("local_path"))
    url = _clean_url(item.get("url"))
    if not name:
        name = clean_text((local or url).rsplit("/", 1)[-1], 120)
    if not name:
        return None
    kind = item.get("kind") if item.get("kind") in KINDS else output_kind(name)
    return {"kind": kind, "name": name, "url": url, "local_path": local}


def make_entry(*, job_id, worker_id, worker_name, tool, label, account=None, status, created_at=None, started_at=None,
               finished_at=None, outputs=(), error="", root=None):
    """Ligne du journal, nettoyée et bornée. Les instants sont des secondes epoch UTC (time.time())."""
    if status not in STATUSES:
        raise ValueError("Statut de job inconnu.")
    tool = str(tool or "")
    if not _TOOL.fullmatch(tool):
        tool = "autre"
    started, finished = epoch(started_at), epoch(finished_at)
    duration = round(max(0.0, finished - started), 1) if started and finished else None
    cleaned, seen = [], set()
    for item in outputs or ():
        output = clean_output(item)
        if output is None:
            continue
        key = (output["local_path"], output["url"], output["name"])
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(output)
        if len(cleaned) >= OUTPUTS_MAX:
            break
    return {"job_id": clean_job_id(job_id) or "local-" + uuid.uuid4().hex, "worker_id": clean_text(worker_id, 80),
            "worker_name": clean_text(worker_name, 80), "tool": tool, "label": clean_text(label, 160) or TOOL_NAMES.get(tool, tool),
            "account": clean_account(account), "status": status, "created_at": epoch(created_at), "started_at": started,
            "finished_at": finished, "duration_s": duration, "outputs": cleaned,
            "error": clean_error(error, root) if status != "termine" else ""}


def normalize(value):
    """Ligne relue (fichier éditable à la main) : rendue au schéma, ou None si elle n'en est pas une."""
    if not isinstance(value, dict) or value.get("status") not in STATUSES:
        return None
    job_id = clean_job_id(value.get("job_id"))
    if not job_id:
        return None
    duration = _number(value.get("duration_s"))
    entry = {"job_id": job_id, "worker_id": clean_text(value.get("worker_id"), 80),
             "worker_name": clean_text(value.get("worker_name"), 80),
             "tool": value.get("tool") if isinstance(value.get("tool"), str) and _TOOL.fullmatch(value["tool"]) else "autre",
             "label": clean_text(value.get("label"), 160), "account": clean_account(value.get("account")),
             "status": value["status"], "duration_s": round(duration, 1) if duration is not None and duration >= 0 else None,
             "outputs": [], "error": clean_text(value.get("error"), 300)}
    for key in ("created_at", "started_at", "finished_at"):
        entry[key] = epoch(value.get(key))
    for item in value.get("outputs") if isinstance(value.get("outputs"), list) else ():
        output = clean_output(item)
        if output is not None and len(entry["outputs"]) < OUTPUTS_MAX:
            entry["outputs"].append(output)
    if not entry["label"]:
        entry["label"] = TOOL_NAMES.get(entry["tool"], entry["tool"])
    return entry


def moment_epoch(entry):
    """Instant d'un job (secondes epoch) : son lancement, à défaut sa réception, à défaut sa fin ; None sinon."""
    for key in ("started_at", "created_at", "finished_at"):
        value = epoch(entry.get(key))
        if value is not None:
            return value
    return None


def moment_of(entry):
    """Date et heure locales d'un job (voir moment_epoch), ou None."""
    value = moment_epoch(entry)
    return local_moment(value) if value is not None else None


# --- Fichiers -----------------------------------------------------------------------------------------------------------

def _paths(root):
    """Fichiers du journal, du plus ancien au plus récent : .3, .2, .1, puis le journal courant."""
    return [checked(root, ARCHIVE.format(number)) for number in range(ARCHIVES, 0, -1)] + [checked(root, JOURNAL)]


def _log_failure(error):
    global _failure_logged
    if not _failure_logged:
        _failure_logged = True
        print(f"Journal local des jobs non écrit ({type(error).__name__}) ; le job lui-même n'est pas touché.", flush=True)


def _retry(action, attempts=5, delay=0.2):
    """Windows : un lecteur qui tient le fichier sans partage de suppression bloque un remplacement un court instant."""
    for attempt in range(attempts):
        try:
            return action()
        except PermissionError:
            if attempt + 1 == attempts:
                raise
            time.sleep(delay)


def _rotate(root, path, incoming):
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    if size + incoming <= MAX_FILE_BYTES:
        return
    try:
        oldest = checked(root, ARCHIVE.format(ARCHIVES))
        if oldest.exists():
            _retry(oldest.unlink)
        for number in range(ARCHIVES - 1, 0, -1):
            older = checked(root, ARCHIVE.format(number))
            if older.exists():
                newer = checked(root, ARCHIVE.format(number + 1))
                _retry(lambda older=older, newer=newer: os.replace(older, newer))
        first = checked(root, ARCHIVE.format(1))
        _retry(lambda: os.replace(path, first))
    except OSError as error:
        # Rotation impossible (fichier tenu) : la ligne s'ajoute quand même, la rotation est retentée au job suivant.
        _log_failure(error)


def _encode(entry):
    """Une ligne UTF-8 terminée par « \\n », d'au plus LINE_MAX_BYTES (sorties réduites au besoin), ou b"" si impossible."""
    for candidate in (entry, dict(entry, outputs=list(entry.get("outputs") or [])[:4], error=str(entry.get("error") or "")[:120]),
                      dict(entry, outputs=[], error="")):
        data = (json.dumps(candidate, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(data) <= LINE_MAX_BYTES:
            return data
    return b""


def append(root, entry):
    """Ajoute une ligne au journal courant (verrou, fsync, rotation). Rend True si elle est écrite ; ne lève jamais."""
    try:
        data = _encode(entry)
        if not data.endswith(b"\n"):
            return False   # entrée impossible à borner : jamais une demi-ligne
        with _LOCK:
            path = checked(root, JOURNAL)
            path.parent.mkdir(parents=True, exist_ok=True)
            checked(root, JOURNAL)   # dossier créé : revérifié (aucun lien ni jonction)
            _rotate(root, path, len(data))
            if is_link(path):
                raise OSError("journal lié")
            with open(path, "a+b") as handle:
                # Une ligne interrompue (coupure de courant) ne colle jamais la suivante : elle reste seule, ignorée.
                handle.seek(0, os.SEEK_END)
                if handle.tell() > 0:
                    handle.seek(-1, os.SEEK_END)
                    if handle.read(1) != b"\n":
                        data = b"\n" + data
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        return True
    except Exception as error:   # le journal ne fait jamais échouer un job
        _log_failure(error)
        return False


def _read_lines(path):
    """Lignes d'un fichier du journal, rien si absent ; partagé en lecture (un lecteur ne bloque pas l'agent)."""
    try:
        if is_link(path) or not path.is_file():
            return []
        with open(path, "rb") as handle:
            return handle.read().splitlines()
    except OSError:
        return []


def entries(root):
    """Toutes les entrées valides, du plus ancien fichier au plus récent (ordre d'écriture)."""
    found = []
    for path in _paths(root):
        for raw in _read_lines(path):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                continue
            entry = normalize(value)
            if entry is not None:
                found.append(entry)
    return found


# --- Filtres ---------------------------------------------------------------------------------------------------------------

def _minutes(text):
    match = _HOUR.fullmatch(text or "")
    return int(match.group(1)) * 60 + int(match.group(2)) if match else None


def _day(text):
    return _dt.date.fromisoformat(text) if text else None


def matches(entry, filters):
    """Vrai si l'entrée passe les filtres : jours (bornes incluses), plage horaire (bornes incluses, peut passer minuit),
    famille ou outil exact, statut, texte cherché (libellé, job_id, compte, fichiers, erreur)."""
    filters = filters or {}
    moment = moment_of(entry)
    day_from, day_to = _day(filters.get("day_from")), _day(filters.get("day_to"))
    if day_from or day_to:
        if moment is None:
            return False
        if day_from and moment.date() < day_from:
            return False
        if day_to and moment.date() > day_to:
            return False
    start, end = _minutes(filters.get("hour_from")), _minutes(filters.get("hour_to"))
    if start is not None or end is not None:
        if moment is None:
            return False
        minute = moment.hour * 60 + moment.minute
        low = 0 if start is None else start
        high = 23 * 60 + 59 if end is None else end
        inside = low <= minute <= high if low <= high else (minute >= low or minute <= high)
        if not inside:
            return False
    family = filters.get("family")
    if family:
        tool = entry["tool"]
        if family == "worker.exe":
            if not tool.startswith("desktop:"):
                return False
        elif tool not in FAMILIES.get(family, ()):
            return False
    if filters.get("tool") and entry["tool"] != filters["tool"]:
        return False
    if filters.get("status") and entry["status"] != filters["status"]:
        return False
    search = str(filters.get("search") or "").casefold().strip()
    if search:
        haystack = " ".join([entry["label"], entry["job_id"], entry["account"]["pseudo"], entry["error"], entry["tool"]]
                            + [item["name"] for item in entry["outputs"]]).casefold()
        if search not in haystack:
            return False
    return True


def _sort_key(entry):
    value = moment_epoch(entry)
    return value if value is not None else 0.0


def _with_presence(root, entry):
    """Copie pour l'interface : « exists » par fichier local, identifiant du Worker réduit à son préfixe, « moment »
    (secondes epoch : lancement, à défaut réception, à défaut fin), sur lequel la liste est triée et groupée par jour."""
    view = dict(entry, outputs=[dict(item) for item in entry["outputs"]], account=dict(entry["account"]))
    view["worker_id"] = entry["worker_id"][:8]
    view["moment"] = moment_epoch(entry)
    for item in view["outputs"]:
        exists = False
        if item["local_path"]:
            try:
                path = checked(root, item["local_path"])
                exists = path.is_file() and not is_link(path)
            except (OSError, ValueError):
                exists = False
        item["exists"] = exists
    return view


def query(root, filters=None, page=1, page_size=50, order="desc"):
    filters = filters or {}
    every = entries(root)
    selected = [entry for entry in every if matches(entry, filters)]
    selected.sort(key=_sort_key, reverse=order != "asc")
    page_size = max(1, min(PAGE_SIZE_MAX, int(page_size or 50)))
    pages = max(1, -(-len(selected) // page_size))
    page = max(1, min(int(page or 1), pages))
    window = selected[(page - 1) * page_size: page * page_size]
    families = {name: 0 for name in FAMILIES}
    statuses = {name: 0 for name in STATUSES}
    tools = {}
    for entry in every:
        statuses[entry["status"]] += 1
        tools[entry["tool"]] = tools.get(entry["tool"], 0) + 1
        for name, members in FAMILIES.items():
            if (name == "worker.exe" and entry["tool"].startswith("desktop:")) or entry["tool"] in members:
                families[name] += 1
    moments = [moment for moment in (moment_epoch(entry) for entry in every) if moment is not None]
    size, files = 0, 0
    for path in _paths(root):
        try:
            if path.is_file() and not is_link(path):
                size += path.stat().st_size
                files += 1
        except OSError:
            continue
    return {"entries": [_with_presence(root, entry) for entry in window], "total": len(selected), "all": len(every),
            "page": page, "page_size": page_size, "pages": pages, "order": "asc" if order == "asc" else "desc",
            "statuses": statuses, "families": families, "tools": dict(sorted(tools.items())),
            "first": min(moments) if moments else None, "last": max(moments) if moments else None,
            "file": {"path": JOURNAL, "bytes": size, "files": files, "max_bytes": MAX_FILE_BYTES * (ARCHIVES + 1)}}


def _rewrite(path, kept):
    """Remplace un fichier du journal par ses lignes gardées (fichier temporaire puis remplacement atomique)."""
    if not kept:
        if path.exists():
            _retry(path.unlink)
        return
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with open(temporary, "xb") as handle:
            handle.write(b"\n".join(kept) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        _retry(lambda: os.replace(temporary, path))
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def delete(root, mode, filters=None, job_ids=()):
    """Retire des entrées du journal LOCAL (rien n'est effacé sur le site). ``mode`` : « all », « filtered », « ids ».
    Rend {"deleted", "remaining"}. Une ligne illisible n'est retirée que par « all »."""
    if mode not in ("all", "filtered", "ids"):
        raise ValueError("Suppression du journal : mode inconnu.")
    wanted = {str(item) for item in job_ids or ()}
    if mode == "ids" and not wanted:
        raise ValueError("Suppression du journal : aucun job choisi.")
    deleted = remaining = 0
    with _LOCK:
        for path in _paths(root):
            lines = _read_lines(path)
            if not lines:
                continue
            kept = []
            for raw in lines:
                if not raw.strip():
                    continue
                entry = None
                if mode != "all":
                    try:
                        entry = normalize(json.loads(raw.decode("utf-8")))
                    except (UnicodeDecodeError, ValueError, RecursionError):
                        entry = None
                drop = mode == "all" or (entry is not None and (
                    entry["job_id"] in wanted if mode == "ids" else matches(entry, filters)))
                if drop:
                    deleted += 1
                else:
                    kept.append(raw)
            if len(kept) != sum(1 for raw in lines if raw.strip()):
                _rewrite(path, kept)
            remaining += len(kept)
    return {"deleted": deleted, "remaining": remaining}


# --- Ce que l'agent inscrit ---------------------------------------------------------------------------------------------

def _identity(agent=None, root=None):
    config = getattr(agent, "config", None) if agent is not None else None
    if not isinstance(config, dict) and root is not None:
        # Machine GRBL : le relais ne porte pas l'agent ; seuls l'identifiant et le nom sont repris de config.json.
        try:
            config = json.loads((Path(root) / "config.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            config = {}
    config = config if isinstance(config, dict) else {}
    return str(config.get("worker_id") or ""), str(config.get("name") or "")


def relative_output(root, path):
    """Chemin relatif (« / ») d'un fichier existant DANS le dossier du Worker, sans lien ; "" sinon."""
    try:
        base = Path(root).resolve()
        target = Path(path)
        target = (base / target) if not target.is_absolute() else target
        relative = target.resolve().relative_to(base)
        if not relative.parts:
            return ""
        located = checked(base, relative.as_posix())
        return relative.as_posix() if located.is_file() and not is_link(located) else ""
    except (OSError, ValueError, RuntimeError):
        return ""


def site_identity(tool_id, parameters, capability=""):
    """(tool, label) d'un job du site, au mot près ceux de son historique (job_history.job_label) : l'outil du site
    (tool_id) et le nom français du job, d'après la capacité et quelques paramètres (jamais le prompt)."""
    parameters = parameters if isinstance(parameters, dict) else {}
    tool = {"image-generation": "image_generation", "3d-generation": "ai3d"}.get(str(tool_id or ""), str(tool_id or ""))
    capability = str(capability or "")
    if tool == "image_generation":
        if parameters.get("lab_tool") or capability in LAB_CAPABILITIES:
            detail = LAB_CAPABILITIES.get(capability)
            return tool, "Labo image" + (f" · {detail}" if detail else "")
        return tool, "Génération IA locale"
    if tool == "ai3d":
        return tool, "Génération 3D IA locale"
    if tool == "converter":
        if parameters.get("cam"):
            cam = parameters.get("cam") if isinstance(parameters.get("cam"), dict) else {}
            return tool, "Préparation CNC · capacités FreeCAD" if cam.get("op") == "capabilities" else "Préparation CNC (FreeCAD CAM)"
        if parameters.get("task") == "mesh_repair":
            return tool, "Analyse de maillage" if parameters.get("analyze_only") else "Réparation de maillage"
        if parameters.get("scene"):
            return tool, "Modélisation paramétrique (FreeCAD)"
        return tool, "Conversion 3D (FreeCAD)"
    if tool == "model-studio":
        if parameters.get("action") == "list_profiles":
            return tool, "Lecture des profils OrcaSlicer"
        return tool, "Tranchage multi-plateaux (OrcaSlicer)" if parameters.get("multi") else "Tranchage (OrcaSlicer)"
    if tool == "laser-studio":
        return tool, LASER_OPERATIONS.get(str(parameters.get("op") or ""), TOOL_NAMES[tool])
    if tool == "cnc-studio":
        return tool, CNC_OPERATIONS.get(str(parameters.get("op") or ""), TOOL_NAMES[tool])
    if tool == "printguard":
        return tool, "Surveillance IA (PrintGuard)"
    tool = tool if _TOOL.fullmatch(tool) else "autre"
    return tool, TOOL_NAMES.get(tool, tool or "Job Worker")


def _job_payload(workdir):
    try:
        value = json.loads((Path(workdir) / "job.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def record_site_job(agent, job_id, entry, response):
    """Fin confirmée d'un job du site (``response`` porte le statut terminal). Rend True si la ligne est écrite."""
    try:
        status = STATE_STATUS.get(str((response or {}).get("status") or ""))
        if status is None:
            return False
        entry = entry if isinstance(entry, dict) else {}
        root = Path(agent.root)
        workdir = Path(str(entry.get("workdir") or "")) if entry.get("workdir") else None
        payload = _job_payload(workdir) if workdir is not None else {}
        parameters = payload.get("parameters") if isinstance(payload.get("parameters"), dict) else {}
        tool_id = str(entry.get("tool_id") or payload.get("tool_id") or "")
        tool, label = site_identity(tool_id, parameters, payload.get("capability") or "")
        outputs = []
        uploaded = entry.get("uploaded") if isinstance(entry.get("uploaded"), dict) else {}
        if status == "termine":
            for candidate, artifact in uploaded.items():
                artifact = artifact if isinstance(artifact, dict) else {}
                name = clean_text(artifact.get("name") or Path(str(candidate)).name, 120)
                local = relative_output(root, workdir / str(candidate)) if workdir is not None else ""
                url = "/api/workers/jobs/" + urllib.parse.quote(str(job_id), safe="") + "/artifacts/" + urllib.parse.quote(name, safe="")
                outputs.append({"kind": output_kind(name), "name": name, "url": url, "local_path": local})
        worker_id, worker_name = _identity(agent)
        # Compte demandeur, si le site le joint au job ({"id", "pseudo"} comme son historique, ou un pseudo seul).
        account = payload.get("account") or payload.get("requester") or None
        line = make_entry(job_id=job_id, worker_id=worker_id, worker_name=worker_name, tool=tool, label=label,
                          account=account, status=status,
                          created_at=entry.get("accepted_at"), started_at=entry.get("started_at"), finished_at=time.time(),
                          outputs=outputs, error=(response or {}).get("error") or (response or {}).get("message") or "",
                          root=root)
        return append(root, line)
    except Exception as error:
        _log_failure(error)
        return False


def _read_result_file(root, token):
    try:
        path = checked(root, f"runtime/desktop/out/{token}.json")
        if is_link(path) or not path.is_file() or path.stat().st_size > RESULT_FILE_MAX:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def desktop_outputs(root, result):
    """Fichiers produits par une tâche Desktop, lus dans son résultat AVANT que worker.exe ne le relise (et n'efface un
    ``result_file``) : clés path / gcode_path / output_path et listes files / outputs, à au plus quatre niveaux."""
    if isinstance(result, dict) and set(result) == {"result_file"} and isinstance(result["result_file"], str) \
            and re.fullmatch(r"[0-9a-f]{32}", result["result_file"]):
        result = _read_result_file(root, result["result_file"])
    found, seen = [], set()

    def add(path, name=""):
        local = relative_output(root, path) if isinstance(path, str) and path else ""
        if not local or local in seen or len(found) >= OUTPUTS_MAX:
            return
        seen.add(local)
        label = clean_text(name, 120) or local.rsplit("/", 1)[-1]
        found.append({"kind": output_kind(label if os.path.splitext(label)[1] else local), "name": label, "url": "",
                      "local_path": local})

    def walk(value, depth):
        if depth > 4 or len(found) >= OUTPUTS_MAX:
            return
        if isinstance(value, dict):
            name = value.get("file_name") or value.get("name") or ""
            for key in OUTPUT_KEYS:
                if isinstance(value.get(key), str):
                    add(value[key], name if key == "path" else "")
            for key, item in value.items():
                if key in OUTPUT_LISTS and isinstance(item, list):
                    for element in item[:OUTPUTS_MAX]:
                        if isinstance(element, dict) and isinstance(element.get("path"), str):
                            add(element["path"], element.get("name") or "")
                elif isinstance(item, dict) and key not in ("source", "simulation", "report", "record"):
                    walk(item, depth + 1)
        elif isinstance(value, list):
            for item in value[:50]:
                walk(item, depth + 1)

    walk(result, 0)
    return found


def desktop_label(tool, label):
    name = DESKTOP_LABELS.get(tool, clean_text(tool, 30))
    text = clean_text(label, 140)
    for prefix in ("Labo · ", "Labo - "):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return name + " · " + text if text else name


def record_desktop_task(agent, task, outputs=None, finished_at=None):
    """Fin d'une tâche Desktop (worker.exe) journalisée : terminée, échouée ou annulée."""
    try:
        status = {"done": "termine", "failed": "echoue", "cancelled": "annule"}.get(str(getattr(task, "state", "")))
        if status is None:
            return False
        root = Path(agent.root)
        now = time.time() if finished_at is None else finished_at
        started = getattr(task, "started", None)
        finished = getattr(task, "finished", None)
        # La tâche compte en temps monotone : son lancement est ramené à l'horloge du PC.
        started_at = now - max(0.0, (finished if finished is not None else time.monotonic()) - started) \
            if isinstance(started, (int, float)) else None
        worker_id, worker_name = _identity(agent)
        tool = str(getattr(task, "tool", "") or "")
        line = make_entry(job_id=getattr(task, "id", ""), worker_id=worker_id, worker_name=worker_name,
                          tool="desktop:" + tool if _TOOL.fullmatch(tool) else "desktop:autre",
                          label=desktop_label(tool, getattr(task, "label", "")), account="", status=status,
                          created_at=started_at, started_at=started_at, finished_at=now,
                          outputs=(outputs or ()) if status == "termine" else (), error=getattr(task, "error", ""), root=root)
        return append(root, line)
    except Exception as error:
        _log_failure(error)
        return False


def record_machine_job(root, spec, job):
    """Fin d'un job GRBL envoyé par ce Worker (gravure laser, usinage CNC). Le cadrage n'est pas un job : ignoré."""
    try:
        job = job if isinstance(job, dict) else {}
        spec = spec if isinstance(spec, dict) else {}
        if str(job.get("command") or "stream") != "stream":
            return False
        laser = spec.get("kind") == "laser"
        tool = "laser" if laser else "cnc"
        machine = clean_text(spec.get("name") or spec.get("id") or "", 60)
        name = clean_text(job.get("name"), 80)
        label = ("Gravure laser" if laser else "Usinage CNC") + (" · " + machine if machine else "") + (" · « " + name + " »" if name else "")
        if job.get("ok") is True:
            status = "termine"
        elif job.get("reason") == "cancelled":
            status = "annule"
        else:
            status = "echoue"
        worker_id, worker_name = _identity(root=root)
        line = make_entry(job_id=job.get("command_id") or "", worker_id=worker_id, worker_name=worker_name, tool=tool,
                          label=label, status=status, created_at=job.get("started_at"), started_at=job.get("started_at"),
                          finished_at=job.get("finished_at") or time.time(), outputs=(), error=job.get("error") or "", root=root)
        return append(root, line)
    except Exception as error:
        _log_failure(error)
        return False


# --- Actions locales (worker.exe, menu) -------------------------------------------------------------------------------

def v_day(value):
    if type(value) is not str or not _DAY.fullmatch(value):
        raise ValueError("Paramètre local invalide : date AAAA-MM-JJ attendue.")
    try:
        _dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError("Paramètre local invalide : date inexistante.") from None
    return value


def v_hour(value):
    match = _HOUR.fullmatch(value) if type(value) is str else None
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise ValueError("Paramètre local invalide : heure HH:MM attendue.")
    return value


def v_number(minimum, maximum):
    def check(value):
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"Paramètre local invalide : entier de {minimum} à {maximum} attendu.")
        return value
    return check


def v_tool(value):
    if type(value) is not str or not _TOOL.fullmatch(value):
        raise ValueError("Paramètre local invalide : outil inconnu.")
    return value


def v_search(value):
    if type(value) is not str or len(value) > 80 or _CONTROL.search(value):
        raise ValueError("Paramètre local invalide : texte cherché d'au plus 80 caractères.")
    return value


def v_job_ids(maximum):
    def check(value):
        if type(value) is not list or not 0 < len(value) <= maximum:
            raise ValueError(f"Paramètre local invalide : de 1 à {maximum} job_id attendus.")
        cleaned = []
        for item in value:
            if type(item) is not str or not _ID.fullmatch(item):
                raise ValueError("Paramètre local invalide : job_id illisible.")
            if item not in cleaned:
                cleaned.append(item)
        return cleaned
    return check


FILTER_PARAMS = {"day_from": v_day, "day_to": v_day, "hour_from": v_hour, "hour_to": v_hour,
                 "family": v_choice(*FAMILIES), "tool": v_tool, "status": v_choice(*STATUSES), "search": v_search}


def _filters(params):
    filters = {key: params[key] for key in FILTER_PARAMS if key in params and params[key] != ""}
    if filters.get("day_from") and filters.get("day_to") and filters["day_from"] > filters["day_to"]:
        raise ValueError("Filtre du journal : le premier jour est après le dernier.")
    return filters


def journal_read(agent, params):
    """Journal des jobs de CE Worker, filtré, trié (plus récent d'abord par défaut) et paginé. Lecture seule."""
    params = params or {}
    worker_id, worker_name = _identity(agent)
    result = query(agent.root, _filters(params), params.get("page", 1), params.get("page_size", 50), params.get("order", "desc"))
    result["worker"] = {"name": clean_text(worker_name, 80), "id_prefix": worker_id[:8]}
    return result


def _ids_from_file(agent, token):
    if __package__:
        from . import self_service
    else:
        import self_service

    def check(value):
        if not isinstance(value, dict) or set(value) != {"job_ids"}:
            raise ValueError("Fichier de sélection illisible : seul « job_ids » est attendu.")
        return {"job_ids": v_job_ids(IDS_FILE_MAX)(value["job_ids"])}
    return self_service.args_from_file(agent, token, check, 256 * 1024)["job_ids"]


def journal_delete(agent, params):
    """Supprime des entrées du journal local (tout, filtrées, ou sélection de job_id). ``confirm`` doit valoir true :
    worker.exe demande la confirmation avant d'envoyer la demande. Le site n'est pas touché."""
    params = params or {}
    if params.get("confirm") is not True:
        raise ValueError("Suppression du journal non confirmée : confirme-la dans worker.exe.")
    mode = params.get("mode")
    if mode not in ("all", "filtered", "ids"):
        raise ValueError("Suppression du journal : précise « all », « filtered » ou « ids ».")
    filters = _filters(params)
    ids = list(params.get("job_ids") or [])
    if "args_file" in params:
        ids += _ids_from_file(agent, params["args_file"])
    if mode == "filtered" and not filters:
        raise ValueError("Suppression filtrée sans filtre : choisis « tout supprimer » pour vider le journal.")
    if mode == "ids" and not ids:
        raise ValueError("Suppression du journal : aucun job choisi.")
    outcome = delete(agent.root, mode, filters=filters, job_ids=ids)
    outcome["mode"] = mode
    return outcome


LOCAL_ACTIONS = {
    "job-journal": {"handler": journal_read, "read": True,
                    "params": {**FILTER_PARAMS, "page": v_number(1, 100000), "page_size": v_number(1, PAGE_SIZE_MAX),
                               "order": v_choice("desc", "asc")}},
    # « read » : aucun heartbeat forcé, rien ne change pour le site (le journal est local).
    "job-journal-delete": {"handler": journal_delete, "read": True,
                           "params": {**FILTER_PARAMS, "mode": v_choice("all", "filtered", "ids"), "confirm": v_bool,
                                      "job_ids": v_job_ids(IDS_MAX), "args_file": v_hex(32)}},
}
