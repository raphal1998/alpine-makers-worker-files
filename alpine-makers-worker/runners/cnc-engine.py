# -*- coding: utf-8 -*-
"""Runner Worker d'Alpine CNC Studio : une analyse ou simulation de programme par job, calculée par le moteur
CNC du composant « cnc-engine » (Python isolé), jamais par le PC qui héberge le site.

Modèle : runners/laser-engine.py — un processus par job (pas de service résident), arrêt vérifié par identité
de processus, reprise après coupure via .runner-state.json. Le job porte ``parameters.op`` (analyze, simulate),
``parameters.machine`` (profil machine du studio : work_area_mm, max_feed_mm_min, spindle_max_rpm, safe_z_mm,
controller), ``parameters.output`` (result.json) et ``parameters.input_file.path`` : le programme G-code
téléchargé par l'agent dans le dossier du job.

Lancement par l'agent : ``python cnc-engine.py <job.json> <workdir>`` avec CNC_ENGINE_PYTHON = python du .venv
du composant. Le runner écrit ``cnc_request.json`` puis exécute ``<CNC_ENGINE_PYTHON> -m cnc_engine.cli
<cnc_request.json> <result.json>`` (cwd = dossier du Worker). Résultat : {"op", "artifacts": ["result.json",
"preview.json"], "summary": {"bytes", "warnings", "errors", "program_ok"}} ; une CncEngineError du moteur →
état failed avec son message (et error_code). Aucune commande n'est jamais envoyée à une machine.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

WORKER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER_DIR))
from safety import safe_relative_name  # noqa: E402
from job_state import RunnerLease, RunnerState, TERMINAL  # noqa: E402
from process_control import process_identity, terminate_owned_process  # noqa: E402

ENGINE = "cnc-engine"
OPERATIONS = ("analyze", "simulate")
ENGINE_FILES = ("cnc_engine/__init__.py", "cnc_engine/analyze.py", "cnc_engine/cli.py", "cam_gcode_check.py")
REQUEST_FILE = "cnc_request.json"
PREVIEW_ARTIFACT = "preview.json"
DEFAULT_OUTPUT = "result.json"
GCODE_SUFFIXES = (".nc", ".gcode", ".gc", ".ngc", ".tap", ".cnc", ".txt")
MAX_GCODE_BYTES = 64 * 1024 * 1024
RESULT_MARKER = "RAPHAL_CNC_RESULT="
# Un programme de plusieurs millions de lignes se relit en dizaines de secondes ; au-delà, état à réconcilier.
TIMEOUT_SECONDS = 600
CANCEL_GRACE_SECONDS = 3
MISSING_ENGINE = ("Le moteur CNC n’est pas installé sur ce Worker. Installe Alpine CNC Studio depuis "
                  "Mes Workers → Installations, puis relance.")


class CncRunnerError(ValueError):
    """Échec définitif du calcul (requête, moteur ou opération), avec un code lisible par le site."""

    def __init__(self, message, code=""):
        super().__init__(message)
        self.code = code


def engine_python():
    value = str(os.getenv("CNC_ENGINE_PYTHON") or "").strip()
    if not value or not Path(value).is_file():
        raise CncRunnerError(MISSING_ENGINE, "ENGINE_MISSING")
    return value


def engine_environment():
    """Le moteur ne voit que le dossier du Worker : ni PYTHONPATH hérité, ni Python du site."""
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() not in {"PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV"}}
    environment["PYTHONPATH"] = str(WORKER_DIR)
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def _reported(stdout_path):
    """Compte rendu ``RAPHAL_CNC_RESULT=<json>`` du moteur (dernière occurrence), sinon {}."""
    if not stdout_path.is_file():
        return {}
    lines = stdout_path.read_text(encoding="utf-8", errors="replace").splitlines()
    reported = next((line[len(RESULT_MARKER):] for line in reversed(lines) if line.startswith(RESULT_MARKER)), "")
    try:
        value = json.loads(reported) if reported else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _stderr_tail(stderr_path):
    if not stderr_path.is_file():
        return ""
    return " ".join(stderr_path.read_text(encoding="utf-8", errors="replace").split())[-600:]


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


def _request(op, source, machine, workdir):
    """Requête du moteur écrite dans le dossier du job : opération, programme, profil machine (rien d'autre)."""
    if machine is not None and not isinstance(machine, dict):
        raise CncRunnerError("Profil machine CNC invalide (objet attendu).", "MACHINE_INVALID")
    request = {"op": op, "gcode_path": str(source), "machine": machine or {}}
    path = workdir / REQUEST_FILE
    path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    return path


def _run(job, workdir, state):
    if state.value["state"] == "completed":
        return state.value["result"]
    if state.value["state"] in {"failed", "cancelled"}:
        raise ValueError(state.value.get("error") or "Exécution déjà terminée.")
    parameters = job.get("parameters") if isinstance(job.get("parameters"), dict) else {}
    # Un runner ne fait jamais confiance au payload : opération, fichiers et noms sont revérifiés ici.
    op = str(parameters.get("op") or "").strip().lower()
    if op not in OPERATIONS:
        raise CncRunnerError(f"Opération CNC inconnue « {op or '?'} » (attendu : {', '.join(OPERATIONS)}).", "UNKNOWN_OPERATION")
    source_info = parameters.get("input_file") if isinstance(parameters.get("input_file"), dict) else {}
    source = Path(str(source_info.get("path") or "")).resolve()
    if not source.is_file() or workdir not in source.parents:
        raise CncRunnerError("Programme G-code absent ou hors du dossier du job.", "GCODE_MISSING")
    if source.suffix.lower() not in GCODE_SUFFIXES:
        raise CncRunnerError("Programme CNC : fichier .nc, .gcode, .ngc, .tap ou .txt attendu.", "GCODE_INVALID")
    if source.stat().st_size > MAX_GCODE_BYTES:
        raise CncRunnerError("Programme G-code trop volumineux (64 Mo maximum).", "GCODE_TOO_LARGE")
    if source.stat().st_size == 0:
        raise CncRunnerError("Programme G-code vide.", "GCODE_EMPTY")
    try:
        output_name = safe_relative_name(str(parameters.get("output") or DEFAULT_OUTPUT), basename=True)
    except ValueError as error:
        raise CncRunnerError(str(error), "OUTPUT_INVALID") from None
    if not output_name.lower().endswith(".json"):
        output_name += ".json"
    destination = workdir / output_name
    if destination.resolve().parent != workdir or destination.name in {source.name, REQUEST_FILE, PREVIEW_ARTIFACT, "job.json"}:
        raise CncRunnerError("Nom de résultat CNC non autorisé.", "OUTPUT_INVALID")
    preview_path = workdir / PREVIEW_ARTIFACT
    stdout_path, stderr_path = workdir / "cnc-engine.stdout", workdir / "cnc-engine.stderr"
    process = None
    if state.value["state"] == "preparing":
        python = engine_python()
        if not all((WORKER_DIR / name).is_file() for name in ENGINE_FILES):
            raise CncRunnerError("Le moteur CNC du Worker est incomplet : mets à jour le Worker puis relance.", "ENGINE_INCOMPLETE")
        if state.cancellation_requested():
            state.finish("cancelled", error="Analyse CNC annulée avant lancement.")
            raise ValueError("Analyse CNC annulée.")
        request_path = _request(op, source, parameters.get("machine"), workdir)
        state.submit_intent(engine=ENGINE, op=op)
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
            try:
                process = subprocess.Popen(
                    [python, "-m", "cnc_engine.cli", str(request_path), str(destination)],
                    cwd=str(WORKER_DIR), env=engine_environment(), stdout=stdout, stderr=stderr,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            except OSError as error:
                state.finish("failed", error=f"Lancement du moteur CNC impossible : {error}")
                raise
        identity = process_identity(process.pid)
        state.update(state="running", pid=process.pid, process_identity=identity, progress=10,
                     message=f"Analyse CNC « {op} » en cours sur le Worker…")
    pid, identity = state.value.get("pid"), state.value.get("process_identity")
    if not pid:
        raise ValueError("Lancement du moteur CNC incertain ; aucun nouveau calcul ne sera lancé.")
    deadline = time.time() + TIMEOUT_SECONDS
    cancel_at = 0.0
    while process.poll() is None if process is not None else process_identity(pid) == identity and identity is not None:
        if state.cancellation_requested():
            if not cancel_at:
                cancel_at = time.time()
                state.update(state="cancel_requested")
            if time.time() - cancel_at < CANCEL_GRACE_SECONDS:
                time.sleep(0.2)
                continue
            if process is not None:
                process.terminate()
            elif not terminate_owned_process(pid, identity):
                raise ValueError("Impossible de confirmer l’identité du processus du moteur CNC ; aucun autre processus arrêté.")
        if time.time() > deadline:
            raise ValueError("Délai de l’analyse CNC dépassé ; état du processus à réconcilier.")
        time.sleep(0.2)
    if process is not None:
        process.wait()
    elif identity is None:
        raise ValueError("Identité du processus du moteur CNC inconnue ; arrêt non confirmé.")
    state.update(engine_stop_confirmed=True)
    if state.cancellation_requested():
        state.finish("cancelled", error="Arrêt du moteur CNC confirmé.")
        raise ValueError("Analyse CNC annulée.")
    details = _reported(stdout_path)
    if (process is not None and process.returncode) or not details.get("ok") or not destination.is_file():
        message = str(details.get("error") or "") or _stderr_tail(stderr_path) or "Moteur CNC : échec ou résultat incomplet."
        raise CncRunnerError(message, str(details.get("code") or "ENGINE_FAILED"))
    artifacts = [destination.name]
    if preview_path.is_file():
        artifacts.append(PREVIEW_ARTIFACT)
    result = {"op": op, "artifacts": artifacts,
              "summary": {"bytes": destination.stat().st_size, "warnings": int(details.get("warnings") or 0),
                          "errors": int(details.get("errors") or 0), "program_ok": bool(details.get("program_ok"))}}
    state.update(progress=100, message="Analyse CNC terminée ; résultat écrit.")
    state.finish("completed", result=result)
    return result


if __name__ == "__main__":
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    with RunnerLease(Path(sys.argv[2])):
        print(json.dumps(run_job(job, Path(sys.argv[2])), ensure_ascii=False))
