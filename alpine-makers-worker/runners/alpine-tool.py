# -*- coding: utf-8 -*-
"""Runner Worker des Alpine Tools (agent 1.53.0) : un calcul par job, dans le Python isolé du composant de l'outil
(atool-<id>), jamais sur le PC qui héberge le site. Modèle : runners/toile-engine.py (processus suivi par identité,
annulation, reprise après coupure par .runner-state.json).

Le job porte ``parameters.tool`` (tool_id du registre), ``parameters.params`` (déjà validés par le site, revalidés
par l'outil), ``parameters.input_key`` et ``parameters.input_file.path`` (fichier envoyé, facultatif selon l'outil).
L'agent fournit ALPINE_TOOL_PYTHON (python du .venv du composant prêt). Le runner écrit ``atool_request.json`` puis
exécute ``<python> -m alpine_tools.cli atool_request.json result.json`` (cwd = dossier du Worker). Résultat :
{"tool", "artifacts": ["result.json", fichiers…, aperçu], "summary": {...}}.
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
from alpine_tools import registry  # noqa: E402

REQUEST_FILE = "atool_request.json"
RESULT_ARTIFACT = "result.json"
RESERVED = {REQUEST_FILE, RESULT_ARTIFACT, "job.json", "alpine-tool.stdout", "alpine-tool.stderr", ".runner-state.json"}
RESULT_MARKER = "RAPHAL_ATOOL_RESULT="
PROGRESS_MARKER = "RAPHAL_ATOOL_PROGRESS="
MAX_INPUT_BYTES = 64 * 1024 * 1024
TIMEOUT_SECONDS = 1800
CANCEL_GRACE_SECONDS = 3
_NOM = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")


class ToolRunnerError(ValueError):
    def __init__(self, message, code=""):
        super().__init__(message)
        self.code = code


def engine_environment():
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() not in {"PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV"}}
    environment["PYTHONPATH"] = str(WORKER_DIR)
    environment["PYTHONIOENCODING"] = "utf-8"
    # numpy réserve sinon ~750 Mo pour un fil de calcul par cœur ; les outils n'en ont pas besoin.
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        environment[name] = "1"
    return environment


def _lignes(path):
    return path.read_text(encoding="utf-8", errors="replace").splitlines() if path.is_file() else []


def _reported(stdout_path):
    lignes = _lignes(stdout_path)
    texte = next((line[len(RESULT_MARKER):] for line in reversed(lignes) if line.startswith(RESULT_MARKER)), "")
    try:
        value = json.loads(texte) if texte else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _progress(stdout_path):
    for line in reversed(_lignes(stdout_path)[-50:]):
        if line.startswith(PROGRESS_MARKER):
            fraction, _, message = line[len(PROGRESS_MARKER):].partition(" ")
            try:
                return float(fraction), message
            except ValueError:
                return None
    return None


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
    tool_id = str(parameters.get("tool") or job.get("tool_id") or "")
    paquet = registry.package_of(tool_id)
    if not paquet:
        raise ToolRunnerError("Outil Alpine Tools inconnu de ce Worker : mets le Worker à jour.", "UNKNOWN_TOOL")
    nom = registry.NAMES[tool_id]
    inputs = {}
    source_info = parameters.get("input_file") if isinstance(parameters.get("input_file"), dict) else {}
    if source_info.get("path"):
        source = Path(str(source_info["path"])).resolve()
        if not source.is_file() or workdir not in source.parents:
            raise ToolRunnerError("Fichier d’entrée absent ou hors du dossier du job.", "INPUT_MISSING")
        if source.stat().st_size == 0 or source.stat().st_size > MAX_INPUT_BYTES:
            raise ToolRunnerError("Fichier d’entrée vide ou trop volumineux (64 Mo au plus).", "INPUT_INVALID")
        if source.name in RESERVED:
            raise ToolRunnerError("Nom du fichier d’entrée réservé.", "INPUT_INVALID")
        key = str(parameters.get("input_key") or "")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,30}", key):
            raise ToolRunnerError("Clé du fichier d’entrée invalide.", "INPUT_INVALID")
        inputs[key] = str(source)
    destination = workdir / RESULT_ARTIFACT
    stdout_path, stderr_path = workdir / "alpine-tool.stdout", workdir / "alpine-tool.stderr"
    process = None
    if state.value["state"] == "preparing":
        python = str(os.getenv("ALPINE_TOOL_PYTHON") or "").strip()
        if not python or not Path(python).is_file():
            raise ToolRunnerError(f"{nom} n’est pas installé sur ce Worker : installe-le depuis Mes Workers → Installations.", "ENGINE_MISSING")
        if not (WORKER_DIR / "alpine_tools" / paquet / "__init__.py").is_file():
            raise ToolRunnerError(f"{nom} manque au paquet du Worker : mets le Worker à jour.", "ENGINE_INCOMPLETE")
        if state.cancellation_requested():
            state.finish("cancelled", error=f"{nom} : calcul annulé avant lancement.")
            raise ValueError(f"{nom} : calcul annulé.")
        destination.unlink(missing_ok=True)
        request = {"paquet": paquet, "params": parameters.get("params") if isinstance(parameters.get("params"), dict) else {},
                   "inputs": inputs, "out_dir": str(workdir)}
        request_path = workdir / REQUEST_FILE
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        state.submit_intent(engine=tool_id, op="run")
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
            try:
                process = subprocess.Popen([python, "-m", "alpine_tools.cli", str(request_path), str(destination)],
                                           cwd=str(WORKER_DIR), env=engine_environment(), stdout=stdout, stderr=stderr,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            except OSError as error:
                state.finish("failed", error=f"Lancement de {nom} impossible : {error}")
                raise
        identity = process_identity(process.pid)
        state.update(state="running", pid=process.pid, process_identity=identity, progress=5, message=f"{nom} : calcul en cours sur le Worker…")
    pid, identity = state.value.get("pid"), state.value.get("process_identity")
    if not pid:
        raise ValueError(f"Lancement de {nom} incertain ; aucun nouveau calcul ne sera lancé.")
    deadline = time.time() + TIMEOUT_SECONDS
    cancel_at = 0.0
    dernier = None
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
                raise ValueError(f"Impossible de confirmer l’identité du processus de {nom} ; aucun autre processus arrêté.")
        if time.time() > deadline:
            raise ValueError(f"Délai du calcul de {nom} dépassé ; état du processus à réconcilier.")
        avance = _progress(stdout_path)
        if avance and avance != dernier:
            dernier = avance
            state.update(progress=int(5 + 90 * avance[0]), message=f"{nom} : {avance[1] or 'calcul en cours'}"[:200])
        time.sleep(0.3)
    if process is not None:
        process.wait()
    elif identity is None:
        raise ValueError(f"Identité du processus de {nom} inconnue ; arrêt non confirmé.")
    state.update(engine_stop_confirmed=True)
    if state.cancellation_requested():
        state.finish("cancelled", error=f"Arrêt de {nom} confirmé.")
        raise ValueError(f"{nom} : calcul annulé.")
    try:
        compte = json.loads(destination.read_text(encoding="utf-8")) if destination.is_file() else {}
    except ValueError:
        compte = {}
    if not compte.get("ok"):
        reported = _reported(stdout_path)
        tail = " ".join(_lignes(stderr_path))[-600:]
        raise ToolRunnerError(str(compte.get("error") or reported.get("error") or tail or f"{nom} : échec du calcul."),
                              str(compte.get("code") or reported.get("code") or "ENGINE_FAILED"))
    noms = [item["name"] for item in compte.get("files") or [] if _NOM.match(str(item.get("name") or ""))]
    apercu = compte.get("preview") if _NOM.match(str(compte.get("preview") or "")) else None
    artifacts = [RESULT_ARTIFACT] + [n for n in noms if n not in RESERVED]
    if apercu and apercu not in artifacts and apercu not in RESERVED:
        artifacts.append(apercu)
    missing = [n for n in artifacts if not (workdir / n).is_file()]
    if missing:
        raise ToolRunnerError(f"{nom} : résultat incomplet ({', '.join(missing)}).", "RESULT_MISSING")
    result = {"tool": tool_id, "artifacts": artifacts,
              "summary": {"files": len(noms), "bytes": sum((workdir / n).stat().st_size for n in artifacts)}}
    state.update(progress=100, message=f"{nom} : terminé.")
    state.finish("completed", result=result)
    return result


if __name__ == "__main__":
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    with RunnerLease(Path(sys.argv[2])):
        print(json.dumps(run_job(job, Path(sys.argv[2])), ensure_ascii=False))
