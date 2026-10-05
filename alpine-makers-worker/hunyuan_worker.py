"""Token-protected Hunyuan3D worker bound exclusively to localhost.

This file runs inside local-ai/venv, never in the dashboard interpreter.  It
loads models lazily, keeps the active pipeline in memory, unloads after an idle
timeout, and writes only below the configured generated directory.
"""

from __future__ import annotations

import argparse
import base64
import gc
import io
import inspect
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


JOB_RE = re.compile(r"^local-[a-f0-9]{32}$")
OUTPUTS = {"glb", "obj", "stl"}
VIEW_NAMES = ("front", "left", "back", "right")
# Fixed local directories; requests can select an ID, never a repository/path.
SHAPE_MODELS = {
    "hunyuan3d-2": ("hunyuan3d-2mini", "hunyuan3d-dit-v2-mini", "image"),
    "hunyuan3d-2mv": ("hunyuan3d-2mv", "hunyuan3d-dit-v2-mv", "multi_image"),
    "hunyuan3d-2-standard": ("hunyuan3d-2-standard", "hunyuan3d-dit-v2-0", "image"),
    "hunyuan3d-2mini-turbo": ("hunyuan3d-2mini-turbo", "hunyuan3d-dit-v2-mini-turbo", "image"),
    "hunyuan3d-2-turbo": ("hunyuan3d-2-turbo", "hunyuan3d-dit-v2-0-turbo", "image"),
    "hunyuan3d-2-fast": ("hunyuan3d-2-fast", "hunyuan3d-dit-v2-0-fast", "image"),
    "hunyuan3d-2mini-fast": ("hunyuan3d-2mini-fast", "hunyuan3d-dit-v2-mini-fast", "image"),
    "hunyuan3d-2mv-turbo": ("hunyuan3d-2mv-turbo", "hunyuan3d-dit-v2-mv-turbo", "multi_image"),
    "hunyuan3d-2mv-fast": ("hunyuan3d-2mv-fast", "hunyuan3d-dit-v2-mv-fast", "multi_image"),
}
# Models served by a backend module (its own environment under components/<module>):
# model folder, module, mode, whether the engine removes the background first.
BACKEND_MODELS = {
    "hunyuan3d-21-shape": ("hunyuan3d-21-shape", "hunyuan3d-21", "image", True),
    "triposg": ("triposg", "open3d-lab", "image", True),
    "triposg-scribble": ("triposg-scribble", "open3d-lab", "image", False),
    "partcrafter": ("partcrafter", "open3d-lab", "image", True),
    "partcrafter-scene": ("partcrafter-scene", "open3d-lab", "image", True),
    "triposr": ("triposr", "open3d-lab", "image", True),
}
# Texture models: model folder, paint subfolder, painter ("hunyuan3d" = native hy3dgen, else a module).
TEXTURE_MODELS = {
    "hunyuan3d-2-paint": ("hunyuan3d-2", "hunyuan3d-paint-v2-0", "hunyuan3d"),
    "hunyuan3d-2-paint-turbo": ("hunyuan3d-2-paint-turbo", "hunyuan3d-paint-v2-0-turbo", "hunyuan3d"),
    "hunyuan3d-21-paint-pbr": ("hunyuan3d-21-paint", "hunyuan3d-paintpbr-v2-1", "hunyuan3d-21"),
}
# Text → image bridges for the text mode (the shape itself comes from Hunyuan3D-2mini).
TEXT_IMAGE_MODELS = {"hunyuan-dit": "hunyuan-dit", "hunyuan-dit-1.2": "hunyuan-dit-1.2"}
BACKEND_TIMEOUT_SECONDS = 3 * 3600
BACKEND_PARAMETERS = ("steps", "detail", "seed", "guidance_scale", "num_parts", "faces", "scribble_confidence")


OUT_OF_MEMORY_SIGNS = (
    "out of memory", "cuda_error_out_of_memory", "cublas_status_alloc_failed",
    "cudnn_status_alloc_failed", "not enough memory", "alloc failed",
    # Surface extraction copies the whole grid to host memory, so the run can
    # also end on a plain MemoryError from numpy.
    "memoryerror", "unable to allocate",
)


def is_out_of_memory(error):
    """True when a failure is really the card running out of room.

    PyTorch raises a typed OutOfMemoryError for its own allocator, but a card
    exhausted by another resident engine usually surfaces through cuBLAS,
    cuDNN or the driver as a plain RuntimeError. Both mean the same thing to
    the person waiting for a model.
    """
    text = f"{type(error).__name__} {error}".casefold()
    return any(sign in text for sign in OUT_OF_MEMORY_SIGNS)


def error_summary(error, limit=220):
    """Short, path-free cause kept alongside the generic failure sentence."""
    detail = " ".join(str(error).split())
    # Hide absolute install paths: the cause is useful, the layout is not.
    detail = re.sub(r"[A-Za-z]:\\\S+|/(?:[\w.-]+/){2,}[\w.-]+", "[chemin]", detail)
    summary = f"{type(error).__name__}: {detail}" if detail else type(error).__name__
    return summary[:limit]


class WorkerError(RuntimeError):
    def __init__(self, message, code="WORKER_ERROR", status=400):
        super().__init__(message)
        self.code = code
        self.status = status


class InferenceRuntime:
    def __init__(self, root: Path, generated_root: Path, idle_timeout: int):
        self.root = root.resolve()
        self.models = (self.root / "models").resolve()
        self.generated_root = generated_root.resolve()
        self.generated_root.mkdir(parents=True, exist_ok=True)
        self.idle_timeout = max(60, int(idle_timeout))
        self.lock = threading.RLock()
        self.execution_lock = threading.Lock()
        self.jobs_path = self.generated_root / ".engine-jobs.json"
        self.jobs = self._load_jobs()
        self._save_jobs_locked()
        self.pipeline = None
        self.pipeline_key = ""
        self.pipeline_tex = None
        self.pipeline_tex_key = ""
        self.last_used = 0.0
        self.busy = False
        self.last_error = ""
        threading.Thread(target=self._idle_watch, name="local-ai3d-idle", daemon=True).start()

    def _load_jobs(self):
        try:
            jobs = json.loads(self.jobs_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            raise RuntimeError("Journal du moteur illisible ; historique conservé.") from error
        if not isinstance(jobs, dict) or any(not JOB_RE.fullmatch(key) or not isinstance(job, dict) for key, job in jobs.items()):
            raise RuntimeError("Journal du moteur invalide ; historique conservé.")
        for job in jobs.values():
            if job.get("state") not in {"completed", "cancelled", "failed", "uncertain"}:
                job.update(state="uncertain", error="Moteur redémarré pendant le calcul ; aucune relance automatique.", error_code="ENGINE_RESTARTED", engine_stop_confirmed=True, finished_at=time.time())
            # A newly-started engine no longer owns the old CUDA context.
            job["engine_stop_confirmed"] = True
        return jobs

    def _save_jobs_locked(self):
        # Only execution metadata/results, never the source images or secrets.
        temporary = self.jobs_path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(self.jobs, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.jobs_path)

    def job(self, job_id):
        with self.lock:
            if not JOB_RE.fullmatch(str(job_id)) or job_id not in self.jobs:
                raise WorkerError("Tâche moteur introuvable.", "JOB_NOT_FOUND", 404)
            return json.loads(json.dumps(self.jobs[job_id]))

    def cancel_job(self, job_id):
        with self.lock:
            job = self.job(job_id)
            if job["state"] not in {"completed", "cancelled", "failed", "uncertain"}:
                if job.get("phase") == "queued":
                    self.jobs[job_id].update(state="cancelled", cancel_requested=True, phase="cancelled", engine_stop_confirmed=True,
                        error="Tâche annulée avant le calcul.", error_code="CANCELLED", finished_at=time.time())
                else:
                    self.jobs[job_id].update(cancel_requested=True, phase="cancelling", engine_stop_confirmed=False)
                self._save_jobs_locked()
            return self.job(job_id)

    def _check_cancel(self, job_id, cancel_check=None):
        with self.lock:
            requested = self.jobs[job_id].get("cancel_requested", False)
        if requested or (cancel_check is not None and cancel_check()):
            raise WorkerError("Génération annulée au prochain point d'interruption du moteur.", "CANCELLED", 409)

    @staticmethod
    def _confirm_engine_stopped():
        # Raising from a callback stops scheduling steps; queued CUDA kernels
        # must also finish before reporting this execution as stopped.
        torch = sys.modules.get("torch")
        if torch is None:
            return True
        try:
            if torch.cuda.is_available():
                torch.cuda.synchronize()
            return True
        except Exception:
            return False

    def generate(self, payload, cancel_check=None):
        job_id = str(payload.get("job_id") or "").lower()
        if not JOB_RE.fullmatch(job_id):
            raise WorkerError("Identifiant de tâche invalide.", "INVALID_JOB", 400)
        with self.lock:
            existing = self.jobs.get(job_id)
            if existing:
                if existing["state"] == "completed":
                    return {**self.job(job_id)["result"], "engine_stop_confirmed": bool(existing.get("engine_stop_confirmed"))}
                code = "JOB_RUNNING" if existing["state"] == "running" else existing.get("error_code", "JOB_ALREADY_FINISHED")
                raise WorkerError(existing.get("error") or "Cette tâche est déjà acceptée. Consulter son statut, sans relancer le calcul.", code, 409)
            if sum(job.get("state") == "running" for job in self.jobs.values()) >= 8:
                raise WorkerError("File du moteur pleine.", "QUEUE_FULL", 429)
            self.jobs[job_id] = {"job_id": job_id, "state": "running", "phase": "queued", "created_at": time.time(), "cancel_requested": False, "engine_stop_confirmed": False}
            try:
                self._save_jobs_locked()
            except OSError:
                self.jobs.pop(job_id, None)
                raise
        # The lock covers preprocessing, model loading, sampling and export.
        # A cancellation affects only this ID, never a different GPU request.
        with self.execution_lock:
            try:
                with self.lock:
                    # Reserve the running phase atomically with the cancel
                    # check; a queued cancellation cannot race into GPU work.
                    self._check_cancel(job_id, cancel_check)
                    self.busy = True
                    self.jobs[job_id].update(phase="generating", started_at=time.time())
                    self._save_jobs_locked()
                result = self._generate_impl(payload, lambda: self._check_cancel(job_id, cancel_check))
                self._check_cancel(job_id, cancel_check)
                stopped = self._confirm_engine_stopped()
                with self.lock:
                    self.jobs[job_id].update(state="completed", phase="completed", result=result, engine_stop_confirmed=stopped, finished_at=time.time())
                    self._save_jobs_locked()
                return {**result, "engine_stop_confirmed": stopped}
            except Exception as error:
                stopped = self._confirm_engine_stopped()
                with self.lock:
                    cancelled = getattr(error, "code", "") == "CANCELLED"
                    self.jobs[job_id].update(state="cancelled" if cancelled else "failed", phase="cancelled" if cancelled else "failed", error=str(error)[:400], error_code=getattr(error, "code", "GENERATION_FAILED"), engine_stop_confirmed=stopped, finished_at=time.time())
                    self._save_jobs_locked()
                raise
            finally:
                self.busy = False
                self.last_used = time.time()

    def _ready(self, name):
        return (self.models / name / ".raphal-ready").is_file()

    def _module_root(self, module):
        """A backend module lives next to this engine under components/<module>; never a link."""
        root = (self.root.parent / module).resolve()
        if root.parent != self.root.parent or root.is_symlink():
            raise WorkerError("Chemin du module 3D non autorisé.", "INVALID_PATH", 400)
        return root

    def _module_ready(self, module):
        try:
            root = self._module_root(module)
        except WorkerError:
            return False
        python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        return (root / ".raphal-ready").is_file() and python.is_file() and (root / "alpine_backend.py").is_file()

    def _backend_model_ready(self, model_id):
        folder, module, _, _ = BACKEND_MODELS[model_id]
        return self._ready(folder) and self._module_ready(module)

    def _texture_model(self, requested=""):
        """Explicit texture model when installed, else the first installed painter."""
        requested = str(requested or "")
        candidates = ([requested] if requested in TEXTURE_MODELS else []) + [name for name in TEXTURE_MODELS if name != requested]
        for texture_id in candidates:
            folder, _, painter = TEXTURE_MODELS[texture_id]
            if self._ready(folder) and (painter == "hunyuan3d" or self._module_ready(painter)):
                return texture_id
        return None

    def capabilities(self):
        return {
            "core": any(self._ready(folder) for folder, _, mode in SHAPE_MODELS.values() if mode == "image")
                    or any(self._backend_model_ready(model_id) for model_id in BACKEND_MODELS),
            "multi_image": self._ready("hunyuan3d-2mv"),
            "text_to_image": any(self._ready(folder) for folder in TEXT_IMAGE_MODELS.values()),
            "texture": self._texture_model() is not None,
            "stable_fast_3d": self._ready("stable-fast-3d"),
            "backends": {module: self._module_ready(module) for module in sorted({spec[1] for spec in BACKEND_MODELS.values()})},
        }

    def health(self):
        info = {
            "ok": True,
            "state": "BUSY" if self.busy else "READY",
            "model_loaded": bool(self.pipeline),
            "loaded_model": self.pipeline_key,
            "last_error": self.last_error,
            "capabilities": self.capabilities(),
            "supported_model_ids": [*SHAPE_MODELS, *BACKEND_MODELS, *TEXT_IMAGE_MODELS, *TEXTURE_MODELS],
            "python": sys.version.split()[0],
            "pid": os.getpid(),
        }
        try:
            import torch

            info.update(
                torch=str(torch.__version__), torch_cuda=str(torch.version.cuda or ""),
                cuda_available=bool(torch.cuda.is_available()),
                gpu=torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU",
                vram_total_mb=round(torch.cuda.get_device_properties(0).total_memory / 1048576) if torch.cuda.is_available() else 0,
                vram_allocated_mb=round(torch.cuda.memory_allocated(0) / 1048576) if torch.cuda.is_available() else 0,
            )
        except Exception as error:
            info.update(cuda_available=False, gpu="Indisponible", torch_error=str(error))
        return info

    def unload(self):
        with self.lock:
            self.pipeline = None
            self.pipeline_tex = None
            self.pipeline_tex_key = ""
            self.pipeline_key = ""
            gc.collect()
            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    torch.cuda.ipc_collect()
            except Exception:
                pass
        return self.health()

    def _idle_watch(self):
        while True:
            time.sleep(15)
            if self.pipeline and not self.busy and self.last_used and time.time() - self.last_used >= self.idle_timeout:
                self.unload()

    @staticmethod
    def _shape_model(mode, model_id=""):
        selected = str(model_id or ("hunyuan3d-2mv" if mode == "multi_image" else "hunyuan3d-2"))
        if mode == "text" and selected in TEXT_IMAGE_MODELS:
            selected = "hunyuan3d-2"
        expected_mode = "image" if mode == "text" else mode
        if selected in BACKEND_MODELS:
            folder, _module, backend_mode, _ = BACKEND_MODELS[selected]
            if backend_mode != expected_mode:
                raise WorkerError("Ce modèle 3D ne prend pas en charge le mode demandé.", "INVALID_MODEL", 400)
            return selected, (folder, "", backend_mode)
        spec = SHAPE_MODELS.get(selected)
        if spec is None or spec[2] != expected_mode:
            raise WorkerError("Ce modèle Hunyuan3D ne prend pas en charge le mode demandé.", "INVALID_MODEL", 400)
        return selected, spec

    def _load_pipeline(self, mode, model_id=""):
        key, (folder, subfolder, _) = self._shape_model(mode, model_id)
        if key in BACKEND_MODELS:
            raise WorkerError("Ce modèle est servi par un module séparé, pas par le pipeline natif.", "INVALID_MODEL", 400)
        import torch
        from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline

        if not torch.cuda.is_available():
            raise WorkerError("CUDA n'est pas disponible dans l'environnement IA local.", "CUDA_UNAVAILABLE", 503)
        model_dir = self.models / folder
        if not (model_dir / ".raphal-ready").is_file():
            raise WorkerError(f"Le modèle {key} n'est pas installé.", "COMPONENT_NOT_INSTALLED", 409)
        if self.pipeline is not None and self.pipeline_key == key:
            return self.pipeline
        self.unload()
        load_options = {"subfolder": subfolder, "use_safetensors": True, "device": "cuda"}
        try:
            self.pipeline = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(str(model_dir), variant="fp16", **load_options)
        except (OSError, ValueError):
            # Some official snapshots contain fp16 weights without a Diffusers
            # variant suffix. Falling back keeps both layouts supported.
            self.pipeline = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(str(model_dir), **load_options)
        self.pipeline_key = key
        self.last_used = time.time()
        return self.pipeline

    def _text_to_image(self, prompt, negative_prompt, seed, steps, resolution, model_id="hunyuan-dit"):
        folder = TEXT_IMAGE_MODELS.get(str(model_id or ""), "")
        if not folder or not self._ready(folder):
            # Fall back to any installed bridge so the text mode keeps working after an update.
            folder = next((name for name in TEXT_IMAGE_MODELS.values() if self._ready(name)), "")
        if not folder:
            raise WorkerError("Le composant HunyuanDiT Texte→Image n'est pas installé.", "COMPONENT_NOT_INSTALLED", 409)
        from hy3dgen.text2image import HunyuanDiTPipeline

        pipeline = HunyuanDiTPipeline(str(self.models / folder), device="cuda")
        options = {"negative_prompt": negative_prompt or None, "seed": seed, "num_inference_steps": steps, "width": resolution, "height": resolution}
        signature = inspect.signature(pipeline.__call__)
        if not any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
            options = {key: value for key, value in options.items() if key in signature.parameters}
        try:
            # Select supported parameters before inference; never retry a
            # TypeError raised inside an already-started costly calculation.
            return pipeline(prompt, **options)
        finally:
            del pipeline
            gc.collect()
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:
                pass

    @staticmethod
    def _decode_images(values):
        from PIL import Image

        images = []
        for encoded in values:
            try:
                data = base64.b64decode(encoded, validate=True)
                if not data or len(data) > 20 * 1024 * 1024:
                    raise ValueError
                image = Image.open(io.BytesIO(data))
                image.load()
                if image.width < 32 or image.height < 32 or image.width * image.height > 40_000_000:
                    raise ValueError
                images.append(image.convert("RGBA"))
            except Exception as error:
                raise WorkerError("Une image locale est invalide ou trop grande.", "INVALID_IMAGE", 400) from error
        return images

    @staticmethod
    def _preprocess(image, remove_background=True):
        if not remove_background or image.mode == "RGBA" and image.getextrema()[-1][0] < 255:
            return image
        from hy3dgen.rembg import BackgroundRemover

        return BackgroundRemover()(image)

    @staticmethod
    def _mesh_stats(mesh):
        import numpy as np

        bounds = mesh.bounds if hasattr(mesh, "bounds") else np.zeros((2, 3))
        extents = mesh.extents if hasattr(mesh, "extents") else np.zeros(3)
        return {
            "vertices": int(len(getattr(mesh, "vertices", []))),
            "faces": int(len(getattr(mesh, "faces", []))),
            "components": int(len(mesh.split(only_watertight=False))) if hasattr(mesh, "split") else 1,
            "watertight": bool(getattr(mesh, "is_watertight", False)),
            "bounds": [[float(value) for value in row] for row in bounds],
        }, {"x": float(extents[0]), "y": float(extents[1]), "z": float(extents[2]), "unit": "model"}

    def _generate_impl(self, payload, check_cancel):
        check_cancel()
        import torch

        job_id = str(payload.get("job_id") or "").lower()
        if not JOB_RE.fullmatch(job_id):
            raise WorkerError("Identifiant de tâche invalide.", "INVALID_JOB", 400)
        mode = str(payload.get("mode") or "").lower()
        if mode not in {"text", "image", "multi_image"}:
            raise WorkerError("Mode local invalide.", "INVALID_MODE", 400)
        output_format = str(payload.get("output_format") or "glb").lower()
        if output_format not in OUTPUTS:
            raise WorkerError("Format de sortie local invalide.", "INVALID_FORMAT", 400)
        parameters = payload.get("parameters") if isinstance(payload.get("parameters"), dict) else {}
        requested_model = str(payload.get("model_id") or parameters.get("model_id") or "")
        selected_model, _ = self._shape_model(mode, requested_model)
        backend = BACKEND_MODELS.get(selected_model)
        seed = max(0, min(int(parameters.get("seed", 1234)), 2 ** 31 - 1))
        quality = str(parameters.get("quality") or "balanced")
        default_steps = 5 if selected_model.endswith("-turbo") else {"fast": 8, "balanced": 20, "max": 30}.get(quality, 20)
        steps = max(3, min(50, int(parameters.get("steps") or default_steps)))
        detail = str(parameters.get("detail") or "").strip()
        octree = max(128, min(512, int(detail))) if detail.isdigit() else {"fast": 256, "balanced": 384, "max": 512}.get(quality, 384)
        images = self._decode_images(payload.get("images") or [])
        if mode == "text":
            prompt = str(payload.get("prompt") or "").strip()
            if len(prompt) < 8:
                raise WorkerError("Le prompt local est trop court.", "INVALID_PROMPT", 400)
            text_image_resolution = 512 if quality == "fast" or octree <= 256 else (768 if quality == "balanced" or octree <= 384 else 1024)
            images = [self._text_to_image(
                prompt,
                str(payload.get("negative_prompt") or ""),
                seed,
                steps,
                text_image_resolution,
                requested_model,
            )]
            check_cancel()
        if mode == "image" and len(images) != 1:
            raise WorkerError("Une image est requise.", "INVALID_IMAGE_COUNT", 400)
        if mode == "multi_image" and not 2 <= len(images) <= len(VIEW_NAMES):
            raise WorkerError("Deux à six vues sont requises.", "INVALID_IMAGE_COUNT", 400)
        remove_background = bool(parameters.get("remove_background", True)) and (backend is None or backend[3])
        images = [self._preprocess(image, remove_background) for image in images]
        check_cancel()
        pipeline = None if backend else self._load_pipeline(mode, selected_model)
        check_cancel()
        self.busy = True
        self.last_error = ""
        textured_file = None
        try:
            if backend:
                mesh = self._run_backend(job_id, selected_model, images, payload, {**parameters, "steps": steps, "detail": octree, "seed": seed}, check_cancel)
            else:
                from hy3dgen.shapegen import DegenerateFaceRemover, FloaterRemover

                if mode == "multi_image":
                    requested_views = parameters.get("views") if isinstance(parameters.get("views"), list) else []
                    used_views = set()
                    resolved_views = []
                    for index in range(len(images)):
                        requested = str(requested_views[index] if index < len(requested_views) else "auto").strip().lower()
                        if requested not in VIEW_NAMES or requested == "auto" or requested in used_views:
                            requested = next((name for name in VIEW_NAMES if name not in used_views), VIEW_NAMES[index % len(VIEW_NAMES)])
                        used_views.add(requested)
                        resolved_views.append(requested)
                    image_input = {resolved_views[index]: image for index, image in enumerate(images)}
                else:
                    image_input = images[0]
                generator = torch.Generator(device="cuda").manual_seed(seed)
                # Guidance réglée par modèle sur le site ; sans valeur, le défaut du pipeline reste.
                guidance = {}
                try:
                    if parameters.get("guidance_scale") not in (None, ""):
                        guidance = {"guidance_scale": max(0.0, min(15.0, float(parameters["guidance_scale"])))}
                except (TypeError, ValueError):
                    guidance = {}
                mesh = pipeline(
                    image=image_input, num_inference_steps=steps, octree_resolution=octree,
                    generator=generator, output_type="trimesh", mc_algo="mc", **guidance,
                    # Hunyuan3D-2 calls this after each diffusion step. Raising
                    # unwinds this request; no global engine shutdown is involved.
                    callback=lambda *_: check_cancel(), callback_steps=1,
                )[0]
                check_cancel()
                mesh = FloaterRemover()(mesh)
                mesh = DegenerateFaceRemover()(mesh)
            check_cancel()
            if bool(parameters.get("texture")):
                texture_id = self._texture_model(parameters.get("texture_model"))
                if texture_id is None:
                    raise WorkerError("Aucun modèle de texture Hunyuan3D Paint n'est installé.", "COMPONENT_NOT_INSTALLED", 409)
                folder, subfolder, painter = TEXTURE_MODELS[texture_id]
                if painter == "hunyuan3d":
                    from hy3dgen.texgen import Hunyuan3DPaintPipeline

                    if self.pipeline_tex is None or self.pipeline_tex_key != texture_id:
                        self.pipeline_tex = Hunyuan3DPaintPipeline.from_pretrained(str(self.models / folder), subfolder=subfolder)
                        self.pipeline_tex_key = texture_id
                    check_cancel()
                    mesh = self.pipeline_tex(self._single_mesh(mesh), image=images[0])
                else:
                    mesh, textured_file = self._run_backend_texture(job_id, texture_id, mesh, images[0], check_cancel)
                # Paint/text preparation can be opaque: remain 'cancelling'
                # until they return, never claim the GPU stopped prematurely.
                check_cancel()
            destination = (self.generated_root / job_id).resolve()
            if self.generated_root not in destination.parents:
                raise WorkerError("Chemin de sortie invalide.", "INVALID_PATH", 400)
            destination.mkdir(parents=True, exist_ok=True)
            files = self._export_outputs(mesh, destination, output_format, check_cancel, textured_file)
            stats, dimensions = self._mesh_stats(self._single_mesh(mesh))
            self.last_used = time.time()
            return {"files": files, "mesh_stats": stats, "dimensions": dimensions}
        except WorkerError:
            raise
        except Exception as error:
            self.last_error = traceback.format_exc()
            if isinstance(error, torch.cuda.OutOfMemoryError) or is_out_of_memory(error):
                self.unload()
                raise WorkerError(
                    "Mémoire insuffisante pour ce calcul. Libère la carte (arrête la génération "
                    "d’images sur ce PC), choisis un modèle Mini ou Turbo, baisse le niveau de "
                    "détail, ou désactive la texture.",
                    "CUDA_OOM", 507,
                ) from error
            raise WorkerError(
                f"Le moteur Hunyuan3D n'a pas pu produire le modèle. Cause : {error_summary(error)}",
                "GENERATION_FAILED", 500,
            ) from error
        finally:
            self.busy = False
            self.last_used = time.time()
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass

    @staticmethod
    def _single_mesh(value):
        """One Trimesh for statistics, painting and OBJ/STL export (scenes are concatenated)."""
        import trimesh

        if isinstance(value, trimesh.Scene):
            geometries = [geometry for geometry in value.dump() if isinstance(geometry, trimesh.Trimesh)]
            if not geometries:
                raise WorkerError("Le résultat 3D ne contient aucune géométrie.", "GENERATION_FAILED", 500)
            return trimesh.util.concatenate(geometries) if len(geometries) > 1 else geometries[0]
        return value

    def _export_outputs(self, mesh, destination, output_format, check_cancel, textured_file=None):
        import trimesh

        files = {}
        single = self._single_mesh(mesh)
        # Export all three formats once so changing download format remains free.
        for extension in sorted(OUTPUTS):
            check_cancel()
            target = destination / f"model.{extension}"
            try:
                if extension == "glb" and textured_file is not None:
                    shutil.copy2(textured_file, target)
                elif extension == "glb" and isinstance(mesh, trimesh.Scene):
                    mesh.export(target)
                else:
                    single.export(target)
                if target.is_file() and target.stat().st_size:
                    files[extension] = target.name
            except Exception:
                if extension == output_format:
                    raise
        return files

    def _backend_workdir(self, job_id):
        work = (self.generated_root / job_id / "backend").resolve()
        if self.generated_root not in work.parents:
            raise WorkerError("Chemin de travail invalide.", "INVALID_PATH", 400)
        work.mkdir(parents=True, exist_ok=True)
        return work

    def _backend_launcher(self, module):
        root = self._module_root(module)
        python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        runner = root / "alpine_backend.py"
        if not self._module_ready(module):
            raise WorkerError(f"Le module {module} n'est pas installé sur ce Worker : installe-le depuis Mes Workers → Installations.", "COMPONENT_NOT_INSTALLED", 409)
        return root, python, runner

    def _run_backend(self, job_id, model_id, images, payload, parameters, check_cancel):
        """Generate the shape through a module runner living in its own environment."""
        folder, module, _, _ = BACKEND_MODELS[model_id]
        root, python, runner = self._backend_launcher(module)
        model_dir = self.models / folder
        if not (model_dir / ".raphal-ready").is_file():
            raise WorkerError(f"Le modèle {model_id} n'est pas installé.", "COMPONENT_NOT_INSTALLED", 409)
        import trimesh

        work = self._backend_workdir(job_id)
        image_paths = []
        for index, image in enumerate(images):
            path = work / f"source-{index + 1:02d}.png"
            image.save(path)
            image_paths.append(str(path))
        spec = {
            "model_id": model_id, "component_root": str(root), "model_dir": str(model_dir), "output_dir": str(work),
            "images": image_paths, "prompt": str(payload.get("prompt") or ""),
            "parameters": {key: parameters[key] for key in BACKEND_PARAMETERS if key in parameters},
        }
        result = self._run_backend_process(job_id, module, python, runner, root, spec, work / "spec.json", check_cancel)
        mesh_path = Path(str(result.get("mesh") or "")).resolve()
        if work not in mesh_path.parents or not mesh_path.is_file():
            raise WorkerError("Le module 3D n'a pas produit de fichier de maillage valide.", "GENERATION_FAILED", 500)
        loaded = trimesh.load(mesh_path, force="scene")
        geometries = [geometry for geometry in loaded.dump() if isinstance(geometry, trimesh.Trimesh)] if isinstance(loaded, trimesh.Scene) else [loaded]
        if not geometries:
            raise WorkerError("Le module 3D a produit un maillage vide.", "GENERATION_FAILED", 500)
        return loaded if isinstance(loaded, trimesh.Scene) and len(geometries) > 1 else geometries[0]

    def _run_backend_texture(self, job_id, texture_id, mesh, image, check_cancel):
        """Texture an existing mesh through a module painter; returns (mesh, textured GLB path)."""
        import trimesh

        folder, _subfolder, module = TEXTURE_MODELS[texture_id]
        root, python, runner = self._backend_launcher(module)
        paint_dir = self.models / folder
        if not (paint_dir / ".raphal-ready").is_file():
            raise WorkerError(f"Le modèle de texture {texture_id} n'est pas installé.", "COMPONENT_NOT_INSTALLED", 409)
        work = self._backend_workdir(job_id)
        shape_path = work / "shape_for_paint.glb"
        self._single_mesh(mesh).export(shape_path)
        source = work / "paint-source.png"
        image.save(source)
        spec = {"model_id": texture_id, "component_root": str(root), "output_dir": str(work), "images": [str(source)],
                "mesh": str(shape_path), "texture": {"model_id": texture_id, "model_dir": str(paint_dir)}, "parameters": {}}
        result = self._run_backend_process(job_id, module, python, runner, root, spec, work / "paint-spec.json", check_cancel)
        textured = Path(str(result.get("mesh") or "")).resolve()
        if work not in textured.parents or not textured.is_file():
            raise WorkerError("Le module de texture n'a pas produit de fichier valide.", "GENERATION_FAILED", 500)
        loaded = trimesh.load(textured, force="mesh")
        return loaded, textured

    def _run_backend_process(self, job_id, module, python, runner, cwd, spec, spec_path, check_cancel):
        """Run a module runner as a child process, relaying progress and honouring cancellation."""
        # Native pipelines hold VRAM; the module needs the whole card.
        self.unload()
        spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        environment = dict(os.environ)
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        try:
            process = subprocess.Popen(
                [str(python), str(runner), "--spec", str(spec_path)], cwd=str(cwd), env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            )
        except OSError as error:
            raise WorkerError(f"Impossible de lancer le module {module} : {error_summary(error)}", "BACKEND_LAUNCH_FAILED", 500) from error
        lines = queue.Queue()

        def pump():
            try:
                for line in process.stdout or []:
                    lines.put(line.rstrip("\r\n"))
            finally:
                lines.put(None)

        threading.Thread(target=pump, name=f"ai3d-backend-{module}", daemon=True).start()
        result, error, tail = None, None, []
        deadline = time.time() + BACKEND_TIMEOUT_SECONDS
        finished = False
        try:
            while not finished:
                try:
                    check_cancel()
                except WorkerError:
                    process.kill()
                    process.wait(timeout=60)
                    raise
                if time.time() > deadline:
                    process.kill()
                    process.wait(timeout=60)
                    raise WorkerError(f"Le module {module} n'a pas terminé dans le délai imparti.", "BACKEND_TIMEOUT", 504)
                try:
                    line = lines.get(timeout=0.5)
                except queue.Empty:
                    continue
                if line is None:
                    finished = True
                    break
                if line.startswith("ALPINE_PROGRESS "):
                    try:
                        update = json.loads(line[len("ALPINE_PROGRESS "):])
                        with self.lock:
                            self.jobs[job_id].update(backend_progress=int(update.get("progress") or 0), backend_message=str(update.get("message") or "")[:300])
                    except (ValueError, TypeError, KeyError):
                        pass
                elif line.startswith("ALPINE_RESULT "):
                    try:
                        result = json.loads(line[len("ALPINE_RESULT "):])
                    except ValueError:
                        result = None
                elif line.startswith("ALPINE_ERROR "):
                    try:
                        error = json.loads(line[len("ALPINE_ERROR "):])
                    except ValueError:
                        error = {"code": "GENERATION_FAILED", "message": line[-300:]}
                else:
                    tail = (tail + [line])[-40:]
            process.wait(timeout=120)
        finally:
            if process.poll() is None:
                process.kill()
        if error is not None:
            message = str(error.get("message") or "Le module 3D a échoué.")
            if is_out_of_memory(RuntimeError(message)):
                raise WorkerError(
                    "Mémoire insuffisante pour ce calcul dans le module 3D. Libère la carte (arrête la génération "
                    "d’images sur ce PC), choisis un modèle plus léger ou baisse le niveau de détail.",
                    "CUDA_OOM", 507,
                )
            code = str(error.get("code") or "GENERATION_FAILED")
            status = 409 if code == "COMPONENT_NOT_INSTALLED" else 400 if code.startswith("INVALID_") else 500
            self.last_error = str(error.get("traceback") or message)
            raise WorkerError(f"Le module {module} n'a pas pu produire le modèle. Cause : {message[:400]}", code, status)
        if result is None or process.returncode:
            self.last_error = "\n".join(tail)
            raise WorkerError(f"Le module {module} s'est arrêté sans résultat (code {process.returncode}). {' '.join(tail[-3:])[:300]}", "GENERATION_FAILED", 500)
        return result

    def prepare(self, payload):
        import trimesh

        job_id = str(payload.get("job_id") or "").lower()
        if not JOB_RE.fullmatch(job_id):
            raise WorkerError("Identifiant de tâche invalide.", "INVALID_JOB", 400)
        root = (self.generated_root / job_id).resolve()
        if self.generated_root not in root.parents:
            raise WorkerError("Chemin de modèle invalide.", "INVALID_PATH", 400)
        source = next((root / f"model.{ext}" for ext in ("glb", "obj", "stl") if (root / f"model.{ext}").is_file()), None)
        if source is None:
            raise WorkerError("Modèle local introuvable.", "MODEL_NOT_FOUND", 404)
        loaded = trimesh.load(source, force="scene")
        mesh = loaded.dump(concatenate=True) if isinstance(loaded, trimesh.Scene) else loaded
        before_components = len(mesh.split(only_watertight=False))
        before_watertight = bool(mesh.is_watertight)
        try:
            mesh.remove_unreferenced_vertices()
            mesh.remove_infinite_values()
            trimesh.repair.fix_normals(mesh, multibody=True)
            trimesh.repair.fill_holes(mesh)
        except Exception:
            pass
        components = list(mesh.split(only_watertight=False))
        if components:
            largest = max(component.volume if component.is_volume else component.area for component in components)
            kept = [component for component in components if (component.volume if component.is_volume else component.area) >= max(largest * 0.0005, 1e-9)]
            if kept:
                mesh = trimesh.util.concatenate(kept)
        height = payload.get("height_mm")
        if height not in (None, ""):
            height = float(height)
            current = float(mesh.extents[2])
            if current > 0:
                mesh.apply_scale(height / current)
        target = root / "prepared.stl"
        mesh.export(target)
        stats, dimensions = self._mesh_stats(mesh)
        try:
            boundary_edges = trimesh.grouping.group_rows(mesh.edges_sorted, require_count=1)
            non_manifold = int(len(boundary_edges))
        except Exception:
            non_manifold = 0
        report = {
            "mesh_closed": bool(mesh.is_watertight), "was_closed": before_watertight,
            "non_manifold": non_manifold, "components_before": before_components,
            "components_after": stats["components"], "dimensions": dimensions,
            "repair": "réussie" if mesh.is_watertight else "partielle",
        }
        return {"files": {"stl": target.name}, "report": report}


def download_model(root: Path, component: str):
    from huggingface_hub import snapshot_download

    mapping = {
        "core": ("tencent/Hunyuan3D-2mini", "hunyuan3d-2mini"),
        "multi_image": ("tencent/Hunyuan3D-2mv", "hunyuan3d-2mv"),
        "text_to_image": ("Tencent-Hunyuan/HunyuanDiT-v1.1-Diffusers-Distilled", "hunyuan-dit"),
        "texture": ("tencent/Hunyuan3D-2", "hunyuan3d-2"),
        "stable_fast_3d": ("stabilityai/stable-fast-3d", "stable-fast-3d"),
    }
    if component not in mapping:
        raise SystemExit(f"Unknown component: {component}")
    repo_id, folder = mapping[component]
    destination = root / "models" / folder
    destination.mkdir(parents=True, exist_ok=True)
    snapshot_download(repo_id=repo_id, local_dir=destination, local_dir_use_symlinks=False)
    (destination / ".raphal-ready").write_text(json.dumps({"repo": repo_id, "installed_at": time.time()}), encoding="utf-8")
    print(json.dumps({"ok": True, "component": component, "model": repo_id, "path": str(destination)}))


class Handler(BaseHTTPRequestHandler):
    server_version = "RaphalLocalAI/1.0"

    def log_message(self, format_string, *args):
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {self.client_address[0]} {format_string % args}", flush=True)

    def _json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        return self.client_address[0] in {"127.0.0.1", "::1"} and secrets_compare(self.headers.get("X-Raphal-Local-Token", ""), self.server.token)

    def _payload(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise WorkerError("Taille de requête invalide.", "INVALID_REQUEST", 400)
        if length <= 0 or length > 150 * 1024 * 1024:
            raise WorkerError("Requête locale vide ou trop volumineuse.", "INVALID_REQUEST", 413)
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise WorkerError("JSON local invalide.", "INVALID_REQUEST", 400) from error
        if not isinstance(value, dict):
            raise WorkerError("Requête locale invalide.", "INVALID_REQUEST", 400)
        return value

    def do_GET(self):
        if not self._authorized():
            return self._json(403, {"ok": False, "error": "Accès local refusé.", "code": "FORBIDDEN"})
        if self.path == "/health":
            return self._json(200, self.server.runtime.health())
        match = re.fullmatch(r"/jobs/(local-[a-f0-9]{32})", self.path)
        if match:
            try:
                return self._json(200, {"ok": True, "job": self.server.runtime.job(match.group(1))})
            except WorkerError as error:
                return self._json(error.status, {"ok": False, "error": str(error), "code": error.code})
        return self._json(404, {"ok": False, "error": "Route inconnue.", "code": "NOT_FOUND"})

    def do_POST(self):
        if not self._authorized():
            return self._json(403, {"ok": False, "error": "Accès local refusé.", "code": "FORBIDDEN"})
        try:
            payload = self._payload()
            if self.path == "/generate":
                return self._json(200, {"ok": True, **self.server.runtime.generate(payload)})
            match = re.fullmatch(r"/jobs/(local-[a-f0-9]{32})/cancel", self.path)
            if match:
                return self._json(200, {"ok": True, "job": self.server.runtime.cancel_job(match.group(1))})
            if self.path == "/prepare":
                return self._json(200, {"ok": True, **self.server.runtime.prepare(payload)})
            if self.path == "/unload":
                return self._json(200, self.server.runtime.unload())
            if self.path == "/shutdown":
                self._json(200, {"ok": True, "stopping": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            return self._json(404, {"ok": False, "error": "Route inconnue.", "code": "NOT_FOUND"})
        except WorkerError as error:
            return self._json(error.status, {"ok": False, "error": str(error), "code": error.code})
        except Exception:
            traceback.print_exc()
            return self._json(500, {"ok": False, "error": "Erreur interne du moteur IA local.", "code": "WORKER_ERROR"})


def secrets_compare(left, right):
    import hmac

    return bool(left and right and hmac.compare_digest(str(left), str(right)))


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=18080)
    serve.add_argument("--token-file", required=True)
    serve.add_argument("--root", required=True)
    serve.add_argument("--generated-root", required=True)
    serve.add_argument("--idle-timeout", type=int, default=900)
    download = subparsers.add_parser("download-model")
    download.add_argument("--root", required=True)
    download.add_argument("--component", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if args.command == "download-model":
        return download_model(root, args.component)
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("The local inference worker must bind to loopback only.")
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise SystemExit("Invalid worker token.")
    runtime = InferenceRuntime(root, Path(args.generated_root), args.idle_timeout)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.token = token
    server.runtime = runtime
    print(f"RAPHAL_LOCAL_AI_READY: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        runtime.unload()
        server.server_close()


if __name__ == "__main__":
    main()
