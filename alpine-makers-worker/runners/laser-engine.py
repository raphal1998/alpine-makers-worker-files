# -*- coding: utf-8 -*-
"""Runner Worker d'Alpine Laser Studio : une opération de parcours par job, calculée par le moteur laser
du composant « laser-engine » (Python isolé + Pillow), jamais par le PC qui héberge le site.

Modèle : runners/freecad.py — un processus par job (pas de service résident), arrêt vérifié par identité
de processus, reprise après coupure via .runner-state.json. Le job porte ``parameters.op`` (parse, plan,
gcode, frame, simulate, raster ; script, assistant, assistant_status pour l'assistant de code),
``parameters.output`` (result.json) et ``parameters.input_file.path`` :
le fichier request.json téléchargé par l'agent, exactement le corps des anciennes routes /api/laser/<op>.

Lancement par l'agent : ``python laser-engine.py <job.json> <workdir>`` avec LASER_ENGINE_PYTHON = python
du .venv du composant. Le moteur est exécuté par ``<LASER_ENGINE_PYTHON> -m laser_engine.cli <request.json>
<result.json> <op>`` (cwd = dossier du Worker). Résultat : {"op", "artifacts": ["result.json",
("output.gcode")], "summary": {"bytes", "warnings"}} ; une ToolpathError du moteur → état failed avec son
message (et error_code). Ce runner n'importe ni Pillow ni le moteur : il fonctionne avec le Python de l'agent.
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

ENGINE = "laser-engine"
OPERATIONS = ("parse", "plan", "gcode", "frame", "simulate", "raster", "script", "assistant", "assistant_status")
# Assistant de code (agent 1.32.0) : Claude Code / Codex du Worker puis bac à sable Python.
ASSISTANT_OPERATIONS = ("script", "assistant", "assistant_status")
ASSISTANT_FILES = ("assist.py", "script_sandbox.py", "assistant.py")
GCODE_OPERATIONS = ("gcode", "frame")
GCODE_ARTIFACT = "output.gcode"
DEFAULT_OUTPUT = "result.json"
MAX_REQUEST_BYTES = 32 * 1024 * 1024
RESULT_MARKER = "RAPHAL_LASER_RESULT="
# Une trame d'image fine ou un gros SVG se calculent en secondes ; au-delà, l'état est à réconcilier.
TIMEOUT_SECONDS = 1800
# L'assistant peut enchaîner trois réponses (20 min chacune au plus en réflexion longue) et leurs scripts.
OPERATION_TIMEOUTS = {"assistant": 4200, "script": 300, "assistant_status": 180}
# Annulation : le moteur voit ce fichier, arrête lui-même la CLI ou le script, puis sort ; sinon arrêt forcé.
CANCEL_MARKER = "cancel.request"
CANCEL_GRACE_SECONDS = 8
MISSING_ENGINE = ("Le moteur laser n’est pas installé sur ce Worker. Installe Alpine Laser Studio depuis "
                  "Mes Workers → Installations, puis relance.")


class LaserRunnerError(ValueError):
    """Échec définitif du calcul (requête, moteur ou opération), avec un code lisible par le site."""

    def __init__(self, message, code=""):
        super().__init__(message)
        self.code = code


def engine_python():
    value = str(os.getenv("LASER_ENGINE_PYTHON") or "").strip()
    if not value or not Path(value).is_file():
        raise LaserRunnerError(MISSING_ENGINE, "ENGINE_MISSING")
    return value


def engine_environment():
    """Le moteur ne voit que le dossier du Worker : ni PYTHONPATH hérité, ni Python du site."""
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() not in {"PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV"}}
    environment["PYTHONPATH"] = str(WORKER_DIR)
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def _reported(stdout_path):
    """Compte rendu ``RAPHAL_LASER_RESULT=<json>`` du moteur (dernière occurrence), sinon {}."""
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


def _run(job, workdir, state):
    if state.value["state"] == "completed":
        return state.value["result"]
    if state.value["state"] in {"failed", "cancelled"}:
        raise ValueError(state.value.get("error") or "Exécution déjà terminée.")
    parameters = job.get("parameters") if isinstance(job.get("parameters"), dict) else {}
    # Un runner ne fait jamais confiance au payload : opération, fichiers et noms sont revérifiés ici.
    op = str(parameters.get("op") or "").strip().lower()
    if op not in OPERATIONS:
        raise LaserRunnerError(f"Opération laser inconnue « {op or '?'} » (attendu : {', '.join(OPERATIONS)}).", "UNKNOWN_OPERATION")
    source_info = parameters.get("input_file") if isinstance(parameters.get("input_file"), dict) else {}
    source = Path(str(source_info.get("path") or "")).resolve()
    if not source.is_file() or workdir not in source.parents:
        raise LaserRunnerError("Requête laser absente ou hors du dossier du job.", "REQUEST_MISSING")
    if source.stat().st_size > MAX_REQUEST_BYTES:
        raise LaserRunnerError("Requête laser trop volumineuse (32 Mo maximum).", "REQUEST_TOO_LARGE")
    try:
        output_name = safe_relative_name(str(parameters.get("output") or DEFAULT_OUTPUT), basename=True)
    except ValueError as error:
        raise LaserRunnerError(str(error), "OUTPUT_INVALID") from None
    if not output_name.lower().endswith(".json"):
        output_name += ".json"
    destination = workdir / output_name
    if destination.resolve().parent != workdir or destination.name in {source.name, GCODE_ARTIFACT, "job.json"}:
        raise LaserRunnerError("Nom de résultat laser non autorisé.", "OUTPUT_INVALID")
    gcode_path = workdir / GCODE_ARTIFACT
    stdout_path, stderr_path = workdir / "laser-engine.stdout", workdir / "laser-engine.stderr"
    process = None
    if state.value["state"] == "preparing":
        python = engine_python()
        engine = WORKER_DIR / "laser_engine"
        required = ("__init__.py", "toolpath.py", "cli.py") + (ASSISTANT_FILES if op in ASSISTANT_OPERATIONS else ())
        if not all((engine / name).is_file() for name in required):
            raise LaserRunnerError("Le moteur laser du Worker est incomplet : mets à jour le Worker puis relance.", "ENGINE_INCOMPLETE")
        if state.cancellation_requested():
            state.finish("cancelled", error="Calcul laser annulé avant lancement.")
            raise ValueError("Calcul laser annulé.")
        state.submit_intent(engine=ENGINE, op=op)
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
            try:
                process = subprocess.Popen(
                    [python, "-m", "laser_engine.cli", str(source), str(destination), op],
                    cwd=str(WORKER_DIR), env=engine_environment(), stdout=stdout, stderr=stderr,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            except OSError as error:
                state.finish("failed", error=f"Lancement du moteur laser impossible : {error}")
                raise
        identity = process_identity(process.pid)
        state.update(state="running", pid=process.pid, process_identity=identity, progress=10,
                     message=f"Calcul laser « {op} » en cours sur le Worker…")
    pid, identity = state.value.get("pid"), state.value.get("process_identity")
    if not pid:
        raise ValueError("Lancement du moteur laser incertain ; aucun nouveau calcul ne sera lancé.")
    deadline = time.time() + OPERATION_TIMEOUTS.get(op, TIMEOUT_SECONDS)
    cancel_at = 0.0
    while process.poll() is None if process is not None else process_identity(pid) == identity and identity is not None:
        if state.cancellation_requested():
            if not cancel_at:
                cancel_at = time.time()
                (workdir / CANCEL_MARKER).touch()
                state.update(state="cancel_requested")
            if time.time() - cancel_at < CANCEL_GRACE_SECONDS:
                time.sleep(0.2)
                continue
            if process is not None:
                process.terminate()
            elif not terminate_owned_process(pid, identity):
                raise ValueError("Impossible de confirmer l’identité du processus du moteur laser ; aucun autre processus arrêté.")
        if time.time() > deadline:
            raise ValueError("Délai du calcul laser dépassé ; état du processus à réconcilier.")
        time.sleep(0.2)
    if process is not None:
        process.wait()
    elif identity is None:
        raise ValueError("Identité du processus du moteur laser inconnue ; arrêt non confirmé.")
    state.update(engine_stop_confirmed=True)
    if state.cancellation_requested():
        state.finish("cancelled", error="Arrêt du moteur laser confirmé.")
        raise ValueError("Calcul laser annulé.")
    details = _reported(stdout_path)
    if (process is not None and process.returncode) or not details.get("ok") or not destination.is_file():
        message = str(details.get("error") or "") or _stderr_tail(stderr_path) or "Moteur laser : échec ou résultat incomplet."
        raise LaserRunnerError(message, str(details.get("code") or "ENGINE_FAILED"))
    artifacts = [destination.name]
    if op in GCODE_OPERATIONS and gcode_path.is_file():
        artifacts.append(GCODE_ARTIFACT)
    result = {"op": op, "artifacts": artifacts,
              "summary": {"bytes": destination.stat().st_size, "warnings": int(details.get("warnings") or 0)}}
    state.update(progress=100, message="Calcul laser terminé ; résultat écrit.")
    state.finish("completed", result=result)
    return result


if __name__ == "__main__":
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    with RunnerLease(Path(sys.argv[2])):
        print(json.dumps(run_job(job, Path(sys.argv[2])), ensure_ascii=False))
