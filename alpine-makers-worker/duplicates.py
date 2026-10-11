"""Doublons de modèles ComfyUI : contrôle local, plan, puis remplacement des copies par des liens physiques.

Seul module de l'agent qui remplace ou supprime des fichiers du propriétaire. Ses règles :

* périmètre : uniquement ``components/comfyui/models`` de CE Worker, chemins résolus, aucun lien ni jonction,
  ni environnement de moteur, ni dossier géré en bloc par son moteur (``scope_of``) ;
* par défaut la copie est remplacée par un lien physique vers l'exemplaire gardé : le nom reste, la place est rendue ;
* la suppression n'existe que pour une copie manifeste (« … - Copie », « … (1) ») rangée à côté de son original ;
* le rapport du contrôle (``data/duplicates-report.json``) ne sert qu'à PROPOSER : taille, date, identité et SHA-256
  de chaque fichier sont relus sur le disque avant chaque opération ; un rapport ancien, tronqué ou fabriqué ne peut
  donc ni faire lier deux fichiers différents, ni sortir du périmètre ;
* chaque opération est atomique ; rien n'est repris automatiquement après une interruption (``recover``).

Les phrases de confirmation et l'empreinte du plan protègent d'une erreur de manipulation : ce ne sont pas une
frontière de sécurité (la boîte aux lettres locale n'authentifie pas l'auteur d'une demande).
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import threading
import time
from pathlib import Path

if __package__:
    from . import comfyui_assets, storage_audit
    from .installers.model_catalog import COMFYUI_BUNDLES, COMFYUI_CATALOG, COMFYUI_LORA_CATALOG, COMFYUI_UTILITY_MODELS
    from .job_state import atomic_json
    from .safety import is_link, remove_worker_file
    from .storage_paths import WorkerStorage
else:
    import comfyui_assets
    import storage_audit
    from installers.model_catalog import COMFYUI_BUNDLES, COMFYUI_CATALOG, COMFYUI_LORA_CATALOG, COMFYUI_UTILITY_MODELS
    from job_state import atomic_json
    from safety import is_link, remove_worker_file
    from storage_paths import WorkerStorage

try:
    # Validateurs communs des actions locales à paramètres (socle de l'agent).
    if __package__:
        from .local_control import v_choice, v_hex
    else:
        from local_control import v_choice, v_hex
except ImportError:
    # Socle plus ancien que ce module : mêmes règles, un type inattendu est toujours refusé.
    def v_hex(length):
        def check(value):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{%d}" % length, value):
                raise ValueError("Paramètre local invalide : empreinte attendue.")
            return value
        return check

    def v_choice(*values):
        def check(value):
            if not isinstance(value, str) or value not in values:
                raise ValueError("Paramètre local invalide : valeur non autorisée.")
            return value
        return check


REPORT_NAME = "duplicates-report.json"
PLAN_NAME = "duplicates-plan.json"
JOURNAL_NAME = "duplicates-journal.jsonl"
JOURNAL_ROTATED = "duplicates-journal.1.jsonl"
JOURNAL_MAX_BYTES = 2 * 1024 * 1024
REPORT_MAX_AGE = 24 * 3600
REPORT_CLOCK_TOLERANCE = 300        # un rapport daté du futur (horloge reculée, fichier fabriqué) n'est pas « récent »
MODELS_RELATIVE = "components/comfyui/models"
EXCLUDED_PARTS = frozenset({".venv", "venv", "site-packages", "custom_nodes", "_internal", ".git", ".cache", ".alpine-tmp", "__pycache__"})
MARKERS = (".raphal-ready", ".alpine-managed.json")
MODEL_SUFFIXES = frozenset({".safetensors", ".sft", ".ckpt", ".pt", ".pth", ".bin", ".gguf"})
PARTIAL_SUFFIXES = (".download", ".partial")
PHRASES = {"link": "LIER LES DOUBLONS", "delete": "SUPPRIMER LES COPIES"}
TEMP_PREFIX = ".alpine-dedup-"
TEMP_PATTERN = re.compile(r"\.alpine-dedup-[0-9a-f]{16}\.tmp")
# Quatre essais : un antivirus tient souvent la cible une fraction de seconde.
REPLACE_WAITS = (0.0, 0.2, 0.4, 0.6)
OUT_OF_SCOPE_MAX = 200
ERRORS_MAX = 50
PREVIEW = 20

REASON_OUTSIDE = "hors du dossier des modèles du Worker"
REASON_ENGINE = "environnement d'un moteur"
REASON_LINKED = "dossier lié"
REASON_MANAGED = "modèle géré en bloc par son moteur"
REASON_TYPE = "type de fichier non géré"
REASON_VOLUME = "volumes différents"
REASON_CHANGED = "modifié depuis le contrôle"
REASON_SHARED = "déjà partagée, aucun gain"
REASON_ENGINE_STARTED = "moteur d'images démarré"
REASON_KEEP_GONE = "exemplaire gardé modifié ou disparu"
REASON_BUSY = "fichier utilisé ou protégé, conservé"
REASON_READONLY = "fichier en lecture seule, conservé"
REASON_STOPPING = "agent en cours d'arrêt"
KEEP_REASONS = ("catalogue", "installe_par_le_site", "deja_partage", "nom_d_origine", "plus_ancien", "nom_le_plus_court", "ordre")

REFUSAL_AUDIT_RUNNING = "Un contrôle des doublons est déjà en cours sur ce Worker."
REFUSAL_PHRASE = "Confirmation absente ou inexacte ; aucun fichier modifié."
REFUSAL_REPORT = "Contrôle trop ancien ou déjà utilisé : relance « Contrôler les doublons »."
REFUSAL_DIGEST = "Le contenu a changé depuis la confirmation : relance l'outil. Aucun fichier modifié."
REFUSAL_MAINTENANCE = "Refusé : une installation ou une maintenance est en cours. Réessaie après sa fin."
REFUSAL_ASSET = "Refusé : une installation de ressource est en cours."
REFUSAL_JOBS = "Refusé : "
REFUSAL_CLEANUP = "Refusé : un nettoyage local est en cours."
REFUSAL_AUDIT = "Refusé : un contrôle des doublons est en cours."
REFUSAL_ENGINE = "Refusé : le moteur d'images tourne et garde ses modèles ouverts. Arrête-le (Moteurs), puis relance."
REFUSAL_APPLY_RUNNING = "Une suppression des doublons est déjà en cours."


def _catalog_names():
    names = {spec[0] for spec in COMFYUI_CATALOG.values()} | {spec[0] for spec in COMFYUI_LORA_CATALOG.values()}
    names |= {entry[1] for files in COMFYUI_BUNDLES.values() for entry in files}
    names |= {entry[1] for spec in COMFYUI_UTILITY_MODELS.values() for entry in spec["files"]}
    return frozenset(names)


# Noms de fichiers que le site et les installateurs connaissent : toujours gardés, jamais supprimés.
CATALOG_NAMES = _catalog_names()
_CATALOG_FOLDED = frozenset(name.casefold() for name in CATALOG_NAMES)   # Windows ne distingue pas la casse des noms
_FOLDER_KINDS = {spec["folder"]: kind for kind, spec in comfyui_assets.ASSET_KINDS.items()}
_COPY_SUFFIX = re.compile(r"(?: - cop(?:ie|y)(?: \(\d+\))?| \(\d+\)|[ _-]copy(?: ?\d+)?)")
_COPY_TAIL = re.compile(_COPY_SUFFIX.pattern + r"$")
_COPY_PREFIXES = ("copie de ", "copy of ")
_STATE_GUARD = threading.Lock()


class AuditCancelled(Exception):
    """Contrôle annulé à un point de progression ; aucun fichier n'a été modifié."""


# ------------------------------------------------------------------ noms de copies
def _name_parts(name):
    path = Path(str(name))
    return path.stem.casefold(), path.suffix.casefold()


def looks_like_copy(name):
    """Le nom porte un motif de copie (« … - Copie », « … (2) », « … copy », « Copie de … »)."""
    stem, _ = _name_parts(name)
    return bool(_COPY_TAIL.search(stem)) or stem.startswith(_COPY_PREFIXES)


def is_copy_name(keep_name, name):
    """``name`` est une copie manifeste de ``keep_name`` : même extension, nom dérivé du sien par un motif de copie."""
    keep_stem, keep_suffix = _name_parts(keep_name)
    stem, suffix = _name_parts(name)
    if not keep_stem or suffix != keep_suffix:
        return False
    if stem.startswith(keep_stem) and _COPY_SUFFIX.fullmatch(stem[len(keep_stem):]):
        return True
    return any(stem == prefix + keep_stem for prefix in _COPY_PREFIXES)


# ------------------------------------------------------------------ périmètre
def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_timestamp(value):
    # Ni NaN, ni infini, ni entier démesuré : un rapport fabriqué ne fait jamais échouer un calcul d'âge.
    return _is_number(value) and 0 <= value < 1e11


def _plain_file(info):
    """Fichier ordinaire, sans point d'analyse (ni lien symbolique, ni jonction)."""
    return stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400


def _read_only(info):
    """Attribut « lecture seule » de Windows : partagé entre les noms d'un même fichier, jamais changé ici."""
    return bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_READONLY)


def _signature(info):
    return info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino


def _birth_ns(info):
    # Date de création : une copie faite dans l'Explorateur garde la date de modification de l'original.
    value = getattr(info, "st_birthtime_ns", None)
    return value if isinstance(value, int) else info.st_ctime_ns


def _same_folder(first, second):
    first, second = Path(first).resolve(), Path(second).resolve()
    try:
        return os.path.samefile(first, second)
    except OSError:
        return os.path.normcase(str(first)) == os.path.normcase(str(second))


class _Located:
    __slots__ = ("ok", "reason", "resolved", "relative", "info")

    def __init__(self, ok, reason, resolved=None, relative=None, info=None):
        self.ok, self.reason, self.resolved, self.relative, self.info = ok, reason, resolved, relative, info


class _Scope:
    """Périmètre d'un plan ou d'une application : le dossier des modèles est résolu une seule fois."""

    def __init__(self, agent):
        self.agent = agent
        self.models_root = (Path(agent.root) / "components" / "comfyui" / "models").resolve()
        self.refresh()

    def refresh(self):
        """Relit ce qui peut changer entre deux opérations : liens, marqueurs, moteur configuré ailleurs."""
        self._folders, self._markers = {}, {}
        self.allowed, self.refusal = self._check_root()
        try:
            self._models_stat = os.stat(self.models_root)
        except OSError:
            self._models_stat = None

    def _check_root(self):
        try:
            root = Path(self.agent.root).resolve()
            managed = root / "components" / "comfyui"
            configured = WorkerStorage(self.agent.root, self.agent.config).component("comfyui")
            # Un ComfyUI configuré ailleurs n'appartient pas au Worker : rien n'est dans le périmètre.
            if not _same_folder(configured, managed):
                return False, REASON_OUTSIDE
            cursor = managed / "models"
            while cursor != root:
                if is_link(cursor):
                    return False, REASON_LINKED
                cursor = cursor.parent
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
            return False, REASON_OUTSIDE
        return True, ""

    def _is_models_root(self, folder):
        # Sans numéro de fichier (volume qui n'en donne pas), deux dossiers ne se comparent que par leur chemin résolu.
        if self._models_stat is not None and self._models_stat.st_ino:
            try:
                return os.path.samestat(os.stat(folder), self._models_stat)
            except OSError:
                pass   # dossier absent : comparaison des chemins résolus
        try:
            return os.path.normcase(str(Path(folder).resolve())) == os.path.normcase(str(self.models_root))
        except (OSError, RuntimeError):
            return False

    def _folder(self, folder):
        """(parties sous le dossier des modèles ou None, un dossier du chemin est un lien) pour le chemin TEL QUE REÇU."""
        key = os.path.normcase(str(folder))
        known = self._folders.get(key)
        if known is None:
            linked = is_link(folder)
            if self._is_models_root(folder):
                known = ((), linked)
            elif folder.parent == folder:
                known = (None, False)
            else:
                parts, above = self._folder(folder.parent)
                known = (None, False) if parts is None else (parts + (folder.name,), above or linked)
            self._folders[key] = known
        return known

    def _has_marker(self, folder):
        key = os.path.normcase(str(folder))
        if key not in self._markers:
            self._markers[key] = any(os.path.lexists(folder / marker) for marker in MARKERS)
        return self._markers[key]

    def locate(self, path):
        if not self.allowed:
            return _Located(False, self.refusal)
        try:
            absolute = Path(os.path.abspath(os.fspath(path)))
            parts, linked = self._folder(absolute.parent)
            if parts is None:
                return _Located(False, REASON_OUTSIDE)
            if linked:
                return _Located(False, REASON_LINKED)
            try:
                info = os.lstat(absolute)
            except OSError:
                info = None
            if info is not None and (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400):
                return _Located(False, REASON_LINKED)
            resolved = absolute.resolve()
            if resolved == self.models_root or not resolved.is_relative_to(self.models_root):
                return _Located(False, REASON_OUTSIDE)
            relative = resolved.relative_to(self.models_root)
        except (OSError, ValueError, TypeError, RuntimeError):
            return _Located(False, REASON_OUTSIDE)
        if any(part.casefold() in EXCLUDED_PARTS for part in relative.parts):
            return _Located(False, REASON_ENGINE, resolved, relative)
        folder = self.models_root
        for part in relative.parts[:-1]:
            # Seuls les dossiers SOUS le dossier des modèles comptent : celui de ComfyUI porte lui-même un marqueur.
            folder = folder / part
            if self._has_marker(folder):
                return _Located(False, REASON_MANAGED, resolved, relative)
        name = relative.name.casefold()
        if Path(name).suffix not in MODEL_SUFFIXES or name.endswith(PARTIAL_SUFFIXES):
            return _Located(False, REASON_TYPE, resolved, relative)
        if info is None:
            return _Located(False, REASON_CHANGED, resolved, relative)
        if not stat.S_ISREG(info.st_mode):
            return _Located(False, REASON_TYPE, resolved, relative)
        return _Located(True, "", resolved, relative, info)


def scope_of(agent, path, scope=None):
    """(dans le périmètre nettoyable, raison du refus) pour un fichier ; ne modifie rien.

    La condition « même volume que l'exemplaire gardé » se juge par paire : elle est appliquée par le plan.
    """
    located = (scope or _Scope(agent)).locate(path)
    return located.ok, located.reason


# ------------------------------------------------------------------ état en mémoire et fichiers de data/
def _idle_audit():
    return {"state": "idle", "progress": 0, "message": "", "started_at": None, "finished_at": None, "error": ""}


def _idle_apply():
    return {"state": "idle", "run": "", "done": 0, "total": 0, "linked": 0, "deleted": 0, "failed": 0, "skipped": 0,
            "busy": 0, "freed_bytes": 0, "current": "", "errors": []}


def _state(agent):
    with _STATE_GUARD:
        state = getattr(agent, "duplicates_state", None)
        if not isinstance(state, dict) or "lock" not in state:
            state = {"lock": threading.Lock(), "cancel": threading.Event(), "audit": _idle_audit(), "apply": _idle_apply(),
                     "summary": None, "thread": None, "apply_thread": None}
            agent.duplicates_state = state
        return state


def _data_path(agent, name):
    data = Path(agent.root) / "data"
    path = data / name
    if is_link(data) or is_link(path):
        raise ValueError("Dossier de données du Worker lié ; aucune écriture.")
    return path


def _worker_prefix(agent):
    config = getattr(agent, "config", None)
    return str((config.get("worker_id") if isinstance(config, dict) else "") or "")[:8]


def _agent_version(agent):
    version = getattr(sys.modules.get(type(agent).__module__), "AGENT_VERSION", None)
    return version if isinstance(version, str) else ""


def _read_report(agent):
    """Rapport du dernier contrôle local s'il est lisible et bien formé, sinon None. Jamais d'exception."""
    try:
        value = json.loads(_data_path(agent, REPORT_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError, MemoryError):
        return None
    if not isinstance(value, dict) or value.get("schema") != 1 or not _is_timestamp(value.get("finished_at")):
        return None
    result = value.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("duplicate_groups"), list):
        return None
    return value


def _report_usable(agent, report):
    """Le rapport vient de ce Worker, a moins de 24 h et n'a pas encore servi."""
    if report is None or report.get("consumed_at") is not None or report.get("worker_id_prefix") != _worker_prefix(agent):
        return False
    age = time.time() - report["finished_at"]
    return -REPORT_CLOCK_TOLERANCE <= age <= REPORT_MAX_AGE


def _usable_report(agent):
    report = _read_report(agent)
    if not _report_usable(agent, report):
        raise ValueError(REFUSAL_REPORT)
    return report


def _journal(agent, run, event, strict=False, **fields):
    """Une ligne JSON par événement, ajoutée puis synchronisée. ``strict`` : un échec d'écriture arrête l'appelant."""
    try:
        path = _data_path(agent, JOURNAL_NAME)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size >= JOURNAL_MAX_BYTES:
                os.replace(path, path.with_name(JOURNAL_ROTATED))
        except FileNotFoundError:
            pass
        line = json.dumps({"at": time.time(), "run": run, "event": event, **fields}, ensure_ascii=False, separators=(",", ":"))
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except (OSError, ValueError) as error:
        if strict:
            raise
        print(f"Journal des doublons non écrit ({type(error).__name__}).", flush=True)


# ------------------------------------------------------------------ plan
def _group_entries(group):
    """(sha256, taille, fichiers bien formés) d'un groupe du rapport, ou None : le rapport n'est jamais cru sur parole."""
    if not isinstance(group, dict):
        return None
    digest, size, files = group.get("sha256"), group.get("size_bytes"), group.get("files")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or not _is_int(size) or size <= 0 \
            or not isinstance(files, list):
        return None
    entries = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not 0 < len(item["path"]) <= 4096 \
                or not all(_is_int(item.get(key)) for key in ("mtime_ns", "device", "inode")):
            continue
        entries.append({"path": item["path"], "size_bytes": size, "mtime_ns": item["mtime_ns"],
                        "device": item["device"], "inode": item["inode"]})
    return digest, size, entries


def _asset_key(relative):
    """(type, nom) d'une ressource tenue dans l'index comfyui-assets.json, ou None."""
    if len(relative.parts) < 2:
        return None
    kind = _FOLDER_KINDS.get(relative.parts[0].casefold())
    return (kind, Path(*relative.parts[1:]).as_posix()) if kind else None


def _sourced(index, relative):
    asset = _asset_key(relative)
    entry = index.get(comfyui_assets.index_key(*asset)) if asset else None
    return isinstance(entry, dict) and bool(entry.get("source"))


def _keep_key(index, located):
    name = located.relative.name
    return (0 if name.casefold() in _CATALOG_FOLDED else 1,
            0 if _sourced(index, located.relative) else 1,
            0 if located.info.st_nlink > 1 else 1,
            1 if looks_like_copy(name) else 0,
            _birth_ns(located.info),
            len(name),
            located.relative.as_posix().casefold())


def _keep_reason(keep_key, other_key):
    for position, reason in enumerate(KEEP_REASONS):
        if keep_key[position] != other_key[position]:
            return reason
    return KEEP_REASONS[-1]


def _deletable(index, keep, target):
    """Copie manifeste, rangée à côté de son original, que ni le catalogue ni le site ne connaissent."""
    return (keep.resolved.parent == target.resolved.parent
            and is_copy_name(keep.relative.name, target.relative.name)
            and target.relative.name.casefold() not in _CATALOG_FOLDED
            and not _sourced(index, target.relative))


class _Plan:
    __slots__ = ("public", "pairs", "counts", "scope")


def _build_plan(agent, report, same_folder, scope=None):
    """Plan des opérations : aucun contenu n'est lu, chaque fichier est seulement relu par ``lstat``."""
    scope = scope or _Scope(agent)
    index = comfyui_assets.load_index(agent.root)
    rows, out_of_scope, skipped, used = [], [], [], set()
    deletable = in_scope_groups = 0
    for group in report["result"]["duplicate_groups"]:
        parsed = _group_entries(group)
        if parsed is None:
            continue
        digest, size, entries = parsed
        candidates, outside = [], {}
        for entry in entries:
            located = scope.locate(entry["path"])
            if not located.ok:
                if located.reason == REASON_CHANGED:
                    skipped.append({"path": located.relative.as_posix(), "reason": REASON_CHANGED})
                else:
                    outside[located.reason] = outside.get(located.reason, 0) + 1
                continue
            key = os.path.normcase(str(located.resolved))
            if key in used:
                continue   # même fichier cité deux fois : une seule place dans le plan
            used.add(key)
            if _signature(located.info) != (size, entry["mtime_ns"], entry["device"], entry["inode"]):
                skipped.append({"path": located.relative.as_posix(), "reason": REASON_CHANGED})
                continue
            candidates.append((_keep_key(index, located), located, entry))
        operations = 0
        if len(candidates) >= 2:
            candidates.sort(key=lambda item: item[0])
            keep_key, keep, keep_entry = candidates[0]
            for other_key, target, target_entry in candidates[1:]:
                if target.info.st_dev != keep.info.st_dev:
                    outside[REASON_VOLUME] = outside.get(REASON_VOLUME, 0) + 1
                    continue
                if target.info.st_nlink > 1:
                    # Son contenu vit déjà sous un autre nom : la remplacer ne rendrait aucune place.
                    skipped.append({"path": target.relative.as_posix(), "reason": REASON_SHARED})
                    continue
                removable = _deletable(index, keep, target)
                deletable += removable
                operations += 1
                rows.append(({"op": "delete" if removable and same_folder == "delete" else "link", "sha256": digest,
                              "size_bytes": size, "keep": keep.relative.as_posix(),
                              "keep_reason": _keep_reason(keep_key, other_key), "target": target.relative.as_posix()},
                             keep_entry, target_entry))
        in_scope_groups += bool(operations)
        out_of_scope.extend({"sha256": digest, "size_bytes": size, "copies": count, "reason": reason}
                            for reason, count in sorted(outside.items()))
    rows.sort(key=lambda row: (-row[0]["size_bytes"], row[0]["keep"].casefold(), row[0]["target"].casefold()))
    out_of_scope.sort(key=lambda item: (-item["size_bytes"], item["sha256"], item["reason"]))
    operations = [row[0] for row in rows]
    core = {"report_finished_at": report["finished_at"], "same_folder": same_folder, "operations": operations}
    plan = _Plan()
    plan.scope = scope
    plan.pairs = rows
    plan.public = {
        "schema": 1, "report_finished_at": report["finished_at"], "same_folder": same_folder, "models_root": MODELS_RELATIVE,
        "operations": operations, "out_of_scope": out_of_scope[:OUT_OF_SCOPE_MAX], "skipped": skipped,
        "freed_bytes": sum(item["size_bytes"] for item in operations),
        "links": sum(item["op"] == "link" for item in operations),
        "deletions": sum(item["op"] == "delete" for item in operations),
        "digest": hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
    }
    plan.counts = {"operations_count": len(operations), "out_of_scope_count": len(out_of_scope), "skipped_count": len(skipped),
                   "deletable_count": int(deletable), "in_scope_groups": int(in_scope_groups)}
    return plan


def plan_action(agent, params):
    same_folder = params.get("same_folder") or "link"
    plan = _build_plan(agent, _usable_report(agent), same_folder)
    atomic_json(_data_path(agent, PLAN_NAME), plan.public)
    # La réponse voyage par la boîte aux lettres : opérations et hors périmètre abrégés, le plan complet est dans data/.
    return {**plan.public, "operations": plan.public["operations"][:PREVIEW], "out_of_scope": plan.public["out_of_scope"][:PREVIEW],
            **{key: plan.counts[key] for key in ("operations_count", "out_of_scope_count", "skipped_count", "deletable_count")}}


# ------------------------------------------------------------------ contrôle local
def _store_report(agent, started_at, result):
    report = {"schema": 1, "source": "local", "agent_version": _agent_version(agent), "worker_id_prefix": _worker_prefix(agent),
              "started_at": started_at, "finished_at": time.time(), "consumed_at": None, "result": result}
    atomic_json(_data_path(agent, REPORT_NAME), report)
    return report


def _audit_worker(agent, state):
    cancel = state["cancel"]
    stop = getattr(agent, "stop_requested", None)
    with state["lock"]:
        audit = state["audit"]   # l'état de CE contrôle : un contrôle suivant a le sien

    def progress(percent, message):
        # Points d'arrêt = points de progression : le contrôle ne modifie aucun fichier.
        if cancel.is_set() or (stop is not None and stop.is_set()):
            raise AuditCancelled("Contrôle des doublons annulé ; aucun fichier modifié.")
        with state["lock"]:
            audit["progress"] = max(0, min(99, int(percent)))
            audit["message"] = str(message)[:200]

    outcome, error, complete = "failed", "", False
    try:
        progress(0, "Inventaire des moteurs, modules et modèles, sans modification…")
        result = storage_audit.scan_roots(agent.storage_audit_roots(), progress=progress, details=True)
        progress(99, "Écriture du rapport…")
        _store_report(agent, audit["started_at"], result)
        outcome, complete = "done", result.get("complete") is True
    except AuditCancelled:
        outcome = "cancelled"   # aucun rapport écrit : le précédent reste en place
    except Exception as failure:   # noqa: BLE001 - un contrôle en échec ne fait jamais tomber l'agent
        error = f"Contrôle interrompu ({type(failure).__name__}) ; aucun fichier modifié."
        print(f"Contrôle local des doublons en échec ({type(failure).__name__}).", flush=True)
    finally:
        agent.storage_audit_lock.release()
        with state["lock"]:
            audit.update({"state": outcome, "finished_at": time.time(), "error": error})
            if outcome == "done":
                audit.update({"progress": 100, "message": "Contrôle terminé, aucun fichier modifié." if complete else
                              "Contrôle incomplet : certains fichiers n'ont pas pu être comparés. Aucun fichier modifié."})
            elif outcome == "cancelled":
                audit["message"] = "Contrôle annulé ; aucun fichier modifié."
            state["summary"] = None
    if outcome == "done":
        try:
            _report_summary(agent)   # calculé ici : l'état demandé ensuite répond tout de suite
        except Exception:   # noqa: BLE001
            pass


def audit_action(agent, params):
    state = _state(agent)
    if getattr(agent, "duplicates_apply_active", False):
        # L'application tient le verrou des contrôles : le vrai motif est dit, pas « un contrôle est en cours ».
        raise ValueError(REFUSAL_APPLY_RUNNING)
    # Verrou partagé avec la commande du site « Contrôler les doublons » : un seul contrôle à la fois.
    if not agent.storage_audit_lock.acquire(blocking=False):
        raise ValueError(REFUSAL_AUDIT_RUNNING)
    try:
        state["cancel"].clear()
        with state["lock"]:
            state["audit"] = {**_idle_audit(), "state": "running", "started_at": time.time()}
        thread = threading.Thread(target=_audit_worker, args=(agent, state), name="local-duplicates-audit", daemon=True)
        state["thread"] = thread
        thread.start()
    except BaseException:
        with state["lock"]:
            state["audit"] = {**_idle_audit(), "state": "failed", "finished_at": time.time(), "error": "Contrôle non démarré."}
        agent.storage_audit_lock.release()
        raise
    return {"started": True}


def cancel_action(agent, params):
    state = _state(agent)
    with state["lock"]:
        running = state["audit"]["state"] == "running"
    if running:
        state["cancel"].set()
    return {"cancel_requested": running}


def _report_summary(agent):
    """Chiffres du rapport ; ceux du périmètre demandent un plan, calculé une fois par rapport écrit."""
    state = _state(agent)
    try:
        info = _data_path(agent, REPORT_NAME).stat()
        stamp = (info.st_mtime_ns, info.st_size)
    except (OSError, ValueError):
        stamp = None
    with state["lock"]:
        cached = state["summary"]
        if cached is not None and cached["stamp"] == stamp:
            return cached["value"]
    value = {"present": False, "finished_at": None, "complete": False, "consumed": False, "groups": 0, "duplicate_bytes": 0,
             "in_scope_groups": 0, "in_scope_reclaimable_bytes": 0, "out_of_scope_groups": 0, "errors": 0}
    report = _read_report(agent) if stamp is not None else None
    if report is not None and report.get("worker_id_prefix") == _worker_prefix(agent):
        result = report["result"]
        groups = len(result["duplicate_groups"])
        duplicate_bytes, errors = result.get("duplicate_bytes"), result.get("errors")
        value.update({"present": True, "finished_at": report["finished_at"], "complete": result.get("complete") is True,
                      "consumed": report.get("consumed_at") is not None, "groups": groups,
                      "duplicate_bytes": duplicate_bytes if _is_int(duplicate_bytes) else 0,
                      "errors": len(errors) if isinstance(errors, list) else 0, "out_of_scope_groups": groups})
        try:
            plan = _build_plan(agent, report, "link")
            value.update({"in_scope_groups": plan.counts["in_scope_groups"], "in_scope_reclaimable_bytes": plan.public["freed_bytes"],
                          "out_of_scope_groups": max(0, groups - plan.counts["in_scope_groups"])})
        except Exception:   # noqa: BLE001 - un rapport abîmé donne des zéros, jamais une erreur d'état
            pass
    with state["lock"]:
        state["summary"] = {"stamp": stamp, "value": value}
    return value


def status_action(agent, params):
    state = _state(agent)
    summary = dict(_report_summary(agent))
    age = max(0, int(time.time() - summary["finished_at"])) if summary["present"] else None
    with state["lock"]:
        audit = dict(state["audit"])
        applying = {**state["apply"], "errors": [dict(item) for item in state["apply"]["errors"]]}
    return {"audit": audit, "apply": applying, "report": {**summary, "age_seconds": age}}


def status_summary(agent):
    """Résumé pour l'action ``status`` : des nombres et des états, jamais un chemin."""
    state = _state(agent)
    summary = _report_summary(agent)
    with state["lock"]:
        audit, apply_state = dict(state["audit"]), state["apply"]["state"]
    return {"audit_state": audit["state"], "progress": int(audit["progress"]),
            "finished_at": summary["finished_at"] if summary["present"] else audit["finished_at"],
            "groups": summary["groups"], "in_scope_groups": summary["in_scope_groups"],
            "reclaimable_bytes": summary["in_scope_reclaimable_bytes"], "apply_state": apply_state}


# ------------------------------------------------------------------ application
def _engine_running(agent):
    try:
        return bool(agent._runtime_running("image_generation"))
    except Exception:   # noqa: BLE001 - état inconnu : on ne touche à rien
        return True


def _still(located, single=False):
    """Relit le fichier par ``lstat`` juste avant d'agir : son état s'il est toujours celui qui vient d'être vérifié, sinon None."""
    try:
        info = os.lstat(located.resolved)
    except OSError:
        return None
    unchanged = _plain_file(info) and _signature(info) == _signature(located.info) and (not single or info.st_nlink == 1)
    return info if unchanged else None


def _system_reason(error):
    if getattr(error, "errno", None) == errno.EXDEV or getattr(error, "winerror", None) == 17:
        return REASON_VOLUME
    code = getattr(error, "winerror", None) or getattr(error, "errno", None)
    return f"refusé par le système ({type(error).__name__}{' ' + str(code) if code else ''}), fichier conservé"


def _index_linked(agent, target, digest):
    """Index des ressources : la copie liée garde son empreinte malgré sa nouvelle date (celle de l'exemplaire gardé)."""
    asset = _asset_key(target.relative)
    if asset is None:
        return
    try:
        info = os.lstat(target.resolved)
        files = comfyui_assets.load_index(agent.root)
        entry = files.get(comfyui_assets.index_key(*asset))
        if isinstance(entry, dict):
            entry.update({"size_bytes": info.st_size, "mtime_ns": info.st_mtime_ns, "sha256": digest})
            comfyui_assets.save_index(agent.root, files)
    except Exception:   # noqa: BLE001 - le lien est fait : un index illisible ne le fait jamais passer pour un échec
        pass   # le relevé suivant refait l'entrée


def _link(agent, keep, target, digest):
    # Verrou des ressources : le relevé scan_assets ne s'intercale pas entre le remplacement et la correction de l'index.
    with agent.assets_lock:
        target_now, keep_now = _still(target, single=True), _still(keep)
        if target_now is None or keep_now is None:
            return "failed", REASON_CHANGED
        if _read_only(keep_now) or _read_only(target_now):
            # Windows refuse de retirer le nom temporaire d'un fichier en lecture seule et de remplacer une copie en
            # lecture seule ; l'attribut, partagé entre les noms d'un fichier, n'est jamais changé : rien n'est créé.
            return "failed", REASON_READONLY
        temporary = target.resolved.parent / (TEMP_PREFIX + secrets.token_hex(8) + ".tmp")
        try:
            os.link(keep.resolved, temporary)   # même volume ; en cas d'échec rien n'a changé
        except OSError as error:
            return "failed", _system_reason(error)
        placed = False
        try:
            linked = os.lstat(temporary)
            if (linked.st_dev, linked.st_ino) != (keep.info.st_dev, keep.info.st_ino):
                return "failed", REASON_CHANGED
            for wait in REPLACE_WAITS:
                if wait:
                    time.sleep(wait)
                try:
                    os.replace(temporary, target.resolved)   # atomique : le nom de la copie existe à tout instant
                    placed = True
                    break
                except PermissionError:
                    continue
            if not placed:
                return "busy", REASON_BUSY
        except OSError as error:
            return "failed", _system_reason(error)
        finally:
            if not placed:
                try:
                    os.unlink(temporary)   # un nom en trop de l'exemplaire gardé, jamais son contenu
                except OSError:
                    pass
        _index_linked(agent, target, digest)
    return "done", ""


def _delete(agent, scope, keep, target):
    with agent.assets_lock:
        if _still(target, single=True) is None:
            return "failed", REASON_CHANGED
        # Dernier regard sur l'exemplaire gardé, après toutes les lectures : sans lui la copie serait le dernier exemplaire.
        try:
            current = os.lstat(keep.resolved)
        except OSError:
            return "failed", REASON_KEEP_GONE
        if not _plain_file(current) or _signature(current) != _signature(keep.info) \
                or (current.st_dev, current.st_ino) == (target.info.st_dev, target.info.st_ino):
            return "failed", REASON_KEEP_GONE
        try:
            remove_worker_file(target.resolved, scope.models_root, missing_ok=False)
        except PermissionError:
            return "busy", REASON_BUSY
        except (OSError, ValueError) as error:
            return "failed", _system_reason(error)
        asset = _asset_key(target.relative)
        if asset is not None:
            try:
                comfyui_assets.forget(agent.root, *asset)
            except Exception:   # noqa: BLE001 - la copie est supprimée : l'index se corrige au relevé suivant
                pass
    return "done", ""


def _run_operation(agent, scope, run, operation, keep_entry, target_entry, verified):
    """Une opération : tout est relu sur le disque, puis lien ou suppression. Rend (issue, raison)."""
    scope.refresh()
    keep, target = scope.locate(keep_entry["path"]), scope.locate(target_entry["path"])
    for located, entry, name in ((keep, keep_entry, operation["keep"]), (target, target_entry, operation["target"])):
        if not located.ok or located.relative.as_posix() != name or not _plain_file(located.info) \
                or _signature(located.info) != (entry["size_bytes"], entry["mtime_ns"], entry["device"], entry["inode"]):
            return "failed", REASON_CHANGED
    # Le nombre de liens de l'exemplaire gardé n'est pas comparé : il grandit à chaque lien créé.
    if target.info.st_nlink != 1 or (keep.info.st_dev, keep.info.st_ino) == (target.info.st_dev, target.info.st_ino):
        return "failed", REASON_CHANGED
    if keep.info.st_dev != target.info.st_dev:
        return "failed", REASON_VOLUME
    try:
        keep_id = os.path.normcase(str(keep.resolved))
        if verified.get(keep_id) != _signature(keep.info):
            verified.pop(keep_id, None)
            if storage_audit.verified_sha256({**keep_entry, "path": str(keep.resolved)}) != operation["sha256"]:
                return "failed", REASON_CHANGED
            verified[keep_id] = _signature(keep.info)
        if storage_audit.verified_sha256({**target_entry, "path": str(target.resolved)}) != operation["sha256"]:
            return "failed", REASON_CHANGED
    except PermissionError:
        return "busy", REASON_BUSY
    except OSError:
        return "failed", REASON_CHANGED
    fields = {key: operation[key] for key in ("op", "keep", "target", "sha256", "size_bytes")}
    try:
        _journal(agent, run, "intent", strict=True, **fields)   # écrit AVANT d'agir
    except (OSError, ValueError):
        return "failed", "journal des doublons non écrit, fichier conservé"
    if operation["op"] == "delete":
        return _delete(agent, scope, keep, target)
    return _link(agent, keep, target, operation["sha256"])


def _record(state, operation, outcome, reason):
    with state["lock"]:
        progress = state["apply"]
        progress["done"] += 1
        if outcome == "done":
            progress["deleted" if operation["op"] == "delete" else "linked"] += 1
            progress["freed_bytes"] += operation["size_bytes"]
        elif outcome == "skipped":
            progress["skipped"] += 1
        else:
            progress["failed"] += 1
            progress["busy"] += outcome == "busy"
            if len(progress["errors"]) < ERRORS_MAX:
                progress["errors"].append({"target": operation["target"], "error": reason})


def _consume_report(agent, finished_at):
    try:
        report = _read_report(agent)
        if report is not None and report["finished_at"] == finished_at and report.get("consumed_at") is None:
            report["consumed_at"] = time.time()
            atomic_json(_data_path(agent, REPORT_NAME), report)
    except (OSError, ValueError) as error:
        print(f"Rapport des doublons non marqué comme utilisé ({type(error).__name__}).", flush=True)


def _release_apply(agent):
    with agent.maintenance_lock:
        agent.maintenance_count -= 1
        agent.local_cleanup_active = False
        agent.duplicates_apply_active = False
        agent.maintenance_active = agent.maintenance_count > 0 or bool(agent.maintenance_recovery)
    agent.storage_audit_lock.release()


def _apply_worker(agent, state, run, plan):
    stop = getattr(agent, "stop_requested", None)
    try:
        _journal(agent, run, "start", strict=True)
        verified, halted, busy = {}, "", False
        for operation, keep_entry, target_entry in plan.pairs:
            fields = {key: operation[key] for key in ("op", "keep", "target", "sha256", "size_bytes")}
            if not halted:
                if stop is not None and stop.is_set():
                    halted = REASON_STOPPING
                elif _engine_running(agent):
                    # Un job déjà attribué a pu démarrer le moteur : il rouvre ses modèles, on s'arrête là.
                    halted = REASON_ENGINE_STARTED
            if halted:
                _record(state, operation, "skipped", halted)
                _journal(agent, run, "skipped", error=halted, **fields)
                continue
            with state["lock"]:
                state["apply"]["current"] = operation["target"]
            try:
                outcome, reason = _run_operation(agent, plan.scope, run, operation, keep_entry, target_entry, verified)
            except Exception as error:   # noqa: BLE001 - une opération en échec n'arrête pas les suivantes
                outcome, reason = "failed", f"erreur inattendue ({type(error).__name__}), fichier conservé"
            busy = busy or outcome == "busy"
            _record(state, operation, outcome, reason)
            _journal(agent, run, "done" if outcome == "done" else "failed", error=reason, **fields)
        if halted == REASON_STOPPING:
            _journal(agent, run, "interrupted")
        else:
            _journal(agent, run, "end")
            # Rapport à usage unique, sauf échec passager : un fichier utilisé se retente sans refaire le contrôle.
            if not busy:
                _consume_report(agent, plan.public["report_finished_at"])
    except Exception as error:   # noqa: BLE001 - passage interrompu : les fichiers restants ne sont pas touchés
        print(f"Suppression des doublons interrompue ({type(error).__name__}).", flush=True)
        with state["lock"]:
            state["apply"]["failed"] += 1
            del state["apply"]["errors"][ERRORS_MAX - 1:]   # la raison de l'arrêt reste lisible, dans la limite de 50
            state["apply"]["errors"].append({"target": "", "error": f"passage interrompu ({type(error).__name__}), fichiers restants conservés"})
        _journal(agent, run, "interrupted")
    finally:
        try:
            _data_path(agent, PLAN_NAME).unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
        _release_apply(agent)
        with agent.inventory_lock:
            agent.inventory_revision += 1
            agent.inventory_updated_at = 0
            agent.inventory_snapshot = {}
        with state["lock"]:
            if state["apply"]["run"] == run:   # les drapeaux sont rendus : un passage suivant a déjà pu poser son état
                state["apply"].update({"state": "done", "current": ""})
            state["summary"] = None
        agent.reconnect_requested.set()


def apply_action(agent, params):
    same_folder = params.get("same_folder") or "link"
    state = _state(agent)
    with agent.maintenance_lock:
        # Gardes de départ : tout refus est sans aucune écriture.
        if same_folder not in PHRASES or params.get("phrase") != PHRASES[same_folder]:
            raise ValueError(REFUSAL_PHRASE)
        if getattr(agent, "duplicates_apply_active", False):
            raise ValueError(REFUSAL_APPLY_RUNNING)
        if agent.local_cleanup_active:
            raise ValueError(REFUSAL_CLEANUP)
        if agent.maintenance_active:
            raise ValueError(REFUSAL_MAINTENANCE)
        if getattr(agent, "asset_commands_active", 0) > 0:
            raise ValueError(REFUSAL_ASSET)
        try:
            agent._assert_no_active_jobs()
        except Exception as error:
            raise ValueError(REFUSAL_JOBS + str(error)) from error
        report = _usable_report(agent)
        # Tenu jusqu'à la fin : ni le contrôle local ni celui du site ne lisent des fichiers en cours de remplacement.
        if not agent.storage_audit_lock.acquire(blocking=False):
            raise ValueError(REFUSAL_AUDIT)
        try:
            if _engine_running(agent):
                raise ValueError(REFUSAL_ENGINE)
            plan = _build_plan(agent, report, same_folder)
            if str(params.get("digest") or "").lower() != plan.public["digest"]:
                raise ValueError(REFUSAL_DIGEST)
            run = secrets.token_hex(8)
            thread = threading.Thread(target=_apply_worker, args=(agent, state, run, plan), name="local-duplicates-apply", daemon=True)
        except BaseException:
            agent.storage_audit_lock.release()
            raise
        agent.maintenance_count += 1
        agent.maintenance_active = True
        agent.local_cleanup_active = True
        agent.duplicates_apply_active = True
        with state["lock"]:
            state["apply"] = {**_idle_apply(), "state": "running", "run": run, "total": len(plan.pairs)}
        state["apply_thread"] = thread
        try:
            thread.start()
        except BaseException:
            with state["lock"]:
                state["apply"]["state"] = "done"
            _release_apply(agent)
            raise
    return {"started": True, "operations": len(plan.pairs)}


# ------------------------------------------------------------------ reprise après interruption
def _remove_leftovers(scope):
    """Retire les noms temporaires d'un passage interrompu : seulement un nom EN TROP d'un contenu qui vit ailleurs."""
    removed, pending = 0, [scope.models_root]
    while pending:
        folder = pending.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        info = os.lstat(entry.path)   # DirEntry ne donne pas le nombre de liens sous Windows
                        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            if entry.name.casefold() not in EXCLUDED_PARTS and not scope._has_marker(Path(entry.path)):
                                pending.append(entry.path)
                        elif stat.S_ISREG(info.st_mode) and TEMP_PATTERN.fullmatch(entry.name) and info.st_nlink > 1:
                            os.unlink(entry.path)
                            removed += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return removed


def _mark_interrupted(agent):
    """Écrit « interrupted » si le dernier passage du journal n'a pas de fin."""
    try:
        path = _data_path(agent, JOURNAL_NAME)
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 256 * 1024))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except (OSError, ValueError):
        return False
    events = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and isinstance(item.get("run"), str) and re.fullmatch(r"[0-9a-f]{16}", item["run"]):
            events.append(item)
    if not events:
        return False
    run = events[-1]["run"]
    if any(item["run"] == run and item.get("event") in ("end", "interrupted") for item in events):
        return False
    _journal(agent, run, "interrupted")
    return True


def recover(agent):
    """Au démarrage de l'agent. Rien n'est repris : chaque opération est atomique, le rapport reste non consommé."""
    scope = _Scope(agent)
    removed = _remove_leftovers(scope) if scope.allowed and scope.models_root.is_dir() else 0
    return {"removed": removed, "interrupted": _mark_interrupted(agent)}


LOCAL_ACTIONS = {
    "duplicates-audit": {"handler": audit_action, "params": {}, "read": False},
    "duplicates-cancel": {"handler": cancel_action, "params": {}, "read": False},
    "duplicates-status": {"handler": status_action, "params": {}, "read": True},
    "duplicates-plan": {"handler": plan_action, "params": {"same_folder": v_choice("link", "delete")}, "read": True},
    "duplicates-apply": {"handler": apply_action, "read": False,
                         "params": {"digest": v_hex(64), "same_folder": v_choice("link", "delete"),
                                    "phrase": v_choice(*PHRASES.values())}},
}
