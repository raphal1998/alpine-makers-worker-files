"""Alpine Makers · « Atelier 3D ouvert » backend runner.

Runs inside the module's own virtual environment (components/open3d-lab/.venv),
launched by the Hunyuan3D engine service for one job at a time. It never talks
to the network: model weights come from the Worker's verified model folders and
the vendored upstream sources (TripoSG, PartCrafter, TripoSR) are pinned by the
installer. Progress and the final mesh path are reported on stdout as
``ALPINE_PROGRESS`` / ``ALPINE_RESULT`` / ``ALPINE_ERROR`` JSON lines.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

VENDOR = {
    "triposg": "TripoSG", "triposg-scribble": "TripoSG",
    "partcrafter": "PartCrafter", "partcrafter-scene": "PartCrafter",
    "triposr": "TripoSR",
}


def emit(kind, payload):
    print(f"ALPINE_{kind} " + json.dumps(payload, ensure_ascii=False), flush=True)


def progress(value, message):
    emit("PROGRESS", {"progress": int(value), "message": str(message)})


class BackendError(RuntimeError):
    def __init__(self, message, code="BACKEND_ERROR"):
        super().__init__(message)
        self.code = code


def load_spec(path):
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(spec, dict):
        raise BackendError("Spécification de job invalide.", "INVALID_SPEC")
    return spec


def add_vendor_paths(component_root: Path, model_id: str):
    """Only the sources needed by this model join sys.path (PartCrafter's package is literally named `src`)."""
    shims = component_root / "shims"
    vendor = component_root / "vendor" / VENDOR[model_id]
    if not vendor.is_dir():
        raise BackendError(f"Sources {VENDOR[model_id]} absentes du module : répare le module Atelier 3D ouvert.", "COMPONENT_NOT_INSTALLED")
    for path in (shims, vendor):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)
    os.chdir(vendor)


def open_images(paths):
    from PIL import Image

    images = []
    for value in paths:
        path = Path(value)
        if not path.is_file():
            raise BackendError("Image source introuvable pour le backend.", "INVALID_IMAGE")
        image = Image.open(path)
        image.load()
        images.append(image.convert("RGBA"))
    if not images:
        raise BackendError("Une image est requise.", "INVALID_IMAGE_COUNT")
    return images


def square_on_background(image, background=(255, 255, 255), margin=0.1):
    """TripoSG/PartCrafter `prepare_image` equivalent for an already cut-out RGBA image."""
    import numpy as np
    from PIL import Image

    rgba = image.convert("RGBA")
    alpha = np.array(rgba)[:, :, 3]
    ys, xs = np.where(alpha > 0)
    if len(xs) and len(ys):
        rgba = rgba.crop((int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1))
    width, height = rgba.size
    side = max(1, int(max(width, height) * (1 + 2 * margin)))
    canvas = Image.new("RGBA", (side, side), (*background, 255))
    canvas.alpha_composite(rgba, ((side - width) // 2, (side - height) // 2))
    return canvas.convert("RGB")


def to_trimesh(vertices, faces):
    import numpy as np
    import trimesh

    return trimesh.Trimesh(np.asarray(vertices, dtype=np.float32), np.ascontiguousarray(np.asarray(faces)), process=True)


def reduce_faces(mesh, target):
    if not target or len(mesh.faces) <= target:
        return mesh
    try:
        import numpy as np
        import pymeshlab

        collection = pymeshlab.MeshSet()
        collection.add_mesh(pymeshlab.Mesh(vertex_matrix=np.asarray(mesh.vertices, dtype=np.float64), face_matrix=np.asarray(mesh.faces, dtype=np.int32)))
        collection.meshing_decimation_quadric_edge_collapse(targetfacenum=int(target))
        current = collection.current_mesh()
        return to_trimesh(current.vertex_matrix(), current.face_matrix())
    except Exception:
        return mesh


def generator_for(seed):
    import torch

    return torch.Generator(device="cuda").manual_seed(int(seed))


def octree_depths(detail):
    detail = int(detail or 384)
    if detail <= 256:
        return 8, 8
    if detail >= 512:
        return 8, 10
    return 8, 9


def run_triposg(spec, model_dir, images, parameters):
    import torch
    from triposg.pipelines.pipeline_triposg import TripoSGPipeline

    progress(12, "Chargement de TripoSG…")
    pipeline = TripoSGPipeline.from_pretrained(str(model_dir)).to("cuda", torch.float16)
    dense, hierarchical = octree_depths(parameters.get("detail"))
    progress(30, "Génération de la forme (TripoSG)…")
    outputs = pipeline(
        image=square_on_background(images[0]),
        num_inference_steps=int(parameters.get("steps") or 50),
        guidance_scale=float(parameters.get("guidance_scale") or 7.0),
        generator=generator_for(parameters.get("seed", 1234)),
        num_tokens=2048,
        dense_octree_depth=dense, hierarchical_octree_depth=hierarchical,
        use_flash_decoder=False,
    ).samples[0]
    progress(82, "Extraction du maillage…")
    return reduce_faces(to_trimesh(outputs[0], outputs[1]), parameters.get("faces"))


def run_triposg_scribble(spec, model_dir, images, parameters):
    import torch
    from triposg.pipelines.pipeline_triposg_scribble import TripoSGScribblePipeline

    prompt = str(spec.get("prompt") or "").strip()
    if len(prompt) < 3:
        raise BackendError("TripoSG-scribble a besoin d'une courte description en plus du croquis.", "INVALID_PROMPT")
    progress(12, "Chargement de TripoSG-scribble…")
    pipeline = TripoSGScribblePipeline.from_pretrained(str(model_dir)).to("cuda", torch.float16)
    progress(30, "Génération de la forme depuis le croquis…")
    outputs = pipeline(
        image=images[0].convert("RGB"), prompt=prompt,
        generator=generator_for(parameters.get("seed", 1234)),
        num_inference_steps=int(parameters.get("steps") or 16),
        guidance_scale=0,
        attention_kwargs={"cross_attention_scale": 1.0, "cross_attention_2_scale": float(parameters.get("scribble_confidence") or 0.4)},
    ).samples[0]
    progress(82, "Extraction du maillage…")
    return reduce_faces(to_trimesh(outputs[0], outputs[1]), parameters.get("faces"))


PART_COLORS = ((82, 170, 220), (215, 91, 78), (45, 136, 117), (247, 172, 83), (124, 121, 121), (127, 171, 209), (243, 152, 101),
               (145, 204, 192), (150, 59, 121), (181, 206, 78), (189, 119, 149), (199, 193, 222), (200, 151, 54), (236, 110, 102), (238, 182, 212))


def run_partcrafter(spec, model_dir, images, parameters, scene=False):
    import torch
    import trimesh
    from src.pipelines.pipeline_partcrafter import PartCrafterPipeline

    parts = max(1, min(16, int(parameters.get("num_parts") or (6 if scene else 4))))
    progress(12, "Chargement de PartCrafter…")
    pipeline = PartCrafterPipeline.from_pretrained(str(model_dir)).to("cuda", torch.float16)
    image = square_on_background(images[0])
    progress(30, f"Génération de {parts} pièce(s)…")
    outputs = pipeline(
        image=[image] * parts,
        attention_kwargs={"num_parts": parts},
        num_tokens=2048 if scene else 1024,
        generator=generator_for(parameters.get("seed", 1234)),
        num_inference_steps=int(parameters.get("steps") or 50),
        guidance_scale=float(parameters.get("guidance_scale") or 7.0),
        max_num_expanded_coords=int(1e9),
        use_flash_decoder=False,
    ).meshes
    progress(82, "Assemblage des pièces…")
    composition = trimesh.Scene()
    kept = 0
    for index, part in enumerate(outputs):
        if part is None or len(getattr(part, "faces", [])) == 0:
            continue
        part.visual = trimesh.visual.ColorVisuals(mesh=part, vertex_colors=PART_COLORS[index % len(PART_COLORS)])
        composition.add_geometry(part, node_name=f"part_{index + 1:02d}", geom_name=f"part_{index + 1:02d}")
        kept += 1
    if not kept:
        raise BackendError("PartCrafter n'a produit aucune pièce exploitable.", "GENERATION_FAILED")
    return composition


# TripoSR's checkpoint (2024) names its ViT image tokenizer the way transformers 4.x did.
# transformers 5 renamed the layers; from_pretrained would translate them, but TripoSR loads
# the raw state dict itself. Only renames that produce an expected key are applied.
VIT_KEY_RULES = (
    (r"\.encoder\.layer\.(\d+)\.attention\.attention\.query\.", r".layers.\1.attention.q_proj."),
    (r"\.encoder\.layer\.(\d+)\.attention\.attention\.key\.", r".layers.\1.attention.k_proj."),
    (r"\.encoder\.layer\.(\d+)\.attention\.attention\.value\.", r".layers.\1.attention.v_proj."),
    (r"\.encoder\.layer\.(\d+)\.attention\.output\.dense\.", r".layers.\1.attention.o_proj."),
    (r"\.encoder\.layer\.(\d+)\.intermediate\.dense\.", r".layers.\1.mlp.fc1."),
    (r"\.encoder\.layer\.(\d+)\.output\.dense\.", r".layers.\1.mlp.fc2."),
    (r"\.encoder\.layer\.(\d+)\.", r".layers.\1."),
)


def remap_vit_state_dict(state_dict, expected_keys):
    import re

    expected = set(expected_keys)
    result = {}
    for key, value in state_dict.items():
        candidate = key
        if key not in expected:
            for pattern, replacement in VIT_KEY_RULES:
                renamed = re.sub(pattern, replacement, key, count=1)
                if renamed in expected:
                    candidate = renamed
                    break
        result[candidate] = value
    return result


def run_triposr(spec, model_dir, images, parameters):
    import numpy as np
    import torch
    import trimesh
    from PIL import Image
    from tsr.system import TSR
    from tsr.utils import resize_foreground

    progress(12, "Chargement de TripoSR…")
    original_load = TSR.load_state_dict

    def load_with_renames(self, state_dict, strict=True):
        return torch.nn.Module.load_state_dict(self, remap_vit_state_dict(state_dict, self.state_dict().keys()), strict=strict)

    TSR.load_state_dict = load_with_renames
    try:
        model = TSR.from_pretrained(str(model_dir), config_name="config.yaml", weight_name="model.ckpt")
    finally:
        TSR.load_state_dict = original_load
    model.renderer.set_chunk_size(8192)
    model.to("cuda")
    image = resize_foreground(images[0], 0.85)
    array = np.array(image).astype(np.float32) / 255.0
    array = array[:, :, :3] * array[:, :, 3:4] + (1 - array[:, :, 3:4]) * 0.5
    image = Image.fromarray((array * 255.0).astype(np.uint8))
    progress(35, "Reconstruction TripoSR…")
    with torch.no_grad():
        scene_codes = model([image], device="cuda")
    resolution = max(128, min(320, int(parameters.get("detail") or 256)))
    progress(70, "Extraction de la surface…")
    meshes = model.extract_mesh(scene_codes, True, resolution=resolution)
    mesh = meshes[0]
    if not isinstance(mesh, trimesh.Trimesh):
        raise BackendError("TripoSR n'a pas produit de maillage.", "GENERATION_FAILED")
    try:
        trimesh.repair.fix_normals(mesh)
    except Exception:
        pass
    return mesh


RUNNERS = {
    "triposg": run_triposg,
    "triposg-scribble": run_triposg_scribble,
    "partcrafter": lambda spec, model_dir, images, parameters: run_partcrafter(spec, model_dir, images, parameters, scene=False),
    "partcrafter-scene": lambda spec, model_dir, images, parameters: run_partcrafter(spec, model_dir, images, parameters, scene=True),
    "triposr": run_triposr,
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    args = parser.parse_args()
    started = time.time()
    try:
        spec = load_spec(args.spec)
        model_id = str(spec.get("model_id") or "")
        if model_id not in RUNNERS:
            raise BackendError("Modèle inconnu pour l'Atelier 3D ouvert.", "INVALID_MODEL")
        component_root = Path(str(spec.get("component_root") or "")).resolve()
        model_dir = Path(str(spec.get("model_dir") or "")).resolve()
        output_dir = Path(str(spec.get("output_dir") or "")).resolve()
        if not component_root.is_dir() or not (model_dir / ".raphal-ready").is_file():
            raise BackendError("Module ou modèle non installé sur ce Worker.", "COMPONENT_NOT_INSTALLED")
        output_dir.mkdir(parents=True, exist_ok=True)
        add_vendor_paths(component_root, model_id)
        import torch

        if not torch.cuda.is_available():
            raise BackendError("CUDA indisponible dans l'environnement du module.", "CUDA_UNAVAILABLE")
        parameters = spec.get("parameters") if isinstance(spec.get("parameters"), dict) else {}
        images = open_images(spec.get("images") or [])
        progress(5, f"Backend {model_id} prêt ({time.time() - started:.0f} s).")
        result = RUNNERS[model_id](spec, model_dir, images, parameters)
        progress(90, "Export du maillage…")
        target = output_dir / "backend_result.glb"
        result.export(target)
        if not target.is_file() or not target.stat().st_size:
            raise BackendError("Le maillage exporté est vide.", "GENERATION_FAILED")
        import trimesh

        parts = len(result.geometry) if isinstance(result, trimesh.Scene) else 1
        emit("RESULT", {"mesh": str(target), "parts": parts, "model_id": model_id, "seconds": round(time.time() - started, 1)})
        return 0
    except BackendError as error:
        emit("ERROR", {"code": error.code, "message": str(error)})
        return 2
    except Exception as error:  # noqa: BLE001 - reported to the engine, which classifies OOM
        text = f"{type(error).__name__}: {error}"
        emit("ERROR", {"code": "GENERATION_FAILED", "message": text[:600], "traceback": traceback.format_exc()[-4000:]})
        return 1


if __name__ == "__main__":
    sys.exit(main())
