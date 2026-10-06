"""Architecture réelle d'un checkpoint image, lue dans le fichier lui-même (agent 1.41.0).

Le nom d'un fichier et sa résolution habituelle ne disent pas son architecture : un SDXL peut s'appeler
« juggernaut », un SD 2.1 « sd_turbo ». L'en-tête safetensors, lui, liste les tenseurs : leurs noms
distinguent sans ambiguïté SD 1.x (encodeur CLIP-L), SD 2.x (OpenCLIP-H), SDXL (deux encodeurs),
Stable Cascade (étages C et B), FLUX.1 et SD 3.5. SD 2.x existe en deux variantes de même forme :
512 px (prédiction du bruit) et 768 px (prédiction « v ») ; la règle de ComfyUI lui-même les sépare
(écart-type d'un biais précis du réseau > 0,09 ⇒ prédiction v), en lisant ce seul tenseur.

Aucun poids n'est chargé, aucun code du fichier n'est exécuté ; un .ckpt (pickle) n'est jamais ouvert.
Familles renvoyées : sd15, sd2, sd2_768, sdxl, cascade (étage C), cascade_b (étage B), flux, sd35, ou "".
"""
from __future__ import annotations

import json
import math
import struct
from pathlib import Path

HEADER_LIMIT = 64 * 1024 * 1024
# Même tenseur et même seuil que comfy/supported_models.py (SD20.model_type) ; mesuré sur les fichiers officiels :
# 0,057 pour v2-1_512-ema-pruned, 0,534 pour v2-1_768-ema-pruned (agent 1.41.1 : 1.41.0 lisait norm3.bias, toujours < 0,09).
V_PREDICTION_KEY = "model.diffusion_model.output_blocks.11.1.transformer_blocks.0.norm1.bias"
V_PREDICTION_STD = 0.09
_DTYPES = {"F32": ("f", 4), "F16": ("e", 2), "F64": ("d", 8)}
_cache: dict = {}


def _header(path: Path) -> tuple[dict, int]:
    size = path.stat().st_size
    with path.open("rb") as source:
        prefix = source.read(8)
        if len(prefix) != 8:
            raise ValueError("en-tête absent")
        length = struct.unpack("<Q", prefix)[0]
        if not 2 <= length <= min(HEADER_LIMIT, max(0, size - 8)):
            raise ValueError("longueur d'en-tête incohérente")
        header = json.loads(source.read(length).decode("utf-8"))
    if not isinstance(header, dict):
        raise ValueError("en-tête inattendu")
    return header, 8 + length


def _tensor_std(path: Path, header: dict, data_start: int, name: str):
    """Écart-type (population) d'un petit tenseur flottant, ou None s'il est absent ou illisible."""
    entry = header.get(name)
    if not isinstance(entry, dict):
        return None
    code = _DTYPES.get(str(entry.get("dtype")))
    offsets = entry.get("data_offsets")
    if code is None or not isinstance(offsets, list) or len(offsets) != 2 or not all(type(v) is int and v >= 0 for v in offsets):
        return None
    length = offsets[1] - offsets[0]
    if length <= 0 or length > 1024 * 1024 or length % code[1]:
        return None
    with path.open("rb") as source:
        source.seek(data_start + offsets[0])
        raw = source.read(length)
    if len(raw) != length:
        return None
    values = struct.unpack("<" + code[0] * (length // code[1]), raw)
    if not values or not all(math.isfinite(v) for v in values):
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def architecture_from_names(names) -> str:
    """Famille d'après les seuls noms de tenseurs ; « sd2 » sans distinction 512 / 768 (voir checkpoint_architecture)."""
    names = [str(name) for name in names]
    has = lambda prefix: any(name.startswith(prefix) for name in names)   # noqa: E731
    if has("model.diffusion_model.clip_txt_mapper.") or has("clip_txt_mapper."):
        return "cascade"
    if has("model.diffusion_model.effnet_mapper.") or has("effnet_mapper."):
        return "cascade_b"
    if has("model.diffusion_model.double_blocks.") or has("double_blocks."):
        return "flux"
    if has("model.diffusion_model.joint_blocks.") or has("joint_blocks."):
        return "sd35"
    if has("conditioner.embedders.1.") or has("conditioner.embedders.0.model."):
        return "sdxl"          # base (CLIP-L + OpenCLIP-G) ou raffineur (OpenCLIP-G seul)
    if has("cond_stage_model.model."):
        return "sd2"           # OpenCLIP-H
    if has("cond_stage_model.transformer."):
        return "sd15"          # CLIP-L
    return ""


def checkpoint_architecture(path) -> str:
    """Famille d'un checkpoint .safetensors ; "" pour un autre format, un fichier illisible ou une architecture inconnue."""
    path = Path(path)
    if path.suffix.lower() not in (".safetensors", ".sft"):
        return ""
    try:
        info = path.stat()
        key = (str(path), info.st_size, info.st_mtime_ns)
        if key in _cache:
            return _cache[key]
        header, data_start = _header(path)
        family = architecture_from_names(name for name in header if name != "__metadata__")
        if family == "sd2":
            deviation = _tensor_std(path, header, data_start, V_PREDICTION_KEY)
            if deviation is not None and deviation > V_PREDICTION_STD:
                family = "sd2_768"
    except (OSError, ValueError, UnicodeDecodeError, RecursionError, MemoryError, struct.error):
        return ""
    if len(_cache) > 512:
        _cache.clear()
    _cache[key] = family
    return family
