"""Recover one explicitly selected Worker identity, never register another PC."""
import argparse
import getpass
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import warnings
from pathlib import Path

if __package__:
    from . import local_sandbox
    from .identity import Identity
    from .job_state import atomic_json
    from .safety import is_link, protect_credentials, validate_server_url
else:
    import local_sandbox
    from identity import Identity
    from job_state import atomic_json
    from safety import is_link, protect_credentials, validate_server_url


class RecoveryError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RecoveryError("Redirection du dashboard refusée ; vérifie son adresse HTTPS.")


def checked_root(value):
    root = Path(os.path.abspath(os.path.expanduser(str(value))))
    local_sandbox.activate_from_config(Path(__file__).resolve().parent)
    local_sandbox.validate_root(root)
    broad = {Path(root.anchor), Path.home(), Path.home().parent, Path.home() / "Desktop", Path.home() / "Documents"}
    broad.update(Path(os.path.abspath(os.environ[name])) for name in (
        "PUBLIC", "PROGRAMDATA", "WINDIR", "SYSTEMROOT", "USERPROFILE", "PROGRAMFILES", "PROGRAMFILES(X86)", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP") if os.environ.get(name))
    if root in broad or str(root).startswith(("\\\\", "//")):
        raise RecoveryError("Sélectionne le dossier précis de l’installation Worker, pas un dossier système, utilisateur ou réseau.")
    for entry in (root, *root.parents, root / "config.json", root / "config", root / "config" / "identity", root / "config" / "identity" / "private.key", root / ".worker-detached"):
        if is_link(entry):
            raise RecoveryError("Un chemin Worker est un lien ou une jonction ; aucune configuration modifiée.")
    if not root.is_dir():
        raise RecoveryError("Le dossier Worker choisi n’existe pas. Sélectionne son installation existante.")
    if (root / ".worker-detached").exists():
        raise RecoveryError("Ce Worker a été détaché. Sa récupération ne peut pas contourner une suppression ou une désinstallation du site.")
    return root


def checked_server(value):
    try:
        normalized = validate_server_url(value)
        parsed = urllib.parse.urlsplit(normalized)
    except (TypeError, ValueError) as error:
        raise RecoveryError("Adresse du dashboard invalide ; utilise son adresse HTTPS, sans identifiants.") from error
    if parsed.scheme != "https" or parsed.path not in ("", "/"):
        raise RecoveryError("Utilise l’adresse HTTPS principale du dashboard, sans chemin supplémentaire.")
    return normalized


def checked_worker_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise RecoveryError("Copie l’identifiant exact du Worker existant depuis Mes Workers ; son nom ou son adresse IP ne suffisent pas.")
    return value


def load_existing(root, server_url=None, worker_id=None):
    """Validate local paths/config without writing or initializing Agent state."""
    root = checked_root(root)
    config_path = root / "config.json"
    try:
        if config_path.exists() and (not config_path.is_file() or config_path.stat().st_size > 1024 * 1024):
            raise RecoveryError("Configuration Worker invalide ; fichier conservé sans modification.")
        original = config_path.read_bytes() if config_path.exists() else None
        config = json.loads(original.decode("utf-8-sig")) if original is not None else {}
        if not isinstance(config, dict):
            raise ValueError()
    except (UnicodeError, ValueError) as error:
        raise RecoveryError("Le JSON de config.json est invalide. Corrige ou restaure ce fichier ; il n’a pas été remplacé.") from error
    configured_server = checked_server(config["server_url"]) if config.get("server_url") else None
    configured_id = checked_worker_id(config["worker_id"]) if config.get("worker_id") else None
    supplied_server = checked_server(server_url) if server_url else None
    supplied_id = checked_worker_id(worker_id) if worker_id else None
    if configured_server and supplied_server and configured_server != supplied_server:
        raise RecoveryError("L’adresse fournie ne correspond pas au dashboard déjà associé ; aucune réaffectation automatique.")
    if configured_id and supplied_id and configured_id != supplied_id:
        raise RecoveryError("L’identifiant fourni ne correspond pas au Worker déjà associé ; aucun doublon ni remplacement créé.")
    return {"root": root, "config": config, "original": original,
            "server_url": configured_server or supplied_server, "worker_id": configured_id or supplied_id}


def request_json(server_url, path, payload, *, identity=None, worker_id=None, opener=None):
    """Bounded HTTPS request with no redirects and no bearer credentials."""
    if path not in {"/api/worker-protocol/identity/challenge", "/api/worker-protocol/identity/enroll"}:
        raise RecoveryError("Opération de récupération non autorisée.")
    request = urllib.request.Request(checked_server(server_url) + path, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "AlpineWorker/recovery"}, method="POST")
    if identity is not None:
        request.add_header("X-Worker-ID", checked_worker_id(worker_id))
    request = protect_credentials(request)
    if identity is not None:
        # Explicitly use this root's key, not another configured process cache.
        for key, value in identity.request_headers(request, worker_id).items():
            request.add_unredirected_header(key, value)
    try:
        open_request = opener or urllib.request.build_opener(NoRedirect()).open
        with open_request(request, timeout=25) as response:
            content = response.read(65537)
        if len(content) > 65536:
            raise RecoveryError("Réponse du dashboard trop volumineuse ; aucune configuration modifiée.")
        value = json.loads(content.decode("utf-8"))
        if not isinstance(value, dict) or value.get("ok") is not True:
            raise ValueError()
        return value
    except urllib.error.HTTPError as error:
        error.close()
        message = ("L’identité actuelle n’est plus autorisée par le dashboard." if error.code in {401, 403} else
                   "Le Worker ou le point de récupération est introuvable. Vérifie l’inscription existante sur le site." if error.code in {404, 410} else
                   "Le dashboard refuse momentanément la requête. Réessaie plus tard, sans recréer le Worker.")
        raise RecoveryError(message, error.code) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise RecoveryError("Dashboard injoignable : vérifie la connexion et réessaie. Aucune identité ni configuration n’a été remplacée.") from error
    except (ValueError, UnicodeError) as error:
        raise RecoveryError("Réponse de récupération invalide ; aucune configuration modifiée.") from error


def verify_existing(context, identity, *, opener=None):
    config = context["config"]
    if not context["server_url"] or not context["worker_id"] or not config.get("identity_server_id") or not config.get("identity_epoch"):
        return False
    identity.audience = config["identity_server_id"]
    identity.epoch = config["identity_epoch"]
    try:
        result = request_json(context["server_url"], "/api/worker-protocol/identity/challenge", {},
                              identity=identity, worker_id=context["worker_id"], opener=opener)
    except RecoveryError as error:
        if error.status in {401, 403}:
            return False
        raise
    if result.get("worker_id") != context["worker_id"] or result.get("purpose") != "heartbeat-v1" or not re.fullmatch(r"[0-9a-f]{64}", str(result.get("nonce") or "")):
        raise RecoveryError("La réponse ne confirme pas l’identité du Worker demandé ; aucune modification.")
    return True


def save_confirmed(context, identity, config):
    """Commit only the verified target, without overwriting a concurrent edit."""
    root = context["root"]
    latest = load_existing(root, context["server_url"], context["worker_id"])
    if latest["original"] != context["original"]:
        raise RecoveryError("La configuration a changé pendant la récupération ; elle n’a pas été écrasée. Vérifie-la avant de réessayer.")
    try:
        if Identity(root, create=False).public != identity.public:
            raise ValueError()
    except (OSError, ValueError) as error:
        raise RecoveryError("La clé locale a changé pendant la récupération ; configuration conservée.") from error
    atomic_json(root / "config.json", config)
    if os.name != "nt":
        os.chmod(root / "config.json", 0o600)


def recover(root, server_url=None, worker_id=None, *, input_fn=input, getpass_fn=getpass.getpass, output=print, opener=None):
    context = load_existing(root, server_url, worker_id)
    root = context["root"]
    try:
        identity = Identity(root, create=False)
    except FileNotFoundError as error:
        if (root / "config" / "identity" / "private.key").exists():
            raise RecoveryError("La clé existante n’est pas lisible ; aucun remplacement effectué.") from error
        identity = None
    except (OSError, ValueError) as error:
        raise RecoveryError("La clé privée existante n’est pas lisible. Relance avec le même compte Windows qui a installé le Worker (protection DPAPI). Aucun fichier de clé n’a été remplacé.") from error
    except ImportError as error:
        raise RecoveryError("Le composant de sécurité Worker manque ; relance le lanceur officiel de récupération pour le préparer.") from error
    if identity is not None and verify_existing(context, identity, opener=opener):
        if (context["config"].get("token") != "identity-v1" or context["config"].get("identity_enabled") is not True
                or context["config"].get("worker_id") != context["worker_id"]
                or context["config"].get("server_url") != context["server_url"]):
            repaired = {**context["config"], "token": "identity-v1", "identity_enabled": True,
                        "server_url": context["server_url"], "worker_id": context["worker_id"]}
            save_confirmed(context, identity, repaired)
            output("Les paramètres locaux d’authentification ont été réparés après vérification signée ; clé et autorisations inchangées.")
        output("Identité existante confirmée par le dashboard. Configuration, identifiant et autorisations conservés ; aucune nouvelle inscription.")
        return {"verified": True, "recovered": False, "worker_id": context["worker_id"]}
    output("Une récupération explicite de l’inscription existante est nécessaire. Ne la lance pas pendant une installation, une génération ou une commande d’imprimante active.")
    if input_fn("Récupérer le même Worker avec une autorisation à usage unique du site ? [oui/N] ").strip().casefold() not in {"oui", "o", "yes", "y"}:
        raise RecoveryError("Récupération annulée ; aucune configuration ni clé remplacée.")
    if not context["server_url"]:
        context["server_url"] = checked_server(input_fn("Adresse HTTPS du dashboard : ").strip())
    if not context["worker_id"]:
        context["worker_id"] = checked_worker_id(input_fn("Identifiant exact du Worker existant (Mes Workers) : ").strip())
    checked_root(root)
    if identity is None:
        try:
            identity = Identity(root, create=True)
        except (OSError, ValueError, ImportError) as error:
            raise RecoveryError("Impossible de créer la clé pour cette récupération explicite. Aucun fichier existant n’a été remplacé.") from error
    output("Worker existant : " + context["worker_id"])
    output("Clé publique : " + identity.public)
    output("Empreinte : " + identity.fingerprint)
    output("Sur ce même Worker, ouvre Mes Workers → Migrer/remplacer l’identité, colle cette clé publique et demande le code à usage unique. Ne crée pas une nouvelle inscription.")
    output("Dans la commande affichée par le site, copie uniquement la valeur après --enroll-identity= (pas la commande complète). Saisis-la ici, sans exécuter aussi cette commande.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            code = getpass_fn("Code à usage unique (saisie masquée) : ").strip()
    except getpass.GetPassWarning as error:
        raise RecoveryError("Un terminal interactif est nécessaire pour saisir le code sans l’afficher.") from error
    if not code or len(code) > 512:
        raise RecoveryError("Code de récupération absent ou invalide ; configuration inchangée.")
    payload = {"worker_id": context["worker_id"], "enrollment_code": code, "identity_public_key": identity.public}
    payload["identity_signature"] = identity.sign(payload)
    checked_root(root)
    response = request_json(context["server_url"], "/api/worker-protocol/identity/enroll", payload, opener=opener)
    if (response.get("worker_id") != context["worker_id"] or
            not re.fullmatch(r"[0-9a-f]{64}", str(response.get("identity_server_id") or "")) or
            not re.fullmatch(r"[0-9a-f]{32}", str(response.get("identity_epoch") or ""))):
        raise RecoveryError("Réponse d’association incohérente ; configuration locale conservée.")
    saved = {**context["config"], "server_url": context["server_url"], "worker_id": context["worker_id"],
             "token": "identity-v1", "identity_enabled": True,
             "identity_server_id": response["identity_server_id"], "identity_epoch": response["identity_epoch"]}
    save_confirmed(context, identity, saved)
    output("Inscription existante récupérée avec le même identifiant. Associations et installations conservées. Ce programme ne démarre pas le Worker ; utilise son lanceur pour choisir son démarrage.")
    return {"verified": False, "recovered": True, "worker_id": context["worker_id"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Récupérer une inscription Worker existante sans créer de doublon.")
    parser.add_argument("--root", required=True)
    parser.add_argument("--server-url")
    parser.add_argument("--worker-id")
    args = parser.parse_args(argv)
    try:
        recover(args.root, args.server_url, args.worker_id)
        return 0
    except (EOFError, KeyboardInterrupt):
        print("Récupération interrompue ; aucune nouvelle inscription créée.")
    except RecoveryError as error:
        print(str(error))
    except (OSError, ValueError):
        print("La récupération n’a pas pu être enregistrée. Configuration existante conservée ; vérifie les droits du compte Windows.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
