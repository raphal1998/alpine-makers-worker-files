# -*- coding: utf-8 -*-
"""Runner Worker d'Alpine Toile Creator : une génération de toile 3D (ou un détourage) par job, calculée par le
moteur du composant « toile-engine » (Python isolé), jamais par le PC qui héberge le site (agent 1.51.0).

Modèle : runners/cnc-engine.py — un processus par job (pas de service résident), arrêt vérifié par identité de
processus, reprise après coupure via .runner-state.json. Le job porte ``parameters.op`` (generer, detourer), les
paramètres du contrat (docs/ALPINE_TOILE_CREATOR.md : largeur_mm, hauteur_mm, pixel_mm, épaisseurs, couleurs,
couleur_base, face_dessous, nom) et ``parameters.input_file.path`` : la carte des couleurs (generer) ou l'image
(detourer) téléchargée par l'agent dans le dossier du job.

Lancement par l'agent : ``python toile-engine.py <job.json> <workdir>`` avec TOILE_ENGINE_PYTHON = python du .venv
du moteur ; pour « detourer », TOILE_DETOURAGE_PYTHON (python du .venv du module toile-detourage) et
TOILE_DETOURAGE_MODEL (son model.onnx vérifié), définis par l'agent seulement quand le module est prêt. Le runner écrit
``toile_request.json`` puis exécute ``<python> -m toile_engine.cli <toile_request.json> result.json`` (cwd = dossier
du Worker). Résultat : {"op", "artifacts": ["result.json", "toile.3mf", "apercu.png"] ou ["result.json",
"masque.png"], "summary": {...}} ; une erreur du moteur → état failed avec son message et son error_code.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

WORKER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER_DIR))
from job_state import RunnerLease, RunnerState, TERMINAL  # noqa: E402
from process_control import process_identity, terminate_owned_process  # noqa: E402

ENGINE = "toile-engine"
OPERATIONS = ("generer", "detourer")
ENGINE_FILES = ("toile_engine/__init__.py", "toile_engine/mesh.py", "toile_engine/troismf.py",
                "toile_engine/detourage.py", "toile_engine/cli.py")
REQUEST_FILE = "toile_request.json"
RESULT_ARTIFACT = "result.json"
ARTIFACTS = {"generer": ("toile.3mf", "apercu.png"), "detourer": ("masque.png",)}
SUFFIXES = {"generer": (".png",), "detourer": (".png", ".jpg", ".jpeg", ".webp")}
MAX_INPUT_BYTES = {"generer": 64 * 1024 * 1024, "detourer": 25 * 1024 * 1024}
# Paramètres du contrat transmis au moteur (rien d'autre) ; le moteur revérifie types et bornes.
PARAMETERS = ("nom", "largeur_mm", "hauteur_mm", "pixel_mm", "epaisseur_base_mm", "epaisseur_image_mm", "couleurs",
              "couleur_base", "face_dessous")
RESULT_MARKER = "RAPHAL_TOILE_RESULT="
# Une carte de 4000 × 4000 px très morcelée se maille en quelques minutes ; au-delà, état à réconcilier.
TIMEOUT_SECONDS = 1200
CANCEL_GRACE_SECONDS = 3
MISSING_ENGINE = ("Le moteur de toile n’est pas installé sur ce Worker. Installe Alpine Toile Creator depuis "
                  "Mes Workers → Installations, puis relance.")
MISSING_MODULE = ("Le module de détourage n’est pas installé sur ce Worker. Installe « Détourage IA » d’Alpine Toile "
                  "Creator depuis Mes Workers → Installations, puis relance.")
LABELS = {"generer": "Génération de la toile 3D", "detourer": "Détourage de l’image"}


class ToileRunnerError(ValueError):
    """Échec définitif du calcul (requête, moteur ou opération), avec un code lisible par le site."""

    def __init__(self, message, code=""):
        super().__init__(message)
        self.code = code


def engine_python(op):
    """Python qui exécute l'opération : celui du moteur (generer) ou celui du module de détourage (detourer)."""
    if op == "detourer":
        python = str(os.getenv("TOILE_DETOURAGE_PYTHON") or "").strip()
        model = str(os.getenv("TOILE_DETOURAGE_MODEL") or "").strip()
        if not python or not Path(python).is_file() or not model or not Path(model).is_file():
            raise ToileRunnerError(MISSING_MODULE, "MODULE_MISSING")
        return python, model
    value = str(os.getenv("TOILE_ENGINE_PYTHON") or "").strip()
    if not value or not Path(value).is_file():
        raise ToileRunnerError(MISSING_ENGINE, "ENGINE_MISSING")
    return value, ""


def engine_environment():
    """Le moteur ne voit que le dossier du Worker : ni PYTHONPATH hérité, ni Python du site."""
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() not in {"PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV"}}
    environment["PYTHONPATH"] = str(WORKER_DIR)
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def _reported(stdout_path):
    """Compte rendu ``RAPHAL_TOILE_RESULT=<json>`` du moteur (dernière occurrence), sinon {}."""
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


def _request(op, source, parameters, model, workdir):
    """Requête du moteur écrite dans le dossier du job : opération, fichier d'entrée, paramètres du contrat."""
    if op == "generer":
        request = {"op": op, "carte_path": str(source),
                   "parametres": {key: parameters[key] for key in PARAMETERS if key in parameters}}
    else:
        request = {"op": op, "image_path": str(source), "modele_path": model}
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
        raise ToileRunnerError(f"Opération de toile inconnue « {op or '?'} » (attendu : {', '.join(OPERATIONS)}).", "UNKNOWN_OPERATION")
    source_info = parameters.get("input_file") if isinstance(parameters.get("input_file"), dict) else {}
    source = Path(str(source_info.get("path") or "")).resolve()
    if not source.is_file() or workdir not in source.parents:
        raise ToileRunnerError("Fichier d’entrée absent ou hors du dossier du job.", "INPUT_MISSING")
    if source.suffix.lower() not in SUFFIXES[op]:
        expected = "carte des couleurs .png" if op == "generer" else "image .png, .jpg, .jpeg ou .webp"
        raise ToileRunnerError(f"Fichier d’entrée : {expected} attendue.", "INPUT_INVALID")
    if source.stat().st_size == 0:
        raise ToileRunnerError("Fichier d’entrée vide.", "INPUT_EMPTY")
    if source.stat().st_size > MAX_INPUT_BYTES[op]:
        raise ToileRunnerError(f"Fichier d’entrée trop volumineux ({MAX_INPUT_BYTES[op] // (1024 * 1024)} Mo maximum).", "INPUT_TOO_LARGE")
    reserved = {REQUEST_FILE, RESULT_ARTIFACT, "job.json", *ARTIFACTS[op]}
    if source.name in reserved:
        raise ToileRunnerError("Nom du fichier d’entrée réservé aux résultats.", "INPUT_INVALID")
    destination = workdir / RESULT_ARTIFACT
    stdout_path, stderr_path = workdir / "toile-engine.stdout", workdir / "toile-engine.stderr"
    process = None
    if state.value["state"] == "preparing":
        python, model = engine_python(op)
        if not all((WORKER_DIR / name).is_file() for name in ENGINE_FILES):
            raise ToileRunnerError("Le moteur de toile du Worker est incomplet : mets à jour le Worker puis relance.", "ENGINE_INCOMPLETE")
        if state.cancellation_requested():
            state.finish("cancelled", error=f"{LABELS[op]} annulée avant lancement.")
            raise ValueError(f"{LABELS[op]} annulée.")
        # Restes d'une tentative précédente : jamais pris pour le résultat de celle-ci.
        for name in (RESULT_ARTIFACT, *ARTIFACTS[op]):
            (workdir / name).unlink(missing_ok=True)
        request_path = _request(op, source, parameters, model, workdir)
        state.submit_intent(engine=ENGINE, op=op)
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
            try:
                process = subprocess.Popen(
                    [python, "-m", "toile_engine.cli", str(request_path), str(destination)],
                    cwd=str(WORKER_DIR), env=engine_environment(), stdout=stdout, stderr=stderr,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            except OSError as error:
                state.finish("failed", error=f"Lancement du moteur de toile impossible : {error}")
                raise
        identity = process_identity(process.pid)
        state.update(state="running", pid=process.pid, process_identity=identity, progress=10,
                     message=f"{LABELS[op]} en cours sur le Worker…")
    pid, identity = state.value.get("pid"), state.value.get("process_identity")
    if not pid:
        raise ValueError("Lancement du moteur de toile incertain ; aucun nouveau calcul ne sera lancé.")
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
                raise ValueError("Impossible de confirmer l’identité du processus du moteur de toile ; aucun autre processus arrêté.")
        if time.time() > deadline:
            raise ValueError("Délai du calcul de la toile dépassé ; état du processus à réconcilier.")
        time.sleep(0.2)
    if process is not None:
        process.wait()
    elif identity is None:
        raise ValueError("Identité du processus du moteur de toile inconnue ; arrêt non confirmé.")
    state.update(engine_stop_confirmed=True)
    if state.cancellation_requested():
        state.finish("cancelled", error="Arrêt du moteur de toile confirmé.")
        raise ValueError(f"{LABELS[op]} annulée.")
    details = _reported(stdout_path)
    produced = all((workdir / name).is_file() for name in ARTIFACTS[op])
    if (process is not None and process.returncode) or not details.get("ok") or not destination.is_file() or not produced:
        message = str(details.get("error") or "") or _stderr_tail(stderr_path) or "Moteur de toile : échec ou résultat incomplet."
        raise ToileRunnerError(message, str(details.get("code") or "ENGINE_FAILED"))
    summary = {"bytes": destination.stat().st_size, "warnings": int(details.get("avertissements") or 0)}
    if op == "generer":
        summary.update(triangles=int(details.get("triangles") or 0), pieces=int(details.get("pieces") or 0),
                       grammes_total=float(details.get("grammes_total") or 0.0),
                       bytes_3mf=(workdir / ARTIFACTS[op][0]).stat().st_size)
    else:
        summary.update(part_premier_plan=float(details.get("part_premier_plan") or 0.0))
    result = {"op": op, "artifacts": [RESULT_ARTIFACT, *ARTIFACTS[op]], "summary": summary}
    state.update(progress=100, message=f"{LABELS[op]} terminée ; résultat écrit.")
    state.finish("completed", result=result)
    return result


if __name__ == "__main__":
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    with RunnerLease(Path(sys.argv[2])):
        print(json.dumps(run_job(job, Path(sys.argv[2])), ensure_ascii=False))
