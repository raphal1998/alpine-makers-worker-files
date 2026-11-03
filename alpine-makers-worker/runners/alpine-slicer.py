# -*- coding: utf-8 -*-
"""Runner Worker du trancheur Alpine Model Engine (agent 1.54.0, docs/MODEL_ENGINE_ARCHITECTURE.md §8) : un tranchage
par job, dans le Python isolé du composant ``alpine-slicer`` (numpy + Pillow), jamais dans celui de l'agent. Modèle :
runners/alpine-tool.py (processus suivi par identité, annulation, reprise après coupure par .runner-state.json,
arrêt prouvé avant tout verdict).

Contrat du job ``model-studio`` : ``parameters`` porte les 25 clés du client (+ ``process_settings``), ``input_file``
{path, name} ou ``input_files`` [{path, name}, …] + ``materials`` (multi), ``multi``. L'agent fournit
ALPINE_SLICER_PYTHON. Le runner écrit ``slicer_job.json`` puis exécute ``<python> -m model_engine.cli slice
slicer_job.json <workdir>`` (cwd = dossier du Worker) et lit ``RAPHAL_SLICER_PROGRESS`` / ``RAPHAL_SLICER_RESULT`` /
``RAPHAL_SLICER_ERROR`` sur stdout. Artefact : ``worker_<execution_id>_sliced.gcode.3mf``. Résultat : dict du §8
(artifacts, output, bytes, exit_code 0, profile_mode « alpine », fallback_used false, engine_version, bridge_version,
bed_type, printer_profile, filament_profiles, plates, warnings). Action ``list_profiles`` : catalogue des machines.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

WORKER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER_DIR))
from job_state import RunnerLease, RunnerState, TERMINAL  # noqa: E402
from process_control import process_identity, terminate_owned_process  # noqa: E402
from safety import safe_relative_name  # noqa: E402

REQUEST_FILE = "slicer_job.json"
STDOUT_FILE, STDERR_FILE = "alpine-slicer.stdout", "alpine-slicer.stderr"
RESERVED = {REQUEST_FILE, "job.json", STDOUT_FILE, STDERR_FILE, ".runner-state.json", ".cancel.json"}
RESULT_MARKER = "RAPHAL_SLICER_RESULT="
PROGRESS_MARKER = "RAPHAL_SLICER_PROGRESS="
ERROR_MARKER = "RAPHAL_SLICER_ERROR="
MAX_INPUT_BYTES = 512 * 1024 * 1024
MAX_INPUT_FILES = 16
INPUT_SUFFIXES = {".stl", ".obj", ".3mf"}
TIMEOUT_SECONDS = 3600
LIST_TIMEOUT_SECONDS = 120
CANCEL_GRACE_SECONDS = 3
STOP_WAIT_SECONDS = 30
NOM = "Trancheur Alpine"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$", re.I)
_PARAM_SKIP = {"input_file", "input_files", "multi", "output_name", "worker_execution_id", "action"}


class SlicerRunnerError(ValueError):
    def __init__(self, message, code=""):
        super().__init__(message)
        self.code = code


def engine_environment():
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() not in {"PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV"}}
    environment["PYTHONPATH"] = str(WORKER_DIR)
    environment["PYTHONIOENCODING"] = "utf-8"
    # Le moteur parallélise par processus (model_engine.pipeline) ; numpy n'a pas besoin de fils BLAS.
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        environment[name] = "1"
    return environment


def _lignes(path):
    return path.read_text(encoding="utf-8", errors="replace").splitlines() if path.is_file() else []


def _marker(lines, marker):
    text = next((line[len(marker):] for line in reversed(lines) if line.startswith(marker)), "")
    try:
        value = json.loads(text) if text else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _progress(stdout_path):
    for line in reversed(_lignes(stdout_path)[-50:]):
        if line.startswith(PROGRESS_MARKER):
            fraction, _, message = line[len(PROGRESS_MARKER):].partition(" ")
            try:
                return max(0.0, min(1.0, float(fraction))), message
            except ValueError:
                return None
    return None


def _python():
    python = str(os.getenv("ALPINE_SLICER_PYTHON") or "").strip()
    if not python or not Path(python).is_file():
        raise SlicerRunnerError(f"{NOM} n’est pas installé sur ce Worker : installe-le depuis Mes Workers → Installations.",
                                "ENGINE_MISSING")
    if not (WORKER_DIR / "model_engine" / "cli.py").is_file():
        raise SlicerRunnerError(f"{NOM} manque au paquet du Worker : mets le Worker à jour.", "ENGINE_INCOMPLETE")
    return python


def _sources(parameters, workdir):
    inputs = parameters.get("input_files") if isinstance(parameters.get("input_files"), list) else []
    single = parameters.get("input_file") if isinstance(parameters.get("input_file"), dict) else None
    items = [item for item in inputs if isinstance(item, dict)] or ([single] if single else [])
    if not items:
        raise SlicerRunnerError("Aucun modèle transmis au trancheur Alpine.", "INPUT_MISSING")
    if len(items) > MAX_INPUT_FILES:
        raise SlicerRunnerError(f"Trop de fichiers à trancher ({len(items)} > {MAX_INPUT_FILES}).", "INPUT_INVALID")
    sources = []
    for item in items:
        source = Path(str(item.get("path") or "")).resolve()
        if not source.is_file() or workdir not in source.parents:
            raise SlicerRunnerError("Fichier de tranchage absent ou hors du dossier du job.", "INPUT_MISSING")
        size = source.stat().st_size
        if size == 0 or size > MAX_INPUT_BYTES:
            raise SlicerRunnerError("Fichier de tranchage vide ou trop volumineux (512 Mo au plus).", "INPUT_INVALID")
        if source.suffix.lower() not in INPUT_SUFFIXES:
            raise SlicerRunnerError("Format non pris en charge par le trancheur Alpine (STL, OBJ ou 3MF).", "INPUT_INVALID")
        if source.name in RESERVED:
            raise SlicerRunnerError("Nom du fichier d’entrée réservé.", "INPUT_INVALID")
        sources.append(str(source))
    return sources


def _list_profiles(state):
    """Catalogue des machines (lecture seule, court) : ``{kind: "printer_profiles", machines, engine_version}``."""
    python = _python()
    completed = subprocess.run([python, "-m", "model_engine.cli", "list_profiles"], cwd=str(WORKER_DIR), env=engine_environment(),
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=LIST_TIMEOUT_SECONDS,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    reported = _marker(completed.stdout.splitlines(), RESULT_MARKER)
    if completed.returncode != 0 or reported.get("kind") != "printer_profiles":
        error = _marker(completed.stdout.splitlines(), ERROR_MARKER)
        raise SlicerRunnerError(str(error.get("message") or completed.stderr[-600:] or f"{NOM} : catalogue indisponible."),
                                str(error.get("code") or "ENGINE_FAILED"))
    machines = []
    for entry in reported.get("machines") or []:
        if isinstance(entry, list) and len(entry) == 4 and all(isinstance(v, str) for v in entry):
            machines.append([v[:120] for v in entry])
    result = {"kind": "printer_profiles", "artifacts": [], "machines": machines,
              "engine_version": str(reported.get("engine_version") or "")[:20],
              "bridge_version": str(reported.get("engine_version") or "")[:20]}
    state.finish("completed", result=result)
    return result


def run_job(job, workdir):
    workdir = Path(workdir).resolve()
    state = RunnerState(workdir, job)
    try:
        return _run(job, workdir, state)
    except Exception as error:
        if state.value["state"] not in TERMINAL:
            if state.value.get("engine_stop_confirmed"):
                state.finish("failed", error=str(error))
                code = getattr(error, "code", "")
                if code:
                    state.update(error_code=str(code))
            else:
                state.uncertain(error)
        raise


def _stop(process, pid, identity):
    """Arrêt prouvé du processus du moteur (celui que ce runner a lancé, ou son identité relue après coupure)."""
    if process is not None:
        process.terminate()
        try:
            process.wait(STOP_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(STOP_WAIT_SECONDS)
        return True
    if not terminate_owned_process(pid, identity):
        return False
    deadline = time.time() + STOP_WAIT_SECONDS
    while time.time() < deadline and process_identity(pid) == identity:
        time.sleep(0.2)
    return process_identity(pid) != identity


def _run(job, workdir, state):
    if state.value["state"] == "completed":
        return state.value["result"]
    if state.value["state"] in {"failed", "cancelled"}:
        raise ValueError(state.value.get("error") or "Exécution déjà terminée.")
    parameters = job.get("parameters") if isinstance(job.get("parameters"), dict) else {}
    if parameters.get("action") == "list_profiles":
        return _list_profiles(state)
    execution_id = state.value["execution_id"]
    expected = safe_relative_name(f"worker_{execution_id}_sliced.gcode.3mf", basename=True)
    stdout_path, stderr_path = workdir / STDOUT_FILE, workdir / STDERR_FILE
    process = None
    if state.value["state"] == "preparing":
        python = _python()
        sources = _sources(parameters, workdir)
        if state.cancellation_requested():
            state.finish("cancelled", error=f"{NOM} : tranchage annulé avant lancement.")
            raise ValueError(f"{NOM} : tranchage annulé.")
        (workdir / expected).unlink(missing_ok=True)
        payload = {key: value for key, value in parameters.items() if key not in _PARAM_SKIP}
        payload["multi"] = bool(parameters.get("multi")) or len(sources) > 1
        request = {"parameters": payload, "inputs": sources, "execution_id": execution_id}
        request_path = workdir / REQUEST_FILE
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        state.submit_intent(engine="alpine-slicer", op="slice")
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
            try:
                process = subprocess.Popen([python, "-m", "model_engine.cli", "slice", str(request_path), str(workdir)],
                                           cwd=str(WORKER_DIR), env=engine_environment(), stdout=stdout, stderr=stderr,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            except OSError as error:
                state.finish("failed", error=f"Lancement de {NOM} impossible : {error}")
                raise
        identity = process_identity(process.pid)
        state.update(state="running", pid=process.pid, process_identity=identity, progress=5,
                     message=f"{NOM} : tranchage en cours sur le Worker…")
    pid, identity = state.value.get("pid"), state.value.get("process_identity")
    if not pid:
        raise ValueError(f"Lancement de {NOM} incertain ; aucun nouveau tranchage ne sera lancé.")
    deadline = time.time() + TIMEOUT_SECONDS
    cancel_at = 0.0
    dernier = None
    timed_out = False
    while process.poll() is None if process is not None else process_identity(pid) == identity and identity is not None:
        if state.cancellation_requested():
            if not cancel_at:
                cancel_at = time.time()
                state.update(state="cancel_requested")
            if time.time() - cancel_at < CANCEL_GRACE_SECONDS:
                time.sleep(0.2)
                continue
            if not _stop(process, pid, identity):
                raise ValueError(f"Impossible de confirmer l’identité du processus de {NOM} ; aucun autre processus arrêté.")
            break
        if time.time() > deadline:
            if not _stop(process, pid, identity):
                raise ValueError(f"Délai de {NOM} dépassé et arrêt non confirmé ; état du processus à réconcilier.")
            timed_out = True
            break
        avance = _progress(stdout_path)
        if avance and avance != dernier:
            dernier = avance
            state.update(progress=int(5 + 90 * avance[0]), message=f"{NOM} : {avance[1] or 'tranchage en cours'}"[:200])
        time.sleep(0.3)
    if process is not None:
        process.wait()
    elif identity is None:
        raise ValueError(f"Identité du processus de {NOM} inconnue ; arrêt non confirmé.")
    state.update(engine_stop_confirmed=True)
    if state.cancellation_requested():
        state.finish("cancelled", error=f"Arrêt de {NOM} confirmé.")
        raise ValueError(f"{NOM} : tranchage annulé.")
    if timed_out:
        raise SlicerRunnerError(f"{NOM} : délai de tranchage dépassé ({TIMEOUT_SECONDS // 60} min), processus arrêté.", "TIMEOUT")
    lines = _lignes(stdout_path)
    reported = _marker(lines, RESULT_MARKER)
    if not reported or reported.get("exit_code") != 0:
        error = _marker(lines, ERROR_MARKER)
        tail = " ".join(_lignes(stderr_path))[-600:]
        raise SlicerRunnerError(str(error.get("message") or tail or f"{NOM} : échec du tranchage."),
                                str(error.get("code") or "ENGINE_FAILED"))
    try:
        filename = safe_relative_name(str(reported.get("output") or ""), basename=True)
    except ValueError:
        raise SlicerRunnerError(f"{NOM} : nom d’artefact invalide.", "RESULT_MISSING") from None
    if filename != expected:
        raise SlicerRunnerError(f"L’artefact de {NOM} appartient à une autre exécution.", "RESULT_MISSING")
    artifact = workdir / filename
    if not artifact.is_file() or artifact.resolve().parent != workdir:
        raise SlicerRunnerError(f"{NOM} n’a produit aucun fichier pour ce job.", "RESULT_MISSING")
    plates = []
    for plate in reported.get("plates") or []:
        if isinstance(plate, dict):
            plates.append({"index": int(plate.get("index") or 1), "prediction_s": float(plate.get("prediction_s") or 0),
                           "weight_g": float(plate.get("weight_g") or 0), "layers": int(plate.get("layers") or 0),
                           "filaments": [f for f in (plate.get("filaments") or []) if isinstance(f, dict)][:16]})
    engine_version = str(reported.get("engine_version") or "")[:20]
    result = {"artifacts": [artifact.name], "output": artifact.name, "bytes": artifact.stat().st_size, "exit_code": 0,
              "profile_mode": str(reported.get("profile_mode") or "alpine")[:20], "fallback_used": False,
              "engine_version": engine_version, "bridge_version": str(reported.get("bridge_version") or engine_version)[:20],
              # Ce que le site affiche avant l'impression : plaque et profils filament réellement utilisés.
              "bed_type": str(reported.get("bed_type") or "")[:40],
              "printer_profile": str(reported.get("printer_profile") or "")[:120],
              "filament_profiles": [str(name)[:120] for name in (reported.get("filament_profiles") or [])
                                    if isinstance(name, str)][:16],
              "plates": plates[:64],
              "warnings": [str(w)[:300] for w in (reported.get("warnings") or []) if isinstance(w, str)][:50]}
    state.update(progress=100, message=f"{NOM} : terminé.")
    state.finish("completed", result=result)
    return result


if __name__ == "__main__":
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    with RunnerLease(Path(sys.argv[2])):
        print(json.dumps(run_job(job, Path(sys.argv[2])), ensure_ascii=False))
