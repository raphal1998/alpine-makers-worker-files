# -*- coding: utf-8 -*-
"""Comptes IA du Worker (agent 1.35.0, capacité « ai_accounts_v1 », corrections 1.35.1) : Claude Code (« Claude ») et
Codex (« ChatGPT »).

Le site gère depuis Mes Workers → Comptes IA les comptes avec lesquels les CLI de CE Worker travaillent
(assistant de code d'Alpine Laser Studio), uniquement par les commandes signées de l'agent :

- ``default`` : profil normal de l'utilisateur qui exécute l'agent (aucune variable imposée), toujours présent ;
- comptes ajoutés : registre ``<Worker>/data/ai-accounts.json``, profil ``<Worker>/data/ai-accounts/<fournisseur>/<compte>/``
  (CLAUDE_CONFIG_DIR pour Claude Code, CODEX_HOME pour Codex) ;
- jeton d'abonnement (``claude setup-token``) ou clé API Anthropic : ``<profil>/alpine-auth.json``, écrit atomiquement,
  jamais renvoyé ni journalisé, réinjecté dans l'environnement de la CLI (``laser_engine.assistant.account_environment``).

Connexions : jeton ou clé (Claude), clé API par l'entrée standard (Codex, jamais sur la ligne de commande), code
d'appareil (Codex) et navigateur (les deux). Le lien et le code à usage unique remontent au site par la progression
de la commande (``login_url``, ``user_code``, ``awaiting_code``) ; le code que Claude affiche après la connexion revient
par ``ai_accounts.login_input``. Toute réponse contient l'état de tous les comptes, sans adresse ni organisation.
Bibliothèque standard seulement (Windows et Linux).
"""

from __future__ import annotations

import codecs
import json
import os
import queue
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

if __package__:
    from .laser_engine import assistant
else:
    from laser_engine import assistant

PROVIDERS = ("claude", "codex")
LABELS = {"claude": "Claude", "codex": "ChatGPT"}
PRODUCTS = assistant.PRODUCTS
DEFAULT_ACCOUNT = assistant.DEFAULT_ACCOUNT
DEFAULT_LABEL = "Compte du PC"
ACCOUNT_PATTERN = assistant.ACCOUNT_PATTERN
METHODS = {"claude": ("browser", "token", "api_key"), "codex": ("device", "browser", "api_key")}
SECRET_METHODS = ("token", "api_key")
COMMANDS = {
    # commande : (clés obligatoires, clés facultatives)
    "ai_accounts.status": (frozenset(), frozenset()),
    "ai_accounts.add": (frozenset({"provider", "label"}), frozenset()),
    "ai_accounts.remove": (frozenset({"provider", "account_id"}), frozenset()),
    "ai_accounts.logout": (frozenset({"provider", "account_id"}), frozenset()),
    "ai_accounts.login": (frozenset({"provider", "account_id", "method"}), frozenset({"secret"})),
    "ai_accounts.login_input": (frozenset({"login_command_id", "code"}), frozenset()),
    "ai_accounts.login_cancel": (frozenset({"login_command_id"}), frozenset()),
}
LABEL_MAX = 40
CODE_MIN, CODE_MAX = 4, 512
COMMAND_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_ADDED_PER_PROVIDER = 15
LOGIN_TIMEOUT = 15 * 60
LOGOUT_TIMEOUT = 60
API_KEY_TIMEOUT = 120
STATUS_WORKERS = 4
OUTPUT_LIMIT = 64 * 1024
URL_MAX = 2048
REGISTRY_VERSION = 1

URL_PATTERN = re.compile(r"https://[^\s<>\"'`\x00-\x1f\x7f]+")
USER_CODE_PATTERN = re.compile(r"(?<![A-Za-z0-9-])([A-Z0-9]{4,8}(?:-[A-Z0-9]{4,8}){1,2})(?![A-Za-z0-9-])")
OSC_LINK = re.compile(r"\x1b\]8;[^;\x07\x1b]*;([^\x07\x1b]*)(?:\x07|\x1b\\)")
OSC_OTHER = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ESCAPE = re.compile(r"\x1b[@-Z\\-_]")
REPROMPT_PATTERN = re.compile(r"paste (?:the )?code|code here|invalid code|try again|r[ée]essa", re.IGNORECASE)


class AiAccountsError(RuntimeError):
    """Refus ou échec d'une commande de comptes IA (message français court, jamais de secret)."""


def clean_output(text: str) -> str:
    """Sortie de CLI lisible : liens OSC 8 gardés, séquences de terminal et caractères de contrôle retirés."""
    text = OSC_LINK.sub(lambda match: f" {match.group(1)} ", str(text or ""))
    text = ESCAPE.sub("", CSI.sub("", OSC_OTHER.sub("", text)))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(character for character in text if character in "\n\t" or ord(character) >= 32 and character != "\x7f")


def sanitize(text: str, hidden: tuple[str, ...] = ()) -> str:
    """Raison courte d'un échec : sans secret, lien (paramètres d'état), adresse ni jeton."""
    text = clean_output(text)
    for value in hidden:
        if value:
            text = text.replace(value, "«masqué»")
    text = re.sub(r"https?://\S+", "<lien>", text)
    text = re.sub(r"[^\s@]+@[^\s@]+\.[^\s@]+", "<adresse>", text)
    text = re.sub(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{6,}", "«clé masquée»", text)
    text = re.sub(r"[A-Za-z0-9_\-.=+/]{32,}", "«masqué»", text)
    return " ".join(text.split())[-300:]


def find_url(text: str) -> str:
    urls = [match.group(0).rstrip(".,;:!?)]}>»'\"") for match in URL_PATTERN.finditer(text)]
    urls = [url for url in urls if len(url) > len("https://") and len(url) <= URL_MAX]
    return urls[-1] if urls else ""


def find_user_code(text: str) -> str:
    for match in USER_CODE_PATTERN.finditer(URL_PATTERN.sub(" ", text)):
        code = match.group(1)
        if 4 <= len(code) <= 24:
            return code
    return ""


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Écriture atomique, lisible par le seul utilisateur de l'agent sous Linux."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _is_link(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        isjunction = getattr(os.path, "isjunction", None)
        if isjunction is not None:
            return bool(isjunction(path))
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except FileNotFoundError:
        return False


def _force_remove(function, path, _info):
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        function(path)
    except OSError:
        pass


class LoginSession:
    """Connexion reçue (en attente de sa file) ou en cours : processus de CLI (navigateur, code d'appareil) ou secret.

    Enregistrée dès la réception de la commande (1.35.1) : ``login_cancel`` et ``cancel_all`` atteignent aussi une
    connexion qui attend encore la commande précédente du même compte. ``started`` passe à vrai, sous le verrou des
    connexions, au moment où la CLI est lancée ou le secret appliqué ; avant, une annulation l'empêche de démarrer."""

    def __init__(self, command_id: str, provider: str = "", account_id: str = "", method: str = ""):
        self.command_id = command_id
        self.provider = provider
        self.account_id = account_id
        self.method = method
        self.process: subprocess.Popen | None = None
        self.reader: threading.Thread | None = None
        self.started = False
        self.cancelled = threading.Event()
        self.cancel_reason = ""
        self.write_lock = threading.Lock()
        self.url = ""
        self.user_code = ""
        self.awaiting_code = False
        self.codes_sent = 0
        # Sortie de la CLI depuis le dernier code collé : une nouvelle invite rouvre la saisie.
        self.after_code = ""

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def cancel(self, reason: str = "") -> None:
        if reason and not self.cancel_reason:
            self.cancel_reason = reason
        self.cancelled.set()

    def extras(self) -> dict[str, Any]:
        extra: dict[str, Any] = {"phase": "login"}
        if self.url:
            extra["login_url"] = self.url
        if self.user_code:
            extra["user_code"] = self.user_code
        if self.provider == "claude" and self.method == "browser":
            extra["awaiting_code"] = self.awaiting_code
        return extra


class AiAccounts:
    """Comptes Claude Code / Codex d'un Worker et leurs connexions, pilotés par les commandes ``ai_accounts.*``."""

    def __init__(self, root: str | Path, progress: Callable[..., Any] | None = None,
                 logins: dict[str, LoginSession] | None = None, logins_lock: threading.Lock | None = None,
                 error: type[Exception] = AiAccountsError):
        self.root = Path(root)
        self.progress_callback = progress
        self.logins = logins if logins is not None else {}
        self.logins_lock = logins_lock if logins_lock is not None else threading.Lock()
        self.error = error
        self.registry_lock = threading.RLock()

    # ------------------------------------------------------------------------------------------ commandes
    def execute(self, command: str, payload: Any, command_id: str | None = None) -> dict[str, Any]:
        if command not in COMMANDS:
            raise self.error("Commande de comptes IA inconnue.")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise self.error("Paramètres de comptes IA invalides (objet attendu).")
        required, optional = COMMANDS[command]
        keys = set(payload)
        if keys - required - optional:
            raise self.error("Paramètre inattendu pour cette commande de comptes IA : refusé.")
        if required - keys:
            raise self.error("Paramètre manquant pour cette commande de comptes IA.")
        handler = {"ai_accounts.status": lambda: self.snapshot(),
                   "ai_accounts.add": lambda: self.add(payload),
                   "ai_accounts.remove": lambda: self.remove(payload),
                   "ai_accounts.logout": lambda: self.logout(payload),
                   "ai_accounts.login": lambda: self.login(payload, command_id),
                   "ai_accounts.login_input": lambda: self.login_input(payload),
                   "ai_accounts.login_cancel": lambda: self.login_cancel(payload)}[command]
        try:
            return handler()
        except assistant.AssistantError as error:
            raise self.error(str(error)) from None

    # ------------------------------------------------------------------------------------------ validation
    def _provider(self, value: Any) -> str:
        if not isinstance(value, str) or value not in PROVIDERS:
            raise self.error("Fournisseur inconnu (claude ou codex).")
        return value

    def _account_id(self, value: Any) -> str:
        if not isinstance(value, str) or not ACCOUNT_PATTERN.fullmatch(value):
            raise self.error("Identifiant de compte invalide (lettres minuscules, chiffres et tirets, 32 caractères au plus).")
        return value

    def _label(self, value: Any) -> str:
        label = value.strip() if isinstance(value, str) else ""
        if not 1 <= len(label) <= LABEL_MAX or not label.isprintable():
            raise self.error(f"Nom du compte : 1 à {LABEL_MAX} caractères imprimables.")
        return label

    def _method(self, provider: str, value: Any) -> str:
        if not isinstance(value, str) or value not in METHODS[provider]:
            raise self.error(f"Méthode de connexion inconnue pour {LABELS[provider]} ({', '.join(METHODS[provider])}).")
        return value

    def _command_id(self, value: Any) -> str:
        if not isinstance(value, str) or not COMMAND_ID_PATTERN.fullmatch(value):
            raise self.error("Identifiant de la connexion invalide.")
        return value

    # ------------------------------------------------------------------------------------------ registre
    @property
    def registry_path(self) -> Path:
        return self.root / "data" / "ai-accounts.json"

    def entries(self) -> list[dict[str, Any]]:
        """Comptes ajoutés (jamais « default »), dans l'ordre d'ajout ; registre illisible → refus explicite."""
        try:
            raw = self.registry_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError:
            raise self.error("Registre des comptes IA illisible sur ce Worker (data/ai-accounts.json).") from None
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
            raise self.error("Registre des comptes IA invalide sur ce Worker (data/ai-accounts.json) : corrige ou "
                             "supprime ce fichier sur le Worker.")
        entries, seen = [], set()
        for item in data["accounts"]:
            if not isinstance(item, dict):
                continue
            provider, account_id = item.get("provider"), item.get("account_id")
            if (provider not in PROVIDERS or not isinstance(account_id, str) or not ACCOUNT_PATTERN.fullmatch(account_id)
                    or account_id == DEFAULT_ACCOUNT or (provider, account_id) in seen):
                continue
            seen.add((provider, account_id))
            label = item.get("label") if isinstance(item.get("label"), str) else ""
            label = label.strip()[:LABEL_MAX] if label.strip().isprintable() else ""
            created = item.get("created_at")
            entries.append({"provider": provider, "account_id": account_id, "label": label or account_id,
                            "created_at": float(created) if isinstance(created, (int, float)) else 0.0})
        return entries

    def _save(self, entries: list[dict[str, Any]]) -> None:
        _atomic_write(self.registry_path, {"version": REGISTRY_VERSION, "accounts": entries})

    def _entry(self, provider: str, account_id: str) -> dict[str, Any] | None:
        if account_id == DEFAULT_ACCOUNT:
            return {"provider": provider, "account_id": DEFAULT_ACCOUNT, "label": DEFAULT_LABEL, "created_at": 0.0}
        return next((entry for entry in self.entries()
                     if entry["provider"] == provider and entry["account_id"] == account_id), None)

    def _require(self, provider: str, account_id: str) -> dict[str, Any]:
        entry = self._entry(provider, account_id)
        if entry is None:
            raise self.error(f"Compte {LABELS[provider]} introuvable sur ce Worker (déjà supprimé ?).")
        return entry

    def _new_id(self, provider: str, label: str, taken: set[str]) -> str:
        ascii_label = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode("ascii").lower()
        slug = re.sub(r"[^a-z0-9]+", "-", ascii_label).strip("-")[:20].strip("-")
        if not slug or slug == DEFAULT_ACCOUNT:
            slug = provider
        for _ in range(32):
            account_id = f"{slug}-{secrets.token_hex(3)}"
            if (ACCOUNT_PATTERN.fullmatch(account_id) and account_id not in taken
                    and not os.path.lexists(assistant.accounts_base(provider, self.root) / account_id)):
                return account_id
        raise self.error("Impossible de créer un identifiant de compte libre : réessaie.")

    # ------------------------------------------------------------------------------------------ secrets Claude
    def _secret_path(self, provider: str, account_id: str) -> Path:
        return assistant.account_dir(provider, account_id, self.root) / assistant.ACCOUNT_SECRET_FILE

    def _write_secret(self, provider: str, account_id: str, kind: str, value: str) -> None:
        folder = assistant.account_dir(provider, account_id, self.root)
        if _is_link(folder):
            raise self.error("Dossier du compte remplacé par un lien : refusé.")
        _atomic_write(folder / assistant.ACCOUNT_SECRET_FILE, {"kind": kind, "value": value})

    def _delete_secret(self, provider: str, account_id: str) -> bool:
        if provider != "claude":
            return False
        try:
            self._secret_path(provider, account_id).unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            raise self.error("Suppression du jeton enregistré impossible sur ce Worker : réessaie.") from None

    # ------------------------------------------------------------------------------------------ état
    def _cwd(self, provider: str, account_id: str) -> Path:
        home = assistant.account_home(provider, account_id, self.root)
        return home if home is not None and home.is_dir() else self.root

    def _account_status(self, entry: dict[str, Any], executable: str) -> dict[str, Any]:
        provider, account_id = entry["provider"], entry["account_id"]
        row: dict[str, Any] = {"provider": provider, "account_id": account_id, "label": entry["label"],
                               "default": account_id == DEFAULT_ACCOUNT, "logged_in": False, "method": "", "message": ""}
        if not executable:
            row["message"] = assistant.account_message(provider, False)
            return row
        home = assistant.account_home(provider, account_id, self.root)
        if home is not None and not home.is_dir():
            row["message"] = "Dossier du compte absent sur ce Worker : supprime ce compte puis ajoute-le à nouveau."
            return row
        secret = assistant.read_account_secret(provider, account_id, self.root)
        kind = secret["kind"] if secret else ""
        state = assistant.login_state(provider, executable, assistant.account_environment(provider, account_id, self.root), kind)
        row["logged_in"], row["method"] = bool(state["logged_in"]), str(state.get("method") or "")
        if state.get("plan"):
            row["plan"] = str(state["plan"])
        row["message"] = assistant.account_message(provider, True, state, kind)
        if row["logged_in"]:
            row["verified"] = not secret or bool(secret.get("verified"))
            if not row["verified"]:
                stored = "Jeton d’abonnement enregistré" if kind == "token" else "Clé API enregistrée"
                row["message"] = (f"{stored} : Claude Code l’accepte sans le contrôler. Il sera confirmé au premier appel "
                                  "réussi de l’assistant de Laser Studio ; s’il est faux, cet appel échouera.")
        return row

    def snapshot(self) -> dict[str, Any]:
        """État de tous les comptes (défaut d'abord) et des deux CLI, renvoyé par chaque commande."""
        entries = self.entries()
        executables, cli = {}, {}
        for provider in PROVIDERS:
            executable = assistant.find_cli(provider)
            executables[provider] = executable
            cli[provider] = {"installed": bool(executable), "version": assistant.cli_version(executable) if executable else ""}
        accounts = []
        for provider in PROVIDERS:
            accounts.append({"provider": provider, "account_id": DEFAULT_ACCOUNT, "label": DEFAULT_LABEL, "created_at": 0.0})
            accounts.extend(sorted((entry for entry in entries if entry["provider"] == provider),
                                   key=lambda entry: entry["created_at"]))

        def describe(entry):
            try:
                return self._account_status(entry, executables[entry["provider"]])
            except assistant.AssistantError as error:
                return {"provider": entry["provider"], "account_id": entry["account_id"], "label": entry["label"],
                        "default": entry["account_id"] == DEFAULT_ACCOUNT, "logged_in": False, "method": "",
                        "message": str(error)[:300]}

        with ThreadPoolExecutor(max_workers=STATUS_WORKERS, thread_name_prefix="ai-accounts-status") as pool:
            rows = list(pool.map(describe, accounts))
        return {"accounts": rows, "cli": cli, "checked_at": time.time()}

    def _row(self, snapshot: dict[str, Any], provider: str, account_id: str) -> dict[str, Any]:
        return next((row for row in snapshot["accounts"] if row["provider"] == provider and row["account_id"] == account_id), {})

    # ------------------------------------------------------------------------------------------ ajout / retrait
    def add(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider, label = self._provider(payload.get("provider")), self._label(payload.get("label"))
        with self.registry_lock:
            entries = self.entries()
            if sum(1 for entry in entries if entry["provider"] == provider) >= MAX_ADDED_PER_PROVIDER:
                raise self.error(f"{MAX_ADDED_PER_PROVIDER} comptes {LABELS[provider]} ajoutés au plus par Worker.")
            account_id = self._new_id(provider, label, {entry["account_id"] for entry in entries if entry["provider"] == provider})
            folder = assistant.account_dir(provider, account_id, self.root)
            folder.mkdir(parents=True, exist_ok=False)
            if os.name != "nt":
                os.chmod(folder, 0o700)
            try:
                self._save(entries + [{"provider": provider, "account_id": account_id, "label": label, "created_at": time.time()}])
            except Exception:
                shutil.rmtree(folder, ignore_errors=True)
                raise
        return {"added": {"provider": provider, "account_id": account_id, "label": label}, **self.snapshot()}

    def _remove_folder(self, provider: str, account_id: str) -> None:
        base = assistant.accounts_base(provider, self.root)
        if _is_link(base / account_id):
            raise self.error("Dossier du compte remplacé par un lien : rien n'a été supprimé.")
        folder = assistant.account_dir(provider, account_id, self.root)
        if not folder.exists():
            return
        shutil.rmtree(folder, **({"onexc": _force_remove} if sys.version_info >= (3, 12) else {"onerror": _force_remove}))
        if folder.exists():
            raise self.error("Suppression du dossier du compte incomplète (fichier en cours d'utilisation ?) : réessaie.")

    def remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider, account_id = self._provider(payload.get("provider")), self._account_id(payload.get("account_id"))
        if account_id == DEFAULT_ACCOUNT:
            raise self.error("Le compte par défaut se déconnecte mais ne se supprime pas.")
        self._require(provider, account_id)
        if self._login_for(provider, account_id):
            raise self.error("Une connexion est en cours pour ce compte : annule-la d'abord.")
        # Déconnexion de bonne foi (jeton révoqué côté CLI quand elle le permet), puis suppression du profil.
        executable = assistant.find_cli(provider)
        folder = assistant.account_dir(provider, account_id, self.root)
        if executable and folder.is_dir() and not _is_link(assistant.accounts_base(provider, self.root) / account_id):
            try:
                self._delete_secret(provider, account_id)
                self._cli_logout(provider, account_id, executable)
            except Exception:  # noqa: BLE001 - de bonne foi : le profil est supprimé juste après
                pass
        with self.registry_lock:
            self._remove_folder(provider, account_id)
            self._save([entry for entry in self.entries()
                        if not (entry["provider"] == provider and entry["account_id"] == account_id)])
        return {"removed": {"provider": provider, "account_id": account_id}, **self.snapshot()}

    # ------------------------------------------------------------------------------------------ déconnexion
    def _cli_logout(self, provider: str, account_id: str, executable: str) -> tuple[int, str]:
        command = [executable, "auth", "logout"] if provider == "claude" else [executable, "logout"]
        try:
            # Borné pour de bon, CLI .cmd comprise (arbre de processus arrêté à l'échéance).
            code, stdout, stderr = assistant.run_bounded(command, LOGOUT_TIMEOUT,
                                                         env=assistant.account_environment(provider, account_id, self.root),
                                                         cwd=str(self._cwd(provider, account_id)))
        except (OSError, subprocess.SubprocessError) as error:
            return -1, type(error).__name__
        return code, stdout + stderr

    def logout(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider, account_id = self._provider(payload.get("provider")), self._account_id(payload.get("account_id"))
        self._require(provider, account_id)
        if self._login_for(provider, account_id):
            raise self.error("Une connexion est en cours pour ce compte : annule-la d'abord.")
        removed = self._delete_secret(provider, account_id)
        executable = assistant.find_cli(provider)
        if not executable:
            raise self.error(f"{PRODUCTS[provider]} n'est pas installé sur ce Worker : déconnexion impossible sans la CLI"
                             + (" (jeton enregistré supprimé)." if removed else "."))
        self._cli_logout(provider, account_id, executable)
        snapshot = self.snapshot()
        if self._row(snapshot, provider, account_id).get("logged_in"):
            raise self.error(f"Déconnexion non confirmée : {PRODUCTS[provider]} indique toujours une connexion pour ce compte "
                             "(identifiants définis hors de son profil, par exemple une variable d'environnement du Worker).")
        return {"logged_out": True, **snapshot}

    # ------------------------------------------------------------------------------------------ connexion
    def reserve_login(self, command_id: Any) -> None:
        """Connexion reçue par l'agent, avant toute attente de file : annulable dès maintenant (1.35.1)."""
        if not isinstance(command_id, str) or not COMMAND_ID_PATTERN.fullmatch(command_id):
            return
        with self.logins_lock:
            if command_id not in self.logins:
                self.logins[command_id] = LoginSession(command_id)

    def release_login(self, command_id: Any) -> None:
        """Fin de la commande de connexion côté agent : une réservation restée sans suite (commande refusée avant
        ``login``) est oubliée."""
        with self.logins_lock:
            session = self.logins.get(command_id)
            if session is not None and not session.alive():
                self.logins.pop(command_id, None)

    def _claim_login(self, command_id: str | None) -> LoginSession:
        identifier = str(command_id or f"local-{uuid.uuid4().hex}")
        with self.logins_lock:
            session = self.logins.get(identifier)
            if session is not None and session.started:
                raise self.error("Cette connexion est déjà en cours.")
            if session is None:
                session = self.logins[identifier] = LoginSession(identifier)
        return session

    def _check_cancelled(self, session: LoginSession) -> None:
        if session.cancelled.is_set():
            raise self.error(session.cancel_reason or "Connexion annulée.")

    def _start_secret_login(self, session: LoginSession) -> None:
        """Dernier point d'annulation d'une connexion par jeton ou clé : vérifié sous le verrou des connexions."""
        with self.logins_lock:
            self._check_cancelled(session)
            session.started = True

    def login(self, payload: dict[str, Any], command_id: str | None) -> dict[str, Any]:
        session = self._claim_login(command_id)
        try:
            return self._login(session, payload)
        finally:
            with self.logins_lock:
                if self.logins.get(session.command_id) is session:
                    self.logins.pop(session.command_id, None)

    def _login(self, session: LoginSession, payload: dict[str, Any]) -> dict[str, Any]:
        # Annulée pendant qu'elle attendait sa file (Annuler, mise à jour, redémarrage) : aucune CLI lancée.
        self._check_cancelled(session)
        provider = self._provider(payload.get("provider"))
        account_id = self._account_id(payload.get("account_id"))
        method = self._method(provider, payload.get("method"))
        secret = payload.get("secret")
        if method in SECRET_METHODS:
            if not assistant.valid_secret(secret):
                raise self.error(f"{'Jeton' if method == 'token' else 'Clé API'} invalide : {assistant.SECRET_MIN_CHARS} à "
                                 f"{assistant.SECRET_MAX_CHARS} caractères, sans espace.")
        elif "secret" in payload:
            raise self.error("Cette méthode de connexion n'accepte aucun jeton ni clé.")
        session.provider, session.account_id, session.method = provider, account_id, method
        self._require(provider, account_id)
        if self._login_for(provider, account_id):
            raise self.error("Une connexion est déjà en cours pour ce compte : termine-la ou annule-la.")
        executable = assistant.find_cli(provider)
        if not executable:
            raise self.error(assistant.account_message(provider, False))
        home = assistant.account_home(provider, account_id, self.root)
        if home is not None and not home.is_dir():
            raise self.error("Dossier du compte absent sur ce Worker : supprime ce compte puis ajoute-le à nouveau.")
        if provider == "claude" and method in SECRET_METHODS:
            # Jeton d'abonnement (claude setup-token) ou clé API : enregistrés sur le Worker seulement, puis vérifiés.
            self._start_secret_login(session)
            self._write_secret(provider, account_id, method, secret)
            return self._login_result(provider, account_id, method)
        if method == "api_key":
            self._start_secret_login(session)
            self._codex_api_key(account_id, executable, secret)
            return self._login_result(provider, account_id, method)
        return self._interactive_login(session, executable)

    def _login_result(self, provider: str, account_id: str, method: str) -> dict[str, Any]:
        snapshot = self.snapshot()
        return {"login": {"provider": provider, "account_id": account_id, "method": method,
                          "logged_in": bool(self._row(snapshot, provider, account_id).get("logged_in"))}, **snapshot}

    def _codex_api_key(self, account_id: str, executable: str, key: str) -> None:
        """``codex login --with-api-key`` : la clé passe par l'entrée standard, jamais par la ligne de commande."""
        try:
            # Borné pour de bon, CLI .cmd comprise (arbre de processus arrêté à l'échéance).
            code, stdout, stderr = assistant.run_bounded([executable, "login", "--with-api-key"], API_KEY_TIMEOUT,
                                                         env=self._login_environment("codex", account_id),
                                                         cwd=str(self._cwd("codex", account_id)), input_text=key + "\n")
        except subprocess.TimeoutExpired:
            raise self.error(f"Codex n'a pas confirmé la clé API en {max(1, API_KEY_TIMEOUT // 60)} min : réessaie.") from None
        except OSError as error:
            raise self.error(f"Lancement de Codex impossible ({type(error).__name__}).") from None
        if code != 0:
            reason = sanitize(stdout + " " + stderr, (key,))
            raise self.error(f"Clé API OpenAI refusée par Codex : {reason or 'code ' + str(code)}.")

    def _login_environment(self, provider: str, account_id: str) -> dict[str, str]:
        environment = assistant.account_environment(provider, account_id, self.root)
        environment["NO_COLOR"] = "1"
        return environment

    def _login_for(self, provider: str, account_id: str) -> LoginSession | None:
        with self.logins_lock:
            return next((session for session in self.logins.values()
                         if session.provider == provider and session.account_id == account_id and session.alive()), None)

    def _spawn(self, command: list[str], environment: dict[str, str], cwd: Path, with_stdin: bool) -> subprocess.Popen:
        options: dict[str, Any] = {"cwd": str(cwd), "env": environment, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                                   "stdin": subprocess.PIPE if with_stdin else subprocess.DEVNULL, "bufsize": 0}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            options["start_new_session"] = True
        process = subprocess.Popen(command, **options)
        assistant.contain(process)   # un plantage de l'agent n'abandonne plus une connexion en cours
        return process

    @staticmethod
    def terminate(process: subprocess.Popen | None) -> bool:
        """Arrête le processus de connexion et ses enfants ; vrai quand l'arrêt est confirmé."""
        return assistant.terminate_tree(process)

    def _progress(self, session: LoginSession, percent: float, message: str) -> None:
        if self.progress_callback is None or not session.command_id:
            return
        try:
            self.progress_callback(session.command_id, percent, message, **session.extras())
        except Exception:
            pass

    def _scan(self, session: LoginSession, text: str) -> bool:
        changed = False
        url = find_url(text)
        if url and url != session.url:
            session.url, changed = url, True
        if session.method == "device" and not session.user_code:
            code = find_user_code(text)
            if code:
                session.user_code, changed = code, True
        if (session.provider == "claude" and session.method == "browser" and session.url and not session.awaiting_code
                and not session.codes_sent):
            session.awaiting_code, changed = True, True
        return changed

    @staticmethod
    def _asks_again(session: LoginSession, chunk: str) -> bool:
        """Après un code collé : Claude redemande un code (refusé ou incomplet) → la saisie se rouvre."""
        if not (session.provider == "claude" and session.method == "browser" and session.codes_sent and not session.awaiting_code):
            return False
        session.after_code = (session.after_code + chunk)[-4096:]
        if REPROMPT_PATTERN.search(clean_output(session.after_code)):
            session.awaiting_code = True
            return True
        return False

    def _waiting_message(self, session: LoginSession) -> str:
        if session.method == "device":
            return ("Ouvre le lien de connexion, connecte-toi à ChatGPT puis saisis le code affiché (valable 15 min)."
                    if session.user_code else "Ouvre le lien de connexion puis connecte-toi à ChatGPT.")
        if session.provider == "claude":
            return "Ouvre le lien, connecte-toi à Claude, puis colle ici le code affiché à la fin."
        return "Ouvre le lien sur le PC Worker : la connexion revient au navigateur de ce PC."

    def _interactive_login(self, session: LoginSession, executable: str) -> dict[str, Any]:
        provider, account_id, method = session.provider, session.account_id, session.method
        command = {("claude", "browser"): [executable, "auth", "login"], ("codex", "browser"): [executable, "login"],
                   ("codex", "device"): [executable, "login", "--device-auth"]}[(provider, method)]
        environment = self._login_environment(provider, account_id)
        cwd = self._cwd(provider, account_id)
        with self.logins_lock:
            # Annulation reçue pendant les vérifications : la CLI n'est jamais lancée (même verrou que cancel_all).
            self._check_cancelled(session)
            if any(other is not session and other.provider == provider and other.account_id == account_id and other.alive()
                   for other in self.logins.values()):
                raise self.error("Une connexion est déjà en cours pour ce compte : termine-la ou annule-la.")
            try:
                session.process = self._spawn(command, environment, cwd, method == "browser")
            except OSError as error:
                raise self.error(f"Lancement de {PRODUCTS[provider]} impossible ({type(error).__name__}).") from None
            session.started = True
        try:
            return self._follow(session)
        finally:
            self.terminate(session.process)
            self._close_streams(session)

    @staticmethod
    def _close_streams(session: LoginSession) -> None:
        process = session.process
        try:
            if process.stdin is not None:
                with session.write_lock:
                    process.stdin.close()
        except OSError:
            pass
        # Le tube de sortie n'est fermé qu'une fois sa lecture finie (fermer pendant une lecture bloquée est risqué).
        reader = session.reader
        if reader is not None:
            reader.join(timeout=3)
        if (reader is None or not reader.is_alive()) and process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass

    def _follow(self, session: LoginSession) -> dict[str, Any]:
        process = session.process
        provider, product = session.provider, PRODUCTS[session.provider]
        chunks: queue.Queue = queue.Queue()

        def read_output():
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            try:
                while True:
                    data = process.stdout.read(4096)
                    if not data:
                        break
                    chunks.put(decoder.decode(data))
            except (OSError, ValueError):
                pass
            finally:
                chunks.put(None)

        session.reader = threading.Thread(target=read_output, name=f"ai-login-{session.command_id[:8]}", daemon=True)
        session.reader.start()
        self._progress(session, 5, f"Connexion {LABELS[provider]} : lancement de {product} sur le Worker…")
        deadline = time.monotonic() + LOGIN_TIMEOUT
        output, finished, exited_at = "", False, None
        while True:
            try:
                chunk = chunks.get(timeout=0.25)
            except queue.Empty:
                chunk = ""
            if chunk is None:
                finished = True
            elif chunk:
                output = (output + chunk)[-OUTPUT_LIMIT:]
                if self._scan(session, clean_output(output)):
                    self._progress(session, 30, self._waiting_message(session))
                elif self._asks_again(session, chunk):
                    self._progress(session, 30, "Code refusé ou incomplet : colle à nouveau le code affiché par Claude.")
            if session.cancelled.is_set():
                self.terminate(process)
                raise self.error(session.cancel_reason or "Connexion annulée.")
            if process.poll() is not None:
                exited_at = exited_at or time.monotonic()
                # Fin de sortie, ou un descendant qui garde le tube ouvert : on n'attend pas plus de 2 s.
                if finished or time.monotonic() - exited_at > 2:
                    break
            if time.monotonic() > deadline:
                self.terminate(process)
                raise self.error(f"Connexion non terminée en {LOGIN_TIMEOUT // 60} min : relance-la.")
        code = process.returncode
        reason = sanitize(output) or f"code {code}"
        alternative = "Jeton d'abonnement" if provider == "claude" else "Code d'appareil" if session.method == "browser" else "Clé API OpenAI"
        if code != 0:
            if not session.url and session.method == "device":
                raise self.error(f"Codex n'a pas fourni de code d'appareil ({reason}). Vérifie que la connexion par code "
                                 "d'appareil est autorisée pour ton compte ChatGPT, ou utilise la méthode « Clé API OpenAI ».")
            if not session.url:
                raise self.error(f"{product} n'a pas pu ouvrir la connexion sur ce Worker ({reason}). "
                                 f"Utilise plutôt la méthode « {alternative} ».")
            raise self.error(f"Connexion {LABELS[provider]} refusée ou interrompue : {reason}.")
        self._progress(session, 95, "Connexion terminée : vérification du compte…")
        result = self._login_result(provider, session.account_id, session.method)
        if not result["login"]["logged_in"] and not session.url:
            raise self.error(f"{product} s'est arrêté sans proposer de lien de connexion (terminal requis ?). "
                             f"Utilise plutôt la méthode « {alternative} ».")
        return result

    def _session(self, login_command_id: str) -> LoginSession:
        with self.logins_lock:
            session = self.logins.get(login_command_id)
        if session is None or not session.alive():
            raise self.error("Aucune connexion en cours pour cette commande (terminée ou annulée).")
        return session

    def login_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        login_command_id = self._command_id(payload.get("login_command_id"))
        code = payload.get("code")
        if (not isinstance(code, str) or not CODE_MIN <= len(code) <= CODE_MAX
                or assistant.REDACTION_MARKER in code.lower()
                or any(character.isspace() or not character.isprintable() for character in code)):
            raise self.error(f"Code de connexion invalide : {CODE_MIN} à {CODE_MAX} caractères, sans espace.")
        session = self._session(login_command_id)
        stdin = session.process.stdin if session.process is not None else None
        if session.method != "browser" or stdin is None:
            raise self.error("Cette connexion n'attend aucun code.")
        try:
            with session.write_lock:
                session.after_code, session.awaiting_code = "", False
                session.codes_sent += 1
                stdin.write((code + "\n").encode("utf-8"))
                stdin.flush()
        except (OSError, ValueError):
            raise self.error("La connexion ne lit plus de code (processus terminé) : relance-la.") from None
        self._progress(session, 60, f"Code transmis à {PRODUCTS[session.provider]} : vérification…")
        return {"sent": True, **self.snapshot()}

    def login_cancel(self, payload: dict[str, Any]) -> dict[str, Any]:
        login_command_id = self._command_id(payload.get("login_command_id"))
        with self.logins_lock:
            session = self.logins.get(login_command_id)
            pending = session is not None and not session.started
            process = session.process if session is not None else None
            if pending or process is not None:
                # Sous le verrou : une connexion encore en attente ne lancera jamais sa CLI.
                session.cancel("Connexion annulée depuis le site.")
        cancelled = pending
        if process is not None:
            cancelled = self.terminate(process)
        return {"cancelled": cancelled, **self.snapshot()}

    def login_ids(self) -> set[str]:
        """Connexions connues (en attente ou en cours) : celles qu'une mise à jour reçue maintenant devra annuler."""
        with self.logins_lock:
            return set(self.logins)

    def cancel_all(self, reason: str, command_ids: set[str] | None = None) -> int:
        """Annule les connexions en attente de leur file et en cours (mise à jour, redémarrage, déconnexion de l'agent,
        suppression du Worker), ou seulement ``command_ids``. Une connexion par jeton ou clé déjà appliquée se termine
        d'elle-même (durée bornée)."""
        with self.logins_lock:
            sessions = [session for session in self.logins.values()
                        if (command_ids is None or session.command_id in command_ids)
                        and (not session.started or session.alive())]
            for session in sessions:
                session.cancel(reason)
        return len(sessions)
