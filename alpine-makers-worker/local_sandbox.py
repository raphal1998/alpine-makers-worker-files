"""Opt-in isolation for the separate local compliance-test Worker only."""
import json
import os
import re
import socket
import urllib.parse
from pathlib import Path

PORTS = {"image_generation": 28188, "ai3d": 28189, "model-studio": 25064, "printguard": 28000}
PRINTGUARD_MEDIA_PORTS = {"api": 29997, "rtsp": 28554, "rtmp": 21935, "hls": 28888}


def enabled():
    return os.getenv("ALPINE_LOCAL_SANDBOX") == "1"


def activate_from_config(root):
    """Persist the test boundary when a user starts its Worker tools manually."""
    root = Path(root).resolve()
    if enabled():
        validate_root(root)
        return
    if (root / "TEST_SANDBOX_ONLY.txt").is_file():
        os.environ["ALPINE_LOCAL_SANDBOX"] = "1"
        os.environ["ALPINE_SANDBOX_WORKER_ROOT"] = str(root)
        os.environ["ALPINE_TEST_DASHBOARD_PORT"] = "5157"
        return
    config_file = root / "config.json"
    if not config_file.is_file() or config_file.stat().st_size > 1024 * 1024:
        return
    config = json.loads(config_file.read_text(encoding="utf-8-sig"))
    if isinstance(config, dict) and config.get("local_sandbox") is True:
        os.environ["ALPINE_LOCAL_SANDBOX"] = "1"
        os.environ["ALPINE_SANDBOX_WORKER_ROOT"] = str(root)
        os.environ["ALPINE_TEST_DASHBOARD_PORT"] = "5157"


def validate_root(root):
    if not enabled():
        return
    expected = os.getenv("ALPINE_SANDBOX_WORKER_ROOT", "")
    if not expected or Path(root).resolve() != Path(expected).resolve():
        raise ValueError("Worker TEST : ALPINE_SANDBOX_WORKER_ROOT doit désigner exactement cette installation isolée.")


def validate_server(value):
    if not enabled():
        return
    parsed = urllib.parse.urlsplit(str(value))
    expected_port = int(os.getenv("ALPINE_TEST_DASHBOARD_PORT", "5157"))
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port != expected_port or parsed.path not in {"", "/"}:
        raise ValueError("Worker TEST : seule la dashboard locale de test est autorisée ; serveur production refusé.")


def environment(root, base):
    env = dict(base)
    if not enabled():
        return env
    validate_root(root)
    for variable in ("COMFYUI_ROOT", "HUNYUAN3D_ROOT", "PRINTGUARD_EXE", "FREECAD_CMD", "FREECAD_ROOT", "ORCA_DIR",
                     "PRINTER_IP", "ACCESS_CODE", "BAMBU_ACCESS_CODE", "BAMBU_SERIAL"):
        env.pop(variable, None)
    env.update({"COMFYUI_PORT": str(PORTS["image_generation"]),
                "HUNYUAN_WORKER_URL": f"http://127.0.0.1:{PORTS['ai3d']}",
                "WORKER_ORCA_PORT": str(PORTS["model-studio"]),
                "DATA_DIR": str(Path(root) / "data" / "printguard")})
    # FreeCAD supports these application-specific locations. Do not redirect
    # USERPROFILE/HOME globally: winget and system dependency installs use them.
    for name, relative in (("FREECAD_USER_HOME", "data/freecad"), ("FREECAD_USER_DATA", "data/freecad/user"),
                           ("FREECAD_USER_TEMP", "temp/freecad")):
        directory = Path(root) / relative
        for candidate in (directory, *directory.parents):
            if candidate == Path(root):
                break
            if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()) or not candidate.resolve().is_relative_to(Path(root).resolve()):
                raise ValueError("Réglages FreeCAD TEST liés hors du Worker.")
        directory.mkdir(parents=True, exist_ok=True)
        env[name] = str(directory)
    return env


def refuse_unsafe_operation(command, payload=None):
    """Keep the independent TEST identity, not a preview-only feature switch.

    Hardware, calculations and system component installation are real operations
    when explicitly requested by the user. Their ordinary auth, input, licence
    and process-ownership checks still run in the agent/installer.
    """
    if not enabled():
        return
    expected = os.getenv("ALPINE_SANDBOX_WORKER_ROOT", "")
    if not expected:
        raise ValueError("Worker TEST : racine isolée absente ; opération refusée.")
    validate_root(expected)


def printguard_environment(root, component_root, base):
    """Configure PrintGuard 2.4's documented PORT and bundled MediaMTX endpoints.

    Only the child environment and a private config are changed. Do not connect
    to or stop an existing server to reclaim a port; an occupied test port is a
    targeted error. The source/binary licensing preflight remains independent.
    """
    env = dict(base)
    if not enabled():
        return env
    validate_root(root)
    root, component_root = Path(root).resolve(), Path(component_root).resolve()
    if not component_root.is_relative_to(root / "components"):
        raise ValueError("PrintGuard TEST doit être installé dans les composants de ce Worker.")
    metadata = component_root / "_internal" / "printguard-2.4.0.dist-info" / "METADATA"
    if not metadata.is_file() or metadata.stat().st_size > 65536 or not metadata.resolve().is_relative_to(component_root):
        raise ValueError("Version PrintGuard non vérifiée pour les ports TEST ; aucun service lancé.")
    metadata_text = metadata.read_text(encoding="utf-8")
    if not re.search(r"(?mi)^Name: printguard\s*$", metadata_text) or not re.search(r"(?m)^Version: 2\.4\.0\s*$", metadata_text):
        raise ValueError("Version PrintGuard non vérifiée pour les ports TEST ; aucun service lancé.")
    # Work with the known bundled schema, not an inherited external config.
    source = component_root / "_internal" / "mediamtx.yml"
    if not source.is_file():
        source = component_root / "mediamtx.yml"
    if not source.is_file() or source.stat().st_size > 65536 or not source.resolve().is_relative_to(component_root):
        raise ValueError("Configuration MediaMTX locale absente ou non vérifiable ; répare PrintGuard.")
    content = source.read_text(encoding="utf-8-sig")
    replacements = {"apiAddress": PRINTGUARD_MEDIA_PORTS["api"], "rtspAddress": PRINTGUARD_MEDIA_PORTS["rtsp"],
                    "rtmpAddress": PRINTGUARD_MEDIA_PORTS["rtmp"], "hlsAddress": PRINTGUARD_MEDIA_PORTS["hls"]}
    for key, port in replacements.items():
        content, count = re.subn(rf"(?m)^{key}:[^\r\n]*$", f"{key}: 127.0.0.1:{port}", content)
        if count != 1:
            raise ValueError("Version de configuration MediaMTX non prise en charge pour l’isolation TEST.")
    # Unknown optional listeners must not silently use their standard ports.
    for key in ("webrtc", "srt"):
        if not re.search(rf"(?m)^{key}:\s*(?:no|false)\s*$", content):
            raise ValueError("Configuration MediaMTX TEST inattendue ; aucun service lancé.")
    for key in ("metrics", "pprof", "playback"):
        if re.search(rf"(?m)^{key}:\s*(?:yes|true)\s*$", content):
            raise ValueError("Service MediaMTX supplémentaire non isolé ; aucun service lancé.")
    for port in (PORTS["printguard"], *PRINTGUARD_MEDIA_PORTS.values()):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind(("127.0.0.1", port))
        except OSError as error:
            raise ValueError(f"Port PrintGuard TEST {port} occupé ; aucun processus existant n’est arrêté.") from error
    data = root / "data" / "printguard"
    target = data / "mediamtx-test.yml"
    for candidate in (root / "data", data, target):
        if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()) or not candidate.resolve().is_relative_to(root):
            raise ValueError("Configuration PrintGuard TEST liée hors du Worker.")
    data.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    for name in tuple(env):
        if name.startswith("MTX_") or name in {"MEDIAMTX_BINARY", "MEDIAMTX_CONFIG", "STATIC_DIR", "LOG_FILE", "UPDATE_ASSET"}:
            env.pop(name, None)
    env.update({"PORT": str(PORTS["printguard"]), "DATA_DIR": str(data), "LOG_FILE": str(data / "printguard.log"),
                "MEDIAMTX_CONFIG": str(target), "MEDIAMTX_API": f"http://127.0.0.1:{PRINTGUARD_MEDIA_PORTS['api']}",
                "MEDIAMTX_RTSP": f"rtsp://127.0.0.1:{PRINTGUARD_MEDIA_PORTS['rtsp']}",
                "MEDIAMTX_HLS": f"http://127.0.0.1:{PRINTGUARD_MEDIA_PORTS['hls']}"})
    return env
