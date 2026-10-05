# -*- coding: utf-8 -*-
"""Runner de réparation de maillage : calcul Python pur (CPU) dans le dossier du job, sans moteur externe.

Le job est un job « converter » dont ``parameters.task`` vaut ``mesh_repair`` ; l'agent le route ici
au lieu du runner FreeCAD. Tout le calcul (mesh_repair_tool) vit dans ce processus : quand le runner
s'arrête, plus rien ne calcule, l'arrêt du « moteur » est donc toujours confirmé. La sortie est écrite
atomiquement par l'outil, ce qui autorise une reprise après une coupure du Worker.

Lancement par l'agent : ``python mesh_repair.py <job.json> <workdir>``.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from job_state import RunnerLease, RunnerState, TERMINAL
from mesh_repair_tool import MeshRepairError, analyze_mesh, run_from_job

ENGINE = "mesh_repair_tool"


def _flag(value, default=False):
    """Booléen tolérant (le site envoie des booléens, un ancien client peut envoyer du texte)."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "oui"}
    return bool(value)


def _sources(parameters):
    """Fichiers d'entrée déclarés par le job (``input_files`` ou ``input_file``), dans l'ordre."""
    inputs = parameters.get("input_files") if isinstance(parameters.get("input_files"), list) else []
    single = parameters.get("input_file") if isinstance(parameters.get("input_file"), dict) else None
    return [item for item in (inputs or ([single] if single else [])) if isinstance(item, dict)]


def _source_path(item, workdir):
    source = Path(str(item.get("path") or "")).resolve()
    if not source.is_file() or workdir not in source.parents:
        raise MeshRepairError("Fichier source absent ou hors du dossier du job.", "INVALID_PARAMETER")
    return source


def _analyze_only(parameters, workdir):
    """Analyse sans réparation : aucun artefact, seulement le rapport (un fichier ou ``{"files": [...]}``)."""
    sources = _sources(parameters)
    if not sources:
        raise MeshRepairError("Aucun maillage transmis au Worker.", "INVALID_PARAMETER")
    reports = []
    for item in sources:
        source = _source_path(item, workdir)
        reports.append({"name": source.name, **analyze_mesh(source)})
    return {"artifacts": [], "report": reports[0] if len(reports) == 1 else {"files": reports}}


def _without_worker_paths(report):
    """Le rapport ne renvoie au site que le nom du fichier écrit, jamais un chemin absolu du Worker."""
    entries = report.get("files") if isinstance(report.get("files"), list) else [report]
    for entry in entries:
        if isinstance(entry, dict) and entry.get("output"):
            entry["output"] = Path(str(entry["output"])).name
    return report


def run_job(job, workdir):
    workdir = Path(workdir).resolve()
    state = RunnerState(workdir, job)
    try:
        return _run(job, workdir, state)
    except Exception as error:
        if state.value["state"] not in TERMINAL:
            # Aucun processus externe : l'exception signifie que le calcul est terminé, en échec.
            state.finish("failed", error=str(error))
            code = getattr(error, "code", "")
            if code:
                state.update(error_code=str(code))
        raise


def _run(job, workdir, state):
    if state.value["state"] == "completed":
        return state.value["result"]
    if state.value["state"] in {"failed", "cancelled"}:
        raise ValueError(state.value.get("error") or "Exécution déjà terminée.")
    parameters = job.get("parameters") if isinstance(job.get("parameters"), dict) else {}
    # Un runner ne fait jamais confiance au payload : seul un job de réparation est exécuté ici.
    if str(parameters.get("task") or "") != "mesh_repair":
        raise ValueError("Ce runner n’exécute que la réparation de maillage (task = mesh_repair).")
    if state.value["state"] != "preparing":
        # Exécution précédente interrompue (coupure du Worker) : le calcul vivait dans ce processus,
        # rien ne tourne plus et la sortie est écrite atomiquement ; on peut recommencer sans risque.
        state.update(state="preparing", engine_stop_confirmed=True)
    if state.cancellation_requested():
        state.finish("cancelled", error="Réparation annulée avant lancement.")
        raise ValueError("Réparation annulée.")
    analyze_only = _flag(parameters.get("analyze_only"))
    state.submit_intent(engine=ENGINE)
    state.update(state="running", progress=5,
                 message="Analyse du maillage…" if analyze_only else "Lecture, analyse et réparation du maillage…")
    outcome = _analyze_only(parameters, workdir) if analyze_only else run_from_job(job, workdir)
    if state.cancellation_requested():
        state.finish("cancelled", error="Réparation annulée ; résultat non transmis.")
        raise ValueError("Réparation annulée.")
    report = _without_worker_paths(outcome["report"])
    artifacts = list(outcome["artifacts"])
    engine = report.get("engine") if isinstance(report.get("engine"), str) else ENGINE
    if analyze_only:
        message = "Analyse terminée : aucun fichier écrit."
    else:
        message = f"Réparation terminée : {len(artifacts)} fichier(s) écrit(s)."
    details = {"task": "mesh_repair", "analyze_only": analyze_only, "engine": engine,
               "artifacts": artifacts, "report": report, "message": message}
    state.update(progress=100, message=message)
    state.finish("completed", result=details)
    return details


if __name__ == "__main__":
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    with RunnerLease(Path(sys.argv[2])):
        print(json.dumps(run_job(job, Path(sys.argv[2])), ensure_ascii=False))
