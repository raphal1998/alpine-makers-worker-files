# -*- coding: utf-8 -*-
"""Runner Worker du Labo image : restauration des visages (GFPGAN v1.4), agent 1.44.0.

Lancé par l'agent comme les autres runners (``python face_restore.py <job.json> <workdir>``, Python de l'agent) pour un
job du moteur image dont ``parameters.lab_tool`` vaut « face_restore ». Ce runner :

1. revalide la demande (``safety.validate_face_restore_parameters`` : type de calcul fermé, force 0–1, 1 à 8 visages) ;
2. localise le Python de l'environnement du moteur image (``<ComfyUI>/.venv``) et les deux fichiers de poids du catalogue
   épinglé (``installers/model_catalog.py``) — jamais un nom ou un chemin venu du site ;
3. lance ``face_restore_script.py`` en sous-processus (``-I -B`` : ni variables PYTHON*, ni site utilisateur, ni .pyc),
   demande et résultats dans le dossier du job, délai maximal, arrêt sur annulation ;
4. rend ``image-1.png`` et ``face_restore.json`` (compte rendu réécrit ici, champ par champ), ou seulement
   ``face_restore.json`` quand aucun visage n'a été trouvé.

Les messages d'échec sont ceux de ce fichier ; la sortie d'erreur du script n'est reprise que tronquée et sans chemin.
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
from safety import (IMAGE_LAB_FACE_RESTORE_MODELS, release_comfyui_vram_when_idle,  # noqa: E402
                    validate_face_restore_parameters)
from job_state import RunnerLease, RunnerState, TERMINAL, atomic_json  # noqa: E402
from process_control import process_identity, terminate_owned_process  # noqa: E402
from installers.model_catalog import COMFYUI_UTILITY_MODELS  # noqa: E402

ENGINE = "face-restore"
SCRIPT = Path(__file__).with_name("face_restore_script.py")
REQUEST_NAME = "face_restore_request.json"
RAW_REPORT_NAME = "face_restore_raw.json"
REPORT_ARTIFACT = "face_restore.json"
IMAGE_ARTIFACT = "image-1.png"
STDERR_NAME = "face_restore.stderr"
DEFAULT_TIMEOUT = 900
STOP_GRACE_SECONDS = 15
RAW_REPORT_MAX_BYTES = 64 * 1024
MISSING_ENGINE = ("Le moteur image (ComfyUI) n’est pas installé sur ce Worker : la restauration des visages utilise son "
                  "environnement Python. Installe-le dans Mes Workers → Installations.")
# Codes du script → message rédigé ici (le texte du script n'est jamais transmis tel quel).
SCRIPT_ERRORS = {
    "WEIGHTS_MISSING": "Fichier de poids absent : installe GFPGAN v1.4 et le détecteur de visages dans Mes Workers → Installations.",
    "WEIGHTS_INVALID": "Fichier de poids altéré ou non reconnu : répare GFPGAN v1.4 et le détecteur de visages dans Mes Workers → Installations.",
    "IMAGE_TOO_LARGE": "Image trop grande : au plus 16 mégapixels et 8192 px de côté.",
    "IMAGE_INVALID": "Image source illisible sur le Worker.",
    "DETECTION_FAILED": "La détection des visages a échoué sur cette image.",
    "LANDMARKS_INVALID": "Repères du visage inexploitables sur cette image.",
    "OUT_OF_MEMORY": "Mémoire insuffisante sur le Worker pour restaurer les visages de cette image.",
    "REQUEST_INVALID": "Demande de restauration refusée par le Worker.",
}
# Chemins dans une sortie d'erreur : entre guillemets (traceback « File "C:\…" »), ou nus — y compris un nom de dossier
# avec espaces (« C:\Users\Prénom Nom\… ») tant que le mot suivant contient encore un séparateur.
_QUOTED_PATH_PATTERN = re.compile(r"([\"'])[^\"'\n]*[\\/][^\"'\n]*\1")
_PATH_PATTERN = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|/(?:home|Users|root|tmp|var|mnt|opt)/)[^\s\"'<>|]*"
                           r"(?:\s+[^\s\"'<>|\\/]*[\\/][^\s\"'<>|]*)*")


class FaceRunnerError(ValueError):
    """Échec définitif et expliqué du calcul (l'état du job devient « failed »)."""


def sanitized_tail(text, limit=300):
    """Fin de la sortie d'erreur, sans chemin (dossier personnel, nom d'utilisateur), sur une ligne."""
    flat = " ".join(str(text or "").split())
    return _PATH_PATTERN.sub("<chemin>", _QUOTED_PATH_PATTERN.sub("<chemin>", flat))[-limit:]


def engine_root(workdir):
    return Path(os.getenv("COMFYUI_ROOT") or workdir.parent.parent / "components" / "comfyui").resolve()


def engine_python(root):
    """Python de l'environnement du moteur image ; jamais celui de l'agent (torch, spandrel, kornia n'y sont pas)."""
    relative = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    for folder in (".venv", "venv"):
        candidate = root.joinpath(folder, *relative)
        if candidate.is_file():
            return candidate
    raise FaceRunnerError(MISSING_ENGINE)


def weight_files(root):
    """{rôle: (chemin, SHA-256 attendu)} des deux fichiers du catalogue épinglé, présents et de la bonne taille."""
    models = (root / "models").resolve()
    found = {}
    for role, model_id in zip(("gfpgan", "detector"), IMAGE_LAB_FACE_RESTORE_MODELS):
        subdir, filename, size, _url, digest = COMFYUI_UTILITY_MODELS[("image_generation", model_id)]["files"][0]
        path = (models / subdir / filename).resolve()
        if models not in path.parents:
            raise FaceRunnerError("Emplacement de modèle non autorisé.")
        label = "GFPGAN v1.4" if role == "gfpgan" else "le détecteur de visages (YuNet)"
        if not path.is_file():
            raise FaceRunnerError(f"Installe {label} dans Mes Workers → Installations (Labo image · restauration du visage).")
        if path.stat().st_size != size:
            raise FaceRunnerError(f"Fichier de {label} incomplet ou altéré : répare-le dans Mes Workers → Installations.")
        found[role] = (path, digest)
    return found


def calculation_device():
    """« auto » (carte graphique si elle a de la place, sinon processeur) ; le propriétaire du Worker peut imposer le
    processeur avec ALPINE_FACE_RESTORE_DEVICE=cpu. Jamais une valeur venue du site."""
    return "cpu" if str(os.getenv("ALPINE_FACE_RESTORE_DEVICE") or "").strip().lower() == "cpu" else "auto"


def script_environment(workdir):
    """Le script ne télécharge rien : hub hors ligne, cache de torch dans le dossier du job."""
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith("PYTHON") and key.upper() not in {"VIRTUAL_ENV", "TORCH_HOME", "HF_HOME"}}
    environment.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TORCH_HOME=str(workdir / ".torch-home"),
                       PYTHONIOENCODING="utf-8")
    return environment


def _clean_report(raw):
    """Compte rendu publié : uniquement des nombres bornés et des valeurs connues, réécrits ici."""
    def count(name, limit=10_000):
        value = raw.get(name)
        if type(value) is not int or not 0 <= value <= limit:
            raise FaceRunnerError("Compte rendu de restauration invalide.")
        return value
    boxes = raw.get("boxes")
    if not isinstance(boxes, list) or len(boxes) > 8 or any(
            not isinstance(box, list) or len(box) != 4 or any(type(value) is not int or not -1 <= value <= 9000 for value in box) for box in boxes):
        raise FaceRunnerError("Compte rendu de restauration invalide.")
    seconds = raw.get("seconds")
    report = {"faces_detected": count("faces_detected"), "faces_restored": count("faces_restored", 8),
              "faces_too_small": count("faces_too_small"), "faces_over_limit": count("faces_over_limit"),
              "boxes": boxes, "device": "cuda" if raw.get("device") == "cuda" else "cpu",
              "cpu_fallback": raw.get("cpu_fallback") is True, "width": count("width", 8192), "height": count("height", 8192),
              "seconds": round(float(seconds), 2) if type(seconds) in (int, float) and 0 <= seconds < 1e6 else None}
    if report["faces_restored"] != len(boxes):
        raise FaceRunnerError("Compte rendu de restauration invalide.")
    return report


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


def _stop(process, pid, identity):
    """Arrête le script (processus de ce runner ou, après reprise, processus d'identité vérifiée) ; True si confirmé."""
    if process is not None:
        process.terminate()
        try:
            process.wait(STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(STOP_GRACE_SECONDS)
        return True
    return bool(identity) and terminate_owned_process(pid, identity)


def _run(job, workdir, state):
    if state.value["state"] == "completed":
        return state.value["result"]
    if state.value["state"] in {"failed", "cancelled"}:
        raise ValueError(state.value.get("error") or "Exécution déjà terminée.")
    try:
        parameters = validate_face_restore_parameters(job.get("parameters"))
    except ValueError as error:
        raise FaceRunnerError(str(error)) from None
    settings = parameters["face_restore"]
    source = Path(str(parameters["input_files"][0].get("path") or "")).resolve()
    if not source.is_file() or workdir not in source.parents:
        raise FaceRunnerError("L’image source de ce job n’a pas été copiée sur le Worker.")
    output, raw_report = workdir / IMAGE_ARTIFACT, workdir / RAW_REPORT_NAME
    process = None
    if state.value["state"] == "preparing":
        root = engine_root(workdir)
        python = engine_python(root)
        weights = weight_files(root)
        if not SCRIPT.is_file():
            raise FaceRunnerError("Le script de restauration manque sur ce Worker : mets à jour le Worker puis relance.")
        if state.cancellation_requested():
            state.finish("cancelled", error="Restauration annulée avant lancement.")
            raise ValueError("Restauration des visages annulée.")
        for stale in (output, raw_report):
            stale.unlink(missing_ok=True)
        atomic_json(workdir / REQUEST_NAME, {
            "workdir": str(workdir), "input": str(source), "output": str(output), "report": str(raw_report),
            "gfpgan": str(weights["gfpgan"][0]), "gfpgan_sha256": weights["gfpgan"][1],
            "detector": str(weights["detector"][0]), "detector_sha256": weights["detector"][1],
            "strength": float(settings["strength"]), "max_faces": int(settings["max_faces"]), "device": calculation_device()})
        # Moteur image au repos : il rend la mémoire graphique avant le calcul (sinon le script se replie sur le processeur).
        release_comfyui_vram_when_idle()
        state.submit_intent(engine=ENGINE)
        with (workdir / STDERR_NAME).open("w", encoding="utf-8") as stderr:
            try:
                process = subprocess.Popen([str(python), "-I", "-B", str(SCRIPT), str(workdir / REQUEST_NAME)],
                                           cwd=str(workdir), env=script_environment(workdir), stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=stderr,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            except OSError:
                state.finish("failed", error="Lancement du calcul de restauration impossible sur ce Worker.")
                raise
        identity = process_identity(process.pid)
        state.update(state="running", pid=process.pid, process_identity=identity, started_at=time.time(), progress=10,
                     message="Restauration des visages en cours sur le Worker…")
    pid, identity = state.value.get("pid"), state.value.get("process_identity")
    if not pid:
        raise ValueError("Lancement de la restauration incertain ; aucun nouveau calcul ne sera lancé.")
    # Après une reprise (agent redémarré), le délai court toujours depuis le lancement réel.
    deadline = float(state.value.get("started_at") or time.time()) + int(parameters.get("timeout_seconds") or DEFAULT_TIMEOUT)
    cancelled = timed_out = False
    while (process.poll() is None) if process is not None else (identity is not None and process_identity(pid) == identity):
        if state.cancellation_requested() and not cancelled:
            state.update(state="cancel_requested")
            cancelled = True
        if cancelled or time.time() > deadline:
            timed_out = not cancelled
            if not _stop(process, pid, identity):
                raise ValueError("Impossible de confirmer l’arrêt du calcul de restauration ; aucun autre processus arrêté.")
            break
        time.sleep(0.25)
    if process is None and identity is None:
        raise ValueError("Identité du processus de restauration inconnue ; arrêt non confirmé.")
    if process is not None:
        process.wait()
    state.update(engine_stop_confirmed=True)
    if cancelled or state.cancellation_requested():
        state.finish("cancelled", error="Arrêt du calcul de restauration confirmé.")
        raise ValueError("Restauration des visages annulée.")
    if timed_out:
        output.unlink(missing_ok=True)
        raise FaceRunnerError("La restauration des visages a dépassé le délai maximal ; calcul arrêté.")
    raw = {}
    if raw_report.is_file() and raw_report.stat().st_size <= RAW_REPORT_MAX_BYTES:
        try:
            value = json.loads(raw_report.read_text(encoding="utf-8"))
            raw = value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            raw = {}
    if raw.get("ok") is not True:
        output.unlink(missing_ok=True)
        code = str(raw.get("code") or "")
        if code in SCRIPT_ERRORS:
            raise FaceRunnerError(SCRIPT_ERRORS[code])
        try:
            tail = sanitized_tail((workdir / STDERR_NAME).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            tail = ""
        exit_code = process.returncode if process is not None else None
        detail = f" (code {exit_code})" if exit_code is not None else ""
        raise FaceRunnerError(f"Le calcul de restauration des visages a échoué{detail}." + (f" Détail : {tail}" if tail else ""))
    report = _clean_report(raw)
    artifacts = [REPORT_ARTIFACT]
    if report["faces_restored"]:
        with output.open("rb") as handle:
            if handle.read(8) != b"\x89PNG\r\n\x1a\n":
                raise FaceRunnerError("Le calcul de restauration n’a pas rendu d’image PNG.")
        artifacts.insert(0, IMAGE_ARTIFACT)
    else:
        output.unlink(missing_ok=True)       # aucun visage : aucune image, le compte rendu le dit
    atomic_json(workdir / REPORT_ARTIFACT, report)
    result = {"artifacts": artifacts, "faces": report["faces_restored"]}
    state.update(progress=100, message="Restauration des visages terminée.")
    state.finish("completed", result=result)
    return result


if __name__ == "__main__":
    try:
        job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        with RunnerLease(Path(sys.argv[2])):
            print(json.dumps(run_job(job, Path(sys.argv[2])), ensure_ascii=False))
    except ValueError as error:
        raise SystemExit(str(error)) from None
