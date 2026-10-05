"""Alpine Makers · Hunyuan3D 2.1 backend runner (shape + optional PBR paint).

Runs inside components/hunyuan3d-21/.venv, launched by the Hunyuan3D engine
service for one job. The repository is the pinned upstream checkout of
Tencent-Hunyuan/Hunyuan3D-2.1; weights come from the Worker's verified model
folders. stdout carries ``ALPINE_PROGRESS`` / ``ALPINE_RESULT`` / ``ALPINE_ERROR``
JSON lines; nothing is downloaded here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

SHAPE_SUBFOLDER = "hunyuan3d-dit-v2-1"
PAINT_SUBFOLDER = "hunyuan3d-paintpbr-v2-1"


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


def add_source_paths(component_root: Path, paint=False):
    for name in (["hy3dshape"] + (["hy3dpaint", "."] if paint else [])):
        path = str((component_root / name).resolve())
        if path not in sys.path:
            sys.path.insert(0, path)
    os.chdir(component_root)


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


def run_shape(spec, model_dir, images, parameters, check_dir):
    import torch
    from hy3dshape.pipelines import Hunyuan3DDiTFlowMatchingPipeline
    from hy3dshape.postprocessors import DegenerateFaceRemover, FloaterRemover

    ckpt = model_dir / SHAPE_SUBFOLDER / "model.fp16.ckpt"
    config = model_dir / SHAPE_SUBFOLDER / "config.yaml"
    if not ckpt.is_file() or not config.is_file():
        raise BackendError("Checkpoint Hunyuan3D 2.1 incomplet : répare le modèle.", "COMPONENT_NOT_INSTALLED")
    progress(12, "Chargement de Hunyuan3D 2.1 (forme)…")
    pipeline = Hunyuan3DDiTFlowMatchingPipeline.from_single_file(ckpt_path=str(ckpt), config_path=str(config), device="cuda", dtype=torch.float16)
    steps = max(3, min(50, int(parameters.get("steps") or 30)))
    octree = max(128, min(512, int(parameters.get("detail") or 384)))
    generator = torch.Generator(device="cuda").manual_seed(int(parameters.get("seed", 1234)))
    progress(30, f"Génération de la forme ({steps} étapes, octree {octree})…")
    mesh = pipeline(
        image=images[0], num_inference_steps=steps, guidance_scale=float(parameters.get("guidance_scale") or 5.0),
        octree_resolution=octree, generator=generator, output_type="trimesh", mc_algo="mc",
    )[0]
    progress(78, "Nettoyage du maillage…")
    mesh = FloaterRemover()(mesh)
    mesh = DegenerateFaceRemover()(mesh)
    return mesh


def run_paint(spec, component_root, paint_dir, mesh_path, image_path, output_dir):
    """PBR texturing with hunyuan3d-paintpbr-v2-1 (experimental: requires the compiled rasterizer)."""
    try:
        from torchvision_fix import apply_fix

        apply_fix()
    except Exception:
        pass
    from textureGenPipeline import Hunyuan3DPaintConfig, Hunyuan3DPaintPipeline

    if not (paint_dir / PAINT_SUBFOLDER / "model_index.json").is_file():
        raise BackendError("Modèle Hunyuan3D 2.1 Paint PBR incomplet : répare le modèle.", "COMPONENT_NOT_INSTALLED")
    progress(84, "Chargement de Hunyuan3D 2.1 Paint PBR…")
    config = Hunyuan3DPaintConfig(max_num_view=6, resolution=512)
    config.realesrgan_ckpt_path = str(paint_dir / "RealESRGAN_x4plus.pth")
    config.multiview_cfg_path = str(component_root / "hy3dpaint" / "cfgs" / "hunyuan-paint-pbr.yaml")
    config.custom_pipeline = str(component_root / "hy3dpaint" / "hunyuanpaintpbr")
    config.multiview_pretrained_path = str(paint_dir)
    painter = Hunyuan3DPaintPipeline(config)
    progress(88, "Synthèse des textures PBR…")
    textured = output_dir / "backend_textured.glb"
    result = painter(mesh_path=str(mesh_path), image_path=str(image_path), output_mesh_path=str(textured), save_glb=True)
    candidate = Path(str(result)) if result else textured
    if not candidate.is_file():
        candidate = textured
    if not candidate.is_file() or not candidate.stat().st_size:
        raise BackendError("Le maillage texturé n'a pas été produit.", "GENERATION_FAILED")
    return candidate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    args = parser.parse_args()
    started = time.time()
    try:
        spec = load_spec(args.spec)
        component_root = Path(str(spec.get("component_root") or "")).resolve()
        output_dir = Path(str(spec.get("output_dir") or "")).resolve()
        texture = spec.get("texture") if isinstance(spec.get("texture"), dict) else None
        if not component_root.is_dir():
            raise BackendError("Module Hunyuan3D 2.1 non installé sur ce Worker.", "COMPONENT_NOT_INSTALLED")
        output_dir.mkdir(parents=True, exist_ok=True)
        add_source_paths(component_root, paint=bool(texture))
        import torch

        if not torch.cuda.is_available():
            raise BackendError("CUDA indisponible dans l'environnement du module.", "CUDA_UNAVAILABLE")
        parameters = spec.get("parameters") if isinstance(spec.get("parameters"), dict) else {}
        images = open_images(spec.get("images") or [])
        progress(5, f"Backend Hunyuan3D 2.1 prêt ({time.time() - started:.0f} s).")
        mesh_input = spec.get("mesh")
        if mesh_input:
            # Texture-only pass on a mesh produced by another model.
            mesh_path = Path(str(mesh_input)).resolve()
            if not mesh_path.is_file():
                raise BackendError("Maillage à texturer introuvable.", "INVALID_MESH")
        else:
            model_dir = Path(str(spec.get("model_dir") or "")).resolve()
            if not (model_dir / ".raphal-ready").is_file():
                raise BackendError("Modèle Hunyuan3D 2.1 non installé.", "COMPONENT_NOT_INSTALLED")
            mesh = run_shape(spec, model_dir, images, parameters, output_dir)
            mesh_path = output_dir / "backend_result.glb"
            mesh.export(mesh_path)
        if not mesh_path.is_file() or not mesh_path.stat().st_size:
            raise BackendError("Le maillage exporté est vide.", "GENERATION_FAILED")
        result = {"mesh": str(mesh_path), "parts": 1, "model_id": str(spec.get("model_id") or ""), "textured": False}
        if texture:
            paint_dir = Path(str(texture.get("model_dir") or "")).resolve()
            if not (paint_dir / ".raphal-ready").is_file():
                raise BackendError("Modèle Hunyuan3D 2.1 Paint PBR non installé.", "COMPONENT_NOT_INSTALLED")
            image_path = output_dir / "paint_source.png"
            images[0].save(image_path)
            textured = run_paint(spec, component_root, paint_dir, mesh_path, image_path, output_dir)
            result.update(mesh=str(textured), textured=True)
        result["seconds"] = round(time.time() - started, 1)
        emit("RESULT", result)
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
