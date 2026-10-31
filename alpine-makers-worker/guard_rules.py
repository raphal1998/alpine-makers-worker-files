"""Règles d'Alpine Guard (surveillance des impressions 3D) : réglages, phases de l'imprimante, fusion des signaux,
décision temporelle, agrégats par minute et lecture des refus de commande de la P1S.

Module pur (bibliothèque standard seulement) partagé par l'agent Worker (``guard.py``) et le site
(``guard_service.py`` à la racine du dashboard). Aucun code, modèle ni service PrintGuard : la décision reprend les
principes du « Hub » (seuil, images consécutives, délai de grâce, cooldown, une exécution suivie par impression,
agrégats de risque par minute) réécrits ici.

Signaux (score 0–1 chacun, calculés par ``guard_engine`` sur le Worker, sauf ``void`` et ``camera_frozen`` déduits
ici de la télémétrie et de l'âge des images) :

- santé de la caméra : ``camera_frozen`` (image figée), ``camera_dark`` (noire), ``camera_masked`` (objectif masqué,
  image sans détail) ; ils signalent une surveillance aveugle (avertissement), jamais une pause ;
- impression : ``spaghetti`` (amas filamenteux dans la zone), ``detachment`` (pièce déplacée ou disparue),
  ``void`` (les couches avancent sans changement visible : impression dans le vide), ``blob`` (silhouette de la
  tête qui grossit : amas sur la buse), ``model`` (modèle facultatif Obico, AGPL-3.0, téléchargé par le Worker).

Le risque d'une image est le plus fort des signaux d'impression pondérés. Les seuils par défaut sont des points de
départ prudents, à régler sur la machine (revue vraie / fausse alerte) : rien ici ne garantit qu'un défaut sera vu.
"""
import math
import re
import time

CAPABILITY = "guard_v1"
MIN_AGENT_VERSION = "1.52.0"
COMPONENT_ID = "guard-engine"
MODEL_COMPONENT_ID = "guard-obico"
TOOL_ID = "alpine-guard"

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

RESPONSES = ("notify", "pause", "stop")
RESPONSE_LABELS = {"notify": "notifier", "pause": "mettre en pause", "stop": "arrêter l'impression"}
CHOICES = ("printguard", "guard")
CAMERA_SIGNALS = ("camera_lost", "camera_frozen", "camera_dark", "camera_masked")
PRINT_SIGNALS = ("spaghetti", "detachment", "void", "blob", "model")
SIGNALS = CAMERA_SIGNALS + PRINT_SIGNALS
SIGNAL_LABELS = {
    "camera_lost": "caméra injoignable", "camera_frozen": "caméra figée", "camera_dark": "caméra noire",
    "camera_masked": "caméra masquée",
    "spaghetti": "amas « spaghetti »", "detachment": "pièce décollée ou déplacée", "void": "impression dans le vide",
    "blob": "amas sur la buse", "model": "modèle de détection",
}
# Poids par défaut : la vision classique décide seule ; le modèle facultatif compte autant qu'elle s'il est activé.
DEFAULT_WEIGHTS = {"spaghetti": 1.0, "detachment": 1.0, "void": 0.9, "blob": 0.7, "model": 1.0}

EVENT_KINDS = ("alert", "warning", "recovered", "error", "action", "run_started", "run_finished")
ACTION_STATUSES = ("requested", "confirmed", "refused", "unconfirmed", "skipped", "failed")
REQUEST_TYPES = ("action",)
MANUAL_ACTIONS = ("pause", "resume", "stop", "ignore")
REVIEW_VERDICTS = ("true", "false")

# Code HMS de la P1S quand une commande MQTT non signée est refusée (firmware 01.10, hors Mode LAN + Mode
# développeur) : constaté le 13.09.2026, la pause était « confirmée » par l'écho alors que l'impression continuait.
HMS_COMMAND_REFUSED = "0500-0500-0001-0007"
PAUSE_REFUSED_HELP = (
    "L’imprimante a refusé la commande (HMS 0500-0500-0001-0007 « MQTT Command verification failed ») : "
    "le firmware récent de la P1S exige une commande signée, sauf en Mode LAN uniquement + Mode développeur. "
    "Sur l’écran de l’imprimante : Réglages → Réseau → activer « LAN uniquement » puis « Mode développeur » ; "
    "ou mets l’impression en pause depuis l’écran ou Bambu Handy. L’impression continue tant que rien n’est fait.")

# Étapes « stg_cur » de la télémétrie Bambu (P1/X1) qui ne sont pas de l'impression : la surveillance ne s'arme pas
# pendant l'étalonnage, la chauffe, le changement de filament AMS ou une pause.
STAGES_PREPARING = frozenset({1, 2, 3, 7, 8, 9, 10, 11, 12, 13, 14, 15, 18, 19, 25, 29, 31})
STAGES_FILAMENT = frozenset({4, 22, 24})
STAGES_PAUSED = frozenset({5, 6, 16, 17, 20, 21, 23, 26, 27, 28, 30, 32, 33, 34, 35})

DEFAULT_CONFIG = {
    "enabled": False,
    "camera_id": None,
    "exclusive": False,          # Guard choisi pour cette imprimante : seules ses réponses automatiques s'appliquent
    "response": "notify",        # notify | pause | stop (pause/stop seulement si exclusive)
    "threshold": 0.60,           # risque d'une image au-delà duquel elle compte
    "warn_ratio": 0.75,          # quasi-alerte : risque ≥ threshold × warn_ratio (image conservée)
    "consecutive": 3,            # images consécutives au-delà du seuil avant l'alerte
    "grace_s": 120,              # délai après le début de l'impression (premières couches) sans décision
    "cooldown_s": 300,           # après une alerte : pas de nouvelle alerte ni réponse pendant ce délai
    "analysis_fps": 1.0,         # images analysées par seconde pendant l'impression (plafond)
    "zone": None,                # 4 coins du plateau [[x, y], …] normalisés 0–1 (cadrage caméra) ; None = image entière
    "zone_height": 0.35,         # part de la hauteur de la zone ajoutée au-dessus du plateau (la pièce monte)
    "signals": {name: {"enabled": True, "weight": DEFAULT_WEIGHTS[name]} for name in PRINT_SIGNALS},
    "camera_checks": True,       # avertir si la caméra est figée, noire ou masquée
    "frozen_s": 30,              # image identique pendant ce délai : caméra figée
    "void_layers": 4,            # couches gagnées sans changement visible avant « impression dans le vide »
    "void_window_s": 240,
    "use_model": False,          # modèle facultatif (module guard-obico installé sur le Worker)
    "notify": True,
    "auto_tune": False,          # revue « fausse alerte » : relève le seuil de ce moniteur
    "dataset_opt_in": False,     # images revues conservées pour entraîner plus tard notre propre modèle
    "retention_days": 30,
}
RANGES = {"threshold": (0.05, 0.95), "warn_ratio": (0.3, 0.95), "consecutive": (1, 30), "grace_s": (0, 1800),
          "cooldown_s": (0, 3600), "analysis_fps": (0.2, 4.0), "zone_height": (0.0, 1.0), "frozen_s": (5, 600),
          "void_layers": (2, 50), "void_window_s": (60, 1800), "retention_days": (1, 365)}
INT_KEYS = ("consecutive", "grace_s", "cooldown_s", "frozen_s", "void_layers", "void_window_s", "retention_days")
BOOL_KEYS = ("enabled", "exclusive", "camera_checks", "use_model", "notify", "auto_tune", "dataset_opt_in")


def clean_identifier(value, label="Identifiant", required=True):
    text = str(value or "").strip()
    if not text:
        if required:
            raise ValueError(f"{label} manquant.")
        return None
    if not IDENTIFIER.fullmatch(text):
        raise ValueError(f"{label} invalide.")
    return text


def _number(value, low, high, label, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{label} : nombre attendu.")
    number = float(value)
    if not low <= number <= high:
        raise ValueError(f"{label} : valeur hors de la plage {low:g}–{high:g}.")
    return int(round(number)) if integer else round(number, 4)


def clean_zone(value):
    """Quatre coins normalisés (cadrage caméra), ou None pour l'image entière."""
    if value in (None, "", []):
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("Zone d'impression : quatre coins attendus.")
    corners = []
    for corner in value:
        if not isinstance(corner, (list, tuple)) or len(corner) != 2:
            raise ValueError("Zone d'impression : coin invalide.")
        corners.append([_number(corner[0], 0.0, 1.0, "Zone"), _number(corner[1], 0.0, 1.0, "Zone")])
    xs, ys = [c[0] for c in corners], [c[1] for c in corners]
    if max(xs) - min(xs) < 0.05 or max(ys) - min(ys) < 0.03:
        raise ValueError("Zone d'impression trop petite.")
    return corners


def clean_config(raw, base=None):
    """Réglages complets d'un moniteur ; ``base`` = réglages actuels (mise à jour partielle). ValueError sinon."""
    config = _copy(base) if isinstance(base, dict) else _copy(DEFAULT_CONFIG)
    for key, value in DEFAULT_CONFIG.items():
        config.setdefault(key, _copy(value))
    if raw is None:
        return config
    if not isinstance(raw, dict):
        raise ValueError("Réglages invalides.")
    for key in BOOL_KEYS:
        if key in raw:
            if not isinstance(raw[key], bool):
                raise ValueError(f"{key} : vrai ou faux attendu.")
            config[key] = raw[key]
    for key, (low, high) in RANGES.items():
        if key in raw:
            config[key] = _number(raw[key], low, high, key, integer=key in INT_KEYS)
    if "response" in raw:
        if raw["response"] not in RESPONSES:
            raise ValueError("Réponse inconnue : notify, pause ou stop.")
        config["response"] = raw["response"]
    if "camera_id" in raw:
        config["camera_id"] = clean_identifier(raw["camera_id"], "Caméra", required=False)
    if "zone" in raw:
        config["zone"] = clean_zone(raw["zone"])
    if "signals" in raw:
        signals = raw["signals"]
        if not isinstance(signals, dict) or set(signals) - set(PRINT_SIGNALS):
            raise ValueError("Signaux inconnus.")
        for name, item in signals.items():
            if not isinstance(item, dict):
                raise ValueError(f"Signal {name} invalide.")
            current = dict(config["signals"].get(name) or DEFAULT_CONFIG["signals"][name])
            if "enabled" in item:
                if not isinstance(item["enabled"], bool):
                    raise ValueError(f"Signal {name} : enabled vrai ou faux.")
                current["enabled"] = item["enabled"]
            if "weight" in item:
                current["weight"] = _number(item["weight"], 0.0, 2.0, f"Poids {name}")
            config["signals"][name] = current
    if config["enabled"] and not config.get("camera_id"):
        raise ValueError("Choisis la caméra qui filme l'imprimante avant d'activer la surveillance.")
    return config


def _copy(value):
    if isinstance(value, dict):
        return {key: _copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy(item) for item in value]
    return value


def effective_response(config):
    """Réponse réellement appliquée : pause / arrêt seulement quand Guard est choisi pour l'imprimante."""
    response = config.get("response") or "notify"
    if response != "notify" and not config.get("exclusive"):
        return "notify"
    return response


def expanded_zone(zone, height):
    """Polygone analysé : le plateau (4 coins) prolongé vers le haut de l'image de ``height`` × sa hauteur."""
    if not zone:
        return None
    ys = [corner[1] for corner in zone]
    lift = (max(ys) - min(ys)) * float(height or 0.0)
    order = sorted(range(4), key=lambda index: zone[index][1])  # deux coins du fond (haut de l'image) d'abord
    far = set(order[:2])
    return [[corner[0], max(0.0, corner[1] - lift) if index in far else corner[1]] for index, corner in enumerate(zone)]


# --------------------------------------------------------------------------------------------- imprimante (P1S)
def hms_codes(telemetry):
    """Codes HMS lisibles « AAAA-BBBB-CCCC-DDDD » de la télémétrie Bambu (liste vide si absente)."""
    codes = []
    for item in (telemetry or {}).get("hms") or []:
        if not isinstance(item, dict):
            continue
        attr, code = item.get("attr"), item.get("code")
        if isinstance(attr, bool) or isinstance(code, bool) or not isinstance(attr, int) or not isinstance(code, int):
            continue
        codes.append(f"{(attr >> 16) & 0xFFFF:04X}-{attr & 0xFFFF:04X}-{(code >> 16) & 0xFFFF:04X}-{code & 0xFFFF:04X}")
    return codes


def command_refused(telemetry):
    return HMS_COMMAND_REFUSED in hms_codes(telemetry)


def _int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,6}", value.strip()):
        return int(value.strip())
    return None


def printer_phase(snapshot):
    """Phase de l'imprimante d'après sa télémétrie : printing, preparing, filament, paused, finished, failed, idle,
    offline ou unknown. Seule ``printing`` arme la surveillance (garde du Hub : une phase positive réveille ou
    endort ; offline / unknown gardent le dernier état connu)."""
    if not isinstance(snapshot, dict) or not snapshot.get("present"):
        return "unknown"
    if not snapshot.get("connected"):
        return "offline"
    telemetry = snapshot.get("telemetry") if isinstance(snapshot.get("telemetry"), dict) else {}
    state = str(telemetry.get("gcode_state") or "").upper()
    stage = _int(telemetry.get("stg_cur"))
    if state == "RUNNING":
        if stage in STAGES_PAUSED:
            return "paused"
        if stage in STAGES_FILAMENT:
            return "filament"
        if stage in STAGES_PREPARING:
            return "preparing"
        return "printing"
    if state == "PREPARE" or state == "SLICING":
        return "preparing"
    if state == "PAUSE":
        return "paused"
    if state == "FINISH":
        return "finished"
    if state == "FAILED":
        return "failed"
    if state == "IDLE":
        return "idle"
    return "unknown"


PHASE_LABELS = {"printing": "impression en cours", "preparing": "préparation / étalonnage", "filament": "changement de filament",
                "paused": "en pause", "finished": "impression terminée", "failed": "impression échouée", "idle": "à l'arrêt",
                "offline": "imprimante déconnectée", "unknown": "état inconnu"}
SLEEP_PHASES = ("paused", "finished", "failed", "idle")


class Gate:
    """Garde de surveillance : armée sur une phase « printing » positive, en veille sur une phase d'arrêt positive ;
    les phases intermédiaires (préparation, filament) suspendent sans clore l'impression suivie."""

    def __init__(self):
        self.phase = "unknown"
        self.printing = False      # impression suivie (exécution ouverte)
        self.armed = False         # décision active
        self.started_at = None     # début de l'exécution (heure murale)
        self.printing_since = None  # première image « printing » de l'exécution
        self.job = None

    def update(self, phase, now, job=None):
        """Renvoie ``"start"`` / ``"end"`` quand une exécution s'ouvre ou se clôt, sinon None."""
        self.phase = phase
        change = None
        if phase == "printing":
            if not self.printing:
                self.printing, self.started_at, change = True, now, "start"
                self.job = job
            if self.printing_since is None:
                self.printing_since = now
            self.armed = True
        elif phase in SLEEP_PHASES:
            if self.printing and phase in ("finished", "failed", "idle"):
                change = "end"
                self.printing, self.started_at, self.printing_since, self.job = False, None, None, None
            self.armed = False
        elif phase in ("preparing", "filament"):
            self.armed = False
        # offline / unknown : dernier état conservé, mais aucune décision sans télémétrie positive.
        elif phase in ("offline", "unknown"):
            self.armed = False
        return change

    def in_grace(self, now, grace_s):
        return self.printing_since is None or now - self.printing_since < float(grace_s)


# ------------------------------------------------------------------------------------------------- signaux
def clamp01(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, min(1.0, number))


def fuse(signals, config):
    """(risque, signal principal) des signaux d'impression activés, pondérés et bornés à 1."""
    best, top = 0.0, None
    for name in PRINT_SIGNALS:
        rule = (config.get("signals") or {}).get(name) or {}
        if not rule.get("enabled", True):
            continue
        if name == "model" and not config.get("use_model"):
            continue
        score = clamp01(signals.get(name)) * float(rule.get("weight", DEFAULT_WEIGHTS[name]))
        score = min(1.0, score)
        if score > best:
            best, top = score, name
    return round(best, 4), top


class VoidTracker:
    """Impression dans le vide : la télémétrie dit que les couches avancent, l'image de fond ne change pas."""

    def __init__(self):
        self.history = []   # (ts, couche, changement de fond sur 60 s ou None)

    def reset(self):
        self.history = []

    def update(self, now, layer, change, config):
        window = float(config.get("void_window_s") or 240)
        self.history.append((now, layer, change))
        self.history = [item for item in self.history if now - item[0] <= window]
        layers = [item[1] for item in self.history if isinstance(item[1], int)]
        changes = [item[2] for item in self.history if isinstance(item[2], (int, float))]
        if len(layers) < 2 or len(changes) < 3 or now - self.history[0][0] < window * 0.6:
            return 0.0
        gained = max(layers) - min(layers)
        if gained < int(config.get("void_layers") or 4):
            return 0.0
        # Changement de fond MÉDIAN pendant la fenêtre (pour mille de la zone, ``guard_engine.vision``) : une pièce qui
        # monte change toujours quelques pour mille de la zone en 60 s (son bord supérieur) ; la médiane ignore les pics
        # isolés (tête restée un instant dans le fond). Mesuré sur scènes synthétiques : ≈ 7 ‰ normal, ≈ 0 dans le vide.
        ordered = sorted(changes)
        middle = ordered[len(ordered) // 2] if len(ordered) % 2 else (ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2
        return round(clamp01((3.0 - middle) / 2.0), 4)


class Decision:
    """Décision temporelle d'un moniteur (reprise du Hub) : ``consecutive`` images au-delà du seuil, délai de grâce,
    cooldown, quasi-alertes et rétablissement. ``step`` renvoie la liste des événements à produire."""

    NEAR_EVERY_S = 60.0
    RECOVER_FRAMES = 5

    def __init__(self):
        self.streak = 0
        self.below = 0
        self.alert = None          # alerte en cours {at, score, signal}
        self.cooldown_until = 0.0
        self.last_near = float("-inf")

    def reset(self):
        self.__init__()

    def ignore(self, now, cooldown_s):
        """« Ignorer » : l'alerte en cours est levée et le cooldown démarre (aucune commande)."""
        self.alert = None
        self.streak = 0
        self.cooldown_until = now + float(cooldown_s)

    def step(self, now, risk, signal, config, armed, in_grace):
        out = []
        threshold = float(config["threshold"])
        if not armed or in_grace:
            self.streak = 0
            return out
        if risk >= threshold:
            self.streak += 1
            self.below = 0
            if (self.streak >= int(config["consecutive"]) and self.alert is None and now >= self.cooldown_until):
                self.alert = {"at": now, "score": risk, "signal": signal}
                self.cooldown_until = now + float(config["cooldown_s"])
                out.append(("alert", risk, signal))
            return out
        self.streak = 0
        if self.alert is not None:
            self.below += 1
            if self.below >= self.RECOVER_FRAMES:
                self.alert = None
                self.below = 0
                out.append(("recovered", risk, signal))
        if risk >= threshold * float(config["warn_ratio"]) and now - self.last_near >= self.NEAR_EVERY_S:
            self.last_near = now
            out.append(("near", risk, signal))
        return out


class CameraHealth:
    """Avertissements de caméra (figée, noire, masquée) tenus pendant ``hold_s`` avant d'être signalés."""

    def __init__(self, hold_s=8.0):
        self.hold_s = hold_s
        self.since = {}
        self.active = set()

    def reset(self):
        self.since, self.active = {}, set()

    def update(self, now, flags):
        """``flags`` : {signal: bool}. Renvoie (nouveaux signaux actifs, signaux rétablis)."""
        raised, cleared = [], []
        for name in CAMERA_SIGNALS:
            on = bool(flags.get(name))
            if on:
                self.since.setdefault(name, now)
                if name not in self.active and now - self.since[name] >= self.hold_s:
                    self.active.add(name)
                    raised.append(name)
            else:
                self.since.pop(name, None)
                if name in self.active:
                    self.active.discard(name)
                    cleared.append(name)
        return raised, cleared


class Buckets:
    """Agrégats de risque par minute UTC (images, somme, maximum, au-delà du seuil, secondes surveillées)."""

    def __init__(self):
        self.current = None
        self.done = []

    def add(self, ts, risk, threshold):
        minute = int(ts // 60) * 60
        bucket = self.current
        if bucket is not None and bucket["minute"] != minute:
            self.done.append(bucket)
            self.done = self.done[-240:]
            bucket = None
        if bucket is None:
            bucket = self.current = {"minute": minute, "frames": 0, "sum": 0.0, "max": 0.0, "over": 0,
                                     "first": ts, "last": ts}
        bucket["frames"] += 1
        bucket["sum"] = round(bucket["sum"] + risk, 4)
        bucket["max"] = max(bucket["max"], risk)
        bucket["over"] += 1 if risk >= threshold else 0
        bucket["last"] = ts

    def take(self):
        """Agrégats terminés (et la minute en cours) pour la synchronisation ; les terminés sont vidés."""
        items = list(self.done)
        self.done = []
        if self.current is not None:
            items.append(dict(self.current, open=True))
        return [{"minute": item["minute"], "frames": item["frames"], "sum": item["sum"], "max": round(item["max"], 4),
                 "over": item["over"], "watched_s": round(min(60.0, item["last"] - item["first"]), 1),
                 "open": bool(item.get("open"))} for item in items]


def suggested_threshold(reviews, current):
    """Seuil proposé par la revue : juste au-dessus des fausses alertes, sans repasser au-dessus des vraies.

    ``reviews`` : [(score, verdict)] ; renvoie None sans fausse alerte notée."""
    false_scores = [score for score, verdict in reviews if verdict == "false" and isinstance(score, (int, float))]
    true_scores = [score for score, verdict in reviews if verdict == "true" and isinstance(score, (int, float))]
    if not false_scores:
        return None
    proposal = max(false_scores) + 0.05
    if true_scores:
        proposal = min(proposal, min(true_scores) - 0.02)
    proposal = max(float(current), proposal)
    low, high = RANGES["threshold"]
    return round(max(low, min(high, proposal)), 2)


def now():
    return time.time()
