"""Allowlisted model installer for ComfyUI and Hunyuan3D workers."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
import http.client
from pathlib import Path

if __package__:
    from .. import worker_legal as legal_compliance, local_sandbox
    from .model_catalog import COMFYUI_CATALOG, COMFYUI_LORA_CATALOG, COMFYUI_BUNDLES, COMFYUI_UTILITY_MODELS, HUNYUAN_CATALOG, HUNYUAN_SNAPSHOT_OPTIONS, AI3D_SNAPSHOTS, AI3D_BACKENDS, COMFYUI_KEY_SOURCES
    from . import asset_installer
    from .transfer_progress import progress, TransferProgress, huggingface_progress_class
    from ..safety import is_link, remove_worker_file, remove_worker_tree
    from ..storage_paths import WorkerStorage
    from ..hardware_profiles import detect_hardware, runtime_profile, model_hardware_error
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import worker_legal as legal_compliance
    import local_sandbox
    from transfer_progress import progress, TransferProgress, huggingface_progress_class
    from safety import is_link, remove_worker_file, remove_worker_tree
    from storage_paths import WorkerStorage
    from hardware_profiles import detect_hardware, runtime_profile, model_hardware_error
    from model_catalog import COMFYUI_CATALOG, COMFYUI_LORA_CATALOG, COMFYUI_BUNDLES, COMFYUI_UTILITY_MODELS, HUNYUAN_CATALOG, HUNYUAN_SNAPSHOT_OPTIONS, AI3D_SNAPSHOTS, AI3D_BACKENDS, COMFYUI_KEY_SOURCES
    import asset_installer


def python_in(environment):
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def configured_component_root(worker_root, component):
    """Use the engine path the Worker actually launches, including legacy installs."""
    return WorkerStorage(worker_root).component(component)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(4 * 1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def checked_model_path(engine_root, path):
    """Never follow a model/cache junction outside the configured engine when mutating."""
    base = engine_root.resolve()
    if not path.resolve().is_relative_to(base):
        raise SystemExit("Le chemin du modèle sort du moteur configuré ; opération annulée.")
    cursor = path
    while cursor != engine_root:
        if is_link(cursor):
            raise SystemExit("Le chemin du modèle est un lien ; opération annulée pour protéger sa cible.")
        if cursor.parent == cursor:
            raise SystemExit("Chemin de modèle invalide.")
        cursor = cursor.parent
    return path


def require_download_space(directory, remaining):
    # Check the actual destination volume, including resumed downloads. Do not
    # delete existing weights/partials to make room automatically.
    directory = Path(directory)
    while not directory.exists():
        directory = directory.parent
    if shutil.disk_usage(directory).free < max(0, remaining) + 256 * 1024**2:
        raise SystemExit("Espace disque insuffisant sur le volume du modèle. Les fichiers existants et le téléchargement partiel sont conservés.")


# Transient network drops (clean EOF before the full size, connection resets,
# CDN 5xx/429) must not abandon a multi-GB checkpoint and cascade the whole
# install batch. The partial file is resumable, so retry from where it stopped a
# few times before surfacing the same message. Backoff/attempts are overridable
# so tests exercise the loop without waiting.
RETRYABLE_HTTP_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
# Taille non publiée par la référence (expected_size 0) : le téléchargement est borné par la taille annoncée
# par le serveur (Content-Length) avec la même marge que pour une taille connue, et jamais au-delà de ce plafond
# absolu ; un partiel qui le dépasse est supprimé (il ne pourra jamais correspondre au fichier épinglé et
# bloquerait toute reprise). Le SHA-256 reste vérifié dans tous les cas : un fichier inattendu est supprimé, pas installé.
UNKNOWN_SIZE_MAXIMUM = 16 * 1024**3
SIZE_MARGIN_BYTES = 64 * 1024 * 1024


def maximum_download_size(expected_size, advertised_total):
    """Borne haute d'un téléchargement : +15 % + 64 Mio de la taille connue, sinon de la taille annoncée (plafond 16 Gio)."""
    if expected_size and expected_size > 0:
        return int(expected_size * 1.15) + SIZE_MARGIN_BYTES
    if advertised_total and advertised_total > 0:
        return min(int(advertised_total * 1.15) + SIZE_MARGIN_BYTES, UNKNOWN_SIZE_MAXIMUM)
    return UNKNOWN_SIZE_MAXIMUM


def refuse_oversized_download(partial, comfy_root, expected_size):
    """Taille maximale dépassée (annonce du serveur, partiel existant ou flux réel).

    Taille de catalogue inconnue : le partiel est déjà au-delà de la borne, il ne peut donc jamais correspondre au
    fichier épinglé et bloquerait chaque tentative suivante dès la reprise (offset > plafond) ; il est supprimé.
    Taille connue : comportement inchangé, le partiel est conservé.
    """
    message = "Le téléchargement dépasse la taille maximale autorisée pour ce modèle."
    if expected_size <= 0:
        remove_worker_file(partial, comfy_root)
        raise SystemExit(message + " Le fichier partiel, qui ne peut plus correspondre au fichier épinglé, a été supprimé.")
    raise SystemExit(message)


KEY_SOURCE_LABELS = {"civitai": "CivitAI"}


class _KeySourceResponse:
    """Réponse d'une source à clé, avec l'interface dont download_catalog_checkpoint se sert (urllib)."""

    def __init__(self, response):
        self._response, self.status = response, response.status
        self.headers = self

    def get(self, name, default=None):
        return self._response.header(name, "") or default

    def read(self, amount):
        return self._response.read(amount)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self._response.close()


def open_key_source(url, headers, provider, token, *, open_url=None):
    """Ouvre un fichier épinglé chez une source à clé (CivitAI) : chaque saut est revérifié par asset_installer
    (hôte public autorisé, https) et la clé n'est envoyée qu'à l'hôte de la source, jamais au serveur de fichiers
    vers lequel elle redirige. Un refus devient une urllib.error.HTTPError, comme pour une source ordinaire."""
    try:
        response = (open_url or asset_installer.guarded_open)(url, headers, provider=provider, token=token, timeout=120)
    except asset_installer.AssetError as error:
        raise SystemExit(f"Téléchargement {KEY_SOURCE_LABELS.get(provider, provider)} refusé : {error}") from error
    if response.status >= 400:
        status = response.status
        response.close()
        raise urllib.error.HTTPError(url, status, "refus de la source", None, None)
    return _KeySourceResponse(response)


def key_source_refusal(provider, code, has_token):
    label = KEY_SOURCE_LABELS.get(provider, provider)
    if code in (401, 403):
        return (f"{label} refuse ce téléchargement (HTTP {code}) : vérifie ta clé {label} dans Mes APIs, puis relance l’installation."
                if has_token else
                f"{label} réserve ce fichier aux comptes connectés (HTTP {code}) : enregistre ta clé {label} dans Mes APIs, puis relance l’installation.")
    if code == 404:
        return f"{label} ne publie plus ce fichier (HTTP 404) : la version épinglée a été retirée par son auteur."
    return f"{label} a refusé le téléchargement (HTTP {code})."


def download_catalog_checkpoint(url, partial, destination, expected_size, expected_hash, comfy_root, model_id, *, key_provider="", token="", open_url=None):
    attempts = max(1, min(10, int(os.getenv("ALPINE_DL_RETRY_ATTEMPTS") or 4)))
    backoff = max(0.0, float(os.getenv("ALPINE_DL_RETRY_BACKOFF") or 3))
    expected_size = max(0, int(expected_size or 0))
    for attempt in range(1, attempts + 1):
        last = attempt >= attempts
        wait = backoff * attempt
        offset = partial.stat().st_size if partial.is_file() else 0
        require_download_space(destination.parent, max(0, expected_size - offset))
        request_headers = {"User-Agent": "AlpineMakersWorker/1.0", **({"Range": f"bytes={offset}-"} if offset else {})}
        try:
            if key_provider:
                download_response = open_key_source(url, request_headers, key_provider, token, open_url=open_url)
            else:
                download_response = urllib.request.urlopen(urllib.request.Request(url, headers=request_headers), timeout=120)
        except urllib.error.HTTPError as error:
            # A crash after the final chunk but before rename leaves a complete partial
            # file. Range EOF (416) must validate/reuse it instead of failing forever.
            if error.code == 416 and partial.is_file() and sha256_file(partial) == expected_hash:
                error.close()
                os.replace(partial, destination)
                progress(99, "Téléchargement précédent complet, modèle vérifié et installé.")
                print(f"{model_id} installé et vérifié.")
                raise SystemExit(0)
            error.close()
            if error.code in RETRYABLE_HTTP_CODES and not last:
                progress(5, f"Service de téléchargement occupé (HTTP {error.code}) ; reprise de {model_id}…")
                time.sleep(wait); continue
            if key_provider:
                raise SystemExit(key_source_refusal(key_provider, error.code, bool(token))) from error
            raise
        except (urllib.error.URLError, OSError) as error:
            if last:
                raise SystemExit("Téléchargement interrompu (réseau) ; les données partielles sont conservées pour la prochaine tentative.")
            progress(5, f"Coupure réseau ; reprise du téléchargement de {model_id}…")
            time.sleep(wait); continue
        with download_response as response:
            mode = "ab" if offset and response.status == 206 else "wb"
            advertised = int(response.headers.get("Content-Length") or 0)
            expected_total = offset + advertised if mode == "ab" else advertised
            maximum_size = maximum_download_size(expected_size, expected_total if advertised else 0)
            if expected_total > maximum_size or offset > maximum_size:
                refuse_oversized_download(partial, comfy_root, expected_size)
            if mode == "ab" and not str(response.headers.get("Content-Range") or "").startswith(f"bytes {offset}-"):
                raise SystemExit("Le serveur n’a pas confirmé la reprise du téléchargement. Le fichier partiel est conservé.")
            downloaded = offset if mode == "ab" else 0
            require_download_space(destination.parent, advertised or max(0, expected_size - downloaded))
            progress(5, f"Téléchargement de {model_id}…")
            transfer = TransferProgress(f"Téléchargement de {model_id}…", initial=downloaded)
            oversized = False
            try:
                with partial.open(mode) as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk: break
                        output.write(chunk)
                        downloaded += len(chunk)
                        if downloaded // (64 * 1024**2) != (downloaded - len(chunk)) // (64 * 1024**2):
                            require_download_space(destination.parent, 0)
                        if downloaded > maximum_size:
                            oversized = True  # le fichier est refermé avant toute suppression (Windows)
                            break
                        transfer.update(downloaded, expected_total)
            except (urllib.error.URLError, http.client.IncompleteRead, OSError) as error:
                if last:
                    raise SystemExit("Téléchargement incomplet ; les données partielles sont conservées pour la prochaine tentative.")
                progress(5, f"Coupure réseau ; reprise du téléchargement de {model_id}…")
                time.sleep(wait); continue
            if oversized:
                refuse_oversized_download(partial, comfy_root, expected_size)
            transfer.update(downloaded, expected_total, force=True)
        if advertised and downloaded != expected_total:
            if last:
                raise SystemExit("Téléchargement incomplet ; les données partielles sont conservées pour la prochaine tentative.")
            progress(5, f"Transfert incomplet ; reprise du téléchargement de {model_id}…")
            time.sleep(wait); continue
        break
    progress(86, "Téléchargement terminé, vérification SHA-256…")
    if sha256_file(partial) != expected_hash:
        remove_worker_file(partial, comfy_root); raise SystemExit("Contrôle SHA-256 du modèle échoué.")
    os.replace(partial, destination)
    progress(99, "Modèle vérifié et installé.")
    print(f"{model_id} installé et vérifié.")


def pinned_file_sets():
    """{identifiant: fichiers épinglés} des ensembles DiT et des modèles utilitaires du Labo image (tables distinctes)."""
    sets = {model_id: tuple(files) for (_, model_id), files in COMFYUI_BUNDLES.items()}
    sets.update({model_id: tuple(spec["files"]) for (_, model_id), spec in COMFYUI_UTILITY_MODELS.items()})
    return sets


def bundle_files(comfy_root, model_id):
    """(sous-dossier, fichier, taille, url, sha256, destination) de chaque fichier d'un ensemble DiT ou d'un modèle
    utilitaire du Labo image."""
    return [(subdir, filename, size, url, digest, comfy_root / "models" / subdir / filename)
            for subdir, filename, size, url, digest in pinned_file_sets()[model_id]]


def _bundle_file_still_needed(comfy_root, model_id, subdir, filename):
    """Un encodeur ou un VAE partagé reste tant qu'un autre ensemble installé (fichier principal présent) s'en sert."""
    for other, files in pinned_file_sets().items():
        if other == model_id or not any(item[0] == subdir and item[1] == filename for item in files):
            continue
        primary_subdir, primary_name = files[0][0], files[0][1]
        if (comfy_root / "models" / primary_subdir / primary_name).is_file():
            return True
    return False


def manage_comfyui_bundle(root, model_id, action):
    """Installe, met à jour, répare ou retire un ensemble multi-fichiers de ComfyUI.

    Chaque fichier est téléchargé et vérifié séparément (reprise possible) ; un fichier déjà
    présent avec le bon SHA-256 n'est pas retéléchargé. Au retrait, un fichier partagé avec un
    autre ensemble installé est conservé.
    """
    comfy_root = configured_component_root(root, "comfyui")
    entries = bundle_files(comfy_root, model_id)
    for *_, destination in entries:
        checked_model_path(comfy_root, destination)
    if action == "uninstall":
        for subdir, filename, _, _, _, destination in entries:
            partial = destination.with_suffix(destination.suffix + ".download")
            remove_worker_file(partial, comfy_root)
            if _bundle_file_still_needed(comfy_root, model_id, subdir, filename):
                print(f"{filename} conservé : un autre ensemble installé l’utilise.")
                continue
            remove_worker_file(destination, comfy_root)
        print("Ensemble supprimé.")
        return
    total = len(entries)
    for index, (subdir, filename, size, url, digest, destination) in enumerate(entries, 1):
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".download")
        checked_model_path(comfy_root, partial)
        if destination.is_file() and sha256_file(destination) == digest:
            remove_worker_file(partial, comfy_root)
            progress(min(95, 2 + index * 90 // total), f"{filename} déjà présent et vérifié ({index}/{total}).")
            continue
        try:
            download_catalog_checkpoint(url, partial, destination, size, digest, comfy_root, f"{model_id} · {filename} ({index}/{total})")
        except SystemExit as stop:
            # Un fichier partiel déjà complet est réutilisé par download_catalog_checkpoint, qui
            # termine alors le processus : ici il reste d'autres fichiers à traiter.
            if stop.code not in (0, None):
                raise
    progress(99, "Ensemble vérifié et installé.")
    print(f"{model_id} installé et vérifié.")


def _download_extra(url, target, expected_size, expected_hash, component_root):
    """Single verified file next to a snapshot (e.g. Real-ESRGAN weights for PBR paint)."""
    checked_model_path(component_root, target)
    if target.is_file() and target.stat().st_size == expected_size and sha256_file(target) == expected_hash:
        return
    partial = target.with_name(target.name + ".download")
    checked_model_path(component_root, partial)
    request = urllib.request.Request(url, headers={"User-Agent": "Raphal-Worker-Installer/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as output:
        copied = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
            copied += len(chunk)
            if copied > expected_size:
                raise SystemExit(f"Fichier complémentaire plus volumineux qu'annoncé ({target.name}).")
    if partial.stat().st_size != expected_size or sha256_file(partial) != expected_hash:
        remove_worker_file(partial, component_root)
        raise SystemExit(f"Contrôle SHA-256 échoué pour {target.name} ; répare le modèle.")
    os.replace(partial, target)


def _verify_pinned_snapshot(component_root, marker_root, pinned):
    """Every pinned file must exist; large ones must match their Hugging Face LFS SHA-256."""
    for relative, expected_hash in pinned["checks"].items():
        path = checked_model_path(component_root, marker_root / relative)
        if not path.is_file() or path.stat().st_size == 0:
            raise SystemExit(f"Fichier manquant après téléchargement : {relative} ; répare le modèle.")
        if expected_hash and sha256_file(path) != expected_hash:
            raise SystemExit(f"Contrôle SHA-256 échoué pour {relative} ; répare le modèle.")


def _post_install_hunyuan3d_21_paint(worker_root):
    """PBR paint needs the compiled rasterizer and the upscaler stack inside the 2.1 module (experimental)."""
    backend_root = WorkerStorage(worker_root).component("hunyuan3d-21")
    python = python_in(backend_root / ".venv")
    if not python.is_file():
        raise SystemExit("Installe d’abord le module Hunyuan3D 2.1 sur ce Worker.")
    if __package__:
        from .component_installer import HUNYUAN3D_21_PAINT_PACKAGES, PIP_WHEEL_PREFERENCE
    else:
        from component_installer import HUNYUAN3D_21_PAINT_PACKAGES, PIP_WHEEL_PREFERENCE
    constraints = backend_root / ".alpine-torch-constraints.txt"
    pin = ["-c", str(constraints)] if constraints.is_file() else []
    environment = dict(os.environ)
    cuda_marker = worker_root / "components" / "cuda-toolkit" / "cuda-path.txt"
    if cuda_marker.is_file():
        cuda_root = cuda_marker.read_text(encoding="utf-8").strip()
        environment["CUDA_HOME"] = environment["CUDA_PATH"] = cuda_root
        environment["PATH"] = os.path.join(cuda_root, "bin") + os.pathsep + environment.get("PATH", "")
    steps = (
        (89, "Installation des dépendances de texturage PBR (Real-ESRGAN, basicsr…)…", [str(python), "-m", "pip", "install", *PIP_WHEEL_PREFERENCE, *pin, *HUNYUAN3D_21_PAINT_PACKAGES]),
        (93, "Compilation du rasteriseur CUDA custom_rasterizer (MSVC + CUDA Toolkit requis)…", [str(python), "-m", "pip", "install", *pin, "--no-build-isolation", str(backend_root / "hy3dpaint" / "custom_rasterizer")]),
        (96, "Compilation du processeur de maillage (mesh_inpaint_processor)…", [str(python), "-c", (
            "import sys, pathlib; from torch.utils.cpp_extension import load; root = pathlib.Path(sys.argv[1]);"
            "load(name='mesh_inpaint_processor', sources=[str(root / 'mesh_inpaint_processor.cpp')], build_directory=str(root), is_python_module=True, verbose=False);"
            "print('mesh_inpaint_processor compilé')"), str(backend_root / "hy3dpaint" / "DifferentiableRenderer")]),
    )
    for value, label, command in steps:
        progress(value, label)
        result = subprocess.run(command, cwd=str(backend_root), env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=7200)
        if result.returncode:
            tail = (result.stdout + result.stderr)[-2500:]
            raise SystemExit(f"{label} échouée. Le texturage PBR 2.1 exige Visual Studio Build Tools (cl.exe) et le CUDA Toolkit ; les poids téléchargés sont conservés.\n{tail}")


def manage_hunyuan(worker_root, model_id, action):
    repo_id, folder = HUNYUAN_CATALOG[model_id]
    component_root = configured_component_root(worker_root, "hunyuan3d")
    marker_root = component_root / "models" / folder
    marker = marker_root / ".raphal-ready"
    environment = WorkerStorage(worker_root).environment()
    cache_home = Path(environment["HF_HOME"])
    checked_model_path(component_root, marker_root)
    if action == "uninstall":
        progress(15, f"Suppression de {model_id}…")
        if marker_root.is_dir():
            remove_worker_tree(marker_root, component_root)
        # Shared/adopted caches may be used by other engines. Explicit model
        # removal removes only its local model, never a shared repository cache.
        print(f"{model_id} désinstallé.")
        return

    if os.getenv("ALPINE_HUNYUAN_MODEL_CHILD") != "1":
        environment_python = python_in(component_root / ".venv")
        if not environment_python.is_file():
            raise SystemExit("Installe d’abord le moteur Hunyuan3D sur ce worker.")
        child_environment = environment
        child_environment["ALPINE_HUNYUAN_MODEL_CHILD"] = "1"
        child_environment["HF_HOME"] = str(cache_home)
        progress(8, f"Préparation du téléchargement de {model_id}…")
        process = subprocess.Popen(
            [str(environment_python), str(Path(__file__).resolve()), "ai3d", model_id, action],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=child_environment,
        )
        output = []
        for line in process.stdout or []:
            print(line.rstrip(), flush=True)
            output.append(line)
            output = output[-100:]
        if process.wait():
            raise SystemExit("".join(output)[-2500:] or "Téléchargement Hunyuan3D interrompu.")
        return

    try:
        from huggingface_hub import snapshot_download
    except ImportError as error:
        raise SystemExit("Le moteur Hunyuan3D doit être réparé : huggingface_hub est absent.") from error
    cache_home.mkdir(parents=True, exist_ok=True)
    snapshot_options = {}
    selected = HUNYUAN_SNAPSHOT_OPTIONS.get(model_id)
    if selected:
        revision, subfolder, expected_hash = selected
        for filename in ("config.yaml", "model.fp16.safetensors"):
            checked_model_path(component_root, marker_root / subfolder / filename)
        snapshot_options = {"revision": revision, "allow_patterns": [
            f"{subfolder}/config.yaml", f"{subfolder}/model.fp16.safetensors",
            "LICENSE*", "NOTICE*", "README.md",
        ]}
    pinned = None if selected else AI3D_SNAPSHOTS.get(model_id)
    if pinned:
        # Multi-file snapshot pinned to one revision: only the listed patterns are fetched.
        for relative in pinned["checks"]:
            checked_model_path(component_root, marker_root / relative)
        snapshot_options = {"revision": pinned["revision"], "allow_patterns": list(pinned["allow_patterns"])}
        if pinned.get("ignore_patterns"):
            snapshot_options["ignore_patterns"] = list(pinned["ignore_patterns"])
    progress(15, f"Téléchargement Hugging Face de {model_id}…")
    # Hugging Face checks its per-file transfers. We reserve a small safety
    # margin here rather than inventing a remaining size for shared snapshots.
    require_download_space(marker_root, 0)
    snapshot = snapshot_download(
        repo_id=repo_id,
        # The authenticated inference service loads this local directory, not
        # the Hugging Face cache path stored in an otherwise empty marker.
        local_dir=str(marker_root),
        cache_dir=environment["HF_HUB_CACHE"],
        force_download=False,
        tqdm_class=huggingface_progress_class(),
        **snapshot_options,
    )
    marker_root.mkdir(parents=True, exist_ok=True)
    if selected:
        checkpoint = checked_model_path(component_root, marker_root / subfolder / "model.fp16.safetensors")
        config_path = checked_model_path(component_root, marker_root / subfolder / "config.yaml")
        progress(88, "Vérification du checkpoint 3D officiel…")
        if not config_path.is_file() or not checkpoint.is_file() or sha256_file(checkpoint) != expected_hash:
            remove_worker_file(marker, component_root)
            raise SystemExit("Checkpoint Hunyuan3D incomplet ou contrôle SHA-256 échoué ; répare le modèle.")
    elif pinned:
        progress(80, "Vérification des fichiers épinglés du modèle…")
        try:
            _verify_pinned_snapshot(component_root, marker_root, pinned)
            for index, extra in enumerate(pinned.get("extras", [])):
                progress(83 + index, f"Fichier complémentaire : {extra['path']}…")
                _download_extra(extra["url"], marker_root / extra["path"], int(extra["size"]), extra["sha256"], component_root)
            for cache in pinned.get("cache_repos", []):
                progress(86, f"Dépendance Hugging Face mise en cache : {cache['repo_id']}…")
                snapshot_download(repo_id=cache["repo_id"], revision=cache["revision"], allow_patterns=list(cache["allow_patterns"]),
                                  cache_dir=environment["HF_HUB_CACHE"], force_download=False, tqdm_class=huggingface_progress_class())
            if pinned.get("post_install") == "hunyuan3d-21-paint":
                _post_install_hunyuan3d_21_paint(worker_root)
        except SystemExit:
            remove_worker_file(marker, component_root)
            raise
    progress(92, "Modèle téléchargé, écriture de la configuration…")
    marker.write_text(
        json.dumps({"repo": repo_id, "snapshot": str(snapshot), "installed_at": time.time()}),
        encoding="utf-8",
    )
    print(f"{model_id} téléchargé et vérifié par Hugging Face.")

tool_id, model_id, action = sys.argv[1:4]
if action not in {"install", "update", "repair", "uninstall"}:
    raise SystemExit("Modèle ou action non autorisé par le catalogue local.")
root = Path(os.environ["ALPINE_WORKER_ROOT"]).resolve()
local_sandbox.activate_from_config(root)
local_sandbox.validate_root(root)
legal_decision = legal_compliance.check_operation(root, "model." + action, {"tool_id": tool_id, "model_id": model_id})
storage = WorkerStorage(root)
storage.initialize()
os.environ.update(storage.environment())
if action != "uninstall":
    hardware = detect_hardware()
    hardware_error = model_hardware_error(tool_id, model_id, hardware)
    if hardware_error:
        raise SystemExit(hardware_error)
    progress(1, runtime_profile(hardware, tool_id)["label"])
if tool_id == "ai3d" and model_id in HUNYUAN_CATALOG:
    manage_hunyuan(root, model_id, action)
    if action != "uninstall":
        legal_compliance.record_notices(root, legal_decision)
    raise SystemExit(0)
if tool_id == "image_generation" and action == "uninstall" and (tool_id, model_id) not in COMFYUI_CATALOG and (tool_id, model_id) not in COMFYUI_LORA_CATALOG and (tool_id, model_id) not in COMFYUI_BUNDLES and (tool_id, model_id) not in COMFYUI_UTILITY_MODELS:
    comfy_root = configured_component_root(root, "comfyui")
    checkpoint_root = comfy_root / "models" / "checkpoints"
    lora_root = comfy_root / "models" / "loras"
    checked_model_path(comfy_root, checkpoint_root)
    checked_model_path(comfy_root, lora_root)
    matches = [p for folder, patterns in ((checkpoint_root, ("*.safetensors", "*.ckpt")), (lora_root, ("*.safetensors", "*.sft")))
               for pattern in patterns for p in folder.glob(pattern) if p.stem == model_id and p.is_file()]
    if len(matches) != 1:
        raise SystemExit("Checkpoint ou LoRA manuel absent ou ambigu. Actualise l’inventaire ; aucun fichier supprimé.")
    target = checked_model_path(comfy_root, matches[0])
    remove_worker_file(target, target.parent, missing_ok=False)
    print(("LoRA" if target.parent == lora_root else "Checkpoint") + " manuel supprimé individuellement.")
    raise SystemExit(0)
# Ensembles DiT et modèles utilitaires du Labo image : mêmes fichiers épinglés, rangés dans leur sous-dossier.
if tool_id == "image_generation" and ((tool_id, model_id) in COMFYUI_BUNDLES or (tool_id, model_id) in COMFYUI_UTILITY_MODELS):
    manage_comfyui_bundle(root, model_id, action)
    if action != "uninstall":
        legal_compliance.record_notices(root, legal_decision)
    raise SystemExit(0)
catalog = COMFYUI_LORA_CATALOG if (tool_id, model_id) in COMFYUI_LORA_CATALOG else COMFYUI_CATALOG
if (tool_id, model_id) not in catalog:
    raise SystemExit("Modèle ou action non autorisé par le catalogue local.")
filename, expected_size, url, expected_hash = catalog[(tool_id, model_id)]
comfy_root = configured_component_root(root, "comfyui")
asset_folder = "loras" if catalog is COMFYUI_LORA_CATALOG else "checkpoints"
destination = comfy_root / "models" / asset_folder / filename
checked_model_path(comfy_root, destination)
destination.parent.mkdir(parents=True, exist_ok=True)
partial = destination.with_suffix(destination.suffix + ".download")
checked_model_path(comfy_root, partial)
if action == "uninstall":
    remove_worker_file(destination, comfy_root)
    remove_worker_file(partial, comfy_root)
    print("Modèle supprimé."); raise SystemExit(0)
# Keep the working checkpoint until a replacement has passed SHA-256. Network
# or disk errors during an update must not remove the user's current model.
managed_copy = root / "components" / "comfyui" / "models" / asset_folder / filename
if destination.resolve() != managed_copy.resolve() and managed_copy.is_file() and not destination.is_file():
    raise SystemExit(f"Un fichier existe déjà dans components/comfyui/models/{asset_folder}. Configure ce moteur pour le réutiliser ; aucun déplacement ni doublon créé.")
if destination.is_file() and action == "install":
    # Catalog sizes are download estimates for some models, not integrity
    # checks. A correct hash must not trigger another multi-GB download just
    # because its actual size differs from that estimate.
    if sha256_file(destination) == expected_hash:
        remove_worker_file(partial, comfy_root)
        progress(99, "Modèle déjà installé et vérifié dans le moteur actif.")
        print(f"{model_id} déjà installé et vérifié.")
        legal_compliance.record_notices(root, legal_decision)
        raise SystemExit(0)
# Source à clé (CivitAI) : la clé du propriétaire arrive par l'environnement du processus, lue une seule fois et
# retirée aussitôt ; elle n'est ni journalisée ni écrite sur disque.
key_provider = COMFYUI_KEY_SOURCES.get(model_id, "") if catalog is COMFYUI_CATALOG else ""
key_token = os.environ.pop("ALPINE_ASSET_TOKEN", "") if key_provider else ""
os.environ.pop("ALPINE_ASSET_TOKEN", None)
download_catalog_checkpoint(url, partial, destination, expected_size, expected_hash, comfy_root, model_id,
                            key_provider=key_provider, token=key_token)
legal_compliance.record_notices(root, legal_decision)
