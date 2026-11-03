"""Ligne de commande du trancheur Alpine Model Engine (docs/MODEL_ENGINE_ARCHITECTURE.md §8), lancée par
``runners/alpine-slicer.py`` dans le Python isolé du composant ``alpine-slicer`` :

    python -m model_engine.cli slice <job.json> <workdir>   job.json = {"parameters": {...}, "inputs": [chemins],
                                                            "execution_id": "..."} ; écrit sur stdout
                                                            ``RAPHAL_SLICER_PROGRESS=<fraction> <message>`` puis
                                                            ``RAPHAL_SLICER_RESULT=<json>`` (code 0) ou
                                                            ``RAPHAL_SLICER_ERROR={"code", "message"}`` (code 1)
    python -m model_engine.cli list_profiles                ``{kind: "printer_profiles", machines, engine_version}``
    python -m model_engine.cli probe                        ``{ok: true, engine_version, numpy, pillow}``

Codes d'erreur stables : MESH_INVALID, OUT_OF_BED, PRINTER_UNSUPPORTED, MATERIAL_UNKNOWN, SETTINGS_INVALID,
EMPTY_SLICE, TIMEOUT (posé par le runner), ENGINE_FAILED (imprévu). Tout le texte est en UTF-8.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PROGRESS_MARKER = "RAPHAL_SLICER_PROGRESS="
RESULT_MARKER = "RAPHAL_SLICER_RESULT="
ERROR_MARKER = "RAPHAL_SLICER_ERROR="
MAX_JOB_BYTES = 4 * 1024 * 1024


def _out(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _error(code: str, message: str) -> int:
    _out(ERROR_MARKER + json.dumps({"code": code, "message": str(message)[:2000]}, ensure_ascii=False))
    return 1


def probe() -> dict:
    """Sonde du composant : versions du moteur, de numpy et de Pillow (importés pour de vrai)."""
    import numpy
    import PIL

    from . import VERSION
    return {"ok": True, "engine_version": VERSION, "numpy": numpy.__version__, "pillow": PIL.__version__}


def list_profiles() -> dict:
    from . import VERSION
    from .machine import bambu
    return {"kind": "printer_profiles", "machines": bambu.catalog(), "engine_version": VERSION,
            "unsupported": bambu.unsupported_catalog()}


def _read_job(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(f"job.json introuvable : {path}")
    if path.stat().st_size > MAX_JOB_BYTES:
        raise ValueError("job.json trop volumineux.")
    job = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(job, dict):
        raise ValueError("job.json : objet JSON attendu.")
    return job


def run_slice(job_path, workdir) -> int:
    from . import pipeline

    try:
        job = _read_job(Path(job_path))
    except (OSError, ValueError) as error:
        return _error("SETTINGS_INVALID", f"Demande illisible : {error}")
    parameters = job.get("parameters") if isinstance(job.get("parameters"), dict) else {}
    inputs = job.get("inputs")
    if isinstance(inputs, str):
        inputs = [inputs]
    if not isinstance(inputs, list):
        inputs = []
    execution_id = str(job.get("execution_id") or "")
    workdir = Path(workdir)
    try:
        workdir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return _error("SETTINGS_INVALID", f"Dossier de travail inaccessible : {error}")
    last = [0.0]

    def progress(fraction, message):
        now = time.monotonic()
        if now - last[0] >= 0.2 or fraction >= 1.0:
            last[0] = now
            _out(f"{PROGRESS_MARKER}{max(0.0, min(1.0, float(fraction))):.4f} {str(message).replace(chr(10), ' ')}")

    try:
        result = pipeline.slice_job(parameters, [Path(p) for p in inputs], workdir, execution_id=execution_id, progress=progress)
    except pipeline.PipelineError as error:
        return _error(error.code, error.message)
    except MemoryError:
        return _error("ENGINE_FAILED", "Mémoire insuffisante pour ce tranchage (modèle trop dense ou plafond du Worker).")
    except KeyboardInterrupt:
        return _error("ENGINE_FAILED", "Tranchage interrompu.")
    except Exception as error:   # imprévu : code stable, message en clair, jamais une trace sur stdout
        return _error("ENGINE_FAILED", f"{type(error).__name__}: {error}")
    _out(RESULT_MARKER + json.dumps(result, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    if not argv:
        _out("usage : python -m model_engine.cli slice <job.json> <workdir> | list_profiles | probe")
        return 2
    action = argv[0]
    if action == "probe":
        try:
            _out(RESULT_MARKER + json.dumps(probe()))
        except Exception as error:
            return _error("ENGINE_FAILED", f"sonde : {error}")
        return 0
    if action == "list_profiles":
        try:
            _out(RESULT_MARKER + json.dumps(list_profiles(), ensure_ascii=False))
        except Exception as error:
            return _error("ENGINE_FAILED", f"profils : {error}")
        return 0
    if action == "slice":
        if len(argv) != 3:
            return _error("SETTINGS_INVALID", "usage : slice <job.json> <workdir>")
        return run_slice(argv[1], argv[2])
    return _error("SETTINGS_INVALID", f"action inconnue : {action}")


if __name__ == "__main__":
    sys.exit(main())
