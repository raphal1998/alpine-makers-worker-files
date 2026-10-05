"""Controlled image adapter; arbitrary ComfyUI graphs are forbidden."""
import json
import os
import re
import shutil
import sys
import time
import urllib.request
import urllib.error
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from safety import (IMAGE_LAB_TEXT_MAX_CHARS, IMAGE_LAB_TEXT_TOOLS, comfyui_server_url, release_comfyui_vram_when_idle,
                    safe_relative_name, validate_comfyui_job_parameters)
from job_state import RunnerLease, RunnerState, TERMINAL, engine_unreachable, engine_unreachable_message


def engine_json(server, path, payload=None):
    request = urllib.request.Request(server + path, data=None if payload is None else json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="GET" if payload is None else "POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read(8 * 1024 * 1024))


MAX_SOURCE_IMAGE_BYTES = 32 * 1024 * 1024
IMAGE_SIGNATURES = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"RIFF")


def upload_source_image(server, source, filename):
    """Place this job's own source picture in ComfyUI under a private name.

    The dashboard never chooses this name: the runner derives it from the
    execution identity, so two jobs sharing one engine can neither read nor
    overwrite each other's source picture.
    """
    data = Path(source).read_bytes()
    if not data or len(data) > MAX_SOURCE_IMAGE_BYTES:
        raise ValueError("Image source ComfyUI absente ou trop volumineuse.")
    if not any(data.startswith(magic) for magic in IMAGE_SIGNATURES):
        raise ValueError("Image source ComfyUI dans un format non pris en charge.")
    boundary = "----alpine" + uuid.uuid4().hex
    lead = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n').encode()
    fields = "".join(
        f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}'
        for name, value in (("type", "input"), ("overwrite", "true"))
    ) + f"\r\n--{boundary}--\r\n"
    body = lead + data + fields.encode()
    request = urllib.request.Request(server + "/upload/image", data=body, method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(request, timeout=120) as response:
        stored = json.loads(response.read(64 * 1024))
    if not isinstance(stored, dict) or stored.get("name") != filename or stored.get("subfolder"):
        raise ValueError("ComfyUI n'a pas confirmé la réception de l'image source.")
    return filename


def recover_prompt(server, execution_id):
    """Read-only reconciliation after the /prompt response was lost."""
    queue = engine_json(server, "/queue")
    history = engine_json(server, "/history?max_items=512")
    entries = queue_entries(queue)
    entries += [item.get("prompt") for item in history.values() if isinstance(item, dict)]
    for item in entries:
        if isinstance(item, list) and len(item) > 3 and isinstance(item[3], dict) and item[3].get("client_id") == execution_id:
            value = item[1]
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
                return value
    return None


def queue_entries(queue):
    if not isinstance(queue, dict) or not isinstance(queue.get("queue_running"), list) or not isinstance(queue.get("queue_pending"), list):
        raise ValueError("État de file ComfyUI invalide : arrêt non confirmé.")
    entries = queue["queue_running"] + queue["queue_pending"]
    if any(not isinstance(item, list) or len(item) < 2 or not isinstance(item[1], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item[1]) for item in entries):
        raise ValueError("Entrée de file ComfyUI invalide : arrêt non confirmé.")
    return entries


def cancel_prompt(server, prompt_id, control_token=""):
    """Never call legacy /interrupt: it may stop a different user's job."""
    endpoints = [f"/api/jobs/{prompt_id}/cancel"]
    if control_token:
        endpoints.append(f"/alpine-worker/jobs/{prompt_id}/cancel")
    for endpoint in endpoints:
        try:
            request = urllib.request.Request(server + endpoint, data=b"{}", method="POST",
                        headers={"Content-Type": "application/json", "X-Alpine-Worker-Token": control_token})
            with urllib.request.urlopen(request, timeout=15) as response:
                response.read(1024 * 1024)
            return
        except urllib.error.HTTPError as error:
            error.close()
            if error.code not in {404, 405}:
                raise
    queue = engine_json(server, "/queue")
    queue_entries(queue)
    if any(isinstance(item, list) and len(item) > 1 and item[1] == prompt_id for item in queue.get("queue_pending", [])):
        engine_json(server, "/queue", {"delete": [prompt_id]})
        return
    raise ValueError("Ce moteur ComfyUI ne permet pas encore l’arrêt ciblé du calcul actif. Mets à jour le Worker puis redémarre son moteur ; l’annulation reste en attente.")


def run_job(job, workdir):
    workdir = Path(workdir).resolve()
    state = RunnerState(workdir, job)
    try:
        return _run_job(job, workdir, state)
    except Exception as error:
        if state.value.get("state") not in TERMINAL:
            if engine_unreachable(error):
                # Aucun processus n'écoute : rien ne calcule, l'état est certain.
                # Sinon le job resterait « incertain » et bloquerait le Worker.
                state.finish("cancelled" if state.cancellation_requested() else "failed",
                             error=engine_unreachable_message("ComfyUI", os.getenv("COMFYUI_PORT", "8188")))
            elif state.value.get("engine_stop_confirmed"):
                state.finish("failed", error=str(error))
            else:
                state.uncertain(error)
        raise
    finally:
        # Une génération terminée ne doit pas garder la carte : sinon le job suivant,
        # image ou 3D, attend « VRAM libre insuffisante sur le Worker ».
        release_comfyui_vram_when_idle()


def _run_job(job, workdir, state):
    if state.value["state"] == "completed":
        return state.value["result"]
    if state.value["state"] in {"failed", "cancelled"}:
        raise ValueError(state.value.get("error") or "Exécution déjà terminée.")
    # Neither a browser-provided prefix nor another job's inputs/outputs may
    # select files in ComfyUI's shared folders.
    prefix = state.value.get("prefix") or f"alpine-worker/{uuid.uuid4().hex}/image"
    state.update(prefix=prefix)
    # Génération ou Labo image (agent 1.39.0) : chaque famille de graphes a son validateur, choisi par le job.
    parameters = validate_comfyui_job_parameters(job.get("parameters"), output_prefix=prefix)
    workflow = parameters["workflow"]
    server = comfyui_server_url()
    root = Path(os.getenv("COMFYUI_ROOT") or workdir.parent.parent / "components" / "comfyui").resolve()
    output_root = (root / "output").resolve()
    job_output = (output_root / prefix).parent.resolve()
    if output_root not in job_output.parents:
        raise ValueError("Dossier de sortie ComfyUI non autorisé.")
    prompt_id = state.value.get("external_id")
    if state.value["state"] == "preparing":
        if state.cancellation_requested():
            state.finish("cancelled", error="Job annulé avant soumission.")
            raise ValueError("Job annulé avant soumission.")
        # Nœud qui lit l'image du job : « 4 » (image → image) ou « 30 » (image de contrôle d'un ControlNet, image
        # source d'un outil du Labo image).
        source_node = next((key for key in ("4", "30") if isinstance(workflow.get(key), dict) and workflow[key].get("class_type") == "LoadImage"), None)
        if source_node:
            # Only a job about to be submitted needs its picture in the engine.
            stored = str(parameters["input_files"][0].get("path") or "")
            if not stored or not Path(stored).is_file():
                raise ValueError("L’image source de ce job n’a pas été copiée sur le Worker.")
            workflow[source_node]["inputs"]["image"] = upload_source_image(
                server, stored, f"alpine-{state.value['execution_id']}.png")
        state.submit_intent(engine="comfyui")
        payload = json.dumps({"prompt": workflow, "client_id": state.value["execution_id"]}).encode()
        request = urllib.request.Request(server + "/prompt", data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=30) as response:
            prompt_id = json.loads(response.read())["prompt_id"]
        state.update(external_id=prompt_id, state="running")
    elif not prompt_id:
        prompt_id = recover_prompt(server, state.value["execution_id"])
        if not prompt_id:
            # Le moteur répond et ne connaît ce job ni en file ni en historique :
            # rien ne calcule (soumission jamais acceptée, ou moteur redémarré).
            state.update(engine_stop_confirmed=True)
            raise ValueError("ComfyUI ne connaît pas cette génération (soumission jamais acceptée ou moteur redémarré) ; "
                             "aucun calcul n’est en cours. Relance la génération.")
        state.update(external_id=prompt_id, state="running")
    if not isinstance(prompt_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", prompt_id):
        raise ValueError("Identifiant ComfyUI invalide.")
    deadline = time.time() + max(60, min(86400, int(parameters.get("timeout_seconds") or 3600)))
    history = None
    while time.time() < deadline:
        with urllib.request.urlopen(server + "/history/" + prompt_id, timeout=20) as response:
            history = json.loads(response.read()).get(prompt_id)
        if history:
            break
        if state.cancellation_requested():
            state.update(state="cancel_requested")
            token_path = Path(os.getenv("ALPINE_WORKER_CONTROL_TOKEN_FILE") or workdir.parent.parent / "config" / "engine-control-token")
            try:
                control_token = token_path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                control_token = ""
            cancel_prompt(server, prompt_id, control_token)
            queue = engine_json(server, "/queue")
            present = any(item[1] == prompt_id for item in queue_entries(queue))
            if not present:
                state.finish("cancelled", error="Calcul ComfyUI arrêté et confirmé.")
                raise ValueError("Calcul ComfyUI annulé.")
        time.sleep(2)
    if not history:
        raise ValueError("ComfyUI n’a pas terminé avant le délai maximal.")
    state.update(engine_stop_confirmed=True)
    if state.cancellation_requested():
        state.finish("cancelled", error="Calcul ComfyUI terminé après la demande d’annulation ; moteur libéré.")
        raise ValueError("Calcul ComfyUI annulé.")
    if parameters.get("lab_tool") in IMAGE_LAB_TEXT_TOOLS:
        # Labo image, description : le texte de PreviewAny (« 7 ») est lu dans l'historique, borné, puis rendu comme
        # un artefact texte ; aucun fichier du moteur n'est lu.
        texts = ((history.get("outputs") or {}).get("7") or {}).get("text")
        if not isinstance(texts, list) or not texts or not isinstance(texts[0], str) or not texts[0].strip():
            raise ValueError("ComfyUI n’a produit aucun texte pour ce job.")
        destination = workdir / "description.txt"
        destination.write_bytes(texts[0][:IMAGE_LAB_TEXT_MAX_CHARS].encode("utf-8"))
        result = {"prompt_id": prompt_id, "artifacts": [destination.name]}
        state.finish("completed", result=result)
        return result
    artifacts = []
    for item in ((history.get("outputs") or {}).get("7") or {}).get("images", []):
        if not isinstance(item, dict) or item.get("type") != "output":
            raise ValueError("Sortie ComfyUI invalide.")
        filename = safe_relative_name(item.get("filename"), basename=True)
        subfolder = safe_relative_name(item.get("subfolder"))
        source = (output_root / subfolder / filename).resolve()
        if source.parent != job_output or source.suffix.lower() != ".png" or not source.is_file():
            raise ValueError("ComfyUI a déclaré un fichier extérieur à ce job.")
        destination = workdir / f"image-{len(artifacts) + 1}.png"
        if destination.resolve().parent != workdir:
            raise ValueError("Artefact ComfyUI non autorisé.")
        shutil.copy2(source, destination)
        artifacts.append(destination.name)
    if not artifacts:
        raise ValueError("ComfyUI n’a produit aucune image pour ce job.")
    result = {"prompt_id": prompt_id, "artifacts": artifacts}
    state.finish("completed", result=result)
    return result


if __name__ == "__main__":
    try:
        job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        with RunnerLease(Path(sys.argv[2])):
            print(json.dumps(run_job(job, Path(sys.argv[2]))))
    except ValueError as error:
        raise SystemExit(str(error)) from None
