"""Installe une ressource ComfyUI (LoRA, embedding, ControlNet / Control-LoRA, VAE) demandée par le propriétaire du Worker.

Contrairement aux modèles du catalogue (épinglés dans le paquet de l'agent), la source vient ici du
dashboard : c'est une donnée non fiable. Garde-fous, tous appliqués sur le Worker :

- https uniquement, sans identifiants dans l'URL, port 443, nom d'hôte public : la résolution DNS est
  vérifiée (aucune adresse privée, locale ou de lien local) et la connexion se fait vers l'adresse vérifiée ;
- redirections suivies à la main (6 au plus), chacune revérifiée ; la clé d'accès éventuelle n'est envoyée
  qu'aux hôtes du fournisseur, jamais à un CDN ou à un autre site ;
- format safetensors seulement (données pures), structure vérifiée avant installation ; un checkpoint complet
  ou un fichier d'une autre nature que celle demandée est refusé ; taille bornée par type de ressource ;
- nom de fichier simple et dossier fixé par le type : jamais de chemin fourni par le site ;
- téléchargement dans un dossier temporaire du même volume (reprise possible), SHA-256 calculé — et comparé
  quand la source l'annonce —, puis déplacement atomique. Un fichier existant n'est remplacé qu'après
  vérification complète du nouveau : un échec laisse l'installation précédente intacte.

Usage (lancé par l'agent, jamais par une ligne de commande venue du site) :
    asset_installer.py install        descripteur JSON dans ALPINE_ASSET_DESCRIPTOR, clé dans ALPINE_ASSET_TOKEN
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import sys
import time
import urllib.parse
from pathlib import Path

if __package__ and "." in __package__:
    from .. import comfyui_assets as assets
    from ..installer_process import installation_commit
    from ..safety import is_link, remove_worker_file
    from ..storage_paths import WorkerStorage
    from .transfer_progress import TransferProgress, progress
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import comfyui_assets as assets
    from installer_process import installation_commit
    from safety import is_link, remove_worker_file
    from storage_paths import WorkerStorage
    from transfer_progress import TransferProgress, progress

AssetError = assets.AssetError
PROVIDER_HOSTS = {
    "huggingface": ("huggingface.co", "hf.co"),
    "civitai": ("civitai.com",),
    "github": ("github.com",),
    "direct": None,  # tout hôte public
}
# Fichier envoyé par le propriétaire depuis son navigateur : aucune adresse ; l'agent le rapatrie du dashboard par
# son canal signé (capacité comfyui_asset_upload_v1), puis ce module applique les mêmes vérifications.
UPLOAD_PROVIDER = "upload"
_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# Seuls ces hôtes EXACTS reçoivent la clé d'accès du fournisseur ; un sous-domaine, un CDN ou une redirection
# vers un autre site partent sans elle.
TOKEN_HOSTS = {"huggingface": ("huggingface.co",), "civitai": ("civitai.com",)}
CONNECT_TIMEOUT = 15
# Débit minimal : sous STALL_MIN_BYTES reçus en STALL_WINDOW_SECONDS, le transfert est coupé puis repris.
STALL_WINDOW_SECONDS = 120
STALL_MIN_BYTES = 512 * 1024
SECRET_QUERY_KEYS = frozenset({"token", "api_key", "apikey", "access_token", "auth", "password", "secret", "key"})
REDACTED = "«redacted»"
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 6
SIZE_MARGIN_BYTES = 64 * 1024 * 1024
FREE_SPACE_MARGIN = 256 * 1024 * 1024
STALE_PARTIAL_SECONDS = 7 * 24 * 3600
_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def host_matches(host, allowed):
    return any(host == item or host.endswith("." + item) for item in allowed or ())


def check_url(url, provider=None, *, first_hop=True):
    """URL https vers un nom d'hôte ; pour le premier saut, l'hôte doit être celui du fournisseur annoncé."""
    if not isinstance(url, str) or not 12 <= len(url) <= 2048 or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise AssetError("Adresse de téléchargement invalide.")
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        raise AssetError("Adresse de téléchargement invalide.") from None
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        raise AssetError("Adresse de téléchargement refusée : https sur le port 443, sans identifiants dans l’adresse.")
    if not _HOST_RE.match(host) or "." not in host or host.endswith((".local", ".internal", ".lan", ".home", ".localhost")):
        raise AssetError("Adresse de téléchargement refusée : nom d’hôte public attendu.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise AssetError("Adresse de téléchargement refusée : une adresse IP n’est pas acceptée, seulement un nom d’hôte.")
    if first_hop:
        if provider not in PROVIDER_HOSTS:
            raise AssetError("Source de téléchargement inconnue.")
        allowed = PROVIDER_HOSTS[provider]
        if allowed is not None and not host_matches(host, allowed):
            raise AssetError("Adresse de téléchargement refusée : elle ne correspond pas à la source annoncée.")
        keys = {key.lower() for key, _ in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)}
        if keys & SECRET_QUERY_KEYS:
            raise AssetError("Adresse de téléchargement refusée : elle contient une clé d’accès. Enregistre la clé dans Mes APIs, pas dans l’adresse.")
    return parts._replace(netloc=host)


def public_addresses(host):
    """Adresses IP du nom d'hôte, toutes publiques, sinon refus (réseau privé, boucle locale, lien local…)."""
    try:
        found = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise AssetError(f"Nom d’hôte introuvable : {host}.") from None
    addresses = []
    for item in found:
        address = item[4][0].split("%", 1)[0]
        ip = ipaddress.ip_address(address)
        if not ip.is_global or ip.is_multicast or ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved:
            raise AssetError("Adresse de téléchargement refusée : elle mène à un réseau privé ou local.")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise AssetError(f"Nom d’hôte introuvable : {host}.")
    # IPv4 d'abord : une route IPv6 annoncée mais coupée ne fait pas attendre chaque tentative.
    return sorted(addresses, key=lambda address: ":" in address)


class _PinnedConnection(http.client.HTTPSConnection):
    """Connexion TLS vers l'adresse déjà vérifiée ; le certificat reste contrôlé pour le nom d'hôte demandé."""

    def __init__(self, host, address, timeout, context):
        super().__init__(host, 443, timeout=timeout, context=context)
        self._pinned_address, self._pinned_context = address, context

    def connect(self):
        sock = socket.create_connection((self._pinned_address, 443), min(CONNECT_TIMEOUT, self.timeout or CONNECT_TIMEOUT))
        sock.settimeout(self.timeout)
        self.sock = self._pinned_context.wrap_socket(sock, server_hostname=self.host)


class Response:
    def __init__(self, raw, connection):
        self._raw, self._connection = raw, connection
        self.status = raw.status

    def header(self, name, default=""):
        return self._raw.getheader(name, default) or default

    def read(self, amount):
        # read1 : rend ce qui est arrivé (une lecture réseau au plus), pour que le débit soit surveillé en continu.
        return self._raw.read1(amount)

    def close(self):
        try:
            self._raw.close()
        finally:
            self._connection.close()


def guarded_open(url, headers, *, provider, token="", timeout=60):
    """GET https avec vérification de chaque saut ; rend la première réponse qui n'est pas une redirection."""
    current, context = url, ssl.create_default_context()
    for hop in range(MAX_REDIRECTS + 1):
        parts = check_url(current, provider, first_hop=hop == 0)
        addresses = public_addresses(parts.hostname)
        sent = dict(headers)
        if token and token != REDACTED and parts.hostname in TOKEN_HOSTS.get(provider, ()):
            sent["Authorization"] = "Bearer " + token
        # Caractères non ASCII (nom de fichier accentué) : encodés, les séquences %XX déjà présentes sont gardées.
        target = urllib.parse.quote((parts.path or "/") + ("?" + parts.query if parts.query else ""), safe="/?&=%:@+,;~!$'()*-._[]")
        last_error = None
        for address in addresses[:4]:
            connection = _PinnedConnection(parts.hostname, address, timeout, context)
            try:
                connection.request("GET", target, headers=sent)
                raw = connection.getresponse()
                break
            except (OSError, http.client.HTTPException) as error:
                connection.close()
                last_error = error
        else:
            raise last_error or OSError("connexion impossible")
        if raw.status in REDIRECT_STATUS:
            location = raw.getheader("Location")
            raw.read(64 * 1024)
            connection.close()
            if not location:
                raise AssetError("Redirection sans destination.")
            current = urllib.parse.urljoin(current, location)
            continue
        return Response(raw, connection)
    raise AssetError("Trop de redirections pendant le téléchargement.")


def require_space(directory, needed):
    directory = Path(directory)
    while not directory.exists():
        directory = directory.parent
    free = shutil.disk_usage(directory).free
    if free < max(0, needed) + FREE_SPACE_MARGIN:
        raise AssetError(f"Espace disque insuffisant : {free // 1024**2} Mo libres, {max(0, needed) // 1024**2} Mo à télécharger "
                         f"(marge de {FREE_SPACE_MARGIN // 1024**2} Mo). Rien n’a été supprimé.")


checked_path = assets.checked_path


def validate_descriptor(value):
    """Descripteur strict : toute clé inconnue ou valeur hors forme est refusée."""
    if not isinstance(value, dict):
        raise AssetError("Descripteur de ressource invalide.")
    allowed = {"kind", "filename", "url", "provider", "sha256", "size_bytes", "replace", "source", "upload_id"}
    if set(value) - allowed:
        raise AssetError("Descripteur de ressource invalide : champ inconnu.")
    kind = assets.install_kind(value.get("kind"))
    filename = assets.safe_asset_filename(value.get("filename"))
    provider = str(value.get("provider") or "")
    if kind == assets.CHECKPOINT_KIND and (provider == UPLOAD_PROVIDER or not _SHA_RE.match(str(value.get("sha256") or "").lower())):
        # Un modèle de génération ne s'installe que par téléchargement, avec une empreinte annoncée et revérifiée.
        raise AssetError("Checkpoint : adresse de téléchargement et empreinte SHA-256 annoncée obligatoires.")
    url, upload_id = str(value.get("url") or ""), str(value.get("upload_id") or "")
    if provider == UPLOAD_PROVIDER:
        if url or not _UPLOAD_ID_RE.match(upload_id):
            raise AssetError("Descripteur d’import invalide.")
    else:
        if upload_id:
            raise AssetError("Descripteur de ressource invalide : champ inconnu.")
        check_url(url, provider)
    sha256 = str(value.get("sha256") or "").lower()
    if sha256 and not _SHA_RE.match(sha256):
        raise AssetError("Empreinte SHA-256 annoncée invalide.")
    size = value.get("size_bytes", 0)
    if type(size) is not int or size < 0:
        raise AssetError("Taille annoncée invalide.")
    if value.get("replace", False) not in (True, False):
        raise AssetError("Option de remplacement invalide.")
    if provider == UPLOAD_PROVIDER and (not sha256 or size <= 0):
        raise AssetError("Import : empreinte SHA-256 et taille du fichier attendues.")
    source = value.get("source") if isinstance(value.get("source"), dict) else {}
    clean_source = {"provider": provider, **({"url": url} if url else {})}
    for key in ("ref", "version", "name", "author", "license", "base_label", "page_url"):
        if isinstance(source.get(key), str) and source[key]:
            clean_source[key] = source[key][:300]
    for key in ("trigger_words", "requires"):
        if isinstance(source.get(key), list):
            clean_source[key] = [str(item)[:120] for item in source[key][:30] if isinstance(item, str)]
    return {"kind": kind, "filename": filename, "provider": provider, "url": url, "sha256": sha256,
            "size_bytes": size, "replace": bool(value.get("replace", False)), "source": clean_source,
            **({"upload_id": upload_id} if provider == UPLOAD_PROVIDER else {})}


def _partial_name(descriptor):
    return hashlib.sha256((descriptor["sha256"] or descriptor["url"]).encode("utf-8")).hexdigest()[:32] + ".part"


def upload_target(worker_root, value):
    """Pour l'agent : (descripteur validé, fichier partiel où rapatrier l'import, taille exacte attendue)."""
    descriptor = validate_descriptor(value)
    if descriptor["provider"] != UPLOAD_PROVIDER:
        raise AssetError("Descripteur d’import invalide.")
    spec = assets.INSTALL_KINDS[descriptor["kind"]]
    if descriptor["size_bytes"] > spec["max_bytes"]:
        raise AssetError(f"Fichier trop volumineux pour ce type de ressource (maximum {spec['max_bytes'] // 1024**2} Mo).")
    comfy_root = WorkerStorage(Path(worker_root)).component("comfyui")
    if not (comfy_root / "models").is_dir():
        raise AssetError("Installe d’abord le moteur d’images (ComfyUI) depuis Mes Workers → Installations.")
    folder = comfy_root / "models" / spec["folder"]
    checked_path(comfy_root, folder / descriptor["filename"])
    folder.mkdir(parents=True, exist_ok=True)
    checked_path(comfy_root, folder)
    temporary = checked_path(comfy_root, folder / assets.TEMP_FOLDER)
    temporary.mkdir(exist_ok=True)
    partial = temporary / _partial_name(descriptor)
    if is_link(partial):
        raise AssetError("Fichier temporaire inattendu (lien) ; opération annulée.")
    require_space(temporary, descriptor["size_bytes"])
    return descriptor, partial, descriptor["size_bytes"]


def _kind_mismatch(kind, detected):
    """Message de refus quand le contenu n'est pas de la nature demandée, sinon chaîne vide."""
    if kind == assets.CHECKPOINT_KIND:
        return "" if detected == "checkpoint" else "Le contenu du fichier n’est pas un checkpoint complet : rien n’a été installé."
    if detected == "checkpoint":
        return "Ce fichier est un checkpoint complet, pas une ressource additionnelle : installe-le comme modèle."
    accepted = {"lora": {"lora", "unknown"}, "embedding": {"embedding", "unknown"},
                "controlnet": {"controlnet", "unknown"}, "vae": {"vae", "unknown"}}[kind]
    if detected not in accepted:
        label = assets.ASSET_KINDS.get(detected, {}).get("label", detected)
        return f"Le contenu du fichier est de type « {label} », pas « {assets.ASSET_KINDS[kind]['label']} » : rien n’a été installé."
    return ""


def download(descriptor, partial, *, maximum, token, open_url, label):
    attempts = max(1, min(10, int(os.getenv("ALPINE_DL_RETRY_ATTEMPTS") or 4)))
    backoff = max(0.0, float(os.getenv("ALPINE_DL_RETRY_BACKOFF") or 3))
    expected = descriptor["size_bytes"]
    for attempt in range(1, attempts + 1):
        last, wait = attempt >= attempts, backoff * attempt
        offset = partial.stat().st_size if partial.is_file() else 0
        if offset > maximum:
            remove_worker_file(partial, partial.parent)
            offset = 0
        require_space(partial.parent, max(0, expected - offset))
        headers = {"User-Agent": "AlpineWorker/assets", "Accept": "*/*", "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            response = open_url(descriptor["url"], headers, provider=descriptor["provider"], token=token)
        except AssetError:
            raise
        except (OSError, http.client.HTTPException):
            if last:
                raise AssetError("Téléchargement interrompu (réseau) ; les données partielles sont conservées pour la prochaine tentative.") from None
            progress(5, f"Coupure réseau ; reprise du téléchargement de {label}…")
            time.sleep(wait)
            continue
        try:
            status = response.status
            if status == 416 and offset:
                return  # fichier partiel déjà complet : la vérification décide
            if status in (401, 403):
                raise AssetError(f"La source refuse le téléchargement (HTTP {status}). Ce fichier exige sans doute une clé d’accès du "
                                 "fournisseur : enregistre-la dans Mes APIs puis relance l’installation.")
            if status == 404:
                raise AssetError("Fichier introuvable à la source (HTTP 404).")
            if status in RETRYABLE_STATUS and not last:
                progress(5, f"Source occupée (HTTP {status}) ; nouvel essai pour {label}…")
                time.sleep(wait)
                continue
            if status not in (200, 206):
                raise AssetError(f"La source a répondu HTTP {status}.")
            if response.header("Content-Type").lower().startswith("text/html"):
                raise AssetError("La source a renvoyé une page web au lieu du fichier (connexion ou clé d’accès requise ?).")
            resumed = status == 206 and offset > 0
            if offset and not resumed:
                offset = 0  # la source ignore la reprise : on recommence proprement
            if resumed and not response.header("Content-Range").startswith(f"bytes {offset}-"):
                raise AssetError("La source n’a pas confirmé la reprise du téléchargement. Le fichier partiel est conservé.")
            advertised = int(response.header("Content-Length") or 0)
            total = offset + advertised if advertised else 0
            if total > maximum:
                raise AssetError(f"Fichier trop volumineux pour ce type de ressource ({total // 1024**2} Mo, maximum {maximum // 1024**2} Mo).")
            require_space(partial.parent, advertised or max(0, expected - offset))
            transfer = TransferProgress(f"Téléchargement de {label}…", initial=offset)
            done, oversized, stalled = offset, False, False
            window_start, window_done = time.monotonic(), offset
            try:
                with partial.open("ab" if resumed else "wb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        done += len(chunk)
                        if done > maximum:
                            oversized = True
                            break
                        if done // (64 * 1024**2) != (done - len(chunk)) // (64 * 1024**2):
                            require_space(partial.parent, 0)
                        transfer.update(done, total)
                        moment = time.monotonic()
                        if moment - window_start >= STALL_WINDOW_SECONDS:
                            if done - window_done < STALL_MIN_BYTES:
                                stalled = True
                                raise OSError("débit insuffisant")
                            window_start, window_done = moment, done
            except (OSError, http.client.HTTPException) as error:
                if isinstance(error, AssetError):
                    raise
                if last:
                    raise AssetError(("La source envoie trop lentement" if stalled else "Téléchargement incomplet")
                                     + " ; les données partielles sont conservées pour la prochaine tentative.") from None
                progress(5, f"Coupure réseau ; reprise du téléchargement de {label}…")
                time.sleep(wait)
                continue
            if oversized:
                remove_worker_file(partial, partial.parent)
                raise AssetError("Le téléchargement dépasse la taille maximale de ce type de ressource ; fichier partiel supprimé.")
            transfer.update(done, total, force=True)
            if total and done != total:
                if last:
                    raise AssetError("Téléchargement incomplet ; les données partielles sont conservées pour la prochaine tentative.")
                progress(5, f"Transfert incomplet ; reprise du téléchargement de {label}…")
                time.sleep(wait)
                continue
            return
        finally:
            response.close()
    raise AssetError("Téléchargement impossible après plusieurs tentatives.")


def _clean_stale_partials(temporary):
    try:
        for path in temporary.iterdir():
            if path.is_file() and path.suffix == ".part" and time.time() - path.stat().st_mtime > STALE_PARTIAL_SECONDS:
                remove_worker_file(path, temporary)
    except OSError:
        pass


def install(worker_root, value, *, token="", open_url=guarded_open):
    """Télécharge, vérifie puis installe. Rend le compte rendu (ressource installée, doublon, déjà présent)."""
    descriptor = validate_descriptor(value)
    kind, filename = descriptor["kind"], descriptor["filename"]
    spec = assets.INSTALL_KINDS[kind]
    # Un checkpoint n'entre pas dans l'index des ressources : le relevé des modèles lit le dossier checkpoints/.
    indexed = kind != assets.CHECKPOINT_KIND
    worker_root = Path(worker_root)
    comfy_root = WorkerStorage(worker_root).component("comfyui")
    if not (comfy_root / "models").is_dir():
        raise AssetError("Installe d’abord le moteur d’images (ComfyUI) depuis Mes Workers → Installations.")
    folder = comfy_root / "models" / spec["folder"]
    destination = checked_path(comfy_root, folder / filename)
    folder.mkdir(parents=True, exist_ok=True)
    checked_path(comfy_root, folder)
    expected_hash = descriptor["sha256"]
    if destination.is_file() and not descriptor["replace"]:
        existing_hash = assets.sha256_file(destination)
        if expected_hash and existing_hash == expected_hash:
            metadata, tensors = assets.read_safetensors_header(destination)
            description = assets.describe_asset(metadata, tensors)
            if indexed:
                assets.record_installed(worker_root, kind, filename, destination, sha256=existing_hash, description=description, source=descriptor["source"])
            progress(99, "Ressource déjà présente et identique : rien à télécharger.")
            return {"already_installed": True, "asset": {"kind": kind, "name": filename, "sha256": existing_hash,
                                                         "size_bytes": destination.stat().st_size, **description}}
        raise AssetError("Un fichier du même nom existe déjà dans ce dossier. Utilise « Mettre à jour » pour le remplacer, ou désinstalle-le d’abord.")
    maximum = spec["max_bytes"]
    if descriptor["size_bytes"]:
        if descriptor["size_bytes"] > maximum:
            raise AssetError(f"Fichier trop volumineux pour ce type de ressource (maximum {maximum // 1024**2} Mo).")
        maximum = min(maximum, int(descriptor["size_bytes"] * 1.15) + SIZE_MARGIN_BYTES)
    temporary = checked_path(comfy_root, folder / assets.TEMP_FOLDER)
    temporary.mkdir(exist_ok=True)
    _clean_stale_partials(temporary)
    partial = temporary / _partial_name(descriptor)
    if is_link(partial):
        raise AssetError("Fichier temporaire inattendu (lien) ; opération annulée.")
    if descriptor["provider"] == UPLOAD_PROVIDER:
        # Import : l'agent a déjà rapatrié le fichier du dashboard dans ce fichier partiel. Rien n'est téléchargé ici ;
        # la suite (empreinte, format, nature du contenu, doublon, mise en place atomique) est la même.
        if not partial.is_file():
            raise AssetError("Fichier importé introuvable sur le Worker ; relance l’import.")
    else:
        progress(3, f"Préparation du téléchargement de {filename}…")
        download(descriptor, partial, maximum=maximum, token=token, open_url=open_url, label=filename)
    if not partial.is_file() or partial.stat().st_size <= 0:
        raise AssetError("Téléchargement vide.")
    progress(86, "Téléchargement terminé, vérification SHA-256…")
    actual_hash = assets.sha256_file(partial)
    if expected_hash and actual_hash != expected_hash:
        remove_worker_file(partial, temporary)
        raise AssetError("Empreinte SHA-256 différente de celle annoncée par la source : fichier supprimé, rien n’a été installé.")
    progress(92, "Vérification du format safetensors…")
    try:
        metadata, tensors = assets.read_safetensors_header(partial)
    except AssetError:
        remove_worker_file(partial, temporary)
        raise
    description = assets.describe_asset(metadata, tensors)
    mismatch = _kind_mismatch(kind, description["detected_kind"])
    if mismatch:
        remove_worker_file(partial, temporary)
        raise AssetError(mismatch)
    duplicate = assets.find_duplicate(worker_root, kind, actual_hash, except_name=filename) if indexed else None
    if duplicate and (folder / duplicate).is_file():
        remove_worker_file(partial, temporary)
        progress(99, f"Contenu identique déjà installé sous le nom {duplicate}.")
        return {"duplicate_of": duplicate, "asset": {"kind": kind, "name": duplicate, "sha256": actual_hash,
                                                     "size_bytes": (folder / duplicate).stat().st_size, **description}}
    progress(96, "Installation dans ComfyUI…")
    try:
        with installation_commit():
            os.replace(partial, destination)
    except PermissionError:
        # Windows : ComfyUI garde ouvert le fichier à remplacer tant qu'une génération s'en sert.
        raise AssetError("Le fichier à remplacer est utilisé par ComfyUI en ce moment. Attends la fin de la génération en cours puis relance : "
                         "le téléchargement vérifié est conservé, rien ne sera retéléchargé.") from None
    if indexed:
        assets.record_installed(worker_root, kind, filename, destination, sha256=actual_hash, description=description, source=descriptor["source"])
    progress(99, "Ressource vérifiée et installée ; ComfyUI la voit sans redémarrage.")
    return {"asset": {"kind": kind, "name": filename, "sha256": actual_hash, "size_bytes": destination.stat().st_size, **description}}


def main(argv):
    if len(argv) != 2 or argv[1] != "install":
        print("Action de ressource non autorisée.")
        return 2
    worker_root = os.environ.get("ALPINE_WORKER_ROOT") or ""
    token = os.environ.pop("ALPINE_ASSET_TOKEN", "")
    result_file = os.environ.get("ALPINE_ASSET_RESULT") or ""
    try:
        descriptor = json.loads(os.environ.get("ALPINE_ASSET_DESCRIPTOR") or "null")
        if not worker_root or not Path(worker_root).is_dir():
            raise AssetError("Dossier du Worker introuvable.")
        result = install(worker_root, descriptor, token=token)
    except AssetError as error:
        print(str(error))
        return 1
    except Exception as error:  # noqa: BLE001 - un refus lisible, jamais une trace (qui pourrait citer l'adresse)
        print(f"Installation de la ressource impossible : {type(error).__name__}.")
        return 1
    if result_file:
        try:
            Path(result_file).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    print("Ressource installée." if not result.get("already_installed") else "Ressource déjà installée.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
