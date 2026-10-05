"""Worker storage policy. Adoption only: never move, copy or delete user resources.

Engine-relative models/venvs and durable job paths deliberately stay compatible.
Only child-process cache/temp settings change; never modify the system environment.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

if __package__:
    from .job_state import atomic_json
    from .safety import is_link
    from . import local_sandbox
else:
    from job_state import atomic_json
    from safety import is_link
    import local_sandbox


def ascii_path(path):
    """Windows 8.3 form of a path when it contains non-ASCII characters (engines with narrow C paths)."""
    text = str(path)
    if os.name != "nt" or text.isascii():
        return text
    import ctypes
    buffer = ctypes.create_unicode_buffer(len(text) + 260)
    if ctypes.windll.kernel32.GetShortPathNameW(text, buffer, len(buffer)) and buffer.value.isascii():
        return buffer.value
    return text


LAYOUT = {
    "engines": "components", "modules": "components", "jobs": "jobs",
    "3d-models": "workspace", "assets": "outputs", "data": "data",
    "cache": "cache", "temp": "temp",
}
WORKER_COMPONENTS = (
    "comfyui", "hunyuan3d", "freecad", "orca", "printguard", "cuda-toolkit",
)
CACHE_DEFAULTS = {
    "HF_HOME": "cache/huggingface", "TORCH_HOME": "cache/torch",
    "PIP_CACHE_DIR": "cache/pip", "XDG_CACHE_HOME": "cache/xdg",
    "TORCH_EXTENSIONS_DIR": "cache/torch-extensions",
    "CUDA_CACHE_PATH": "cache/cuda", "NUMBA_CACHE_DIR": "cache/numba",
    "TRITON_CACHE_DIR": "cache/triton", "MPLCONFIGDIR": "cache/matplotlib",
}
PATH_VARIABLES = set(CACHE_DEFAULTS) | {
    "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_ASSETS_CACHE", "HF_XET_CACHE",
    "TRANSFORMERS_CACHE", "DIFFUSERS_CACHE", "SENTENCE_TRANSFORMERS_HOME",
    "TMP", "TEMP", "TMPDIR",
}
MANAGED_PORTABLE_COMPONENTS = frozenset({"orca", "printguard"})


class WorkerStorage:
    def __init__(self, root, config=None):
        self.root = Path(root).resolve()
        local_sandbox.activate_from_config(self.root)
        local_sandbox.validate_root(self.root)
        self.config = config
        self.file = self.root / "storage.json"

    def path(self, category):
        return self.root / LAYOUT[category]

    def resolve(self, value):
        path = Path(os.path.expandvars(os.path.expanduser(str(value))))
        return (path if path.is_absolute() else self.root / path).resolve()

    def component(self, name, config=None):
        if local_sandbox.enabled():
            if not isinstance(name, str) or not name or any(c in name for c in "/\\:") or name in {".", ".."}:
                raise ValueError("Nom de composant TEST invalide.")
            target = self.path("engines") / name
            if is_link(target) or not target.resolve().is_relative_to(self.root):
                raise ValueError("Composant TEST hors de son installation isolée.")
            return target
        if config is None:
            config = self.config
        if config is None:
            config_file = self.root / "config.json"
            config = json.loads(config_file.read_text(encoding="utf-8-sig")) if config_file.exists() else {}
        paths = config.get("component_paths") or {}
        variable = {"comfyui": "COMFYUI_ROOT", "hunyuan3d": "HUNYUAN3D_ROOT"}.get(name)
        value = paths.get(name) or (os.environ.get(variable) if variable else None)
        return self.resolve(value) if value else self.path("engines") / name

    def component_ownership(self, name, executable=None):
        """Separate the selected runtime from the controlled install destination.

        Legacy configurations can name an executable, including one that no
        longer exists. Its presence in config does not make a fresh portable
        installation under Worker/components an external filesystem mutation.
        """
        if not isinstance(name, str) or not name or any(char in name for char in "/\\:") or Path(name).name != name or name in {".", ".."}:
            raise ValueError("Nom de composant invalide.")
        selected = self.component(name)
        engine_root = self.resolve(executable).parent if executable else selected
        if not executable and (selected.is_file() or selected.suffix.lower() == ".exe"):
            engine_root = selected.parent
        managed_root = self.path("engines") / name
        safe_target = (
            not is_link(self.path("engines")) and not is_link(managed_root)
            and managed_root.resolve().parent == self.path("engines")
        )
        # Compare containment, not equality: portable packages may put their
        # executable in a subdirectory of the Worker-owned component.
        managed = safe_target and engine_root.resolve().is_relative_to(managed_root)
        return {
            "engine_root": str(engine_root.resolve()),
            "managed_root": str(managed_root),
            "configured_path": str(selected),
            "managed": bool(managed),
            "managed_install_available": name in MANAGED_PORTABLE_COMPONENTS and safe_target,
        }

    def activate_managed_component(self, name, expected_configured_path):
        """Select a verified portable copy after installation, preserving settings.

        Never alter the external application or overwrite a path preference
        changed locally while the installer was running.
        """
        if name not in MANAGED_PORTABLE_COMPONENTS:
            raise ValueError("Ce composant ne propose pas d’installation portable gérée.")
        config_file = self.root / "config.json"
        if is_link(config_file):
            raise ValueError("La configuration Worker est un lien ; aucun chemin modifié.")
        configuration = json.loads(config_file.read_text(encoding="utf-8-sig"))
        if not isinstance(configuration, dict) or not isinstance(configuration.get("component_paths") or {}, dict):
            raise ValueError("Configuration Worker invalide ; aucun chemin modifié.")
        current = WorkerStorage(self.root, configuration)
        ownership = current.component_ownership(name)
        if current.component(name) != self.resolve(expected_configured_path):
            raise ValueError("Le chemin du moteur a changé pendant l’installation ; son activation automatique est annulée.")
        if not ownership["managed_install_available"]:
            raise ValueError("Le dossier géré du composant est un lien ou sort de l’espace Worker.")
        if __package__:
            from .installers.component_installer import component_ready
        else:
            from installers.component_installer import component_ready
        managed_root = Path(ownership["managed_root"])
        if not component_ready(name, managed_root):
            raise ValueError("La nouvelle installation du moteur est incomplète ; le chemin précédent est conservé.")
        # Config is read again after the long download, so other settings and
        # credentials retain their current values. Only this path is changed.
        configuration["component_paths"] = {**(configuration.get("component_paths") or {}), name: str(managed_root)}
        atomic_json(config_file, configuration)
        self.config = configuration
        return str(managed_root)

    def _relative(self, path):
        path = self.resolve(path)
        return path.relative_to(self.root).as_posix() if path.is_relative_to(self.root) else str(path)

    @staticmethod
    def _populated(path):
        # No recursive scan/hash of multi-GB model collections.
        return path.is_dir() and next(path.iterdir(), None) is not None

    def settings(self):
        if local_sandbox.enabled():
            # Never adopt global caches or legacy storage.json in the test copy.
            return {"version": 1, "environment": dict(CACHE_DEFAULTS), "adopted": [],
                    "migration": "isolated local sandbox; no external resources adopted"}
        if self.file.exists():
            value = json.loads(self.file.read_text(encoding="utf-8-sig"))
            if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("environment"), dict):
                raise ValueError("storage.json invalide : aucun chemin n’a été remplacé.")
            unknown = set(value["environment"]) - PATH_VARIABLES
            if unknown or any(not isinstance(v, str) or not v.strip() for v in value["environment"].values()):
                raise ValueError("storage.json : variable ou chemin de stockage invalide.")
            # Old installations automatically reused the user's global pip
            # cache. A writable cache root does not guarantee that existing
            # cached wheels are readable by the Worker account. Unlike model
            # caches, this is disposable installer state, not runtime data.
            # Migrate only our own unchanged automatic choice, never a manual
            # override or an explicit environment setting. Keep all old files.
            pip_cache = value["environment"].get("PIP_CACHE_DIR")
            adoptions = value.get("adopted")
            for adopted in adoptions if isinstance(adoptions, list) else []:
                if (isinstance(adopted, dict) and adopted.get("variable") == "PIP_CACHE_DIR"
                        and adopted.get("reason") == "existing-cache" and pip_cache
                        and isinstance(adopted.get("path"), str)
                        and self.resolve(pip_cache) == self.resolve(adopted["path"])
                        and not self.resolve(pip_cache).is_relative_to(self.root)):
                    value["environment"]["PIP_CACHE_DIR"] = CACHE_DEFAULTS["PIP_CACHE_DIR"]
                    value["pip_cache_migration"] = {
                        "previous_path": pip_cache,
                        "reason": "automatic global cache isolated; original files retained",
                    }
                    break
            return value
        environment = dict(CACHE_DEFAULTS)
        adopted = []
        # Preserve explicit cache overrides and existing caches. Repointing an
        # existing model cache silently would trigger multi-GB duplicate downloads.
        legacy = {
            "HF_HOME": Path.home() / ".cache/huggingface",
            "TORCH_HOME": Path.home() / ".cache/torch",
            "TRITON_CACHE_DIR": Path.home() / ".triton/cache",
        }
        engine_cache = self.component("hunyuan3d") / ".cache/huggingface"
        if self._populated(engine_cache):
            legacy["HF_HOME"] = engine_cache
        for key in PATH_VARIABLES - {"TMP", "TEMP", "TMPDIR"}:
            explicit = os.environ.get(key)
            old = legacy.get(key)
            if explicit or (old is not None and self._populated(old)):
                selected = self.resolve(explicit or old)
                environment[key] = self._relative(selected)
                adopted.append({"variable": key, "path": self._relative(selected),
                                "reason": "environment" if explicit else "existing-cache"})
        return {"version": 1, "environment": environment, "adopted": adopted,
                "migration": "in-place; no resources moved or deleted"}

    def initialize(self):
        settings = self.settings()  # Invalid config must fail before any rewrite.
        self.root.mkdir(parents=True, exist_ok=True)
        # This is the complete, empty Worker-owned layout. Empty component
        # directories are deliberately not installation evidence: readiness is
        # checked from executable, virtualenv and completion-marker files.
        # Creating the layout is therefore safe on a new Worker and does not
        # download, move or adopt an AI engine/model.
        for name in ("components", "cache", "workspace", "outputs", "data", "jobs", "temp"):
            if local_sandbox.enabled() and (is_link(self.root / name) or not (self.root / name).resolve().is_relative_to(self.root)):
                raise ValueError("Dossier TEST lié ou hors de l’installation isolée.")
            (self.root / name).mkdir(parents=True, exist_ok=True)
        for component in WORKER_COMPONENTS:
            (self.path("engines") / component).mkdir(parents=True, exist_ok=True)
        if not self.file.exists() or json.loads(self.file.read_text(encoding="utf-8-sig")) != settings:
            atomic_json(self.file, settings)
        return settings

    def environment(self, base=None):
        settings = self.settings()
        env = local_sandbox.environment(self.root, os.environ if base is None else base)
        values = dict(CACHE_DEFAULTS)
        values.update(settings["environment"])
        hf = self.resolve(values["HF_HOME"])
        hub = values.get("HF_HUB_CACHE") or values.get("HUGGINGFACE_HUB_CACHE") or str(hf / "hub")
        values.setdefault("HF_HUB_CACHE", hub)
        values.setdefault("HUGGINGFACE_HUB_CACHE", hub)
        values.setdefault("HF_ASSETS_CACHE", str(hf / "assets"))
        values.setdefault("HF_XET_CACHE", str(hf / "xet"))
        # Older Transformers honors this independently from HF_HOME.
        values.setdefault("TRANSFORMERS_CACHE", hub)
        values.setdefault("DIFFUSERS_CACHE", hub)
        values.setdefault("SENTENCE_TRANSFORMERS_HOME", hub)
        for key in ("TMP", "TEMP", "TMPDIR"):
            values.setdefault(key, "temp")
        for key, value in values.items():
            path = self.resolve(value)
            if local_sandbox.enabled() and (not path.is_relative_to(self.root) or is_link(path)):
                raise ValueError("Cache TEST hors de l’installation isolée.")
            path.mkdir(parents=True, exist_ok=True)
            # Raise the real permission/disk error instead of silently falling
            # back to a global temp or model directory.
            with tempfile.TemporaryFile(dir=path):
                pass
            # Native engine libraries (pymeshlab, LiteRT) open temp files with
            # narrow C paths: a non-ASCII Worker folder must reach them as 8.3.
            env[key] = ascii_path(path) if key in ("TMP", "TEMP", "TMPDIR") else str(path)
        env["ALPINE_WORKER_ROOT"] = str(self.root)
        return env

    def report(self):
        settings = self.settings()
        return {"root": str(self.root), "layout": LAYOUT, "storage": settings,
                "external": {k: str(self.resolve(v)) for k, v in settings["environment"].items()
                             if not self.resolve(v).is_relative_to(self.root)}}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Stockage Worker : inventaire sans déplacement de fichiers")
    parser.add_argument("--root", required=True)
    parser.add_argument("--initialize", action="store_true")
    args = parser.parse_args()
    storage = WorkerStorage(args.root)
    if args.initialize:
        storage.initialize()
    print(json.dumps(storage.report(), ensure_ascii=True, indent=2))
