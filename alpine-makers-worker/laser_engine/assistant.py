# -*- coding: utf-8 -*-
"""Assistants de code d'Alpine Laser Studio : Claude (Claude Code) et GPT (Codex), sur le Worker de l'utilisateur.

Chaque utilisateur branche son propre Worker, où Claude Code et/ou Codex sont installés et connectés à SES
comptes (abonnement ou clé) ; le site n'a jamais ces identifiants. Une demande :

1. appelle la CLI en mode non interactif, sans outil (Claude : ``--tools "" --safe-mode``) ou en lecture
   seule (Codex : ``sandbox_mode="read-only"``), texte par l'entrée standard, image jointe comprise ;
   ``session_id`` reprend une discussion existante (identifiant choisi par l'utilisateur dans le studio) ;
2. extrait le bloc ```python de la réponse et l'exécute dans le bac à sable (``script_sandbox``) ;
3. si le script échoue, renvoie l'erreur dans la même discussion pour correction (``repairs`` fois au plus).

Les dossiers de travail des CLI sont fixes (``<Worker>/runtime/laser-assistant/<fournisseur>``) : une
discussion créée ici se reprend toujours d'ici. Rien n'est envoyé ailleurs qu'au fournisseur choisi.

Comptes IA (agent 1.35.0) : ``account_id`` choisit le compte de la CLI. ``default`` = profil normal de
l'utilisateur qui exécute l'agent ; un compte ajouté dans Mes Workers → Comptes IA a son propre profil
(``<Worker>/data/ai-accounts/<fournisseur>/<compte>`` en CLAUDE_CONFIG_DIR / CODEX_HOME) ; un jeton ou une clé
Claude enregistrés par le site sont réinjectés dans l'environnement de la CLI (jamais renvoyés).
"""

from __future__ import annotations

import base64
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import script_sandbox
from .toolpath import ToolpathError

PROVIDERS = {"claude": "Claude", "codex": "GPT"}
SESSION_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,79}$")
CODE_BLOCK = re.compile(r"```[ \t]*(?:python|py|python3)?[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
MAX_PROMPT_CHARS = 8000
MAX_CONTEXT_CHARS = 600
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_REPAIRS = 2
CLI_TIMEOUT = 600
# Réflexion longue (xhigh, max, ultra) : 20 min par réponse.
LONG_EFFORT_TIMEOUT = 1200
STATUS_TIMEOUT = 30
# Appels courts des CLI (état, version, déconnexion, clé API) : sortie gardée au plus, lecture finale bornée.
BOUNDED_OUTPUT_LIMIT = 256 * 1024
BOUNDED_READ_GRACE = 3.0
SCRIPT_TIMEOUT = 120
MAX_REPLY_CHARS = 4000
IMAGE_TYPES = {b"\x89PNG": ("png", "image/png"), b"\xff\xd8\xff": ("jpg", "image/jpeg"), b"RIFF": ("webp", "image/webp")}

SYSTEM_PROMPT = """Tu es l'assistant de code d'Alpine Laser Studio. Tu écris des scripts Python qui produisent des \
fichiers de gravure et de découpe laser mathématiquement exacts. Réponds en français.

FORMAT DE RÉPONSE
- Une ou deux phrases d'explication, puis le script COMPLET dans un seul bloc ```python (jamais un extrait,
  même pour une petite retouche). Si la demande est impossible ou ambiguë, explique-le sans bloc de code.
- Pas de texte gravé : aucune police n'est disponible. Si du texte est demandé, dis-le et réserve sa zone.

CONTRAT DU SCRIPT (exécuté dans un bac à sable, sans fichier, réseau, classe ni « with »)
PARAMS = {"largeur": {"value": 120, "min": 10, "max": 600, "label": "Largeur (mm)"}, "trous": 4, ...}
def build(p, ctx):
    d = Drawing(p["largeur"], 80)   # zone de dessin en mm
    ...
    return d
- PARAMS : toutes les cotes et quantités réglables (nombre, booléen, texte, liste de nombres), sous forme
  simple ou {"value", "min", "max", "step", "label", "choices"}. p contient les valeurs effectives.
- ctx : levels (nombre de niveaux de gravure choisi), detail (1 à 10), tolerance (mm), width_mm, height_mm
  (zone demandée dans le studio), has_image, seed (random est déjà initialisé : résultat reproductible).
- Unités : millimètres. Origine en haut à gauche, y vers le BAS (comme le SVG). Angles en degrés.
- Imports autorisés uniquement : laser_assist, shapely (geometry, ops, affinity, validation), math, cmath,
  itertools, functools, random, statistics, collections, heapq, bisect, fractions, decimal, svgwrite, segno.
  Aucun attribut commençant par « _ », aucune modification d'attribut (obj.x = …), pas de getattr/open/eval.

BIBLIOTHÈQUE (from laser_assist import *)
- Drawing(w, h) : d.engrave(geom, level=None) surface gravée (level 1 = le plus clair … ctx.levels = le plus
  foncé, défaut) ; d.cut(geom) découpe (contours et trous des surfaces, lignes) ; d.mark(geom) trait gravé.
  Sur une zone commune, le niveau le plus foncé l'emporte ; les doublons de découpe sont fusionnés.
- Formes : rect(x, y, w, h, r=0) coin haut-gauche ; circle(cx, cy, r) ; ellipse(cx, cy, rx, ry, angle) ;
  regular_polygon(cx, cy, r, sides, rotation=-90) ; star(cx, cy, r_ext, r_int, points=5) ;
  polygon(points, holes=None) ; polyline(points, closed=False) ; line(x1, y1, x2, y2) ;
  arc(cx, cy, r, start_deg, end_deg) ; slot(cx, cy, longueur, largeur, angle=0, rounded=True).
- Opérations : move, rotate(geom, angle, origin="center"), scale, mirror(geom, axis="x"), union(*geoms),
  difference(a, b), intersection(a, b), offset(geom, distance, join="round"|"mitre"|"bevel").
- Répétitions : grid_points(nx, ny, dx, dy, x0=0, y0=0), pattern(geom, nx, ny, dx, dy),
  polar_pattern(geom, count, cx, cy, total_angle=360).
- Assemblages : panel(w, h, épaisseur, créneau, edges="ffff", kerf=0) panneau de boîte à créneaux (haut,
  droite, bas, gauche ; "f" tenons, "h" mortaises, "s" droit ; un bord "f" s'emboîte dans un "h") ;
  finger_count(longueur, créneau).
- QR : qr(texte, x, y, taille, style="square"|"dots"|"rounded", error="m", border=0).
- Image jointe (mode Image) : trace_levels(ctx, levels=None, width=None, height=None, x=0, y=0,
  invert=False, contrast=1.0) → liste de surfaces du niveau 1 (clair) au niveau N (foncé), à graver chacune
  à son niveau ; trace(ctx, threshold=0.5, width=None, height=None, x=0, y=0, invert=False) → surface des
  zones sombres (contour à découper, pochoir…) ; image_size() → (px, py). La finesse suit ctx.detail.
- Divers : tolerance() ; warn(message) ; shapely (Polygon, box, unary_union…) pour tout le reste.

RÈGLES DE QUALITÉ
- Géométrie exacte et paramétrée : calcule les positions (pas de valeurs magiques), centre ou aligne par
  calcul, garde une marge. Surfaces fermées pour la gravure, contours fermés pour la découpe.
- Utilise les niveaux de ctx.levels quand la demande comporte des nuances ; sinon le niveau par défaut.
- Coupe les pièces imbriquées proprement (la bibliothèque ordonne et nettoie les tracés).
- Reste dans la zone de dessin annoncée, sauf si l'utilisateur demande une autre taille."""


class AssistantError(ToolpathError):
    """Échec d'une demande à l'assistant (CLI absente, non connectée, discussion introuvable…)."""

    def __init__(self, message: str, code: str = "ASSISTANT_FAILED", status_code: int = 409):
        super().__init__(message, status_code, code)


# ---------------------------------------------------------------------------------------------------------
# Localisation des CLI
# ---------------------------------------------------------------------------------------------------------
def _existing(paths: list[str]) -> list[str]:
    seen, result = set(), []
    for path in paths:
        if path and path not in seen and Path(path).is_file():
            seen.add(path)
            result.append(path)
    return result


def _globbed(*patterns: str) -> list[str]:
    found = []
    for pattern in patterns:
        matches = [path for path in glob.glob(pattern) if Path(path).is_file()]
        matches.sort(key=lambda path: Path(path).stat().st_mtime, reverse=True)
        found.extend(matches)
    return found


def _shim(path: str) -> bool:
    return Path(path).suffix.lower() in (".cmd", ".bat")


CLI_OVERRIDES = {"claude": "CLAUDE_CLI_PATH", "codex": "CODEX_CLI_PATH"}


def find_cli(provider: str) -> str:
    """Exécutable de la CLI du fournisseur : chemin imposé (CLAUDE_CLI_PATH / CODEX_CLI_PATH) s'il existe,
    sinon recherche habituelle où un exécutable natif est préféré à un script .cmd."""
    if provider not in CLI_OVERRIDES:
        raise AssistantError("Assistant inconnu (claude ou codex).", "PROVIDER_INVALID", 400)
    forced = os.getenv(CLI_OVERRIDES[provider], "").strip()
    if forced:
        return forced if Path(forced).is_file() else ""
    home, appdata, local = str(Path.home()), os.getenv("APPDATA", ""), os.getenv("LOCALAPPDATA", "")
    if provider == "claude":
        candidates = [shutil.which("claude") or "",
                      os.path.join(home, ".local", "bin", "claude.exe"), os.path.join(home, ".local", "bin", "claude"),
                      os.path.join(home, ".claude", "local", "claude.exe"), os.path.join(home, ".claude", "local", "claude")]
        if local:
            candidates += _globbed(os.path.join(local, "Microsoft", "WinGet", "Packages", "Anthropic.ClaudeCode_*", "claude.exe"))
            candidates.append(os.path.join(local, "Microsoft", "WinGet", "Links", "claude.exe"))
        if appdata:
            candidates += _globbed(os.path.join(appdata, "Claude", "claude-code", "*", "claude.exe"))
            candidates.append(os.path.join(appdata, "npm", "claude.cmd"))
    else:
        candidates = []
        roots = [os.path.join(appdata, "npm")] if appdata else []
        which = shutil.which("codex") or ""
        if which:
            roots.append(str(Path(which).parent))
        for root in roots:
            package = os.path.join(root, "node_modules", "@openai", "codex")
            candidates += _globbed(os.path.join(package, "node_modules", "@openai", "codex-win32-*", "vendor", "*", "bin", "codex.exe"),
                                   os.path.join(package, "vendor", "*", "bin", "codex.exe"))
        candidates.append(which)
    existing = _existing(candidates)
    native = [path for path in existing if not _shim(path)]
    return (native or existing or [""])[0]


def cli_environment() -> dict[str, str]:
    """Environnement de l'utilisateur du Worker (ses identifiants CLI), sans variables Python ni de session Claude."""
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() not in {"PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"}}
    for key in list(environment):
        upper = key.upper()
        if upper == "CLAUDECODE" or (upper.startswith("CLAUDE_CODE_") and upper != "CLAUDE_CODE_USE_BEDROCK"):
            environment.pop(key, None)
    environment["CLAUDE_CODE_ENTRYPOINT"] = "alpine-laser-assistant"
    return environment


# ---------------------------------------------------------------------------------------------------------
# Comptes IA (agent 1.35.0) : profil de CLI par compte, partagé avec ai_accounts.py (commandes du site)
# ---------------------------------------------------------------------------------------------------------
PRODUCTS = {"claude": "Claude Code", "codex": "Codex"}
ACCOUNT_LABELS = {"claude": "Claude", "codex": "ChatGPT"}
INSTALL_HINTS = {"claude": "https://claude.com/claude-code", "codex": "npm i -g @openai/codex"}
DEFAULT_ACCOUNT = "default"
ACCOUNT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
# Profil d'un compte ajouté : dossier de configuration propre à la CLI (identifiants, discussions, modèles).
ACCOUNT_HOME_ENV = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
# Jeton d'abonnement (claude setup-token) ou clé API Anthropic enregistrés par le site pour un compte Claude.
ACCOUNT_SECRET_FILE = "alpine-auth.json"
ACCOUNT_SECRET_ENV = {"token": "CLAUDE_CODE_OAUTH_TOKEN", "api_key": "ANTHROPIC_API_KEY"}
SECRET_MIN_CHARS, SECRET_MAX_CHARS = 8, 4096
# Identifiants hérités de l'environnement de l'agent : jamais prêtés à un compte ajouté (son profil seul fait foi).
INHERITED_CREDENTIALS = {"claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CONFIG_DIR"),
                         "codex": ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_HOME")}
ACCOUNTS_HINT = "Mes Workers → Comptes IA"


def worker_root(root: str | Path | None = None) -> Path:
    """Dossier du Worker : ``root`` (agent) ou ALPINE_WORKER_ROOT (moteur laser lancé par l'agent)."""
    return Path(root) if root else Path(os.getenv("ALPINE_WORKER_ROOT") or Path.home() / ".alpine-laser-assistant")


def check_account_id(value: Any) -> str:
    account_id = str(value if value is not None else "").strip() or DEFAULT_ACCOUNT
    if not ACCOUNT_PATTERN.fullmatch(account_id):
        raise AssistantError("Compte IA invalide (lettres minuscules, chiffres et tirets, 32 caractères au plus).",
                             "ACCOUNT_INVALID", 400)
    return account_id


def accounts_base(provider: str, root: str | Path | None = None) -> Path:
    if provider not in PROVIDERS:
        raise AssistantError("Assistant inconnu (claude ou codex).", "PROVIDER_INVALID", 400)
    return worker_root(root) / "data" / "ai-accounts" / provider


def account_dir(provider: str, account_id: str, root: str | Path | None = None) -> Path:
    """Dossier du compte, vérifié : toujours un enfant direct de ``<Worker>/data/ai-accounts/<fournisseur>``."""
    account_id = check_account_id(account_id)
    base = accounts_base(provider, root).resolve()
    target = (base / account_id).resolve()
    if target.parent != base or target.name != account_id:
        raise AssistantError("Dossier de compte IA hors de l'espace du Worker : refusé.", "ACCOUNT_PATH_INVALID", 400)
    return target


REDACTION_MARKER = "redacted"


def valid_secret(value: Any) -> bool:
    # « «redacted» » : charge déjà masquée par le site (commande relivrée) ; l'enregistrer écraserait le vrai secret.
    return (isinstance(value, str) and SECRET_MIN_CHARS <= len(value) <= SECRET_MAX_CHARS
            and REDACTION_MARKER not in value.lower()
            and not any(character.isspace() or not character.isprintable() for character in value))


def read_account_secret(provider: str, account_id: str, root: str | Path | None = None) -> dict[str, str] | None:
    """Jeton ou clé Claude enregistrés pour ce compte ({kind, value}), sinon None ; un fichier invalide est ignoré."""
    if provider != "claude":
        return None
    data = _read_json(account_dir(provider, account_id, root) / ACCOUNT_SECRET_FILE)
    if not isinstance(data, dict) or data.get("kind") not in ACCOUNT_SECRET_ENV or not valid_secret(data.get("value")):
        return None
    # Claude Code annonce « connecté » pour n'importe quel jeton fourni par l'environnement, sans le vérifier
    # (constaté le 2026-10-02 avec un faux jeton) : seul un appel réussi de l'assistant le confirme.
    return {"kind": str(data["kind"]), "value": str(data["value"]), "verified": bool(data.get("verified_at"))}


def mark_account_secret_verified(provider: str, account_id: str, root: str | Path | None = None) -> bool:
    """Note qu'un appel réel a réussi avec le jeton / la clé enregistrés de ce compte (vrai si la note a été posée)."""
    if provider != "claude":
        return False
    path = account_dir(provider, account_id, root) / ACCOUNT_SECRET_FILE
    data = _read_json(path)
    if not isinstance(data, dict) or data.get("kind") not in ACCOUNT_SECRET_ENV or data.get("verified_at"):
        return False
    data["verified_at"] = time.time()
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(data), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        return False
    return True


def account_home(provider: str, account_id: str = DEFAULT_ACCOUNT, root: str | Path | None = None) -> Path | None:
    """Profil de CLI d'un compte ajouté (None pour le compte par défaut : profil normal de l'utilisateur)."""
    account_id = check_account_id(account_id)
    return None if account_id == DEFAULT_ACCOUNT else account_dir(provider, account_id, root)


def account_environment(provider: str, account_id: str = DEFAULT_ACCOUNT, root: str | Path | None = None) -> dict[str, str]:
    """Environnement de la CLI pour ce compte : ``cli_environment()`` puis profil (CLAUDE_CONFIG_DIR / CODEX_HOME) et
    secret enregistré, réinjectés APRÈS le nettoyage (qui retire les CLAUDE_CODE_* hérités)."""
    environment = cli_environment()
    account_id = check_account_id(account_id)
    home = account_home(provider, account_id, root)
    if home is not None:
        if not home.is_dir():
            raise AssistantError(f"Compte {ACCOUNT_LABELS[provider]} « {account_id} » introuvable sur ce Worker : ajoute-le dans "
                                 f"{ACCOUNTS_HINT}.", "ACCOUNT_NOT_FOUND", 404)
        inherited = {name.upper() for name in INHERITED_CREDENTIALS[provider]}
        for key in [key for key in environment if key.upper() in inherited]:
            environment.pop(key, None)
        environment[ACCOUNT_HOME_ENV[provider]] = str(home)
    secret = read_account_secret(provider, account_id, root)
    if secret:
        for key in [key for key in environment if key.upper() in set(ACCOUNT_SECRET_ENV.values())]:
            environment.pop(key, None)
        environment[ACCOUNT_SECRET_ENV[secret["kind"]]] = secret["value"]
    return environment


_CONTAINER: dict[str, Any] = {"kernel": None, "handle": None, "failed": False}
_CONTAINER_LOCK = threading.Lock()


def contain(process: subprocess.Popen | None) -> bool:
    """Windows : rattache une CLI lancée par ce processus à un groupe (job) « fermé avec son parent » : si l'agent
    ou le runner s'arrête brutalement, Windows arrête aussi la CLI et ses descendants (connexion orpheline évitée).
    Au mieux : un refus du système laisse la CLI tourner comme avant, l'arrêt normal reste ``terminate_tree``."""
    if os.name != "nt" or process is None:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        with _CONTAINER_LOCK:
            if _CONTAINER["handle"] is None and not _CONTAINER["failed"]:
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
                kernel.CreateJobObjectW.restype = wintypes.HANDLE
                kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
                kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

                class BasicLimits(ctypes.Structure):
                    _fields_ = [("ProcessTime", ctypes.c_longlong), ("JobTime", ctypes.c_longlong),
                                ("Flags", wintypes.DWORD), ("MinWorking", ctypes.c_size_t), ("MaxWorking", ctypes.c_size_t),
                                ("ActiveLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
                                ("Priority", wintypes.DWORD), ("Scheduling", wintypes.DWORD)]

                class ExtendedLimits(ctypes.Structure):
                    _fields_ = [("Basic", BasicLimits), ("IO", ctypes.c_ulonglong * 6), ("ProcessMemory", ctypes.c_size_t),
                                ("JobMemory", ctypes.c_size_t), ("PeakProcessMemory", ctypes.c_size_t),
                                ("PeakJobMemory", ctypes.c_size_t)]
                handle = kernel.CreateJobObjectW(None, None)
                limits = ExtendedLimits()
                limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                    _CONTAINER["failed"] = True
                else:
                    _CONTAINER["kernel"], _CONTAINER["handle"] = kernel, handle
            kernel, handle = _CONTAINER["kernel"], _CONTAINER["handle"]
        return bool(handle and kernel.AssignProcessToJobObject(handle, int(process._handle)))
    except (OSError, AttributeError, ValueError):
        return False


def terminate_tree(process: subprocess.Popen | None) -> bool:
    """Arrête le processus et tous ses descendants ; vrai quand l'arrêt est confirmé.

    Windows : ``taskkill /T /F`` (une CLI npm est un script .cmd : cmd.exe, puis node en petit-enfant). Ailleurs : le
    groupe du processus (lancé avec ``start_new_session``), SIGTERM puis SIGKILL."""
    if process is None or process.poll() is not None:
        return True
    if os.name == "nt":
        taskkill = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "taskkill.exe")
        try:
            subprocess.run([taskkill, "/PID", str(process.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return False
    return True


def run_bounded(command: list[str], timeout: float, *, env: dict[str, str] | None = None, cwd: str | None = None,
                input_text: str | None = None) -> tuple[int, str, str]:
    """``subprocess.run(..., timeout=)`` réellement borné : (code, sortie, erreurs).

    ``subprocess.run`` n'arrête à l'échéance que l'enfant direct puis attend SANS limite la fermeture des tubes. Avec
    une CLI .cmd (Claude Code installé par npm), l'enfant est cmd.exe et la vraie CLI un petit-enfant qui garde les
    tubes : l'appel durait autant que la CLI bloquée. Ici l'arbre entier est arrêté à l'échéance et la lecture finale
    des sorties est bornée elle aussi. Lève ``OSError`` (lancement impossible) ou ``subprocess.TimeoutExpired``."""
    options: dict[str, Any] = {"stdin": subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                               "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "bufsize": 0, "env": env, "cwd": cwd}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(command, **options)
    contain(process)
    captured = {"stdout": bytearray(), "stderr": bytearray()}

    def drain(stream, sink: bytearray) -> None:
        try:
            while True:
                data = stream.read(65536)
                if not data:
                    break
                if len(sink) < BOUNDED_OUTPUT_LIMIT:
                    sink.extend(data[:BOUNDED_OUTPUT_LIMIT - len(sink)])
        except (OSError, ValueError):
            pass

    def feed() -> None:
        try:
            process.stdin.write(input_text.encode("utf-8"))
        except (OSError, ValueError):
            pass
        finally:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass

    threads = {name: threading.Thread(target=drain, args=(getattr(process, name), captured[name]), daemon=True,
                                      name=f"cli-{name}") for name in ("stdout", "stderr")}
    if input_text is not None:
        threads["stdin"] = threading.Thread(target=feed, daemon=True, name="cli-stdin")
    for thread in threads.values():
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_tree(process)
    grace = time.monotonic() + BOUNDED_READ_GRACE
    for name, thread in threads.items():
        thread.join(timeout=max(0.0, grace - time.monotonic()))
        stream = getattr(process, name)
        # Un tube n'est fermé qu'une fois sa lecture finie ; un descendant détaché qui le garde ne retient pas l'appel.
        if not thread.is_alive() and stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def text(name: str) -> str:
        return bytes(captured[name]).decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")

    if timed_out:
        raise subprocess.TimeoutExpired(command, timeout, output=text("stdout"), stderr=text("stderr"))
    return process.returncode, text("stdout"), text("stderr")


def _quick(command: list[str], timeout: float | None = None, env: dict[str, str] | None = None) -> tuple[int, str]:
    try:
        code, stdout, stderr = run_bounded(command, STATUS_TIMEOUT if timeout is None else timeout,
                                           env=env if env is not None else cli_environment())
    except (OSError, subprocess.SubprocessError) as error:
        return -1, str(error)
    return code, stdout + stderr


def cli_version(executable: str) -> str:
    code, output = _quick([executable, "--version"])
    return output.strip().splitlines()[0][:80] if code == 0 and output.strip() else ""


CLAUDE_METHODS = {"claude.ai": "compte Claude", "claudeai": "compte Claude", "api_key": "clé API", "apikey": "clé API",
                  "console": "clé API", "oauth_token": "jeton d’abonnement", "oauthtoken": "jeton d’abonnement"}
SECRET_METHODS = {"token": "jeton d’abonnement", "api_key": "clé API"}


def login_state(provider: str, executable: str, environment: dict[str, str] | None = None,
                secret_kind: str = "") -> dict[str, Any]:
    """{logged_in, method, plan?, checked} d'après la CLI — jamais l'adresse ni l'organisation du compte."""
    state: dict[str, Any] = {"logged_in": False, "method": "", "checked": True}
    if provider == "claude":
        code, output = _quick([executable, "auth", "status", "--json"], env=environment)
        try:
            data = json.loads(output[output.find("{"):output.rfind("}") + 1]) if "{" in output else {}
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        state["checked"] = code != -1 and (bool(data) or code == 0)
        state["logged_in"] = bool(data.get("loggedIn"))
        method = str(data.get("authMethod") or "")
        plan = str(data.get("subscriptionType") or "")
        if state["logged_in"]:
            state["method"] = (SECRET_METHODS.get(secret_kind) if secret_kind else
                               CLAUDE_METHODS.get(method.lower(), method if re.fullmatch(r"[A-Za-z0-9._-]{1,30}", method) else ""))
        if plan and re.fullmatch(r"[A-Za-z0-9 ._-]{1,40}", plan):
            state["plan"] = plan
    else:
        code, output = _quick([executable, "login", "status"], env=environment)
        text = output.strip().lower()
        state["checked"] = code != -1
        state["logged_in"] = code == 0 and "logged in" in text and "not logged" not in text
        state["method"] = "compte ChatGPT" if "chatgpt" in text else "clé API" if "api key" in text else ""
    return state


def account_message(provider: str, installed: bool, state: dict[str, Any] | None = None, secret_kind: str = "") -> str:
    product = PRODUCTS[provider]
    if not installed:
        return (f"{product} n'est pas installé sur ce Worker (ou pas pour l'utilisateur qui exécute l'agent) : "
                f"installe-le ({INSTALL_HINTS[provider]}) puis vérifie à nouveau.")
    state = state or {}
    if state.get("logged_in"):
        return f"{product} prêt ({state.get('method') or 'connecté'})."
    if not state.get("checked", True):
        return f"{product} n'a pas répondu à la vérification de connexion : réessaie."
    if secret_kind:
        stored = {"token": "Jeton d’abonnement enregistré", "api_key": "Clé API enregistrée"}.get(secret_kind, "Identifiant enregistré")
        return (f"{stored} sur ce Worker, mais {product} ne confirme pas la connexion : vérifie-le ou choisis une "
                f"autre méthode dans {ACCOUNTS_HINT}.")
    return f"{product} est installé mais pas connecté sur ce Worker : connecte un compte dans {ACCOUNTS_HINT}."


def cli_status(provider: str, account_id: str = DEFAULT_ACCOUNT, root: str | Path | None = None) -> dict[str, Any]:
    """Installée ? connectée ? (pour ce compte) — sans jamais renvoyer l'adresse ni l'organisation du compte."""
    label = PROVIDERS.get(provider, provider)
    path = find_cli(provider)
    account_id = check_account_id(account_id)
    if not path:
        return {"provider": provider, "label": label, "account_id": account_id, "installed": False, "logged_in": False,
                "message": account_message(provider, False)}
    status: dict[str, Any] = {"provider": provider, "label": label, "account_id": account_id, "installed": True,
                              "version": cli_version(path), "executable": Path(path).name, "logged_in": False, "method": ""}
    try:
        environment = account_environment(provider, account_id, root)
    except AssistantError as error:
        return {**status, "message": str(error)}
    secret = read_account_secret(provider, account_id, root)
    state = login_state(provider, path, environment, secret["kind"] if secret else "")
    status["logged_in"], status["method"] = state["logged_in"], state["method"]
    if state.get("plan"):
        status["plan"] = state["plan"]
    home = account_home(provider, account_id, root)
    status["models"] = claude_models(home) if provider == "claude" else codex_models(home)
    status["efforts"] = list(CLAUDE_EFFORTS) if provider == "claude" else [effort for effort in EFFORTS if effort != "minimal"]
    if provider == "codex":
        status["defaults"] = codex_defaults(home)
    status["message"] = account_message(provider, True, state, secret["kind"] if secret else "")
    return status


# Modèles de Claude Code : alias (dernière version de chaque famille) et noms complets ; s'y ajoutent ceux que
# le compte débloque (``additionalModelOptionsCache`` de .claude.json, ex. Fable en contexte 1M).
CLAUDE_MODELS = (
    ("fable", "Fable (dernière version)", "Le plus capable : pièces complexes, longues demandes."),
    ("opus", "Opus (dernière version)", "Très capable, plus rapide que Fable."),
    ("sonnet", "Sonnet (dernière version)", "Équilibré : usage courant."),
    ("haiku", "Haiku (dernière version)", "Le plus rapide : retouches simples."),
    ("claude-fable-5-1", "Claude Fable 5.1", ""),
    ("claude-opus-5-5", "Claude Opus 5.5", ""),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5", ""),
    ("claude-haiku-4-5-20251001", "Claude Haiku 4.5", ""),
)
MAX_MODELS = 40
# Effort de réflexion : Claude Code « --effort » ; Codex « model_reasoning_effort » (niveaux propres à chaque modèle).
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def claude_models(config_dir: Path | None = None) -> list[dict[str, Any]]:
    """``config_dir`` : profil d'un compte ajouté (seul lu) ; sinon CLAUDE_CONFIG_DIR puis le dossier de l'utilisateur."""
    models = [{"id": model, "label": label, "description": description} for model, label, description in CLAUDE_MODELS]
    if config_dir is not None:
        folders = [Path(config_dir)]
    else:
        folders = [Path(os.environ["CLAUDE_CONFIG_DIR"])] if os.getenv("CLAUDE_CONFIG_DIR") else []
        folders.append(Path.home())
    data = next((value for value in (_read_json(folder / ".claude.json") for folder in folders) if isinstance(value, dict)), {})
    for option in data.get("additionalModelOptionsCache") or []:
        value = str(option.get("value") or "") if isinstance(option, dict) else ""
        if not MODEL_PATTERN.fullmatch(value) or any(model["id"] == value for model in models):
            continue
        label = str(option.get("label") or value)[:60] + (" · contexte 1M" if "[1m]" in value.lower() else "")
        models.append({"id": value, "label": label, "description": str(option.get("description") or "")[:160]})
    return models[:MAX_MODELS]


def _codex_home(home: Path | None = None) -> Path:
    return Path(home) if home is not None else Path(os.getenv("CODEX_HOME") or Path.home() / ".codex")


def codex_defaults(home: Path | None = None) -> dict[str, str]:
    """Modèle et effort par défaut de la configuration Codex du compte (config.toml, clés de premier niveau)."""
    try:
        import tomllib

        data = tomllib.loads((_codex_home(home) / "config.toml").read_text(encoding="utf-8"))
    except (ImportError, OSError, ValueError):
        return {}
    model, effort = str(data.get("model") or ""), str(data.get("model_reasoning_effort") or "")
    return {"model": model if MODEL_PATTERN.fullmatch(model) else "", "effort": effort if effort in EFFORTS else ""}


def codex_models(home: Path | None = None) -> list[dict[str, Any]]:
    """Modèles que le compte ChatGPT propose à Codex (cache models_cache.json de la CLI), dans son ordre."""
    data = _read_json(_codex_home(home) / "models_cache.json")
    entries = [item for item in (data.get("models") if isinstance(data, dict) else None) or [] if isinstance(item, dict)]
    listed = [item for item in entries if item.get("visibility") == "list"] or [item for item in entries if item.get("visibility") != "hide"]
    listed.sort(key=lambda item: item.get("priority") if isinstance(item.get("priority"), int) else 999)
    models = []
    for item in listed:
        slug = str(item.get("slug") or "")
        if not MODEL_PATTERN.fullmatch(slug):
            continue
        efforts = [str(level.get("effort")) for level in item.get("supported_reasoning_levels") or [] if isinstance(level, dict) and level.get("effort")]
        models.append({"id": slug, "label": str(item.get("display_name") or slug)[:60], "description": str(item.get("description") or "")[:160],
                       "efforts": efforts[:10], "default_effort": str(item.get("default_reasoning_level") or "")[:20]})
    return models[:MAX_MODELS]


def packages_status() -> dict[str, Any]:
    missing = []
    versions = {}
    for name in ("shapely", "svgwrite", "segno", "PIL"):
        try:
            module = __import__(name)
            versions[name] = str(getattr(module, "__version__", "") or "")
        except ImportError:
            missing.append("Pillow" if name == "PIL" else name)
    return {"ready": not missing, "missing": missing, "versions": versions}


def require_packages() -> None:
    state = packages_status()
    if not state["ready"]:
        raise AssistantError("Les bibliothèques de dessin de l'assistant manquent sur ce Worker (" + ", ".join(state["missing"])
                             + ") : clique « Installer les bibliothèques de dessin » dans la fenêtre de l'assistant de code "
                             "(Alpine Laser Studio).", "ASSIST_PACKAGES_MISSING")


# ---------------------------------------------------------------------------------------------------------
# Demande
# ---------------------------------------------------------------------------------------------------------
def _text(value: Any, limit: int) -> str:
    return str(value or "").replace("\x00", "").strip()[:limit]


def decode_image(value: Any, workdir: Path) -> tuple[Path, str] | None:
    """Image jointe (base64 ou data URL) → fichier du job et type MIME ; PNG, JPEG ou WebP."""
    encoded = str(value or "")
    if not encoded:
        return None
    if encoded.lstrip().startswith("data:") and "," in encoded:
        encoded = encoded.split(",", 1)[1]
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise AssistantError("Image jointe illisible (base64 invalide).", "IMAGE_INVALID", 400) from None
    if len(raw) > MAX_IMAGE_BYTES:
        raise AssistantError("Image jointe trop lourde (8 Mo au plus).", "IMAGE_TOO_LARGE", 413)
    kind = next((value for magic, value in IMAGE_TYPES.items() if raw.startswith(magic)), None)
    if not kind or (kind[0] == "webp" and raw[8:12] != b"WEBP"):
        raise AssistantError("Image jointe : PNG, JPEG ou WebP attendu.", "IMAGE_INVALID", 400)
    path = workdir / f"assistant-image.{kind[0]}"
    path.write_bytes(raw)
    return path, kind[1]


def settings_from(data: dict[str, Any]) -> dict[str, Any]:
    check = script_sandbox._clean_number
    return {"levels": int(check(data.get("levels", 1), "Nombre de niveaux", 1, 16)),
            "detail": check(data.get("detail", 6), "Niveau de détail", 1, 10),
            "width_mm": check(data.get("width_mm", 100), "Largeur", 1, 3000),
            "height_mm": check(data.get("height_mm", 100), "Hauteur", 1, 3000),
            "seed": int(check(data.get("seed", 1), "Graine", 0, 2**31 - 1))}


def turn_text(prompt: str, mode: str, settings: dict[str, Any], script: str, context: str, has_image: bool) -> str:
    lines = [f"Mode : {'image — une image est jointe, vectorise-la avec trace_levels/trace' if mode == 'image' else 'programme'}.",
             f"Réglages du studio : {settings['levels']} niveau(x) de gravure, niveau de détail {settings['detail']:g}/10 "
             f"(tolérance {script_sandbox.detail_tolerance(settings['detail']):g} mm), zone de dessin {settings['width_mm']:g} × {settings['height_mm']:g} mm."]
    if context:
        lines.append(f"Machine et matériau : {context}")
    if mode == "image" and not has_image:
        lines.append("Attention : aucune image n'a été jointe à cette demande.")
    if script:
        lines.append("Script actuel (modifie-le plutôt que de repartir de zéro, sauf demande contraire) :\n```python\n"
                     + script.strip() + "\n```")
    lines.append("Demande :\n" + prompt)
    return "\n\n".join(lines)


def repair_text(outcome: dict[str, Any]) -> str:
    where = f", ligne {outcome['line']}" if outcome.get("line") else ""
    return (f"Le script a échoué dans le bac à sable du Worker ({outcome.get('code') or 'SCRIPT_ERROR'}{where}) :\n"
            f"{_text(outcome.get('error'), 1500)}\n\nCorrige-le en respectant le contrat et renvoie le script COMPLET "
            "dans un seul bloc ```python.")


def extract_script(text: str) -> tuple[str, str]:
    """(script, explication) : le dernier bloc Python qui définit build(), sinon le dernier bloc Python."""
    blocks = [match.group(1) for match in CODE_BLOCK.finditer(text or "")]
    chosen = next((block for block in reversed(blocks) if "def build" in block), blocks[-1] if blocks else "")
    reply = CODE_BLOCK.sub("", text or "").strip()
    reply = re.sub(r"\n{3,}", "\n\n", reply)
    return chosen.strip() + ("\n" if chosen.strip() else ""), reply[:MAX_REPLY_CHARS]


# ---------------------------------------------------------------------------------------------------------
# Appel des CLI
# ---------------------------------------------------------------------------------------------------------
def _run(command: list[str], stdin_text: str, cwd: Path, workdir: Path, name: str, timeout: float,
         cancelled: Callable[[], bool] | None, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Lance la CLI (environnement du compte choisi), envoie la demande sur l'entrée standard et attend."""
    stdout_path, stderr_path = workdir / f"{name}.stdout", workdir / f"{name}.stderr"
    input_path = workdir / f"{name}.stdin"
    input_path.write_text(stdin_text, encoding="utf-8", newline="\n")
    environment = env if env is not None else cli_environment()
    try:
        with input_path.open("rb") as stdin, stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            # Groupe de processus à part (POSIX) / groupe Windows : l'annulation et l'échéance arrêtent toute l'arborescence
            # (une CLI npm est un .cmd : cmd.exe puis node en petit-enfant, qui continuait sinon à consommer le quota).
            process = subprocess.Popen(command, cwd=str(cwd), env=environment, stdin=stdin, stdout=stdout, stderr=stderr,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                       start_new_session=os.name != "nt")
            contain(process)
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if cancelled and cancelled():
                    terminate_tree(process)
                    raise AssistantError("Demande à l'assistant annulée.", "CANCELLED")
                if time.monotonic() > deadline:
                    terminate_tree(process)
                    raise AssistantError(f"L'assistant n'a pas répondu en {timeout // 60:g} min : réessaie ou simplifie la demande.",
                                         "ASSISTANT_TIMEOUT")
                time.sleep(0.2)
    except OSError as error:
        raise AssistantError(f"Lancement de la CLI impossible : {error}", "CLI_LAUNCH_FAILED") from None
    finally:
        # Sous Windows, un descendant tout juste arrêté peut garder l'entrée ouverte quelques instants : la suppression
        # est réessayée puis abandonnée (dossier de travail du job), elle ne masque jamais l'erreur réelle.
        for attempt in range(20):
            try:
                input_path.unlink(missing_ok=True)
                break
            except OSError:
                time.sleep(0.1)
    read = lambda path: path.read_bytes().decode("utf-8", errors="replace") if path.is_file() else ""  # noqa: E731
    return process.returncode, read(stdout_path), read(stderr_path)


def _tail(text: str, limit: int = 500) -> str:
    return " ".join(str(text or "").split())[-limit:]


def cli_timeout(effort: str) -> int:
    return LONG_EFFORT_TIMEOUT if effort in ("xhigh", "max", "ultra") else CLI_TIMEOUT


def ask_claude(executable: str, text: str, image: tuple[Path, str] | None, session_id: str, model: str,
               home: Path, workdir: Path, name: str, cancelled: Callable[[], bool] | None, effort: str = "",
               env: dict[str, str] | None = None, *, system_prompt: str | None = None, max_turns: int = 2,
               timeout: float | None = None, extra_flags: tuple[str, ...] = ()) -> dict[str, Any]:
    """``system_prompt``, ``max_turns``, ``timeout`` et ``extra_flags`` servent à l'Assistant IA du site
    (site_assistant.py, agent 1.40.0) ; sans eux, l'appel est celui d'Alpine Laser Studio, inchangé."""
    system_file = workdir / "assistant-system.txt"
    system_file.write_text(SYSTEM_PROMPT if system_prompt is None else system_prompt, encoding="utf-8", newline="\n")
    command = [executable, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--tools", "", "--safe-mode", "--strict-mcp-config", "--disable-slash-commands",
               "--system-prompt-file", str(system_file), "--max-turns", str(int(max_turns))]
    command += list(extra_flags)
    if model:
        command += ["--model", model]
    if effort:
        command += ["--effort", effort]
    if session_id:
        command += ["--resume", session_id]
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    if image:
        content.append({"type": "image", "source": {"type": "base64", "media_type": image[1],
                                                     "data": base64.b64encode(image[0].read_bytes()).decode("ascii")}})
    message = json.dumps({"type": "user", "message": {"role": "user", "content": content}}, ensure_ascii=False) + "\n"
    code, stdout, stderr = _run(command, message, home, workdir, name, cli_timeout(effort) if timeout is None else timeout,
                                cancelled, env)
    result, texts, session = None, [], ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        session = str(event.get("session_id") or session)
        if event.get("type") == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(str(block.get("text") or ""))
        elif event.get("type") == "result":
            result = event
    if result is None:
        detail = _tail(stderr) or _tail(stdout) or f"code {code}"
        if session_id and re.search(r"no conversation found|session.*not found", detail, re.IGNORECASE):
            raise AssistantError("Discussion Claude introuvable sur ce Worker : choisis-en une autre ou crée une nouvelle discussion.",
                                 "SESSION_NOT_FOUND")
        if re.search(r"log ?in|auth|credential|401", detail, re.IGNORECASE):
            raise AssistantError(f"Claude Code n'est pas connecté sur ce Worker : connecte un compte dans {ACCOUNTS_HINT}.",
                                 "CLI_NOT_LOGGED_IN")
        raise AssistantError(f"Claude Code s'est arrêté sans réponse : {detail}", "ASSISTANT_FAILED")
    answer = str(result.get("result") or "") or "\n".join(texts)
    if result.get("is_error"):
        raise AssistantError(f"Claude Code : {_tail(answer) or result.get('subtype') or 'erreur'}", "ASSISTANT_FAILED")
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    return {"text": answer, "session_id": str(result.get("session_id") or session),
            "usage": {"input_tokens": int(usage.get("input_tokens") or 0), "output_tokens": int(usage.get("output_tokens") or 0),
                      "cost_usd": float(result.get("total_cost_usd") or 0.0)}}


def ask_codex(executable: str, text: str, image: tuple[Path, str] | None, session_id: str, model: str,
              home: Path, workdir: Path, name: str, cancelled: Callable[[], bool] | None, effort: str = "",
              env: dict[str, str] | None = None, *, preamble: str = "", timeout: float | None = None,
              extra_flags: tuple[str, ...] = ()) -> dict[str, Any]:
    """``preamble`` (consignes placées en tête de l'entrée standard : Codex n'a pas de fichier d'invite système),
    ``timeout`` et ``extra_flags`` servent à l'Assistant IA du site ; sans eux, l'appel de Laser Studio est inchangé."""
    last = workdir / f"{name}.last.txt"
    last.unlink(missing_ok=True)
    command = [executable, "exec"] + (["resume"] if session_id else [])
    if image:
        command += ["-i", str(image[0])]
    command += ["--json", "--skip-git-repo-check", "-o", str(last), "-c", 'sandbox_mode="read-only"',
                "-c", 'approval_policy="never"', "-c", "features.computer_use=false", "-c", "features.browser_use=false",
                "-c", "features.image_generation=false"]
    command += list(extra_flags)
    if model:
        command += ["-m", model]
    if effort:
        command += ["-c", f'model_reasoning_effort="{effort}"']
    if session_id:
        command += [session_id, "-"]
    else:
        command += ["-C", str(home), "-"]
    code, stdout, stderr = _run(command, preamble + text if preamble else text, home, workdir, name,
                                cli_timeout(effort) if timeout is None else timeout, cancelled, env)
    session, messages, failure, usage = "", [], "", {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type") or "")
        if event.get("thread_id"):
            session = str(event["thread_id"])
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if kind == "item.completed" and item.get("type") in ("agent_message", "assistant_message"):
            messages.append(str(item.get("text") or ""))
        elif kind in ("turn.failed", "error"):
            error = event.get("error") if isinstance(event.get("error"), dict) else {}
            failure = str(error.get("message") or event.get("message") or failure)
        elif kind == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
    answer = messages[-1] if messages else (last.read_text(encoding="utf-8", errors="replace") if last.is_file() else "")
    if not answer.strip():
        detail = _tail(failure) or _tail(stderr) or _tail(stdout) or f"code {code}"
        if session_id and re.search(r"no (rollout|session|thread)|not found", detail, re.IGNORECASE):
            raise AssistantError("Discussion GPT introuvable sur ce Worker : choisis-en une autre ou crée une nouvelle discussion.",
                                 "SESSION_NOT_FOUND")
        if re.search(r"log ?in|auth|401|unauthori", detail, re.IGNORECASE):
            raise AssistantError(f"Codex n'est pas connecté sur ce Worker : connecte un compte ChatGPT dans {ACCOUNTS_HINT}.",
                                 "CLI_NOT_LOGGED_IN")
        raise AssistantError(f"Codex s'est arrêté sans réponse : {detail}", "ASSISTANT_FAILED")
    return {"text": answer, "session_id": session or session_id,
            "usage": {"input_tokens": int(usage.get("input_tokens") or 0), "output_tokens": int(usage.get("output_tokens") or 0),
                      "cost_usd": 0.0}}


ASK = {"claude": ask_claude, "codex": ask_codex}


def assistant_home(provider: str) -> Path:
    """Dossier de travail fixe de la CLI : les discussions créées ici se reprennent d'ici."""
    root = Path(os.getenv("ALPINE_WORKER_ROOT") or Path.home() / ".alpine-laser-assistant")
    home = root / "runtime" / "laser-assistant" / provider
    home.mkdir(parents=True, exist_ok=True)
    return home


def run_assistant(data: dict[str, Any], workdir: Path, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Demande complète : CLI, extraction du script, bac à sable, corrections ; renvoie le résultat du job."""
    provider = str(data.get("provider") or "").strip().lower()
    if provider not in PROVIDERS:
        raise AssistantError("Assistant inconnu (claude ou codex).", "PROVIDER_INVALID", 400)
    session_id = str(data.get("session_id") or "").strip().lower()
    if session_id and not SESSION_PATTERN.fullmatch(session_id):
        raise AssistantError("Identifiant de discussion invalide (UUID attendu).", "SESSION_INVALID", 400)
    model = str(data.get("model") or "").strip()
    if model and not MODEL_PATTERN.fullmatch(model):
        raise AssistantError("Nom de modèle invalide.", "MODEL_INVALID", 400)
    effort = str(data.get("effort") or "").strip().lower()
    if effort and effort not in (CLAUDE_EFFORTS if provider == "claude" else EFFORTS):
        raise AssistantError(f"Effort « {effort[:20]} » inconnu pour {PROVIDERS[provider]}.", "EFFORT_INVALID", 400)
    prompt = _text(data.get("prompt"), MAX_PROMPT_CHARS)
    if not prompt:
        raise AssistantError("Décris ce que l'assistant doit dessiner.", "PROMPT_EMPTY", 400)
    mode = "image" if data.get("mode") == "image" else "program"
    settings = settings_from(data)
    current = str(data.get("script") or "")[: script_sandbox.MAX_SCRIPT_CHARS]
    repairs = max(0, min(MAX_REPAIRS, int(data.get("repairs", 1) or 0)))
    context = _text(data.get("context"), MAX_CONTEXT_CHARS)
    # Compte de la CLI (Mes Workers → Comptes IA) ; une discussion se reprend toujours avec son propre compte.
    account_id = check_account_id(data.get("account_id"))
    environment = account_environment(provider, account_id)
    require_packages()
    executable = find_cli(provider)
    if not executable:
        raise AssistantError(account_message(provider, False), "CLI_MISSING")
    workdir.mkdir(parents=True, exist_ok=True)
    image = decode_image(data.get("image_b64"), workdir) if mode == "image" else None
    home = assistant_home(provider)
    ask = ASK[provider]
    answer = ask(executable, turn_text(prompt, mode, settings, current, context, bool(image)), image, session_id, model,
                 home, workdir, "assistant-1", cancelled, effort, environment)
    # Première réponse obtenue avec un jeton / une clé collés depuis le site : ils sont désormais vérifiés.
    if read_account_secret(provider, account_id) is not None:
        mark_account_secret_verified(provider, account_id)
    session_id = answer["session_id"] or session_id
    usage = dict(answer["usage"])
    script, reply = extract_script(answer["text"])
    result: dict[str, Any] = {"provider": provider, "account_id": account_id, "session_id": session_id, "model": model,
                              "effort": effort, "reply": reply, "script": script, "attempts": 1, "repairs": 0, "script_ok": False}
    if not script:
        result["script_error"] = {"error": "La réponse ne contient pas de script : précise la demande.", "code": "NO_SCRIPT"}
        result["usage"] = usage
        return result
    # Les valeurs par défaut du script font foi : une cote changée par l'assistant n'est pas écrasée par l'ancienne.
    request = {**settings, "params": {}, "image_path": str(image[0]) if image else ""}
    for attempt in range(repairs + 1):
        outcome = script_sandbox.run_isolated({**request, "script": script}, workdir / "sandbox", SCRIPT_TIMEOUT, cancelled)
        if outcome.get("ok"):
            result.update({key: outcome.get(key) for key in ("svg", "preview_svg", "layers", "stats", "warnings", "params",
                                                             "params_spec", "log", "width_mm", "height_mm", "tolerance_mm")})
            result.update(script=script, script_ok=True)
            result.pop("script_error", None)
            break
        result["script_error"] = {key: outcome.get(key) for key in ("error", "code", "line")}
        result["script"] = script
        if outcome.get("code") == "CANCELLED":
            raise AssistantError("Demande à l'assistant annulée.", "CANCELLED")
        if attempt >= repairs or not session_id:
            break
        answer = ask(executable, repair_text(outcome), None, session_id, model, home, workdir, f"assistant-{attempt + 2}", cancelled,
                     effort, environment)
        result["attempts"] += 1
        result["repairs"] += 1
        for key in ("input_tokens", "output_tokens", "cost_usd"):
            usage[key] = usage.get(key, 0) + answer["usage"].get(key, 0)
        fixed, fixed_reply = extract_script(answer["text"])
        if fixed_reply:
            result["reply"] = (reply + "\n\n" + fixed_reply).strip()[:MAX_REPLY_CHARS]
        if not fixed:
            break
        script = fixed
    result["usage"] = usage
    return result


def run_script(data: dict[str, Any], workdir: Path, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Réexécution d'un script (modifié ou avec d'autres paramètres) sans appeler d'assistant."""
    require_packages()
    settings = settings_from(data)
    script = str(data.get("script") or "")
    workdir.mkdir(parents=True, exist_ok=True)
    image = decode_image(data.get("image_b64"), workdir) if data.get("image_b64") else None
    request = {**settings, "script": script, "params": data.get("params") if isinstance(data.get("params"), dict) else {},
               "image_path": str(image[0]) if image else ""}
    outcome = script_sandbox.run_isolated(request, workdir / "sandbox", SCRIPT_TIMEOUT, cancelled)
    if outcome.get("code") == "CANCELLED":
        raise AssistantError("Exécution du script annulée.", "CANCELLED")
    return {"script": script, "script_ok": bool(outcome.get("ok")), **outcome}


def requested_accounts(data: dict[str, Any] | None) -> dict[str, str]:
    """Compte vérifié par fournisseur : ``accounts`` {claude, codex}, ou ``account_id`` (+ ``provider``) ; sinon défaut.

    Un ``account_id`` sans fournisseur s'applique au fournisseur qui possède ce compte (identifiants propres à chacun)."""
    data = data if isinstance(data, dict) else {}
    chosen = {provider: DEFAULT_ACCOUNT for provider in PROVIDERS}
    accounts = data.get("accounts")
    if isinstance(accounts, dict):
        for provider in PROVIDERS:
            if accounts.get(provider) is not None:
                chosen[provider] = check_account_id(accounts.get(provider))
        return chosen
    account_id = check_account_id(data.get("account_id"))
    provider = str(data.get("provider") or "").strip().lower()
    if account_id == DEFAULT_ACCOUNT:
        return chosen
    if provider:
        if provider not in PROVIDERS:
            raise AssistantError("Assistant inconnu (claude ou codex).", "PROVIDER_INVALID", 400)
        chosen[provider] = account_id
        return chosen
    owners = [name for name in PROVIDERS if account_dir(name, account_id).is_dir()]
    for name in owners or list(PROVIDERS):
        chosen[name] = account_id
    return chosen


def status(data: dict[str, Any] | None = None) -> dict[str, Any]:
    """État des deux CLI ; compte par défaut sauf ``account_id`` / ``accounts`` (voir ``requested_accounts``)."""
    accounts = requested_accounts(data)
    return {"providers": {provider: cli_status(provider, accounts[provider]) for provider in PROVIDERS},
            "packages": packages_status()}
