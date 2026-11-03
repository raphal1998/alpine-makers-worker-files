"""Famille CoreXY Bambu Lab (P1S, P1P, X1 Carbon, X1E) pour Alpine Model Engine.

Les constantes sont les caractéristiques publiques des machines (volume 256 × 256 × 256 mm, zone exclue avant-gauche
18 × 28 mm, 500 mm/s, 20 000 mm/s², buses 0,2/0,4/0,6/0,8, identifiants C12/C11/BL-P001/C13). Les séquences G-code
sont écrites ici à partir de la sémantique des commandes (docs/MODEL_ENGINE_GCODE_P1S.md) ; aucun template d'un autre
trancheur n'a été lu. Statut V1 : aucune commande vérifiée sur machine, tout est « à vérifier lors de l'impression
témoin ». A1, A1 mini et H2D : fiches de volume pour la mise en place, tranchage refusé avec un message clair.
"""
from __future__ import annotations

import math

from .base import (AxisLimits, Bed, Fans, GcodeSequence, Machine, PrintContext, PrinterUnsupportedError, Retraction)

VENDOR = "Bambu Lab"
NOZZLES_MM = (0.2, 0.4, 0.6, 0.8)
BED_CORE_XY = Bed(256.0, 256.0, 256.0, exclude_mm=(0.0, 0.0, 18.0, 28.0))

# Bannières des séquences : repères stables pour gcode.py et les tests.
START_BANNER = "; === Alpine Model Engine : démarrage"
START_END_BANNER = "; === Alpine Model Engine : fin du démarrage ==="
END_BANNER = "; === Alpine Model Engine : fin de l'impression"
END_END_BANNER = "; === Alpine Model Engine : fin ==="
FILAMENT_CHANGE_BANNER = "; === Alpine Model Engine : changement de filament"
PAUSE_BANNER = "; === Alpine Model Engine : pause"
TIMELAPSE_MARKER = "; TIMELAPSE_TAKE_FRAME"

# Étapes affichées à l'écran (M1002 gcode_claim_action : <n>) : numéros d'étape de la télémétrie Bambu « stg_cur »
# (liste communautaire, déjà utilisée par worker_agent/guard_rules.py). À vérifier lors de l'impression témoin.
STAGE_CLEAR = 0
STAGE_BED_LEVELLING = 1
STAGE_BED_PREHEAT = 2
STAGE_FILAMENT_CHANGE = 4
STAGE_NOZZLE_HEATING = 7
STAGE_HOMING = 13

# Ligne d'amorçage : le long du bord avant, hors zone exclue (x ≥ 18 mm, y ≤ 28 mm pris en compte par X_START).
PURGE_Y_MM = 5.0
PURGE_X_START_MM = 30.0
PURGE_X_END_MM = 200.0
PURGE_SPEED_MM_S = 25.0
PURGE_WIDTH_FACTOR = 2.0  # largeur de la ligne = 2 × diamètre de buse
PURGE_CLEARANCE_MM = 3.0  # dégagement Z avant/après la ligne

# Dégagement de fin : montée bornée et recul vers l'arrière, à l'intérieur du plateau.
END_LIFT_MM = 10.0
END_BACK_MARGIN_MM = 6.0

# Durées forfaitaires (secondes) des étapes sans mouvement planifié : estimation Alpine, à recaler sur la machine.
OVERHEAD_HOMING_S = 25.0
OVERHEAD_LEVELLING_S = 150.0
OVERHEAD_AMS_LOAD_S = 45.0
OVERHEAD_AMS_UNLOAD_S = 30.0
OVERHEAD_VIBRATION_S = 60.0
OVERHEAD_END_S = 15.0
OVERHEAD_FILAMENT_CHANGE_S = 60.0

_PRINTERS = {
    # clé: (nom affiché, identifiant modèle, buse d'origine, ventilateur auxiliaire, ventilateur de chambre, lit max)
    "P1S": ("Bambu Lab P1S", "C12", "stainless_steel", True, True, 100),
    "P1P": ("Bambu Lab P1P", "C11", "stainless_steel", False, False, 100),
    "X1C": ("Bambu Lab X1 Carbon", "BL-P001", "hardened_steel", True, True, 110),
    "X1E": ("Bambu Lab X1E", "C13", "hardened_steel", True, True, 110),
}
_ALIASES = {"X1 CARBON": "X1C", "X1-CARBON": "X1C", "X1CARBON": "X1C", "P1-S": "P1S", "P1-P": "P1P"}

# Machines connues pour la mise en place (volume public) mais non tranchables en V1.
UNSUPPORTED = {
    "A1": (Bed(256.0, 256.0, 256.0), "Bambu Lab A1 : lit mobile, séquences de démarrage non vérifiées ; tranchage prévu en V2."),
    "A1 mini": (Bed(180.0, 180.0, 180.0), "Bambu Lab A1 mini : lit mobile, séquences de démarrage non vérifiées ; tranchage prévu en V2."),
    "H2D": (Bed(350.0, 320.0, 325.0), "Bambu Lab H2D : double extrudeur, séquences non vérifiées ; tranchage prévu en V2."),
}


def _fmt(value: float, decimals: int = 3) -> str:
    """Nombre décimal sans zéros inutiles (déterministe)."""
    text = f"{float(value):.{decimals}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _feed(speed_mm_s: float) -> int:
    return max(1, int(round(float(speed_mm_s) * 60.0)))


def extrusion_area_mm2(width_mm: float, height_mm: float) -> float:
    """Section d'un cordon (rectangle à bouts arrondis) : (w − h)·h + π·h²/4 (spécification §6.1)."""
    w, h = float(width_mm), float(height_mm)
    return max(w - h, 0.0) * h + math.pi * h * h / 4.0


def filament_area_mm2(diameter_mm: float) -> float:
    return math.pi * (float(diameter_mm) / 2.0) ** 2


class BambuMachine(Machine):
    """CoreXY Bambu à lit fixe. Les séquences respectent : Z ∈ [0, hauteur], purge hors zone exclue, températures
    bornées, ventilation 0–255, E relatif, et seulement les commandes documentées dans MODEL_ENGINE_GCODE_P1S.md."""

    # --- séquence de démarrage ------------------------------------------------------------------
    def start_gcode(self, ctx: PrintContext) -> GcodeSequence:
        self.ensure_sliceable()
        bed_t = self.clamp_bed_temp(ctx.bed_temperature_initial_layer)
        nozzle_t = self.clamp_nozzle_temp(ctx.nozzle_temperature_initial_layer)
        standby_t = min(self.clamp_nozzle_temp(ctx.nozzle_standby_temperature), nozzle_t)
        g: list[str] = []
        g.append(f"{START_BANNER} {self.model} (buse {_fmt(self.nozzle_mm, 1)} mm) ===")
        g.append(f"M73 P0 R{max(0, int(ctx.estimated_minutes))} ; progression initiale (0 %, minutes restantes)")
        g.append("G90 ; coordonnées absolues")
        g.append("G21 ; unités en millimètres")
        g.append("M83 ; extrusion relative")
        a, v, j = self.max_accel, self.max_speed, self.max_jerk
        g.append(f"M201 X{_fmt(a.x, 0)} Y{_fmt(a.y, 0)} Z{_fmt(a.z, 0)} E{_fmt(a.e, 0)} ; accélérations maximales (mm/s²)")
        g.append(f"M203 X{_fmt(v.x, 0)} Y{_fmt(v.y, 0)} Z{_fmt(v.z, 0)} E{_fmt(v.e, 0)} ; vitesses maximales (mm/s)")
        default_accel = min(float(ctx.default_acceleration) or a.x, a.x)
        g.append(f"M204 S{_fmt(default_accel, 0)} ; accélération par défaut (mm/s²)")
        g.append(f"M205 X{_fmt(j.x, 1)} Y{_fmt(j.y, 1)} Z{_fmt(j.z, 1)} E{_fmt(j.e, 1)} ; jerk (mm/s)")
        g.append("M106 S0 ; ventilateur de la pièce éteint")
        if self.fans.aux:
            g.append("M106 P2 S0 ; ventilateur auxiliaire éteint")
        if self.fans.chamber:
            g.append("M106 P3 S0 ; ventilateur de chambre éteint")
        g.append(f"M1002 gcode_claim_action : {STAGE_BED_PREHEAT} ; écran : préchauffe du lit")
        g.append(f"M140 S{bed_t} ; chauffe du lit (première couche, plaque {ctx.bed_type})")
        g.append(f"M104 S{standby_t} ; préchauffe de la buse à la température d'attente")
        g.append(f"M190 S{bed_t} ; attente de la température du lit")
        g.append(f"M1002 gcode_claim_action : {STAGE_HOMING} ; écran : référencement")
        g.append("G28 ; référencement des trois axes")
        seconds = OVERHEAD_HOMING_S
        if ctx.vibration_cali:
            g.append("M975 S1 ; compensation de vibrations active")
            seconds += OVERHEAD_VIBRATION_S
        if ctx.bed_levelling:
            g.append(f"M1002 gcode_claim_action : {STAGE_BED_LEVELLING} ; écran : nivellement du lit")
            if ctx.first_layer_box_mm:
                x0, y0, x1, y1 = ctx.first_layer_box_mm
                g.append(f"; zone de première couche : X{_fmt(x0, 1)} Y{_fmt(y0, 1)} à X{_fmt(x1, 1)} Y{_fmt(y1, 1)} (information)")
            g.append("G29 ; nivellement automatique du lit (complet en V1, zone ciblée à vérifier)")
            seconds += OVERHEAD_LEVELLING_S
        g.append(f"M1002 gcode_claim_action : {STAGE_NOZZLE_HEATING} ; écran : chauffe de la buse")
        if ctx.use_ams and self.supports_ams:
            slot = ctx.ams_slot()
            g.append(f"M1002 gcode_claim_action : {STAGE_FILAMENT_CHANGE} ; écran : chargement du filament")
            g.append(f"M620 S{slot}A ; AMS : préparation de l'emplacement {slot}")
            g.append(f"M109 S{nozzle_t} ; buse à la température de première couche pour charger")
            g.append(f"T{slot} ; chargement du filament de l'emplacement {slot}")
            g.append(f"M621 S{slot}A ; AMS : fin du chargement")
            seconds += OVERHEAD_AMS_LOAD_S
        else:
            g.append(f"M109 S{nozzle_t} ; buse à la température de première couche (bobine externe)")
        g.append("M412 S1 ; détection de fin de filament active")
        extruded = 0.0
        position = None
        retracted = False
        h = max(0.05, float(ctx.first_layer_height_mm))
        z_clear = self.clamp_z(h + PURGE_CLEARANCE_MM)
        z_f = _feed(min(ctx.z_speed_mm_s, self.max_speed.z))
        if ctx.purge_line:
            width = PURGE_WIDTH_FACTOR * float(self.nozzle_mm)
            x0 = PURGE_X_START_MM
            x1 = min(PURGE_X_END_MM, self.bed.width_mm - PURGE_X_START_MM)
            y0 = PURGE_Y_MM
            y1 = y0 + width * 1.2
            if self.bed.in_exclusion(x0, y0) or self.bed.in_exclusion(x0, y1) or not self.bed.contains_xy(x1, y1):
                raise ValueError("Ligne d'amorçage hors plateau ou dans la zone exclue : constantes PURGE_* à revoir.")
            e_per_mm = extrusion_area_mm2(width, h) * float(ctx.flow_ratio) / filament_area_mm2(ctx.filament_diameter_mm)
            length = x1 - x0
            e_line = e_per_mm * length
            travel_f = _feed(min(ctx.travel_speed_mm_s, self.max_speed.x))
            g.append("; ligne d'amorçage le long du bord avant, hors zone exclue")
            g.append(f"G1 Z{_fmt(z_clear)} F{z_f} ; dégagement vertical")
            g.append(f"G1 X{_fmt(x0)} Y{_fmt(y0)} F{travel_f} ; départ de la ligne d'amorçage")
            g.append(f"G1 Z{_fmt(h)} F{z_f} ; descente à la hauteur de première couche")
            g.append(f"G1 X{_fmt(x1)} Y{_fmt(y0)} E{e_line:.5f} F{_feed(PURGE_SPEED_MM_S)} ; amorçage, aller")
            g.append(f"G1 X{_fmt(x1)} Y{_fmt(y1)} E{e_per_mm * (y1 - y0):.5f} ; raccord")
            g.append(f"G1 X{_fmt(x0)} Y{_fmt(y1)} E{e_line:.5f} ; amorçage, retour")
            retract = max(0.0, float(ctx.retraction_length_mm))
            if retract > 0:
                g.append(f"G1 E-{retract:.5f} F{_feed(ctx.retraction_speed_mm_s)} ; rétraction après l'amorçage")
                retracted = True
            g.append(f"G1 Z{_fmt(z_clear)} F{z_f} ; dégagement après l'amorçage")
            extruded = 2 * e_line + e_per_mm * (y1 - y0)
            position = (x0, y1, z_clear)
            seconds += 2 * length / PURGE_SPEED_MM_S + 10.0
        else:
            # Sans amorçage, la hauteur reste connue : le premier déplacement vers l'objet se fait en dégagement.
            g.append(f"G1 Z{_fmt(z_clear)} F{z_f} ; dégagement vertical (sans ligne d'amorçage)")
        g.append(f"M1002 gcode_claim_action : {STAGE_CLEAR} ; écran : préparatifs terminés")
        g.append(START_END_BANNER)
        seconds += self._heating_seconds(bed_t, nozzle_t)
        return GcodeSequence(lines=g, position_mm=position, z_mm=z_clear, retracted=retracted, extruded_mm=extruded, seconds=seconds)

    def _heating_seconds(self, bed_t: int, nozzle_t: int, from_c: float = 25.0) -> float:
        bed = max(0.0, bed_t - from_c) / max(0.05, self.bed_heat_rate_c_s)
        nozzle = max(0.0, nozzle_t - from_c) / max(0.05, self.nozzle_heat_rate_c_s)
        return bed + nozzle

    # --- fin d'impression -------------------------------------------------------------------------
    def end_gcode(self, ctx: PrintContext) -> GcodeSequence:
        g: list[str] = []
        g.append(f"{END_BANNER} ===")
        retract = max(0.0, float(ctx.retraction_length_mm))
        if retract > 0 and not ctx.retracted:
            g.append(f"G1 E-{retract:.5f} F{_feed(ctx.retraction_speed_mm_s)} ; rétraction finale")
        z_lift = self.clamp_z(float(ctx.z_mm) + END_LIFT_MM)
        z_f = _feed(min(ctx.z_speed_mm_s, self.max_speed.z))
        travel_f = _feed(min(ctx.travel_speed_mm_s, self.max_speed.x))
        g.append(f"G1 Z{_fmt(z_lift)} F{z_f} ; montée bornée à la hauteur imprimable")
        y_back = max(0.0, self.bed.depth_mm - END_BACK_MARGIN_MM)
        g.append(f"G1 Y{_fmt(y_back)} F{travel_f} ; dégagement de la tête vers l'arrière")
        seconds = OVERHEAD_END_S
        if ctx.use_ams and self.supports_ams:
            g.append("M620 S255 ; AMS : préparation du retour du filament")
            g.append("T255 ; retour du filament dans l'AMS")
            g.append("M621 S255 ; AMS : fin du retour")
            seconds += OVERHEAD_AMS_UNLOAD_S
        g.append("M140 S0 ; lit éteint")
        g.append("M104 S0 ; buse éteinte")
        g.append("M106 S0 ; ventilateur de la pièce éteint")
        if self.fans.aux:
            g.append("M106 P2 S0 ; ventilateur auxiliaire éteint")
        if self.fans.chamber:
            g.append("M106 P3 S0 ; ventilateur de chambre éteint")
        g.append("M73 P100 R0 ; impression terminée")
        g.append(END_END_BANNER)
        return GcodeSequence(lines=g, position_mm=None, retracted=True, extruded_mm=0.0, seconds=seconds)

    # --- changement de couche -----------------------------------------------------------------------
    def layer_change_gcode(self, ctx: PrintContext) -> GcodeSequence:
        index = max(1, int(ctx.layer_index))
        g = [f"M73 L{index} ; couche courante", f"M991 S0 P{index - 1} ; notification de couche au firmware"]
        if ctx.timelapse:
            g.append(f"{TIMELAPSE_MARKER} ; timelapse demandé : M971/M1004 non émis en V1 (paramètres non documentés)")
        return GcodeSequence(lines=g, position_mm=None, retracted=ctx.retracted, seconds=0.0)

    # --- changement de filament (multi-matériau, vague D) -------------------------------------------
    def filament_change_gcode(self, ctx: PrintContext) -> GcodeSequence:
        new_slot = ctx.ams_slot(ctx.next_extruder)
        nozzle_t = self.clamp_nozzle_temp(ctx.nozzle_temperature)
        g: list[str] = []
        g.append(f"{FILAMENT_CHANGE_BANNER} {ctx.previous_extruder} -> {ctx.next_extruder} ===")
        g.append(f"M1002 gcode_claim_action : {STAGE_FILAMENT_CHANGE} ; écran : changement de filament")
        retract = max(0.0, float(ctx.retraction_length_mm))
        if retract > 0 and not ctx.retracted:
            g.append(f"G1 E-{retract:.5f} F{_feed(ctx.retraction_speed_mm_s)} ; rétraction avant le changement")
        z_lift = self.clamp_z(float(ctx.z_mm) + 2.0)
        g.append(f"G1 Z{_fmt(z_lift)} F{_feed(min(ctx.z_speed_mm_s, self.max_speed.z))} ; dégagement vertical borné")
        if ctx.use_ams and self.supports_ams:
            g.append(f"M620 S{new_slot}A ; AMS : préparation de l'emplacement {new_slot}")
            g.append(f"M109 S{nozzle_t} ; température du nouveau filament")
            g.append(f"T{new_slot} ; chargement du filament de l'emplacement {new_slot}")
            g.append(f"M621 S{new_slot}A ; AMS : fin du chargement")
        else:
            g.append(f"M109 S{nozzle_t} ; température du nouveau filament (changement manuel non géré en V1)")
        g.append("; purge dans la tour : parcours fournis par le planificateur (feature Custom)")
        g.append(f"M1002 gcode_claim_action : {STAGE_CLEAR} ; écran : reprise")
        # XY inconnue (le chargement AMS peut déplacer la tête) ; Z connue : l'émetteur redescend avant d'extruder.
        return GcodeSequence(lines=g, position_mm=None, z_mm=z_lift, retracted=True, seconds=OVERHEAD_FILAMENT_CHANGE_S)

    # --- pause demandée --------------------------------------------------------------------------------
    def pause_gcode(self, ctx: PrintContext | None = None) -> GcodeSequence:
        g = [f"{PAUSE_BANNER} ===", "M400 U1 ; attendre la fin des mouvements puis pause utilisateur (à vérifier)"]
        return GcodeSequence(lines=g, position_mm=None, retracted=bool(ctx.retracted) if ctx else False, seconds=0.0)


def _normalise_model(model: str) -> str:
    key = str(model or "").strip()
    for prefix in (VENDOR + " ", "bambu lab ", "bambulab "):
        if key.lower().startswith(prefix.lower()):
            key = key[len(prefix):]
    key = key.strip().upper()
    return _ALIASES.get(key, key)


def _nozzle_value(nozzle) -> float:
    try:
        value = round(float(str(nozzle).replace("mm", "").strip()), 2)
    except (TypeError, ValueError):
        raise PrinterUnsupportedError(f"Diamètre de buse invalide : {nozzle!r}.") from None
    if value not in NOZZLES_MM:
        raise PrinterUnsupportedError(f"Buse {value} mm inconnue pour {VENDOR} (buses : {', '.join(str(n) for n in NOZZLES_MM)}).")
    return value


def preset_name(model_key: str, nozzle_mm: float) -> str:
    """Nom de préréglage conservé pour les clients, ex. « Bambu Lab P1S 0.4 nozzle »."""
    return f"{_PRINTERS[model_key][0]} {_fmt(nozzle_mm, 1)} nozzle"


def machine(model: str, nozzle_mm=0.4) -> BambuMachine:
    """Fiche d'une imprimante tranchable (P1S, P1P, X1C, X1E) pour une buse donnée. A1/A1 mini/H2D : refus clair."""
    key = _normalise_model(model)
    for name, (_bed, reason) in UNSUPPORTED.items():
        if key == name.upper():
            raise PrinterUnsupportedError(reason)
    if key not in _PRINTERS:
        raise PrinterUnsupportedError(f"Imprimante inconnue : {model!r}. Modèles pris en charge : P1S, P1P, X1C, X1E.")
    nozzle = _nozzle_value(nozzle_mm)
    display, model_id, nozzle_type, aux, chamber, bed_max = _PRINTERS[key]
    return BambuMachine(
        id=f"bambu-{key.lower()}-{_fmt(nozzle, 1)}",
        model_id=model_id,
        name=preset_name(key, nozzle),
        vendor=VENDOR,
        model=display,
        bed=BED_CORE_XY,
        nozzle_mm=nozzle,
        nozzle_type=nozzle_type,
        max_speed=AxisLimits(500.0, 500.0, 20.0, 30.0),
        max_accel=AxisLimits(20000.0, 20000.0, 500.0, 5000.0),
        max_jerk=AxisLimits(9.0, 9.0, 3.0, 2.5),
        retraction=Retraction(length_mm=0.8, speed_mm_s=30.0, deretraction_speed_mm_s=30.0, min_travel_mm=2.0, z_hop_mm=0.4),
        fans=Fans(part=True, aux=aux, chamber=chamber),
        max_bed_temp_c=bed_max,
        supports_ams=True,
    )


def machine_for_preset(preset: str) -> BambuMachine:
    """Fiche à partir du nom de préréglage des clients (« Bambu Lab X1 Carbon 0.6 nozzle »)."""
    text = str(preset or "").strip()
    lowered = text.lower()
    if not lowered.endswith(" nozzle"):
        raise PrinterUnsupportedError(f"Préréglage d'imprimante inconnu : {preset!r}.")
    body = text[: -len(" nozzle")].strip()
    head, _, nozzle = body.rpartition(" ")
    if not head:
        raise PrinterUnsupportedError(f"Préréglage d'imprimante inconnu : {preset!r}.")
    return machine(head, nozzle)


def bed_for_model(model: str) -> Bed:
    """Volume d'une machine Bambu connue (tranchable ou non) pour la mise en place sur le plateau."""
    key = _normalise_model(model)
    if key in _PRINTERS:
        return BED_CORE_XY
    for name, (bed, _reason) in UNSUPPORTED.items():
        if key == name.upper():
            return bed
    raise PrinterUnsupportedError(f"Imprimante inconnue : {model!r}.")


def is_sliceable(model: str) -> bool:
    try:
        return _normalise_model(model) in _PRINTERS
    except Exception:
        return False


def catalog() -> list[list[str]]:
    """[[vendor, preset_name, model, nozzle]] pour l'action ``list_profiles`` (mêmes valeurs que les clients attendent)."""
    rows = []
    for key in ("P1S", "P1P", "X1C", "X1E"):
        for nozzle in NOZZLES_MM:
            rows.append([VENDOR, preset_name(key, nozzle), _PRINTERS[key][0], _fmt(nozzle, 1)])
    return rows


def unsupported_catalog() -> list[dict]:
    """Machines connues mais refusées en V1 (pour un message clair côté site)."""
    return [{"model": f"{VENDOR} {name}", "bed": [bed.width_mm, bed.depth_mm, bed.height_mm], "reason": reason}
            for name, (bed, reason) in UNSUPPORTED.items()]
