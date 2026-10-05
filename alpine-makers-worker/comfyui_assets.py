"""Ressources ComfyUI gérées depuis le dashboard : LoRA, embeddings, ControlNet / Control-LoRA, VAE.

Ce module ne télécharge rien : il décrit les dossiers de ComfyUI, lit l'en-tête des fichiers
safetensors (métadonnées et noms de tenseurs, jamais les poids), en déduit le modèle de base, et tient
un index local (taille, date, SHA-256, source d'installation). L'installation elle-même est faite par
``installers/asset_installer.py``.

Seul le format safetensors est géré : c'est un format de données pur (en-tête JSON + tenseurs), sans
désérialisation de code. Les formats pickle (.ckpt, .pt, .pth, .bin) sont listés pour information et
jamais installés par ce module.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import time
from pathlib import Path

if __package__:
    from .job_state import atomic_json, read_json
    from .safety import is_link, remove_worker_file, safe_relative_name
else:
    from job_state import atomic_json, read_json
    from safety import is_link, remove_worker_file, safe_relative_name

CAPABILITY = "comfyui_assets_v1"
ASSET_KINDS = {
    "lora": {"folder": "loras", "label": "LoRA", "max_bytes": 4 * 1024**3},
    "embedding": {"folder": "embeddings", "label": "Embedding", "max_bytes": 256 * 1024**2},
    "controlnet": {"folder": "controlnet", "label": "ControlNet / Control-LoRA", "max_bytes": 8 * 1024**3},
    "vae": {"folder": "vae", "label": "VAE", "max_bytes": 2 * 1024**3},
}
ASSET_SUFFIXES = (".safetensors", ".sft")
PICKLE_SUFFIXES = (".ckpt", ".pt", ".pth", ".bin")
INDEX_NAME = "comfyui-assets.json"
INDEX_VERSION = 1
# Version des règles de détection (base, nature) : une entrée d'index lue avec d'anciennes règles est relue.
DETECTOR_VERSION = 3
# En-tête safetensors : quelques Mo pour les plus gros modèles. Au-delà, ce n'est pas une ressource à décrire.
HEADER_LIMIT = 16 * 1024 * 1024
TENSOR_LIMIT = 100_000
_RESERVED_NAMES = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))})
TEMP_FOLDER = ".alpine-tmp"
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+()\[\]@,=-]{0,150}$")
# Familles du générateur d'images (mêmes noms que le site) et libellés affichés.
BASE_LABELS = {
    "sd15": "SD 1.5", "sd2": "SD 2.x", "sdxl": "SDXL", "flux": "FLUX.1", "sd35": "SD 3.5", "qwen": "Qwen-Image",
    "zimage": "Z-Image", "chroma": "Chroma", "hidream": "HiDream", "lumina2": "Lumina 2", "flux2": "FLUX.2", "unknown": "Inconnu",
}


class AssetError(ValueError):
    """Refus affichable : nom, format ou chemin non autorisé."""


def safe_asset_filename(value):
    """Nom de fichier simple (aucun dossier), portable, en .safetensors / .sft."""
    try:
        name = safe_relative_name(str(value or "").strip(), basename=True)
    except ValueError as error:
        raise AssetError("Nom de fichier de ressource non autorisé.") from error
    suffix = Path(name).suffix.lower()
    if suffix in PICKLE_SUFFIXES:
        raise AssetError("Format pickle refusé (.ckpt, .pt, .pth, .bin) : seul le format safetensors est installé.")
    # « nul .safetensors » : Windows ignore les espaces avant le point et ouvrirait le périphérique NUL.
    if suffix not in ASSET_SUFFIXES or not _FILENAME_RE.match(name) or ".." in name or name.split(".")[0].strip().casefold() in _RESERVED_NAMES \
            or name.split(".")[0] != name.split(".")[0].strip():
        raise AssetError("Nom de fichier de ressource non autorisé : lettres, chiffres, espaces et . _ + - ( ) seulement, en .safetensors.")
    return name


def asset_kind(value):
    kind = str(value or "").strip().lower()
    if kind not in ASSET_KINDS:
        raise AssetError("Type de ressource inconnu.")
    return kind


def read_safetensors_header(path, limit=HEADER_LIMIT):
    """(métadonnées, {tenseur: forme}) d'un fichier safetensors, sans lire les poids.

    Vérifie aussi la structure : longueur d'en-tête plausible, JSON objet, et étendue des tenseurs égale à
    la taille du fichier. Une page d'erreur HTML, un fichier tronqué ou un autre format sont refusés.
    """
    path = Path(path)
    size = path.stat().st_size
    with path.open("rb") as source:
        prefix = source.read(8)
        if len(prefix) != 8:
            raise AssetError("Fichier safetensors invalide : en-tête absent.")
        length = struct.unpack("<Q", prefix)[0]
        if not 2 <= length <= min(limit, max(0, size - 8)):
            raise AssetError("Fichier safetensors invalide : longueur d’en-tête incohérente.")
        raw = source.read(length)
    if len(raw) != length:
        raise AssetError("Fichier safetensors invalide : en-tête tronqué.")
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError, MemoryError):
        # RecursionError / MemoryError : en-tête forgé (imbrication profonde) ; un refus, jamais un arrêt de l'agent.
        raise AssetError("Fichier safetensors invalide : en-tête illisible.") from None
    if not isinstance(header, dict):
        raise AssetError("Fichier safetensors invalide : en-tête inattendu.")
    if len(header) > TENSOR_LIMIT:
        raise AssetError("Fichier safetensors invalide : nombre de tenseurs déraisonnable.")
    metadata = header.pop("__metadata__", None)
    metadata = {str(k): str(v) for k, v in metadata.items()} if isinstance(metadata, dict) else {}
    tensors, end = {}, 0
    for name, entry in header.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("shape"), list) or not isinstance(entry.get("data_offsets"), list) \
                or len(entry["data_offsets"]) != 2 or not all(type(v) is int and v >= 0 for v in entry["data_offsets"]):
            raise AssetError("Fichier safetensors invalide : description de tenseur inattendue.")
        end = max(end, entry["data_offsets"][1])
        tensors[str(name)] = [int(v) for v in entry["shape"] if type(v) is int]
    if not tensors:
        raise AssetError("Fichier safetensors invalide : aucun tenseur.")
    if 8 + length + end != size:
        raise AssetError("Fichier safetensors incomplet ou altéré : la taille ne correspond pas à son en-tête.")
    return metadata, tensors


def family_from_text(value):
    """Famille du générateur d'après un libellé libre (« SDXL 1.0 », « Flux.1 D », identifiant de dépôt…), ou chaîne vide."""
    text = str(value or "").lower().replace("_", "-").replace(" ", "-")
    if not text:
        return ""
    for needle, family in (("qwen", "qwen"), ("hidream", "hidream"), ("chroma", "chroma"), ("z-image", "zimage"), ("zimage", "zimage"),
                           ("lumina", "lumina2"), ("flux.2", "flux2"), ("flux-2", "flux2"), ("flux2", "flux2"), ("flux", "flux"),
                           ("stable-diffusion-3", "sd35"), ("sd3", "sd35"), ("sd-3", "sd35"),
                           ("stable-diffusion-xl", "sdxl"), ("sdxl", "sdxl"), ("pony", "sdxl"), ("illustrious", "sdxl"), ("noobai", "sdxl"),
                           ("stable-diffusion-v2", "sd2"), ("sd-v2", "sd2"), ("sd-2", "sd2"),
                           ("stable-diffusion-v1", "sd15"), ("sd-v1", "sd15"), ("sd-1", "sd15"), ("sd1", "sd15")):
        if needle in text:
            return family
    return ""


def _cross_attention_family(tensors):
    """Largeur du texte vue par l'attention croisée du réseau : 768 = SD 1.5, 1024 = SD 2.x, 2048 = SDXL."""
    for name, shape in tensors.items():
        lowered = name.lower()
        if "attn2" not in lowered or "to_k" not in lowered or len(shape) < 2:
            continue
        if not any(mark in lowered for mark in ("lora_down", "lora_a", ".down", "to_k.weight")):
            continue
        width = shape[1]
        if width in (768, 1024, 2048):
            return {768: "sd15", 1024: "sd2", 2048: "sdxl"}[width]
    return ""


def detect_kind(tensors):
    """Nature du fichier d'après ses tenseurs : lora, embedding, controlnet, vae, checkpoint ou inconnu."""
    names = [name.lower() for name in tensors]
    if any(name == "lora_controlnet" or name.startswith(("control_model.", "controlnet_")) or "input_hint_block" in name
           or "controlnet_cond_embedding" in name for name in names):
        return "controlnet"
    if any(mark in name for name in names for mark in ("lora_down", "lora_up", "lora.down", "lora.up", ".lora_a.", ".lora_b.", "lora_a.weight", "lora_b.weight",
                                                         "lokr_", "hada_", "dora_scale", ".alpha")):
        return "lora"
    if len(names) <= 8 and any(name in ("emb_params", "clip_l", "clip_g", "string_to_param.*", "t5xxl") or name.startswith("string_to_param")
                               for name in names):
        return "embedding"
    if any(name.startswith(("model.diffusion_model.", "cond_stage_model.", "conditioner.")) for name in names):
        return "checkpoint"
    if any(name.startswith(("decoder.", "encoder.", "first_stage_model.")) for name in names) and not any("lora" in name for name in names):
        return "vae"
    return "unknown"


def _structural_family(kind, tensors):
    """Famille déduite des seuls noms et formes des tenseurs, ou chaîne vide."""
    names = [name.lower() for name in tensors]
    joined = " ".join(names)
    blocks = [int(match.group(1)) for name in names for match in [re.search(r"transformer_blocks[._](\d+)", name)] if match
              and "single_transformer_blocks" not in name]
    if kind == "embedding":
        widths = {name: (shape[-1] if shape else 0) for name, shape in tensors.items()}
        if "clip_g" in widths or 1280 in widths.values():
            return "sdxl"
        if 768 in widths.values():
            return "sd15"
        return "sd2" if 1024 in widths.values() else ""
    if "lora_te2_" in joined or "text_encoder_2" in joined:
        return "sdxl"
    if any(mark in joined for mark in ("double_blocks", "single_blocks", "single_transformer_blocks", "lora_transformer_single")):
        return "flux"
    if "joint_blocks" in joined or ("transformer_blocks" in joined and "ff_context" in joined):
        return "sd35"
    if "transformer_blocks" in joined and (any(mark in joined for mark in ("img_mlp", "txt_mlp", "img_mod", "txt_mod"))
                                           or (blocks and max(blocks) >= 38)):
        return "qwen"
    family = _cross_attention_family(tensors)
    flat = joined.replace(".", "_")
    unet = any(mark in flat for mark in ("down_blocks_", "up_blocks_", "mid_block_"))
    if not family and "lora_unet_input_blocks_" in joined:
        return "sdxl"   # nommage LDM de kohya, propre à SDXL
    if not family and unet and re.search(r"transformer_blocks_[1-9]", flat):
        # UNet SDXL : jusqu'à dix blocs transformeurs par étage ; SD 1.5 / 2.x n'en ont qu'un (indice 0).
        # Sert aux LoRA sans attention croisée (« curseurs » LECO : attn1 seulement).
        return "sdxl"
    if not family and unet and ("down_blocks_0_attentions" in flat or "up_blocks_3_attentions" in flat):
        return "sd15"   # attention dès le premier étage : UNet SD 1.x (SD 2.x a la même forme, plus rare)
    if not family and "lora_te_text_model_" in joined and "lora_unet_down_blocks_" in joined:
        return "sd15"
    return family


def describe_asset(metadata, tensors):
    """Modèle de base, nature et mots déclencheurs lus dans le fichier lui-même (jamais dans son nom)."""
    kind = detect_kind(tensors)
    family, source = "", ""
    for key in ("modelspec.architecture", "ss_base_model_version", "ss_sd_model_name", "modelspec.title"):
        family = family_from_text(metadata.get(key))
        if family:
            source = "metadata"
            break
    # Beaucoup de LoRA (diffusers, ai-toolkit) n'ont aucune métadonnée d'entraînement : la structure décide.
    structural = _structural_family(kind, tensors)
    if not family:
        family, source = structural, "structure" if structural else ""
    elif family in ("sd15", "sd2", "sdxl") and structural in ("flux", "sd35", "qwen"):
        # Métadonnée héritée d'un réglage par défaut (ai-toolkit écrit « sd_1.5 » dans des LoRA FLUX) : des blocs de
        # transformeur ne peuvent pas appartenir à un UNet, la structure l'emporte.
        family, source = structural, "structure"
    words = []
    phrase = metadata.get("modelspec.trigger_phrase") or metadata.get("ss_trigger_words") or ""
    for part in re.split(r"[,;\n]", str(phrase)):
        part = part.strip()
        if part and part not in words:
            words.append(part[:80])
    training_tags = []
    try:
        frequency = json.loads(metadata.get("ss_tag_frequency") or "{}")
        totals = {}
        for dataset in frequency.values() if isinstance(frequency, dict) else []:
            for tag, count in dataset.items() if isinstance(dataset, dict) else []:
                totals[str(tag).strip()] = totals.get(str(tag).strip(), 0) + (count if type(count) is int else 0)
        training_tags = [tag[:80] for tag, _ in sorted(totals.items(), key=lambda item: (-item[1], item[0]))[:12] if tag]
    except (ValueError, AttributeError):
        training_tags = []
    return {
        "detected_kind": kind, "base_family": family or "unknown", "base_source": source or "unknown",
        "trigger_words": words[:20], "training_tags": training_tags,
        "network": str(metadata.get("ss_network_module") or "")[:80],
        "title": str(metadata.get("modelspec.title") or metadata.get("ss_output_name") or "")[:160],
    }


def sha256_file(path, progress=None):
    digest, done = hashlib.sha256(), 0
    with Path(path).open("rb") as source:
        while True:
            chunk = source.read(4 * 1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)
            done += len(chunk)
            if progress:
                progress(done)


def _extra_model_paths(comfy_root):
    """Dossiers supplémentaires déclarés dans extra_model_paths.yaml (lecture tolérante, sans PyYAML).

    Reconnaît la forme documentée par ComfyUI : sections de premier niveau, ``base_path`` et une ligne
    ``dossier: chemin`` ou un bloc ``dossier: |`` suivi de chemins. Tout le reste est ignoré.
    """
    result = {kind: [] for kind in ASSET_KINDS}
    file = Path(comfy_root) / "extra_model_paths.yaml"
    try:
        if is_link(file) or not file.is_file() or file.stat().st_size > 256 * 1024:
            return result
        lines = file.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return result
    folders = {spec["folder"]: kind for kind, spec in ASSET_KINDS.items()}
    key_line = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(?:\s+(.*))?$")  # « D:\dossier » n'est pas une clé
    base, block_kind, block_indent = None, None, 0
    pending = []
    for raw in lines + ["end:"]:
        line = raw.split(" #", 1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        text = line.strip()
        match = key_line.match(text)
        if block_kind and indent > block_indent and not match:
            pending.append((block_kind, text.strip("'\"")))
            continue
        block_kind = None
        if indent == 0:
            for kind, value in pending:
                value = os.path.expandvars(os.path.expanduser(value))
                target = Path(value) if Path(value).is_absolute() or not base else Path(base) / value
                if target.is_absolute():
                    result[kind].append(target)
            base, pending = None, []
            continue
        if not match:
            continue
        key, value = match.group(1), (match.group(2) or "").strip().strip("'\"")
        if key == "base_path":
            base = os.path.expandvars(os.path.expanduser(value))
        elif key in folders:
            if value in ("|", ">", ""):
                block_kind, block_indent = folders[key], indent
            else:
                pending.append((folders[key], value))
    return result


def asset_dirs(comfy_root):
    """{type: [dossier principal, dossiers supplémentaires…]} ; le premier est celui où l'on installe."""
    comfy_root = Path(comfy_root)
    extra = _extra_model_paths(comfy_root)
    result = {}
    for kind, spec in ASSET_KINDS.items():
        primary = comfy_root / "models" / spec["folder"]
        seen, ordered = {os.path.normcase(str(primary))}, [primary]
        for path in extra[kind]:
            key = os.path.normcase(str(path))
            if key not in seen:
                seen.add(key)
                ordered.append(path)
        result[kind] = ordered
    return result


def index_path(worker_root):
    return Path(worker_root) / "data" / INDEX_NAME


def load_index(worker_root):
    """Index local ; illisible ou abîmé, il vaut un index vide (il se reconstruit au relevé suivant)."""
    try:
        value = read_json(index_path(worker_root), {})
    except (OSError, ValueError, RecursionError, MemoryError):
        return {}
    files = value.get("files") if isinstance(value, dict) and value.get("version") == INDEX_VERSION else None
    return files if isinstance(files, dict) else {}


def save_index(worker_root, files):
    atomic_json(index_path(worker_root), {"version": INDEX_VERSION, "files": files})


def index_key(kind, name):
    return f"{kind}/{name}"


def scan_assets(worker_root, comfy_root, *, hashes=False, progress=None, kinds=None):
    """Inventaire des ressources présentes. L'en-tête n'est relu que si taille ou date ont changé.

    ``hashes`` : calcule aussi le SHA-256 des fichiers dont il n'est pas déjà connu (commande asset.scan) ;
    le relevé périodique de l'agent ne le fait jamais.
    """
    index = load_index(worker_root)
    fresh, assets, changed = {}, [], False
    for kind, folders in asset_dirs(comfy_root).items():
        if kinds and kind not in kinds:
            continue
        for position, folder in enumerate(folders):
            try:
                if not folder.is_dir():
                    continue
                # Dossier lié (jonction vers un autre disque) : ComfyUI s'en sert, il est donc relevé — en lecture
                # seule ; l'installation et la suppression, elles, refusent toujours de traverser un lien.
                linked = is_link(folder)
                candidates = sorted(path for path in folder.rglob("*") if path.is_file() and TEMP_FOLDER not in path.parts
                                    and path.suffix.lower() in ASSET_SUFFIXES + PICKLE_SUFFIXES)
            except OSError:
                continue
            for path in candidates[:2000]:
                try:
                    info = path.stat()
                    name = path.relative_to(folder).as_posix()
                except (OSError, ValueError):
                    continue
                key = index_key(kind, name)
                if key in fresh:
                    continue  # même nom dans un dossier supplémentaire : ComfyUI prend le premier
                cached = index.get(key) if isinstance(index.get(key), dict) else {}
                entry = {k: cached[k] for k in ("source", "installed_at") if k in cached}
                same = cached.get("size_bytes") == info.st_size and cached.get("mtime_ns") == info.st_mtime_ns
                current = same and cached.get("detector") == DETECTOR_VERSION
                entry.update({"size_bytes": info.st_size, "mtime_ns": info.st_mtime_ns})
                if path.suffix.lower() in PICKLE_SUFFIXES:
                    entry.update({"format": "pickle", "base_family": "unknown", "base_source": "unknown"})
                elif current and "format" in cached:
                    entry.update({k: cached[k] for k in ("format", "error", "detected_kind", "base_family", "base_source", "trigger_words",
                                                         "training_tags", "network", "title", "sha256", "detector") if k in cached})
                else:
                    try:
                        metadata, tensors = read_safetensors_header(path)
                        entry.update({"format": "safetensors", **describe_asset(metadata, tensors)})
                    except (AssetError, OSError) as error:
                        entry.update({"format": "invalid", "error": str(error)[:200], "base_family": "unknown", "base_source": "unknown"})
                    entry["detector"] = DETECTOR_VERSION
                    if same and cached.get("sha256"):
                        entry["sha256"] = cached["sha256"]   # même fichier, règles de détection plus récentes
                if hashes and entry.get("format") == "safetensors" and not entry.get("sha256"):
                    if progress:
                        progress(f"Empreinte de {name}…")
                    try:
                        entry["sha256"] = sha256_file(path)
                    except OSError:
                        pass
                changed = changed or entry != cached
                fresh[key] = entry
                assets.append({"kind": kind, "name": name, "primary_folder": position == 0 and not linked,
                               **{k: v for k, v in entry.items() if k not in ("mtime_ns", "detector")}, "modified_at": info.st_mtime})
    if kinds:
        for key, value in index.items():
            if key.split("/", 1)[0] not in kinds:
                fresh[key] = value
    if changed or set(fresh) != set(index):
        try:
            # Une installation ou une suppression a pu passer pendant ce relevé (les empreintes prennent du temps) :
            # ce qu'elle a écrit dans l'index l'emporte sur la copie lue au départ.
            latest = load_index(worker_root)
            for key in set(index) - set(latest):
                fresh.pop(key, None)
            for key, value in latest.items():
                if not isinstance(value, dict) or value == index.get(key):
                    continue
                mine = fresh.get(key)
                if mine is not None and mine.get("size_bytes") == value.get("size_bytes") and mine.get("mtime_ns") == value.get("mtime_ns"):
                    fresh[key] = {**mine, **{k: value[k] for k in ("source", "installed_at", "sha256") if k in value}}
                else:
                    fresh[key] = value
            save_index(worker_root, fresh)
        except OSError:
            pass
    return assets


def find_duplicate(worker_root, kind, sha256, *, except_name=""):
    """Nom d'une ressource du même type déjà indexée avec cette empreinte (doublon par contenu)."""
    for key, entry in load_index(worker_root).items():
        entry_kind, _, name = key.partition("/")
        if entry_kind == kind and name != except_name and isinstance(entry, dict) and entry.get("sha256") == sha256:
            return name
    return ""


def record_installed(worker_root, kind, name, path, *, sha256, description, source):
    """Inscrit une ressource installée (source sans secret) dans l'index local."""
    info = Path(path).stat()
    files = load_index(worker_root)
    files[index_key(kind, name)] = {
        "size_bytes": info.st_size, "mtime_ns": info.st_mtime_ns, "format": "safetensors", "sha256": sha256,
        **description, "detector": DETECTOR_VERSION, "source": source, "installed_at": time.time(),
    }
    save_index(worker_root, files)


def forget(worker_root, kind, name):
    files = load_index(worker_root)
    if files.pop(index_key(kind, name), None) is not None:
        save_index(worker_root, files)


def checked_path(engine_root, path):
    """Le chemin reste dans le moteur configuré et ne traverse aucun lien."""
    engine_root, path = Path(engine_root), Path(path)
    if not path.resolve().is_relative_to(engine_root.resolve()):
        raise AssetError("Le chemin de la ressource sort du moteur configuré ; opération annulée.")
    cursor = path
    while cursor != engine_root:
        if is_link(cursor):
            raise AssetError("Le chemin de la ressource traverse un lien ; opération annulée pour protéger sa cible.")
        if cursor.parent == cursor:
            raise AssetError("Chemin de ressource invalide.")
        cursor = cursor.parent
    return path


def remove_asset(worker_root, comfy_root, kind, name):
    """Supprime UNE ressource du dossier principal de ComfyUI ; jamais un dossier externe ni un lien."""
    kind = asset_kind(kind)
    try:
        name = safe_relative_name(str(name or ""))
    except ValueError as error:
        raise AssetError("Nom de ressource non autorisé.") from error
    if Path(name).suffix.lower() not in ASSET_SUFFIXES + PICKLE_SUFFIXES or TEMP_FOLDER in Path(name).parts:
        raise AssetError("Nom de ressource non autorisé.")
    comfy_root = Path(comfy_root)
    folder = comfy_root / "models" / ASSET_KINDS[kind]["folder"]
    target = checked_path(comfy_root, folder / name)
    if not target.is_file():
        raise AssetError("Ressource absente du dossier principal de ComfyUI (dossier externe ou déjà supprimée) ; aucun fichier supprimé.")
    try:
        remove_worker_file(target, folder, missing_ok=False)
    except PermissionError:
        # Windows : ComfyUI garde le fichier ouvert tant qu'une génération s'en sert.
        raise AssetError("Fichier utilisé par ComfyUI en ce moment : attends la fin de la génération en cours, puis relance la suppression.") from None
    forget(worker_root, kind, name)
    return {"kind": kind, "name": name}


def diagnostic(worker_root, comfy_root):
    """Faits vérifiables pour le diagnostic du dashboard : dossiers, droits d'écriture, espace, index."""
    import shutil
    comfy_root = Path(comfy_root)
    facts = {"comfyui_root_found": comfy_root.is_dir(), "folders": {}, "custom_nodes": [], "manager_present": False}
    for kind, folders in asset_dirs(comfy_root).items():
        primary = folders[0]
        writable = False
        probe = primary / (".alpine-write-test-" + hashlib.sha256(os.urandom(8)).hexdigest()[:8])
        try:
            if primary.is_dir() and not is_link(primary):
                probe.write_bytes(b"")
                writable = True
        except OSError:
            writable = False
        finally:
            try:
                probe.unlink()
            except OSError:
                pass
        facts["folders"][kind] = {"exists": primary.is_dir(), "writable": writable, "extra_folders": len(folders) - 1}
    try:
        base = comfy_root if comfy_root.is_dir() else Path(worker_root)
        usage = shutil.disk_usage(base)
        facts["disk_free_bytes"], facts["disk_total_bytes"] = usage.free, usage.total
    except OSError:
        pass
    nodes = comfy_root / "custom_nodes"
    try:
        if nodes.is_dir() and not is_link(nodes):
            names = sorted(p.name for p in nodes.iterdir() if p.is_dir() and not p.name.startswith((".", "__")))
            facts["custom_nodes"] = [name[:80] for name in names[:100]]
            facts["manager_present"] = any(name.lower() in ("comfyui-manager", "comfyui_manager") for name in names)
    except OSError:
        pass
    try:
        facts["index_readable"] = isinstance(read_json(index_path(worker_root), {}), dict)
    except (OSError, ValueError, RecursionError, MemoryError):
        facts["index_readable"] = False
    return facts
