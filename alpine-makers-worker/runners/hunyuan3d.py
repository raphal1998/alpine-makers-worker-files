"""Durable adapter for the token-protected Hunyuan3D engine."""
import base64
import hashlib
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from safety import release_comfyui_vram, safe_relative_name
from job_state import RunnerLease, RunnerState, TERMINAL, engine_unreachable, engine_unreachable_message
from installers.model_catalog import HUNYUAN_SNAPSHOT_OPTIONS, AI3D_SNAPSHOTS

# Models an older engine service cannot serve: it must be restarted from Installations first.
ENGINE_GATED_MODELS = frozenset(HUNYUAN_SNAPSHOT_OPTIONS) | frozenset(AI3D_SNAPSHOTS)


def engine_request(server, token, path, payload=None, timeout=30):
    request = urllib.request.Request(server + path, data=None if payload is None else json.dumps(payload).encode(),
        headers={"X-Raphal-Local-Token": token, "Content-Type": "application/json"}, method="GET" if payload is None else "POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(8 * 1024 * 1024))


def release_image_engine_vram(timeout=20):
    """Ask a co-resident ComfyUI on this Worker to hand back its VRAM.

    Both engines share one card. A 3D model needs several gigabytes at once, so
    a 3D job started after an image job could exhaust it. Same demand — and same
    best-effort contract — as the one the image runner makes at the end of its
    own job: `safety.release_comfyui_vram`.
    """
    return release_comfyui_vram(timeout)


def run_job(job, workdir):
    workdir = Path(workdir).resolve()
    state = RunnerState(workdir, job)
    try:
        return _run(job, workdir, state)
    except Exception as error:
        if state.value["state"] not in TERMINAL:
            if engine_unreachable(error):
                # Aucun processus n'écoute : rien ne calcule, l'état est certain.
                port = os.getenv("HUNYUAN_WORKER_URL", "http://127.0.0.1:8189").rstrip("/").rsplit(":", 1)[-1]
                state.finish("cancelled" if state.cancellation_requested() else "failed",
                             error=engine_unreachable_message("Hunyuan3D", port))
            elif state.value.get("engine_stop_confirmed"):
                state.finish("failed", error=str(error))
            else:
                state.uncertain(error)
        raise


def _run(job, workdir, state):
    if state.value["state"] == "completed":
        return state.value["result"]
    if state.value["state"] in {"failed", "cancelled"}:
        raise ValueError(state.value.get("error") or "Exécution déjà terminée.")
    parameters = job.get("parameters") if isinstance(job.get("parameters"), dict) else {}
    server = os.getenv("HUNYUAN_WORKER_URL", "http://127.0.0.1:8189").rstrip("/")
    token_file = Path(os.getenv("HUNYUAN_WORKER_TOKEN_FILE") or workdir.parent.parent / "config" / "hunyuan-token")
    token = token_file.read_text(encoding="utf-8").strip()
    task = dict(parameters.get("task")) if isinstance(parameters.get("task"), dict) else dict(parameters)
    selected_model = str(job.get("model_id") or task.get("model_id") or "")
    if selected_model:
        task["model_id"] = selected_model
    job_id = str(job.get("job_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
        raise ValueError("Identifiant du job Hunyuan3D invalide.")
    task["job_id"] = "local-" + hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:32]
    external_id = task["job_id"]
    input_files = parameters.get("input_files") if isinstance(parameters.get("input_files"), list) else []
    if input_files:
        images = []
        for item in input_files:
            path = Path(str(item.get("path") or "")) if isinstance(item, dict) else Path()
            if not path.is_file() or workdir not in path.resolve().parents:
                raise ValueError("Une image source Hunyuan3D est invalide.")
            images.append(base64.b64encode(path.read_bytes()).decode("ascii"))
        task["images"] = images
    pending = queue.Queue()
    timeout = max(60, min(86400, int(parameters.get("timeout_seconds") or 7200)))
    if state.value["state"] == "preparing":
        if state.cancellation_requested():
            state.finish("cancelled", error="Annulé avant soumission Hunyuan3D.")
            raise ValueError("Job Hunyuan3D annulé.")
        if selected_model in ENGINE_GATED_MODELS:
            health = engine_request(server, token, "/health")
            if selected_model not in health.get("supported_model_ids", []):
                state.update(engine_stop_confirmed=True)
                raise ValueError("Redémarre le moteur Hunyuan3D depuis Installations pour activer les nouveaux modèles.")
        release_image_engine_vram()
        state.submit_intent(engine="hunyuan3d", external_id=external_id)
        def submit():
            try:
                pending.put((True, engine_request(server, token, "/generate", task, timeout)))
            except Exception as error:
                pending.put((False, str(error)))
        threading.Thread(target=submit, name="hunyuan-submit-once", daemon=True).start()
    deadline = time.time() + timeout
    result = None
    while time.time() < deadline:
        try:
            accepted, reply = pending.get(timeout=0.2)
        except queue.Empty:
            accepted, reply = False, None
        if accepted and reply.get("engine_stop_confirmed") is True:
            result = reply
            state.update(engine_stop_confirmed=True)
            break
        try:
            remote = engine_request(server, token, f"/jobs/{external_id}")
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 404:
                raise ValueError("Acceptation Hunyuan3D incertaine ; aucune nouvelle soumission automatique.") from None
            raise
        remote = remote.get("job", remote)
        remote_state = str(remote.get("state") or "uncertain").lower()
        if remote_state in TERMINAL and remote.get("engine_stop_confirmed") is True:
            state.update(engine_stop_confirmed=True)
            if remote_state != "completed":
                state.finish(remote_state, error=remote.get("error") or "Calcul Hunyuan3D interrompu.")
                raise ValueError(state.value["error"])
            result = remote.get("result") or {}
            break
        if state.cancellation_requested():
            state.update(state="cancel_requested")
            engine_request(server, token, f"/jobs/{external_id}/cancel", {})
        else:
            state.update(state="running", external_id=external_id)
        time.sleep(1)
    if result is None:
        raise ValueError("Hunyuan3D n’a pas confirmé la fin du calcul ; reprise de statut uniquement.")
    if state.cancellation_requested():
        state.finish("cancelled", error="Calcul Hunyuan3D arrêté ou terminé après la demande d’annulation.")
        raise ValueError("Calcul Hunyuan3D annulé.")
    output_root = (workdir.parent.parent / "outputs").resolve()
    source_root = (output_root / external_id).resolve()
    if source_root.parent != output_root:
        raise ValueError("Dossier de sortie Hunyuan3D non autorisé.")
    artifacts = []
    for extension, filename in (result.get("files") or {}).items():
        if extension not in {"glb", "obj", "stl", "ply", "3mf"}:
            continue
        source = (source_root / safe_relative_name(filename, basename=True)).resolve()
        if source_root not in source.parents or not source.is_file():
            continue
        destination = workdir / f"result.{extension}"
        if destination.resolve().parent != workdir:
            raise ValueError("Artefact Hunyuan3D non autorisé.")
        shutil.copy2(source, destination)
        artifacts.append(destination.name)
    if not artifacts:
        raise ValueError("Hunyuan3D n’a produit aucun modèle pour ce job.")
    output = {"artifacts": artifacts, "mesh_stats": result.get("mesh_stats") or {}, "dimensions": result.get("dimensions") or {}}
    state.finish("completed", result=output)
    return output


if __name__ == "__main__":
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    with RunnerLease(Path(sys.argv[2])):
        print(json.dumps(run_job(job, Path(sys.argv[2]))))
