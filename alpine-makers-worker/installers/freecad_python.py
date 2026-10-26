# -*- coding: utf-8 -*-
"""Python embarqué de FreeCAD sur le Worker : détection, sonde OpenCAMLib (ocl) et commandes pip.

Tout ce qui touche OpenCAMLib se fait DANS l'interpréteur livré avec FreeCAD (``bin/python.exe`` à côté de
FreeCADCmd sous Windows), jamais dans le Python du Worker ni dans un ``.venv`` de composant : l'atelier CAM
de FreeCAD importe ``ocl`` (ou ``opencamlib`` en repli, ``Mod/CAM/Path/Op/Surface.py`` et ``Waterline.py``)
depuis son propre ``site-packages``.

Module pur en dehors de ``probe()`` (qui lance le Python de FreeCAD avec un petit script) : testable avec un
faux dossier FreeCAD et un lanceur simulé. Aucune valeur inventée : la compatibilité d'une roue n'est jamais
présumée, c'est pip qui répond et son refus est traduit en consigne lisible (``explain_pip_failure``).

Constaté sur le Worker de référence (FreeCAD 1.1.3 Windows, Python 3.11.14 embarqué, pip 25.3) :
``opencamlib 2022.12.18`` est déjà fourni dans ``bin/Lib/site-packages`` (paquet conda, licence LGPL-2.1,
``import opencamlib`` fonctionne, ``import ocl`` échoue ; FreeCAD se rabat sur ``opencamlib``). La version épinglée
ici est celle de la distribution pip (dist-info) ; le module lui-même répond ``ocl.version()`` = 2023.01.11
(audit docs/cam_audit_freecad_1_1_3.md §1) : les deux numéros désignent le même binaire.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

OPENCAMLIB_PACKAGE = "opencamlib"
# Seule version vérifiée (celle que FreeCAD 1.1.3 Windows embarque) : épinglée, jamais « la dernière ».
OPENCAMLIB_VERSION = "2022.12.18"
OPENCAMLIB_LICENSE = "LGPL-2.1-or-later"
OPENCAMLIB_PROJECT_URL = "https://github.com/aewallin/opencamlib"
# Noms de module que l'atelier CAM essaie, dans cet ordre (Surface.py / Waterline.py de FreeCAD 1.x).
OCL_MODULE_NAMES = ("ocl", "opencamlib")
PROBE_MARKER = "ALPINE_FREECAD_PROBE="
PROBE_TIMEOUT = 120
FREECAD_MIN_VERSION = (1, 0, 0)
# Dossier (sous components/opencamlib/) où déposer une roue téléchargée à la main pour une installation hors ligne.
WHEELS_DIRNAME = "wheels"
_FREECAD_FOLDER = re.compile(r"FreeCAD[ _-]?(\d{1,3}\.\d{1,3}(?:\.\d{1,4})?)", re.IGNORECASE)
_WHEEL_NAME = re.compile(r"(?i)^opencamlib-(?P<version>[0-9][0-9A-Za-z.]*)-(?P<tags>cp\d+-[^-]+-[^-]+)\.whl$")

# Exécuté par le Python de FreeCAD (-I : ni PYTHONPATH ni site utilisateur, seulement son site-packages).
PROBE_SCRIPT = (
    "import importlib, importlib.util, json, platform, struct, sys\n"
    "report = {'ok': True, 'python_version': platform.python_version(), 'executable': sys.executable,\n"
    "          'bits': struct.calcsize('P') * 8, 'platform': sys.platform, 'modules': {}, 'pip': {'available': False, 'version': None}}\n"
    "for name in ('ocl', 'opencamlib'):\n"
    "    entry = {'importable': False, 'version': None, 'file': None, 'error': ''}\n"
    "    try:\n"
    "        module = importlib.import_module(name)\n"
    "        entry['importable'] = True\n"
    "        entry['file'] = getattr(module, '__file__', None)\n"
    "        entry['version'] = getattr(module, '__version__', None)\n"
    "    except BaseException as error:\n"
    "        entry['error'] = (type(error).__name__ + ': ' + str(error))[:300]\n"
    "    report['modules'][name] = entry\n"
    "try:\n"
    "    from importlib import metadata\n"
    "    report['distribution_version'] = metadata.version('opencamlib')\n"
    "except BaseException:\n"
    "    report['distribution_version'] = None\n"
    "try:\n"
    "    import pip\n"
    "    report['pip'] = {'available': True, 'version': getattr(pip, '__version__', None)}\n"
    "except BaseException as error:\n"
    "    report['pip'] = {'available': False, 'version': None, 'error': (type(error).__name__ + ': ' + str(error))[:200]}\n"
    f"print({PROBE_MARKER!r} + json.dumps(report))\n"
)


def freecad_python(freecadcmd):
    """Interpréteur Python livré avec FreeCAD, à côté de FreeCADCmd ; None s'il n'y est pas.

    Windows : ``bin/python.exe`` (installateur officiel et winget). Linux/AppImage : ``python3`` ou ``python``
    du même dossier. Le Python du Worker (``sys.executable``) n'est jamais un repli."""
    if not freecadcmd:
        return None
    folder = Path(str(freecadcmd)).resolve().parent
    names = ("python.exe",) if os.name == "nt" else ("python3", "python")
    for name in names:
        candidate = folder / name
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return None


def freecad_version_from_path(freecadcmd):
    """Version lue dans le nom du dossier d'installation (« FreeCAD 1.1 » → « 1.1 »), sans lancer FreeCAD ; '' sinon.

    La version complète (1.1.3) vient de ``capabilities`` (FreeCADCmd lui-même) : ici seulement un repère."""
    for part in reversed(Path(str(freecadcmd or "")).parts):
        match = _FREECAD_FOLDER.search(part)
        if match:
            return match.group(1)
    return ""


def version_tuple(value):
    try:
        return tuple(int(part) for part in str(value or "").strip().split("."))
    except ValueError:
        return ()


def freecad_supported(version):
    """FreeCAD ≥ 1.0 (atelier « CAM », modules ``Path.Op.*``) ; une version inconnue n'est pas refusée ici."""
    parts = version_tuple(version)
    return not parts or parts >= FREECAD_MIN_VERSION[:len(parts)]


def parse_probe_output(output):
    """Dernière ligne ``ALPINE_FREECAD_PROBE=`` d'une sortie → rapport ; sinon rapport d'échec explicite."""
    lines = [line for line in str(output or "").splitlines() if line.startswith(PROBE_MARKER)]
    if not lines:
        tail = " ".join(str(output or "").split())[-400:]
        return {"ok": False, "error": "Le Python de FreeCAD n'a pas rendu de rapport" + (f" : {tail}" if tail else "."),
                "modules": {}, "pip": {"available": False, "version": None}}
    try:
        report = json.loads(lines[-1][len(PROBE_MARKER):])
    except ValueError as error:
        return {"ok": False, "error": f"Rapport du Python de FreeCAD illisible : {error}", "modules": {},
                "pip": {"available": False, "version": None}}
    if not isinstance(report, dict):
        return {"ok": False, "error": "Rapport du Python de FreeCAD invalide.", "modules": {}, "pip": {"available": False, "version": None}}
    report.setdefault("ok", True)
    report.setdefault("modules", {})
    report.setdefault("pip", {"available": False, "version": None})
    return report


def _default_runner(arguments, timeout):
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "VIRTUAL_ENV"))}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    options = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
    result = subprocess.run([str(a) for a in arguments], capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=timeout, check=False, env=env, stdin=subprocess.DEVNULL, **options)
    return (result.stdout or "") + "\n" + (result.stderr or "")


def probe(python, *, runner=None, timeout=PROBE_TIMEOUT):
    """Lance ``PROBE_SCRIPT`` avec le Python de FreeCAD ; ``runner(arguments, timeout) -> texte`` remplaçable (tests).

    Ne lève jamais pour une sortie inattendue : le rapport dit ``ok: False`` avec la raison."""
    python = Path(str(python))
    if not python.is_file():
        return {"ok": False, "error": f"Python de FreeCAD introuvable : {python.name} absent du dossier de FreeCADCmd.",
                "modules": {}, "pip": {"available": False, "version": None}}
    run = runner or _default_runner
    try:
        output = run([str(python), "-I", "-c", PROBE_SCRIPT], timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Le Python de FreeCAD n'a pas répondu en {timeout} s.", "modules": {},
                "pip": {"available": False, "version": None}}
    except OSError as error:
        return {"ok": False, "error": f"Impossible de lancer le Python de FreeCAD : {error}", "modules": {},
                "pip": {"available": False, "version": None}}
    report = parse_probe_output(output)
    report["python"] = str(python)
    return report


def ocl_state(report):
    """Ce que l'atelier CAM verra : ``{present, module, version, error}`` (module = premier nom importable)."""
    modules = report.get("modules") if isinstance(report.get("modules"), dict) else {}
    errors = []
    for name in OCL_MODULE_NAMES:
        entry = modules.get(name) if isinstance(modules.get(name), dict) else {}
        if entry.get("importable"):
            version = entry.get("version") or report.get("distribution_version")
            return {"present": True, "module": name, "version": str(version) if version else None, "error": ""}
        if entry.get("error"):
            errors.append(f"{name} : {entry['error']}")
    return {"present": False, "module": None, "version": str(report.get("distribution_version")) if report.get("distribution_version") else None,
            "error": " ; ".join(errors)[:600]}


def python_tag(report):
    """« cp311-win_amd64 » d'après le rapport : pour nommer la roue attendue dans les consignes hors ligne."""
    version = str(report.get("python_version") or "")
    parts = version.split(".")
    if len(parts) < 2 or not all(part.isdigit() for part in parts[:2]):
        return ""
    bits = report.get("bits")
    platform = str(report.get("platform") or "")
    if platform.startswith("win"):
        arch = "win_amd64" if bits == 64 else "win32"
    elif platform.startswith("linux"):
        arch = "manylinux_x86_64"
    elif platform == "darwin":
        arch = "macosx"
    else:
        arch = platform or "inconnu"
    return f"cp{parts[0]}{parts[1]}-{arch}"


def local_wheel(root, version=OPENCAMLIB_VERSION):
    """Roue ``opencamlib-<version>-*.whl`` déposée dans ``<root>/wheels`` (installation hors ligne), ou None."""
    folder = Path(root) / WHEELS_DIRNAME
    if not folder.is_dir() or folder.is_symlink():
        return None
    for candidate in sorted(folder.iterdir()):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        match = _WHEEL_NAME.match(candidate.name)
        if match and match.group("version") == version:
            return candidate
    return None


def pip_install_arguments(python, *, version=OPENCAMLIB_VERSION, wheel=None):
    """Commande pip du Python de FreeCAD : roue publiée seulement (jamais de compilation), sans dépendance tierce
    ajoutée à FreeCAD (``--no-deps`` : opencamlib n'en déclare aucune), version épinglée ; ``wheel`` = fichier local
    installé sans index (hors ligne)."""
    arguments = [str(python), "-m", "pip", "install", "--no-input", "--disable-pip-version-check", "--no-deps",
                 "--only-binary", ":all:"]
    if wheel is not None:
        arguments += ["--no-index", str(wheel)]
    else:
        arguments.append(f"{OPENCAMLIB_PACKAGE}=={version}")
    return arguments


def pip_uninstall_arguments(python):
    return [str(python), "-m", "pip", "uninstall", "--yes", "--disable-pip-version-check", OPENCAMLIB_PACKAGE]


def offline_instructions(report, root, version=OPENCAMLIB_VERSION):
    tag = python_tag(report) or "la version Python de FreeCAD"
    folder = Path(root) / WHEELS_DIRNAME
    return (f"Installation hors ligne : depuis un PC connecté, télécharge sur https://pypi.org/project/{OPENCAMLIB_PACKAGE}/{version}/#files "
            f"la roue opencamlib-{version} pour {tag}, dépose-la dans « {folder} » sur le Worker, puis relance « Installer ». "
            f"Alternative : FreeCAD 1.1 complet (freecad.org) fournit déjà OpenCAMLib dans son Python.")


def explain_pip_failure(text, report=None, root=None, version=OPENCAMLIB_VERSION):
    """Message pip brut → consigne en français, précise et sans « failed to fetch »."""
    raw = " ".join(str(text or "").split())
    lowered = raw.lower()
    report = report or {}
    python = str(report.get("python_version") or "inconnu")
    offline = offline_instructions(report, root or Path("components") / OPENCAMLIB_PACKAGE, version)
    if "no matching distribution" in lowered or "could not find a version" in lowered:
        return (f"Aucune roue OpenCAMLib {version} publiée pour le Python {python} de FreeCAD ({python_tag(report) or 'plateforme inconnue'}) : "
                f"pip ne compile jamais sur un Worker. {offline}")
    if any(key in lowered for key in ("could not fetch url", "connection error", "connectionerror", "proxyerror", "name resolution",
                                       "retrying (retry", "newconnectionerror", "read timed out", "max retries exceeded")):
        return f"Le Worker n'a pas pu joindre PyPI (réseau, proxy ou pare-feu) : rien n'a été modifié dans FreeCAD. Vérifie la connexion du Worker puis relance. {offline}"
    if "winerror 5" in lowered or "permission denied" in lowered or "access is denied" in lowered or "errno 13" in lowered:
        return ("Droits insuffisants pour écrire dans le dossier de FreeCAD (installation pour tous les utilisateurs, par exemple sous "
                "Program Files) : installe FreeCAD pour ton compte Windows (installateur officiel, option « utilisateur courant ») ou "
                "lance le Worker avec les droits sur ce dossier, puis relance. Rien n'a été modifié.")
    if "no space left" in lowered or "errno 28" in lowered:
        return "Disque du Worker plein : libère de la place, puis relance l'installation d'OpenCAMLib."
    if "no module named pip" in lowered:
        return (f"pip est absent du Python de FreeCAD ({python}) : essaie « python -m ensurepip » dans le dossier bin de FreeCAD, "
                f"ou réinstalle FreeCAD 1.1 complet depuis freecad.org (il fournit pip et OpenCAMLib).")
    if "hash mismatch" in lowered or "these packages do not match the hashes" in lowered:
        return "La roue téléchargée ne correspond pas à son empreinte publiée : rien n'a été installé. Relance plus tard ou utilise l'installation hors ligne."
    return f"pip a échoué dans le Python de FreeCAD : {raw[-600:] or 'sans message'}. {offline}"


def report_summary(report, freecadcmd=None):
    """Résumé lisible et borné (journal du Worker, marqueur du composant), sans chemin de compte."""
    ocl = ocl_state(report)
    return {
        "freecad_cmd_name": Path(str(freecadcmd)).name if freecadcmd else None,
        "freecad_version_hint": freecad_version_from_path(freecadcmd) if freecadcmd else "",
        "python_version": report.get("python_version"),
        "python_tag": python_tag(report),
        "pip": report.get("pip") if isinstance(report.get("pip"), dict) else {"available": False, "version": None},
        "ocl": ocl,
        "ok": bool(report.get("ok")),
        "error": str(report.get("error") or "")[:400],
    }


if __name__ == "__main__":  # diagnostic manuel sur le Worker : python freecad_python.py <FreeCADCmd>
    target = freecad_python(sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps(report_summary(probe(target) if target else {"ok": False, "error": "Python de FreeCAD introuvable."},
                                    sys.argv[1] if len(sys.argv) > 1 else None), ensure_ascii=False, indent=2))
