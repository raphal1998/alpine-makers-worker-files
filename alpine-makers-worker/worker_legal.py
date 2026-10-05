"""Targeted, versioned legal preflight for the test Worker.

This is not DRM: stop/uninstall and unrestricted local runtimes do not contact
the dashboard. An installation is checked before any download or engine stop.
The authenticated server owns account permissions; local metadata is not a grant.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

if __package__:
    from .identity import configure as configure_identity
    from .job_state import atomic_json
    from .safety import is_link, protect_credentials, validate_server_url
else:
    from identity import configure as configure_identity
    from job_state import atomic_json
    from safety import is_link, protect_credentials, validate_server_url

POLICY_VERSION = "2026-10-03.3"
CHECK_PATH = "/api/worker-protocol/legal/check"
INSTALL_ACTIONS = frozenset({"install", "update", "repair"})
ALIASES = {"hunyuan3d": "ai3d", "comfyui": "image_generation", "orca": "model-studio",
           "freecad": "converter", "laser-engine": "laser-studio"}
OFFICIAL_HOSTS = frozenset({"github.com", "raw.githubusercontent.com", "huggingface.co", "www.gnu.org", "gnu.org",
                            "www.ultralytics.com", "docs.nvidia.com", "www.python.org", "docs.python.org",
                            "www.freecad.org", "freecad.org", "comfy.org", "www.comfy.org", "stability.ai",
                            "blackforestlabs.ai", "bfl.ai", "freedevproject.org", "www.apache.org", "opensource.org",
                            "developer.nvidia.com", "civitai.com"})
MAX_NOTICE_BYTES = 256 * 1024
MAX_EVIDENCE_INDEX_BYTES = 2 * 1024 * 1024


class LegalCheckError(RuntimeError):
    pass


def _payload_ids(payload):
    payload = payload if isinstance(payload, dict) else {}
    parameters = payload.get("parameters") if isinstance(payload.get("parameters"), dict) else {}
    values = [payload.get(key) for key in ("tool_id", "component_id", "model_id")]
    values.extend(parameters.get(key) for key in ("model_id", "model", "checkpoint", "model_name"))
    ids = {str(value).strip().lower() for value in values if isinstance(value, str) and value.strip()}
    return {ALIASES.get(value, value) for value in ids}


def _checked_job_payload(command, payload):
    if command not in {"job.run", "job.create", "generation"} or payload.get("tool_id") != "image_generation":
        return payload
    parameters = payload.get("parameters") if isinstance(payload.get("parameters"), dict) else {}
    workflow = parameters.get("workflow") if isinstance(parameters.get("workflow"), dict) else {}
    primary = workflow.get("1") if isinstance(workflow.get("1"), dict) else {}
    inputs = primary.get("inputs") if isinstance(primary.get("inputs"), dict) else {}
    # Labo image (agent 1.39.0) : le chargeur du nœud « 1 » nomme le modèle utilitaire (agrandisseur, profondeur,
    # détourage, description) ; il doit correspondre au modèle déclaré par le job, comme un checkpoint de génération.
    filename = (inputs.get("ckpt_name") or inputs.get("unet_name") or inputs.get("model_name") or inputs.get("bg_removal_name")
                or inputs.get("clip_name"))
    if not filename:
        return payload  # Existing workflow validation handles malformed graphs.
    if __package__:
        from .installers.model_catalog import COMFYUI_CATALOG, COMFYUI_BUNDLES, COMFYUI_UTILITY_MODELS
    else:
        from installers.model_catalog import COMFYUI_CATALOG, COMFYUI_BUNDLES, COMFYUI_UTILITY_MODELS
    known = {spec[0]: key[1] for key, spec in COMFYUI_CATALOG.items()}
    known.update({spec[0][1]: key[1] for key, spec in COMFYUI_BUNDLES.items()})
    known.update({spec["files"][0][1]: key[1] for key, spec in COMFYUI_UTILITY_MODELS.items()})
    selected = known.get(str(filename))
    declared = payload.get("model_id")
    if declared and ((selected and selected != declared) or (not selected and declared in known.values())):
        raise LegalCheckError("Le modèle du workflow ne correspond pas au modèle autorisé ; aucun calcul lancé.")
    return {**payload, "model_id": selected or declared or str(filename)}


def requires_check(command, payload):
    kind, _, action = str(command).partition(".")
    if action in {"stop", "uninstall", "cancel"}:
        return False
    if kind in {"tool", "component", "model"} and action in INSTALL_ACTIONS:
        return True
    if command not in {"tool.start", "job.run", "job.create", "generation"}:
        return False
    ids = _payload_ids(payload)
    # The current AI3D service itself imports Hunyuan, including for alternative
    # model runners. Do not advertise an independent backend that does not exist.
    policy = json.loads(Path(__file__).with_name("legal_policy.json").read_text(encoding="utf-8"))
    if policy.get("policy_version") != POLICY_VERSION:
        raise LegalCheckError("Registre juridique local incompatible : mets à jour le Worker.")
    if command in {"job.run", "job.create", "generation"} and ids == {"image_generation"}:
        return True  # An unspecified/manual checkpoint is not a grant.
    for identifier in ids:
        rule = next((r for r in policy["rules"] if identifier in r.get("ids", [])
                     or any(identifier.startswith(p) for p in r.get("prefixes", []))), {})
        if rule.get("status") != "notice_required":
            return True
    return not ids


def check_operation(root, command, payload, *, request=None):
    payload = payload if isinstance(payload, dict) else {}
    payload = _checked_job_payload(command, payload)
    if not requires_check(command, payload):
        return {"allowed": True, "policy_version": POLICY_VERSION, "components": []}
    try:
        if request is None:
            response = _request(root, {"command": command, "payload": payload})
        else:
            response = request(CHECK_PATH, "POST", {"command": command, "payload": payload})
    except Exception as error:
        raise LegalCheckError("Vérification des conditions indisponible. Réessaie depuis Mes Workers → Installations avant de lancer cette opération.") from error
    if not isinstance(response, dict) or response.get("allowed") is not True:
        decision = response.get("decision") if isinstance(response, dict) else {}
        decision = decision if isinstance(decision, dict) else {}
        reason = str(decision.get("reason") or (response.get("reason") if isinstance(response, dict) else "") or "Autorisation ou acceptation requise pour ce composant.")[:1500]
        raise LegalCheckError(reason)
    if response.get("policy_version") != POLICY_VERSION:
        raise LegalCheckError("La version des conditions a changé. Mets à jour le Worker avant cette opération.")
    return response


def _request(root, payload):
    root = Path(root).resolve()
    config = json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict) or not config.get("worker_id") or not config.get("token"):
        raise LegalCheckError("Associe ce Worker au dashboard avant une installation.")
    server = validate_server_url(config.get("server_url"))
    configure_identity(root, config)
    request = protect_credentials(urllib.request.Request(server + CHECK_PATH,
        data=json.dumps(payload).encode("utf-8"), method="POST", headers={
            "Accept": "application/json", "Content-Type": "application/json",
            # Devant le site, Cloudflare refuse le User-Agent par défaut d'urllib (403, erreur 1010) :
            # sans cet en-tête, toute installation lancée par l'installateur échouait au précontrôle.
            "User-Agent": "AlpineWorker/legal-check",
            "Authorization": "Bearer " + str(config["token"]), "X-Worker-ID": str(config["worker_id"]),
        }))
    with urllib.request.urlopen(request, timeout=15) as response:
        content = response.read(MAX_NOTICE_BYTES + 1)
    if len(content) > MAX_NOTICE_BYTES:
        raise LegalCheckError("Réponse de conformité trop volumineuse.")
    return json.loads(content)


def official_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("URL officielle invalide.")
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in OFFICIAL_HOSTS or parsed.username
            or parsed.password or parsed.port not in {None, 443} or parsed.fragment):
        raise ValueError("Source de notice non autorisée.")
    return value


def _notice_directory(root, component_id):
    if not isinstance(component_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", component_id):
        raise ValueError("Identifiant de notice invalide.")
    root = Path(root).resolve()
    directory = root / "licenses" / component_id
    for path in (root / "licenses", directory):
        if is_link(path) or not path.resolve().is_relative_to(root):
            raise ValueError("Dossier de notices hors du Worker.")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _verified_evidence(component_id):
    """Read only reviewed, package-shipped text; URLs never trigger downloads."""
    package = Path(__file__).resolve().parent
    index_path = package / "legal_evidence.json"
    if is_link(index_path) or not index_path.is_file() or index_path.stat().st_size > MAX_EVIDENCE_INDEX_BYTES:
        raise ValueError("Dossier de notices Worker absent ou invalide ; répare le paquet Worker.")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if index.get("policy_version") != POLICY_VERSION:
        raise ValueError("Version du dossier documentaire incompatible avec le Worker.")
    entry = (index.get("components") or {}).get(component_id)
    if not isinstance(entry, dict):
        raise ValueError("Notice documentaire absente pour ce composant ; aucune permission n’est présumée.")
    descriptors = list(entry.get("evidence_files") or [])
    if isinstance(entry.get("notice_file"), dict):
        descriptors.append(entry["notice_file"])
    if not 1 <= len(descriptors) <= 32:
        raise ValueError("Nombre de documents de licence invalide.")
    verified = []
    total = 0
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            raise ValueError("Référence de licence invalide.")
        relative = str(descriptor.get("local_path") or "")
        if not re.fullmatch(r"legal_sources/[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.txt", relative):
            raise ValueError("Document de licence hors du paquet autorisé.")
        source = package / relative
        if is_link(package / "legal_sources") or is_link(source) or not source.resolve().is_relative_to(package):
            raise ValueError("Document de licence lié hors du Worker.")
        if not source.is_file() or source.stat().st_size > MAX_NOTICE_BYTES:
            raise ValueError("Document de licence absent ou trop volumineux.")
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        total += len(content)
        if digest != descriptor.get("sha256") or len(content) != descriptor.get("bytes") or total > MAX_EVIDENCE_INDEX_BYTES:
            raise ValueError("Empreinte du document de licence incorrecte ; notices existantes conservées.")
        verified.append((descriptor, content, digest))
    return entry, verified


def _record_evidence(directory, entry, verified):
    destination = directory / "evidence"
    if is_link(destination) or not destination.resolve().is_relative_to(directory.resolve()):
        raise ValueError("Dossier de licences lié hors du Worker.")
    destination.mkdir(parents=True, exist_ok=True)
    installed = []
    for descriptor, content, digest in verified:
        target = destination / (digest + ".txt")
        if is_link(target):
            raise ValueError("Fichier de licence lié ; copie refusée.")
        if target.exists():
            if target.stat().st_size != len(content) or target.read_bytes() != content:
                raise ValueError("Licence existante différente ; aucun remplacement ni effacement.")
        else:
            # A content-addressed filename retains older notices when a future
            # reviewed policy changes. Never erase or overwrite upstream files.
            with target.open("xb") as output:
                output.write(content)
        installed.append({**descriptor, "installed_path": "evidence/" + target.name})
    metadata = {**entry, "policy_version": POLICY_VERSION, "installed_documents": installed,
                "scope": "documentary evidence only; not complete corresponding binary/model sources or a legal permission"}
    target = directory / ("evidence-index." + POLICY_VERSION + ".json")
    if is_link(target):
        raise ValueError("Index de licences lié ; écriture refusée.")
    atomic_json(target, metadata)
    return metadata


def record_notices(root, decision):
    """Persist bounded attribution and hash-verified, package-shipped texts.

Upstream LICENSE/NOTICE files remain with their original downloads. This small
index preserves attribution and exact official links; it does not claim that a
link replaces the full license text/source obligations of a redistributed work.
"""
    components = decision.get("components") if isinstance(decision, dict) else []
    for component in components[:100] if isinstance(components, list) else []:
        if not isinstance(component, dict):
            raise ValueError("Métadonnées de notice invalides.")
        identifier = component.get("id")
        directory = _notice_directory(root, identifier)
        evidence, verified = _verified_evidence(identifier)
        _record_evidence(directory, evidence, verified)
        record = {key: str(component.get(key) or "")[:2000] for key in
                  ("id", "name", "publisher", "license", "status", "revision", "artifact_sha256", "policy_digest")}
        for key in ("official_url", "license_url", "source_url"):
            value = component.get(key)
            if value:
                record[key] = official_url(value)
        record.update({"policy_version": decision.get("policy_version"), "recorded_at": int(time.time()),
                       "scope": "attribution-index plus verified documentary texts; retain upstream full licenses and corresponding sources",
                       "evidence_index": "evidence-index." + POLICY_VERSION + ".json"})
        target = directory / "provenance.json"
        if is_link(target):
            raise ValueError("Le fichier de provenance est un lien.")
        atomic_json(target, record)
        text = "\n".join(f"{key}: {value}" for key, value in record.items()) + "\n"
        target = directory / "NOTICE.txt"
        if is_link(target):
            raise ValueError("Le fichier de notice est un lien.")
        target.write_text(text, encoding="utf-8")
