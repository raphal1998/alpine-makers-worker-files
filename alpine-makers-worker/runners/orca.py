"""Orca jobs are submitted once, then reconciled through per-execution receipts."""
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from safety import safe_relative_name
from job_state import RunnerLease, RunnerState, TERMINAL, atomic_json, read_json


def installed_bridge_version(worker_root):
    """Version du bridge installé sur ce Worker (ligne ``$BridgeVersion`` du script), ``None`` si illisible."""
    try:
        text = (Path(worker_root) / "orca_core_bridge.ps1").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r'^\$BridgeVersion\s*=\s*"([^"]{1,20})"', text, re.M)
    return match.group(1) if match else None


def require_current_bridge(info, worker_root):
    """Le bridge en marche garde l'ancien script en mémoire après une mise à jour de l'agent : on refuse de
    trancher avec lui (mauvais profils) plutôt que de produire un fichier faux."""
    installed = installed_bridge_version(worker_root)
    running = str(info.get("bridge_version") or "")
    if installed and running != installed:
        raise ValueError(f"Le bridge OrcaSlicer en marche (version {running or 'inconnue'}) est plus ancien que celui "
                         f"installé ({installed}) : arrête puis relance Alpine Model Studio (Mes Workers → ON/OFF IA "
                         "locales), puis relance le tranchage.")


def run_job(job, workdir):
    workdir = Path(workdir).resolve()
    state = RunnerState(workdir, job)
    try:
        return _run(job, workdir, state)
    except Exception as error:
        if state.value["state"] not in TERMINAL:
            if state.value.get("engine_stop_confirmed"):
                state.finish("failed", error=str(error))
            else:
                state.uncertain(error)
        raise


def _run(job, workdir, state):
    if state.value["state"] == "completed":
        return state.value["result"]
    if state.value["state"] in {"failed", "cancelled"}:
        raise RuntimeError(state.value.get("error") or "Exécution déjà terminée.")
    parameters = dict(job.get("parameters") or {})
    worker_root = Path(os.environ.get("ALPINE_WORKER_ROOT") or workdir.parent.parent).resolve()
    bridge_workspace = worker_root / "workspace"
    bridge_workspace.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("WORKER_ORCA_PORT") or "15064")
    execution_id = state.value["execution_id"]
    receipt_root = bridge_workspace / "_worker_jobs" / execution_id
    receipt_root.mkdir(parents=True, exist_ok=True)
    if (bridge_workspace / "_worker_jobs").resolve() not in receipt_root.resolve().parents:
        raise ValueError("Dossier du journal Orca non autorisé.")
    pending = queue.Queue()
    if state.value["state"] == "preparing":
        inputs = parameters.get("input_files") if isinstance(parameters.get("input_files"), list) else []
        single = parameters.get("input_file") if isinstance(parameters.get("input_file"), dict) else None
        sources = inputs or ([single] if single else [])
        if not sources:
            raise RuntimeError("Aucun modèle transmis au Worker OrcaSlicer.")
        copied = []
        for index, item in enumerate(sources):
            source = Path(str(item.get("path") or "")).resolve()
            if not source.is_file() or workdir not in source.parents:
                raise RuntimeError("Fichier de tranchage extérieur au job Worker.")
            name = safe_relative_name(f"worker_{execution_id}_{index}{source.suffix}", basename=True)
            destination = bridge_workspace / name
            if destination.resolve().parent != bridge_workspace.resolve():
                raise ValueError("Destination de tranchage non autorisée.")
            shutil.copy2(source, destination)
            copied.append(name)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=10) as response:
            info = json.loads(response.read(1024 * 1024))
        if info.get("worker_job_protocol_version") != 1:
            raise ValueError("Le bridge Orca doit être mis à jour et redémarré pour le suivi durable et l’annulation ciblée.")
        require_current_bridge(info, worker_root)
        multi = len(copied) > 1 or bool(parameters.get("multi"))
        payload = {key: value for key, value in parameters.items()
                   if key not in {"input_file", "input_files", "multi", "output_name", "worker_execution_id"}}
        payload["worker_execution_id"] = execution_id
        payload["files" if multi else "file"] = copied if multi else copied[0]
        if state.cancellation_requested():
            state.finish("cancelled", error="Tranchage annulé avant soumission.")
            raise ValueError("Tranchage annulé.")
        state.submit_intent(engine="orca", external_id=execution_id, receipt_root=str(receipt_root))
        def submit():
            try:
                request = urllib.request.Request(f"http://127.0.0.1:{port}/{'slice-multi' if multi else 'slice'}",
                    data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(request, timeout=3600) as response:
                    pending.put((True, json.loads(response.read(8 * 1024 * 1024))))
            except Exception as error:
                pending.put((False, str(error)))
        threading.Thread(target=submit, name="orca-submit-once", daemon=True).start()
    deadline = time.time() + 3600
    result = None
    while time.time() < deadline:
        if state.cancellation_requested():
            atomic_json(receipt_root / "cancel.json", {"execution_id": execution_id})
            state.update(state="cancel_requested")
        receipt = read_json(receipt_root / "status.json", {})
        if receipt and receipt.get("execution_id") != execution_id:
            raise ValueError("Le reçu Orca appartient à une autre exécution.")
        if receipt.get("state") in TERMINAL and receipt.get("engine_stop_confirmed") is True:
            state.update(engine_stop_confirmed=True)
            if receipt["state"] != "completed":
                state.finish(receipt["state"], error=receipt.get("error") or "Tranchage interrompu.")
                raise ValueError(state.value["error"])
            result = receipt.get("result") or {}
            break
        try:
            accepted, reply = pending.get(timeout=0.2)
        except queue.Empty:
            accepted, reply = False, None
        if reply is not None and not accepted and not receipt:
            raise ValueError("Acceptation du tranchage incertaine ; aucune nouvelle soumission automatique.")
        if receipt:
            state.update(state="cancel_requested" if state.cancellation_requested() else "running")
        time.sleep(0.5)
    if result is None:
        raise ValueError("Orca n’a pas confirmé l’arrêt ou la fin du tranchage ; réconciliation requise.")
    if state.cancellation_requested():
        state.finish("cancelled", error="Tranchage arrêté ou terminé après annulation ; processus libéré.")
        raise ValueError("Tranchage annulé.")
    filename = safe_relative_name(result.get("output"), basename=True)
    if filename != f"worker_{execution_id}_sliced.gcode.3mf":
        raise ValueError("L’artefact Orca appartient à une autre exécution.")
    output = bridge_workspace / filename
    if not output.is_file() or output.resolve().parent != bridge_workspace.resolve():
        raise ValueError("OrcaSlicer n’a produit aucun fichier pour ce job.")
    artifact = workdir / filename
    if artifact.resolve().parent != workdir:
        raise ValueError("Artefact Orca non autorisé.")
    shutil.copy2(output, artifact)
    result = {"artifacts": [artifact.name], "output": artifact.name, "bytes": artifact.stat().st_size,
              "exit_code": result.get("exit_code"), "profile_mode": result.get("profile_mode"),
              "fallback_used": bool(result.get("fallback_used")), "bridge_version": result.get("bridge_version"),
              # Ce que le site affiche avant l'impression : plaque et profils filament réellement utilisés.
              "bed_type": str(result.get("bed_type") or "")[:40],
              "filament_profiles": [str(name)[:120] for name in (result.get("filament_profiles") or [])
                                    if isinstance(name, str)][:16]}
    state.finish("completed", result=result)
    return result


if __name__ == "__main__":
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    with RunnerLease(Path(sys.argv[2])):
        print(json.dumps(run_job(job, Path(sys.argv[2]))))
