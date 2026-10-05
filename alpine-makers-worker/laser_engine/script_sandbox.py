# -*- coding: utf-8 -*-
"""Exécution isolée des scripts de l'assistant de code d'Alpine Laser Studio.

``python -m laser_engine.script_sandbox <requête.json> <résultat.json>`` (processus dédié, lancé par
``run_isolated`` avec un délai et un environnement réduit) exécute un script Python qui déclare :

    PARAMS = {"largeur": 120, "pas": {"value": 10, "min": 2, "max": 50, "label": "Pas (mm)"}}
    def build(p, ctx):          # p : valeurs effectives de PARAMS ; ctx : niveaux, détail, taille, image
        d = Drawing(p["largeur"], 80)
        ...
        return d

Le script ne voit que des copies filtrées des modules autorisés (``ALLOWED_MODULES``) : ni fichier, ni
réseau, ni processus, ni introspection. L'arbre syntaxique est vérifié avant l'exécution (pas de classe,
d'attribut commençant par « _ », de nom dunder, d'import hors liste, d'async/with). ``random`` est
initialisé avec ``seed`` : un même script et les mêmes paramètres donnent toujours le même fichier.

Requête : ``{script, params?, levels, detail, width_mm, height_mm, seed?, image_path?}``.
Résultat : ``{ok: true, svg, preview_svg, layers, stats, warnings, params, params_spec, log}`` ou
``{ok: false, error, line, code}`` (code SCRIPT_INVALID, SCRIPT_ERROR, SCRIPT_TIMEOUT, SCRIPT_OUTPUT).
"""

from __future__ import annotations

import ast
import builtins
import importlib
import inspect
import json
import math
import os
import subprocess
import sys
import time
import traceback
import types
import uuid
from pathlib import Path
from typing import Any, Callable

MAX_SCRIPT_CHARS = 200_000
MAX_AST_NODES = 60_000
MAX_LOG_CHARS = 20_000
MAX_SVG_BYTES = 24 * 1024 * 1024
MAX_PARAMS = 60
DEFAULT_TIMEOUT = 120
RECURSION_LIMIT = 800
MEMORY_LIMIT_BYTES = 3 * 1024 ** 3

ALLOWED_MODULES = {
    "math": None, "cmath": None, "itertools": None, "functools": None, "random": None, "statistics": None,
    "collections": None, "heapq": None, "bisect": None, "fractions": None, "decimal": None,
    "shapely": ("geometry", "ops", "affinity", "validation"), "shapely.geometry": None, "shapely.ops": None,
    "shapely.affinity": None, "shapely.validation": None, "svgwrite": None, "segno": None,
    "laser_assist": None,
}
MODULE_SOURCES = {"laser_assist": "laser_engine.assist"}
# Attributs publics qui écrivent, lisent un fichier, formatent par introspection ou exposent un cadre d'exécution
# (dont ceux des tableaux numpy que renvoient certaines fonctions shapely : tofile écrit un fichier, ctypes la mémoire).
BLOCKED_ATTRIBUTES = frozenset({
    "format", "format_map", "vformat", "save", "saveas", "write", "writelines", "embed_font", "embed_stylesheet",
    "tofile", "ctypes", "terminal",
    "embed_google_web_font", "to_pil", "to_artistic", "gi_frame", "gi_code", "gi_yieldfrom", "gi_running", "cr_frame",
    "cr_code", "cr_await", "ag_frame", "ag_code", "ag_await", "f_globals", "f_locals", "f_back", "f_builtins", "f_code",
    "tb_frame", "tb_next", "mro", "load", "loads", "dump", "dumps", "system", "popen", "open", "show",
})
BLOCKED_NODES = (ast.ClassDef, ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith, ast.With, ast.Global,
                 ast.Nonlocal)
SAFE_BUILTIN_NAMES = (
    "abs", "all", "any", "bool", "chr", "complex", "dict", "divmod", "enumerate", "filter", "float", "frozenset",
    "hash", "int", "isinstance", "iter", "len", "list", "map", "max", "min", "next", "ord", "pow", "range", "repr",
    "reversed", "round", "set", "slice", "sorted", "str", "sum", "tuple", "zip", "bytes", "callable",
    "ArithmeticError", "AssertionError", "Exception", "IndexError", "KeyError", "LookupError", "NotImplementedError",
    "OverflowError", "RuntimeError", "StopIteration", "TypeError", "ValueError", "ZeroDivisionError",
)


def detail_tolerance(detail: float) -> float:
    """Niveau de détail 1 (grossier) … 10 (très fin) → écart maximal toléré entre courbe et segments (mm)."""
    d = min(10.0, max(1.0, float(detail)))
    return round(0.2 * (0.005 / 0.2) ** ((d - 1.0) / 9.0), 5)


def detail_pitch(detail: float) -> float:
    """Niveau de détail → taille d'un pixel de vectorisation d'image (1 mm … 0,08 mm)."""
    d = min(10.0, max(1.0, float(detail)))
    return round(1.0 * 0.08 ** ((d - 1.0) / 9.0), 4)


class ScriptFailure(ValueError):
    """Script refusé ou en échec ; hors du bac à sable, une valeur de requête hors plage (→ REQUEST_INVALID)."""

    def __init__(self, message: str, code: str = "SCRIPT_ERROR", line: int | None = None):
        super().__init__(message)
        self.code = code
        self.line = line


# ---------------------------------------------------------------------------------------------------------
# Vérification statique
# ---------------------------------------------------------------------------------------------------------
def check(source: str) -> ast.Module:
    """Arbre syntaxique du script, refusé au premier élément interdit (message et ligne)."""
    if not isinstance(source, str) or not source.strip():
        raise ScriptFailure("Script vide.", "SCRIPT_INVALID")
    if len(source) > MAX_SCRIPT_CHARS:
        raise ScriptFailure(f"Script trop long ({len(source)} caractères, {MAX_SCRIPT_CHARS} au plus).", "SCRIPT_INVALID")
    try:
        tree = ast.parse(source, "<script>", "exec")
    except SyntaxError as error:
        raise ScriptFailure(f"Erreur de syntaxe : {error.msg}", "SCRIPT_INVALID", error.lineno) from None
    count = 0
    for node in ast.walk(tree):
        count += 1
        if count > MAX_AST_NODES:
            raise ScriptFailure("Script trop complexe.", "SCRIPT_INVALID")
        line = getattr(node, "lineno", None)
        if isinstance(node, BLOCKED_NODES):
            label = {ast.ClassDef: "les classes", ast.With: "« with »", ast.Global: "« global »",
                     ast.Nonlocal: "« nonlocal »"}.get(type(node), "le code asynchrone")
            raise ScriptFailure(f"Construction non autorisée dans un script laser : {label}.", "SCRIPT_INVALID", line)
        if isinstance(node, ast.Attribute) and (node.attr.startswith("_") or node.attr in BLOCKED_ATTRIBUTES):
            raise ScriptFailure(f"Attribut non autorisé : .{node.attr}", "SCRIPT_INVALID", line)
        if isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Load):
            # Modifier un attribut toucherait les classes partagées avec le code de confiance (shapely…).
            raise ScriptFailure(f"Modification d'attribut non autorisée : .{node.attr}", "SCRIPT_INVALID", line)
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ScriptFailure(f"Nom non autorisé : {node.id}", "SCRIPT_INVALID", line)
        if isinstance(node, (ast.FunctionDef, ast.Lambda)):
            names = [arg.arg for arg in node.args.args + node.args.kwonlyargs + node.args.posonlyargs]
            names += [arg.arg for arg in (node.args.vararg, node.args.kwarg) if arg]
            if isinstance(node, ast.FunctionDef):
                names.append(node.name)
            bad = next((name for name in names if name.startswith("__")), None)
            if bad:
                raise ScriptFailure(f"Nom non autorisé : {bad}", "SCRIPT_INVALID", line)
        if isinstance(node, ast.ExceptHandler) and node.name and node.name.startswith("__"):
            raise ScriptFailure(f"Nom non autorisé : {node.name}", "SCRIPT_INVALID", line)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_MODULES:
                    raise ScriptFailure(f"Import non autorisé : {alias.name} (autorisés : {', '.join(sorted(ALLOWED_MODULES))}).",
                                        "SCRIPT_INVALID", line)
        if isinstance(node, ast.ImportFrom):
            if node.level or node.module not in ALLOWED_MODULES:
                raise ScriptFailure(f"Import non autorisé : {node.module or '.'} (autorisés : {', '.join(sorted(ALLOWED_MODULES))}).",
                                    "SCRIPT_INVALID", line)
            bad = next((alias.name for alias in node.names if alias.name != "*" and alias.name.startswith("_")), None)
            if bad:
                raise ScriptFailure(f"Import non autorisé : {bad}", "SCRIPT_INVALID", line)
    return tree


# ---------------------------------------------------------------------------------------------------------
# Modules filtrés et fonctions natives
# ---------------------------------------------------------------------------------------------------------
def _proxy(name: str, cache: dict[str, Any]) -> Any:
    if name in cache:
        return cache[name]
    module = importlib.import_module(MODULE_SOURCES.get(name, name))
    public = getattr(module, "__all__", None) if name == "laser_assist" else None
    values = {}
    for attr in (public or dir(module)):
        if attr.startswith("_"):
            continue
        value = getattr(module, attr, None)
        if isinstance(value, types.ModuleType) or value is None:
            continue
        values[attr] = value
    for child in ALLOWED_MODULES.get(name) or ():
        values[child] = _proxy(f"{name}.{child}", cache)
    proxy = types.SimpleNamespace(**values)
    cache[name] = proxy
    return proxy


def _guarded_import(cache: dict[str, Any]) -> Callable[..., Any]:
    def guarded(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002 - signature d'__import__
        if level or name not in ALLOWED_MODULES:
            raise ImportError(f"Import non autorisé : {name}")
        if fromlist:
            return _proxy(name, cache)
        return _proxy(name.split(".", 1)[0], cache)
    return guarded


def _safe_builtins(log: list[str], cache: dict[str, Any]) -> dict[str, Any]:
    values = {name: getattr(builtins, name) for name in SAFE_BUILTIN_NAMES}
    size = [0]

    def captured_print(*args, sep=" ", end="\n"):
        text = str(sep).join(str(arg) for arg in args) + str(end)
        if size[0] < MAX_LOG_CHARS:
            log.append(text[: MAX_LOG_CHARS - size[0]])
            size[0] += len(text)

    values.update(print=captured_print, __import__=_guarded_import(cache))
    return values


# ---------------------------------------------------------------------------------------------------------
# Paramètres
# ---------------------------------------------------------------------------------------------------------
def _param_spec(name: str, raw: Any) -> dict[str, Any]:
    meta = raw if isinstance(raw, dict) and "value" in raw else {"value": raw}
    value = meta.get("value")
    if isinstance(value, bool):
        kind = "bool"
    elif isinstance(value, (int, float)) and math.isfinite(float(value)):
        kind = "int" if isinstance(value, int) else "float"
    elif isinstance(value, str):
        kind = "str"
    elif isinstance(value, (list, tuple)) and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
        kind, value = "list", [float(v) for v in value]
    else:
        raise ScriptFailure(f"PARAMS[{name!r}] : nombre, booléen, texte ou liste de nombres attendu.", "SCRIPT_INVALID")
    spec = {"name": name, "type": kind, "value": value, "default": value}
    for key in ("min", "max", "step"):
        if isinstance(meta.get(key), (int, float)) and not isinstance(meta.get(key), bool) and math.isfinite(float(meta[key])):
            spec[key] = meta[key]
    if isinstance(meta.get("label"), str):
        spec["label"] = meta["label"][:80]
    if isinstance(meta.get("choices"), (list, tuple)) and kind == "str":
        spec["choices"] = [str(choice)[:80] for choice in meta["choices"]][:40]
    return spec


def _coerce(spec: dict[str, Any], value: Any) -> Any:
    kind = spec["type"]
    try:
        if kind == "bool":
            result = value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "oui", "on", "yes")
        elif kind == "int":
            result = int(round(float(value)))
        elif kind == "float":
            result = float(value)
            if not math.isfinite(result):
                raise ValueError
        elif kind == "str":
            result = str(value)[:2000]
            if spec.get("choices") and result not in spec["choices"]:
                raise ValueError
        else:
            result = [float(v) for v in value][:1000]
    except (TypeError, ValueError):
        raise ScriptFailure(f"Paramètre {spec['name']} : valeur invalide ({value!r}).", "SCRIPT_INVALID") from None
    if kind in ("int", "float"):
        if "min" in spec:
            result = max(spec["min"], result)
        if "max" in spec:
            result = min(spec["max"], result)
    return result


def resolve_params(declared: Any, overrides: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if declared is None:
        declared = {}
    if not isinstance(declared, dict) or len(declared) > MAX_PARAMS:
        raise ScriptFailure(f"PARAMS doit être un dictionnaire de {MAX_PARAMS} entrées au plus.", "SCRIPT_INVALID")
    overrides = overrides if isinstance(overrides, dict) else {}
    values, specs = {}, []
    for name, raw in declared.items():
        if not isinstance(name, str) or not name or len(name) > 60:
            raise ScriptFailure("PARAMS : chaque nom doit être un texte court.", "SCRIPT_INVALID")
        spec = _param_spec(name, raw)
        if name in overrides:
            spec["value"] = _coerce(spec, overrides[name])
        values[name] = spec["value"]
        specs.append(spec)
    return values, specs


# ---------------------------------------------------------------------------------------------------------
# Exécution (dans le processus dédié)
# ---------------------------------------------------------------------------------------------------------
def _script_line(error: BaseException) -> int | None:
    frames = [frame for frame in traceback.extract_tb(error.__traceback__) if frame.filename == "<script>"]
    return frames[-1].lineno if frames else None


def _clean_number(value: Any, name: str, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ScriptFailure(f"{name} invalide.", "SCRIPT_INVALID") from None
    if not math.isfinite(number) or not low <= number <= high:
        raise ScriptFailure(f"{name} hors plage ({low:g} à {high:g}).", "SCRIPT_INVALID")
    return number


def _external_svg(text: str) -> dict[str, Any]:
    """SVG rendu tel quel par le script (svgwrite ou texte) : analysé mais non nettoyé."""
    from .toolpath import ToolpathError, parse_svg

    if len(text.encode("utf-8")) > MAX_SVG_BYTES:
        raise ScriptFailure("SVG produit trop volumineux.", "SCRIPT_OUTPUT")
    try:
        document = parse_svg(text)
    except ToolpathError as error:
        raise ScriptFailure(f"SVG produit illisible : {error}", "SCRIPT_OUTPUT") from None
    colors: dict[str, int] = {}
    for shape in document.shapes:
        colors[shape.layer_key] = colors.get(shape.layer_key, 0) + 1
    nodes = sum(len(path) for shape in document.shapes for path in shape.paths)
    return {"svg": text, "preview_svg": text,
            "layers": [{"color": color, "kind": "svg", "name": f"Calque {color}", "shapes": count} for color, count in colors.items()],
            "stats": {"paths": len(document.shapes), "nodes": nodes},
            "warnings": list(document.warnings) + ["SVG fourni tel quel par le script : nettoyage géométrique non appliqué "
                                                   "(renvoie un Drawing pour des chemins fermés et sans doublons garantis)."],
            "width_mm": document.width_mm, "height_mm": document.height_mm}


def run(request: dict[str, Any]) -> dict[str, Any]:
    """Exécute le script de la requête et renvoie le résultat (lève ScriptFailure)."""
    from . import assist

    source = str(request.get("script") or "")
    tree = check(source)
    levels = int(_clean_number(request.get("levels", 1), "Nombre de niveaux", 1, assist.MAX_LEVELS))
    detail = _clean_number(request.get("detail", 6), "Niveau de détail", 1, 10)
    width = _clean_number(request.get("width_mm", 100), "Largeur", 1, assist.MAX_SIZE_MM)
    height = _clean_number(request.get("height_mm", 100), "Hauteur", 1, assist.MAX_SIZE_MM)
    seed = int(_clean_number(request.get("seed", 1), "Graine", 0, 2**31 - 1))
    image = str(request.get("image_path") or "") or None
    if image and not Path(image).is_file():
        raise ScriptFailure("Image jointe introuvable sur le Worker.", "SCRIPT_INVALID")
    assist._configure(levels, detail, image)
    log: list[str] = []
    cache: dict[str, Any] = {}
    import random

    random.seed(seed)
    namespace: dict[str, Any] = {"__builtins__": _safe_builtins(log, cache), "__name__": "script_laser"}
    code = compile(tree, "<script>", "exec")
    sys.setrecursionlimit(RECURSION_LIMIT)
    try:
        exec(code, namespace)  # noqa: S102 - arbre vérifié, fonctions natives et modules filtrés
        values, specs = resolve_params(namespace.get("PARAMS"), request.get("params"))
        build = namespace.get("build")
        if not callable(build):
            raise ScriptFailure("Le script doit définir build(p, ctx).", "SCRIPT_INVALID")
        ctx = assist.Context(levels, detail, width, height, bool(image), seed)
        try:
            arity = len(inspect.signature(build).parameters)
        except (TypeError, ValueError):
            arity = 2
        output = build(values, ctx) if arity >= 2 else build(values)
    except ScriptFailure:
        raise
    except assist.ScriptError as error:
        raise ScriptFailure(str(error), "SCRIPT_ERROR", _script_line(error)) from None
    except RecursionError as error:
        raise ScriptFailure("Récursion trop profonde dans le script.", "SCRIPT_ERROR", _script_line(error)) from None
    except MemoryError:
        raise ScriptFailure("Mémoire insuffisante : réduis le nombre de formes ou le niveau de détail.", "SCRIPT_ERROR") from None
    except Exception as error:  # noqa: BLE001 - toute erreur du script est rapportée, ligne comprise
        raise ScriptFailure(f"{type(error).__name__}: {error}", "SCRIPT_ERROR", _script_line(error)) from None
    try:
        if isinstance(output, assist.Drawing):
            result = assist._finalize(output)
        elif isinstance(output, str) and output.lstrip().startswith("<"):
            result = _external_svg(output)
        elif hasattr(output, "tostring") and type(output).__module__.startswith("svgwrite"):
            result = _external_svg(output.tostring())
        else:
            raise ScriptFailure("build() doit renvoyer un Drawing (ou, à défaut, un SVG).", "SCRIPT_OUTPUT")
    except assist.ScriptError as error:
        raise ScriptFailure(str(error), "SCRIPT_OUTPUT") from None
    if len(result["svg"].encode("utf-8")) > MAX_SVG_BYTES:
        raise ScriptFailure("SVG produit trop volumineux : baisse le niveau de détail ou le nombre de formes.", "SCRIPT_OUTPUT")
    result.update(ok=True, params=values, params_spec=specs, log="".join(log), levels=levels, detail=detail, seed=seed)
    return result


# ---------------------------------------------------------------------------------------------------------
# Lancement dans un processus dédié
# ---------------------------------------------------------------------------------------------------------
def _interpreter() -> tuple[str, dict[str, str]]:
    """Interpréteur à lancer. Sous Windows, le python.exe d'un venv n'est qu'un lanceur qui crée un second
    processus : l'objet job (un seul processus) le bloquerait. On lance donc l'interpréteur de base en lui
    désignant le venv (``__PYVENV_LAUNCHER__``, le mécanisme du lanceur lui-même)."""
    base = getattr(sys, "_base_executable", "") or sys.executable
    if os.name == "nt" and sys.prefix != sys.base_prefix and base and Path(base).is_file() and Path(base) != Path(sys.executable):
        return base, {"__PYVENV_LAUNCHER__": sys.executable}
    return sys.executable, {}


def _environment(workdir: Path, extra: dict[str, str]) -> dict[str, str]:
    """Environnement minimal : pas de clés, pas de profil, dossier temporaire du job."""
    worker_dir = Path(__file__).resolve().parents[1]
    keep = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "SystemRoot") if key in os.environ}
    keep.update(PYTHONPATH=str(worker_dir), PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
                TEMP=str(workdir), TMP=str(workdir), PATH=str(Path(sys.executable).parent), **extra)
    return keep


def _confine(process: subprocess.Popen) -> Any:
    """Windows : objet job qui plafonne la mémoire du script, lui interdit tout processus enfant et le
    termine avec son handle. Renvoie le handle (à fermer) ou None si le système refuse."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in ("read_ops", "write_ops", "other_ops", "read_bytes",
                                                            "write_bytes", "other_bytes")]

    class BasicLimits(ctypes.Structure):
        _fields_ = [("user_time", ctypes.c_int64), ("job_user_time", ctypes.c_int64), ("flags", wintypes.DWORD),
                    ("min_working_set", ctypes.c_size_t), ("max_working_set", ctypes.c_size_t),
                    ("active_processes", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                    ("priority_class", wintypes.DWORD), ("scheduling_class", wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io", IoCounters), ("process_memory", ctypes.c_size_t),
                    ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    limits = ExtendedLimits()
    # JOB_OBJECT_LIMIT_ACTIVE_PROCESS | JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    limits.basic.flags = 0x0008 | 0x0100 | 0x2000
    limits.basic.active_processes = 1
    limits.process_memory = MEMORY_LIMIT_BYTES
    handle = getattr(process, "_handle", None)
    if (not kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            or handle is None or not kernel32.AssignProcessToJobObject(job, int(handle))):
        kernel32.CloseHandle(job)
        return None
    return (kernel32, job)


def _release(confinement: Any) -> None:
    if confinement:
        kernel32, job = confinement
        kernel32.CloseHandle(job)


def _posix_limits() -> None:  # pragma: no cover - Linux uniquement
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))


def run_isolated(request: dict[str, Any], workdir: Path, timeout: float = DEFAULT_TIMEOUT,
                 cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Exécute ``run`` dans un processus Python dédié ; renvoie ``{ok: true, …}`` ou ``{ok: false, error, code, line}``."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    source, target = workdir / f"script-{token}.json", workdir / f"script-{token}.result.json"
    source.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    stderr_path = workdir / f"script-{token}.stderr"
    try:
        with stderr_path.open("w", encoding="utf-8") as stderr:
            python, extra = _interpreter()
            process = subprocess.Popen([python, "-s", "-m", "laser_engine.script_sandbox", str(source), str(target)],
                                       cwd=str(workdir), env=_environment(workdir, extra), stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=stderr,
                                       preexec_fn=_posix_limits if os.name != "nt" else None,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            confinement = _confine(process)
            try:
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    if cancelled and cancelled():
                        process.kill()
                        process.wait()
                        return {"ok": False, "error": "Exécution du script annulée.", "code": "CANCELLED"}
                    if time.monotonic() > deadline:
                        process.kill()
                        process.wait()
                        return {"ok": False, "error": f"Script arrêté après {timeout:g} s : boucle infinie ou calcul trop lourd.",
                                "code": "SCRIPT_TIMEOUT"}
                    time.sleep(0.05)
            finally:
                _release(confinement)
        if target.is_file():
            value = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
        tail = " ".join(stderr_path.read_text(encoding="utf-8", errors="replace").split())[-600:] if stderr_path.is_file() else ""
        return {"ok": False, "error": f"Le script s'est arrêté sans résultat (code {process.returncode}). {tail}".strip(),
                "code": "SCRIPT_ERROR"}
    finally:
        for path in (source, target, stderr_path):
            path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2:
        print("Usage : python -m laser_engine.script_sandbox <requête.json> <résultat.json>", file=sys.stderr)
        return 64
    source, target = Path(arguments[0]), Path(arguments[1])
    try:
        request = json.loads(source.read_text(encoding="utf-8"))
        result = run(request if isinstance(request, dict) else {})
    except ScriptFailure as error:
        result = {"ok": False, "error": str(error), "code": error.code, "line": error.line}
    except Exception as error:  # noqa: BLE001 - le parent reçoit toujours un compte rendu
        result = {"ok": False, "error": f"Erreur interne du bac à sable : {type(error).__name__}: {error}", "code": "SANDBOX_ERROR"}
    target.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
