"""Fiches filaments Alpine du trancheur Alpine Model Engine (spécification §6.3, ``profiles/filament.py``).

Une fiche par type de matériau : températures de buse (première couche, suivantes, plage), température du lit par
plaque (Cool / Textured PEI / High Temp (Smooth PEI) / Engineering), ventilation (minimum, maximum, surplombs,
premières couches, ventilateur auxiliaire, extraction de chambre), débit, débit volumique maximal, densité, diamètre,
coût indicatif, rétraction spécifique, temps minimal par couche.

Toutes les valeurs sont choisies par Alpine Makers à partir des plages publiques des fabricants de filament et de la
pratique courante sur une CoreXY fermée à lit fixe (P1S : lit ≤ 100 °C, buse ≤ 300 °C, ventilateur auxiliaire et
extraction de chambre). Aucune valeur ne provient d'un profil OrcaSlicer / Bambu Studio. Chaque fiche porte une note
en français qui explique les choix ; l'utilisateur pourra les modifier plus tard (réglages du processus).

Pure Python (pas de numpy) : le site peut importer ce module pour afficher les fiches.
"""
from __future__ import annotations

import copy
import re

FILAMENT_DIAMETER_MM = 1.75
NOZZLE_TEMP_MIN_C = 150
NOZZLE_TEMP_MAX_C = 300
BED_TEMP_MAX_C = 110          # borne absolue d'une fiche (la machine applique ensuite sa propre limite, 100 °C sur P1S)
VENDOR = "Alpine Makers"

# Plaques reconnues (noms longs = contrat client ``bed_type``), nom court du fichier tranché et clé de température.
PLATES = ("Cool Plate", "Textured PEI Plate", "High Temp Plate", "Engineering Plate")
PLATE_SHORT = {"Cool Plate": "cool_plate", "Textured PEI Plate": "textured_plate",
               "High Temp Plate": "hot_plate", "Engineering Plate": "eng_plate"}
PLATE_TEMP_KEYS = {"Cool Plate": "cool_plate_temp", "Textured PEI Plate": "textured_plate_temp",
                   "High Temp Plate": "hot_plate_temp", "Engineering Plate": "eng_plate_temp"}
_PLATE_ALIASES = {
    "coolplate": "Cool Plate", "cool": "Cool Plate", "coolplatepla": "Cool Plate",
    "texturedpeiplate": "Textured PEI Plate", "texturedplate": "Textured PEI Plate", "textured": "Textured PEI Plate",
    "texturedpei": "Textured PEI Plate",
    "hightempplate": "High Temp Plate", "hotplate": "High Temp Plate", "smoothpeiplate": "High Temp Plate",
    "smoothpeiplatehightempplate": "High Temp Plate", "smoothpei": "High Temp Plate", "hightemp": "High Temp Plate",
    "engineeringplate": "Engineering Plate", "engplate": "Engineering Plate", "engineering": "Engineering Plate",
}


class MaterialUnknownError(ValueError):
    """Matériau sans fiche Alpine (code d'erreur stable ``MATERIAL_UNKNOWN`` pour la CLI)."""

    code = "MATERIAL_UNKNOWN"


def _normalise(text) -> str:
    """Clé de comparaison : minuscules, sans espaces ni ponctuation (« PLA Basic » → « plabasic »)."""
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def normalise_plate(bed_type) -> str | None:
    """Nom long d'une plaque à partir d'un nom long, court (``textured_plate``) ou approchant ; None si inconnu."""
    key = _normalise(bed_type)
    if not key:
        return None
    if key in _PLATE_ALIASES:
        return _PLATE_ALIASES[key]
    for plate in PLATES:
        if _normalise(plate) == key:
            return plate
    return None


def _fiche(type_, name, *, nozzle, nozzle_range, beds, fan, colour, flow, max_volumetric, density, cost,
           slow_down=(6.0, 20.0), fan_layer_time=60.0, retraction=None, soluble=False, support=False, ams=True,
           hardened=False, dry=False, shrink=100.0, chamber=0, note=""):
    """Construit une fiche plate (dict JSON-sérialisable).

    ``nozzle`` = (première couche, suivantes) ; ``nozzle_range`` = (min, max) ; ``beds`` = {plaque: (première, suivantes)}
    avec (0, 0) pour une plaque déconseillée ; ``fan`` = (min, max, surplombs, couches sans ventilation, auxiliaire,
    extraction de chambre) ; ``slow_down`` = (temps mini par couche en s, vitesse mini en mm/s) ; ``retraction`` =
    dict d'écarts à la rétraction machine (TPU) ou None.
    """
    first, other = nozzle
    low, high = nozzle_range
    fan_min, fan_max, fan_overhang, fan_off_layers, fan_aux, fan_exhaust = fan
    fiche = {
        "type": type_,
        "name": name,
        "settings_id": f"{name} @Alpine",
        "vendor": VENDOR,
        "tray_info_idx": "",                 # identifiant AMS générique inconnu : laissé vide (spécification §6.3)
        "colour": colour,
        "nozzle_temperature_initial_layer": int(first),
        "nozzle_temperature": int(other),
        "nozzle_temperature_range_low": int(low),
        "nozzle_temperature_range_high": int(high),
        "fan_min_speed": int(fan_min),
        "fan_max_speed": int(fan_max),
        "overhang_fan_speed": int(fan_overhang),
        "overhang_fan_threshold": 25,        # % de porte-à-faux à partir duquel la ventilation de surplomb s'applique
        "close_fan_the_first_x_layers": int(fan_off_layers),
        "full_fan_speed_layer": int(fan_off_layers) + 2 if fan_max > 0 else 0,
        "additional_cooling_fan_speed": int(fan_aux),
        "exhaust_fan_speed": int(fan_exhaust),
        "chamber_temperature": int(chamber),
        "fan_cooling_layer_time": float(fan_layer_time),
        "slow_down_layer_time": float(slow_down[0]),
        "slow_down_min_speed": float(slow_down[1]),
        "flow_ratio": float(flow),
        "filament_max_volumetric_speed": float(max_volumetric),
        "filament_density": float(density),
        "filament_diameter": FILAMENT_DIAMETER_MM,
        "filament_cost": float(cost),
        "filament_shrink": float(shrink),
        "filament_retraction_length": None,
        "filament_retraction_speed": None,
        "filament_deretraction_speed": None,
        "filament_z_hop": None,
        "filament_wipe": None,
        "soluble": bool(soluble),
        "support_material": bool(support),
        "ams_compatible": bool(ams),
        "nozzle_hardened_required": bool(hardened),
        "dry_before_use": bool(dry),
        "note": note,
    }
    for plate in PLATES:
        initial, following = beds.get(plate, (0, 0))
        fiche[f"{PLATE_TEMP_KEYS[plate]}_initial_layer"] = int(initial)
        fiche[PLATE_TEMP_KEYS[plate]] = int(following)
    if retraction:
        for key, value in retraction.items():
            fiche[f"filament_{key}"] = value
    return fiche


# Lits usuels : (première couche, suivantes). Les plaques déconseillées valent (0, 0).
_BED_PLA = {"Cool Plate": (35, 35), "Textured PEI Plate": (60, 55), "High Temp Plate": (60, 55), "Engineering Plate": (60, 55)}
_BED_PETG = {"Cool Plate": (0, 0), "Textured PEI Plate": (70, 65), "High Temp Plate": (70, 65), "Engineering Plate": (70, 65)}
_BED_HOT = {"Cool Plate": (0, 0), "Textured PEI Plate": (100, 95), "High Temp Plate": (100, 95), "Engineering Plate": (100, 95)}
_BED_TPU = {"Cool Plate": (35, 35), "Textured PEI Plate": (35, 35), "High Temp Plate": (35, 35), "Engineering Plate": (35, 35)}
_BED_PCTG = {"Cool Plate": (0, 0), "Textured PEI Plate": (75, 70), "High Temp Plate": (75, 70), "Engineering Plate": (75, 70)}

# Fiches Alpine, dans l'ordre d'affichage. Les notes expliquent les choix (plages publiques + pratique courante).
FILAMENTS = {
    "PLA": _fiche(
        "PLA", "Alpine PLA", nozzle=(215, 210), nozzle_range=(190, 230), beds=_BED_PLA,
        fan=(60, 100, 100, 1, 70, 0), colour="#F2F2F2", flow=0.98, max_volumetric=15.0, density=1.24, cost=20.0,
        slow_down=(6.0, 20.0), fan_layer_time=60.0,
        note="PLA courant : 190–230 °C selon les fabricants, 215 °C en première couche pour l'adhérence, lit 55–60 °C sur "
             "PEI, 35 °C suffit sur la plaque froide. Ventilation forte dès la deuxième couche ; 15 mm³/s en buse acier "
             "inox de 0,4 mm."),
    "PLA Silk": _fiche(
        "PLA Silk", "Alpine PLA Silk", nozzle=(225, 220), nozzle_range=(200, 240), beds=_BED_PLA,
        fan=(60, 90, 100, 1, 50, 0), colour="#E6C35C", flow=0.97, max_volumetric=10.0, density=1.25, cost=25.0,
        slow_down=(8.0, 15.0), fan_layer_time=60.0,
        note="PLA brillant (additifs) : un peu plus chaud que le PLA pour garder le brillant, débit limité à 10 mm³/s car "
             "il s'étire mal à grande vitesse ; ventilation légèrement réduite."),
    "PLA-CF": _fiche(
        "PLA-CF", "Alpine PLA-CF", nozzle=(230, 225), nozzle_range=(210, 250), beds=_BED_PLA,
        fan=(50, 90, 100, 1, 50, 0), colour="#3A3A3A", flow=0.96, max_volumetric=12.0, density=1.26, cost=40.0,
        slow_down=(6.0, 20.0), fan_layer_time=60.0, hardened=True, dry=True,
        note="PLA chargé en fibres de carbone : plus chaud et plus abrasif (buse acier trempé recommandée, la buse "
             "d'origine de la P1S s'use), débit modéré, à sécher avant usage."),
    "PETG": _fiche(
        "PETG", "Alpine PETG", nozzle=(245, 240), nozzle_range=(220, 260), beds=_BED_PETG,
        fan=(30, 60, 80, 3, 20, 0), colour="#4FB3E8", flow=0.95, max_volumetric=10.0, density=1.27, cost=22.0,
        slow_down=(8.0, 15.0), fan_layer_time=80.0, dry=True,
        note="PETG : 230–250 °C, lit 70 °C (la plaque froide est déconseillée, l'adhérence y est trop forte ou "
             "insuffisante), ventilation modérée pour la résistance entre couches, trois couches sans ventilation. "
             "Débit 10 mm³/s : le PETG file au-delà."),
    "PETG HF": _fiche(
        "PETG HF", "Alpine PETG HF", nozzle=(240, 235), nozzle_range=(220, 260), beds=_BED_PETG,
        fan=(30, 70, 80, 3, 30, 0), colour="#2E86C1", flow=0.95, max_volumetric=18.0, density=1.27, cost=24.0,
        slow_down=(6.0, 20.0), fan_layer_time=70.0, dry=True,
        note="PETG haut débit (formulé pour les imprimantes rapides) : mêmes températures qu'un PETG, débit porté à "
             "18 mm³/s et ventilation un peu plus forte."),
    "PETG-CF": _fiche(
        "PETG-CF", "Alpine PETG-CF", nozzle=(255, 250), nozzle_range=(230, 270), beds=_BED_PETG,
        fan=(20, 50, 70, 3, 20, 0), colour="#2F3B45", flow=0.95, max_volumetric=10.0, density=1.30, cost=45.0,
        slow_down=(8.0, 15.0), fan_layer_time=80.0, hardened=True, dry=True,
        note="PETG chargé en fibres de carbone : 10 °C de plus que le PETG, ventilation réduite, buse trempée "
             "recommandée, séchage indispensable."),
    "ABS": _fiche(
        "ABS", "Alpine ABS", nozzle=(260, 255), nozzle_range=(240, 280), beds=_BED_HOT,
        fan=(10, 30, 50, 3, 0, 0), colour="#D9D9D9", flow=0.95, max_volumetric=12.0, density=1.04, cost=22.0,
        slow_down=(8.0, 15.0), fan_layer_time=90.0, shrink=99.5,
        note="ABS : 240–280 °C, lit 100 °C (maximum de la P1S), chambre fermée sans extraction pour garder la chaleur, "
             "ventilation faible (10–30 %) afin de limiter le retrait et le décollement ; plaque froide exclue."),
    "ASA": _fiche(
        "ASA", "Alpine ASA", nozzle=(260, 255), nozzle_range=(240, 280), beds=_BED_HOT,
        fan=(10, 30, 50, 3, 0, 0), colour="#F4F4F4", flow=0.95, max_volumetric=12.0, density=1.07, cost=28.0,
        slow_down=(8.0, 15.0), fan_layer_time=90.0, shrink=99.5,
        note="ASA (ABS résistant aux UV) : mêmes réglages que l'ABS, densité légèrement supérieure."),
    "TPU": _fiche(
        "TPU", "Alpine TPU 95A", nozzle=(230, 225), nozzle_range=(210, 240), beds=_BED_TPU,
        fan=(50, 80, 100, 2, 30, 0), colour="#FF8A3D", flow=1.02, max_volumetric=3.2, density=1.21, cost=35.0,
        slow_down=(10.0, 10.0), fan_layer_time=60.0, ams=False, dry=True,
        retraction={"retraction_length": 0.8, "retraction_speed": 20.0, "deretraction_speed": 20.0, "z_hop": 0.2,
                    "wipe": False},
        note="TPU souple 95A : lent (3,2 mm³/s), rétraction courte et lente sans essuyage pour éviter le bourrage dans "
             "l'extrudeur direct, lit tiède, débit légèrement augmenté (le TPU se comprime). À ne pas charger dans "
             "l'AMS : alimentation par la bobine externe."),
    "PC": _fiche(
        "PC", "Alpine PC", nozzle=(270, 265), nozzle_range=(250, 290), beds=_BED_HOT,
        fan=(0, 20, 40, 3, 0, 0), colour="#E8E8E8", flow=0.94, max_volumetric=8.0, density=1.20, cost=45.0,
        slow_down=(10.0, 15.0), fan_layer_time=100.0, dry=True, shrink=99.5,
        note="Polycarbonate (mélanges imprimables) : 250–290 °C, lit au maximum de la P1S, ventilation quasi nulle, "
             "débit faible ; sécher avant usage, chambre fermée."),
    "PA": _fiche(
        "PA", "Alpine PA", nozzle=(275, 270), nozzle_range=(255, 295), beds=_BED_HOT,
        fan=(0, 20, 40, 3, 0, 0), colour="#F7F1E1", flow=0.95, max_volumetric=8.0, density=1.12, cost=50.0,
        slow_down=(10.0, 15.0), fan_layer_time=100.0, dry=True, shrink=99.0,
        note="Polyamide (nylon) : 255–295 °C, lit 100 °C, ventilation quasi nulle, très hygroscopique : séchage "
             "indispensable ; retrait marqué, brim conseillé."),
    "PA-CF": _fiche(
        "PA-CF", "Alpine PA-CF", nozzle=(280, 275), nozzle_range=(260, 300), beds=_BED_HOT,
        fan=(0, 20, 40, 3, 0, 0), colour="#2B2B2B", flow=0.95, max_volumetric=8.0, density=1.17, cost=70.0,
        slow_down=(10.0, 15.0), fan_layer_time=100.0, hardened=True, dry=True, shrink=99.5,
        note="Nylon chargé en fibres de carbone : 5 °C de plus que le PA, retrait réduit par les fibres, buse trempée "
             "recommandée, séchage indispensable."),
    "PVA": _fiche(
        "PVA", "Alpine PVA", nozzle=(215, 210), nozzle_range=(190, 230), beds=_BED_PLA,
        fan=(60, 100, 100, 1, 50, 0), colour="#FFF4C2", flow=0.98, max_volumetric=6.0, density=1.23, cost=70.0,
        slow_down=(8.0, 15.0), fan_layer_time=60.0, soluble=True, support=True, dry=True,
        note="Support soluble à l'eau, à imprimer comme un PLA mais lentement (6 mm³/s) ; se dégrade dans la buse s'il "
             "y stagne chaud ; très sensible à l'humidité."),
    "HIPS": _fiche(
        "HIPS", "Alpine HIPS", nozzle=(250, 245), nozzle_range=(230, 270), beds=_BED_HOT,
        fan=(10, 30, 50, 3, 0, 0), colour="#FAFAFA", flow=0.95, max_volumetric=10.0, density=1.04, cost=28.0,
        slow_down=(8.0, 15.0), fan_layer_time=90.0, support=True, shrink=99.5,
        note="Polystyrène choc : support de l'ABS (soluble au limonène) ou matériau léger, réglages proches de l'ABS, "
             "10 °C plus froid."),
    "PCTG": _fiche(
        "PCTG", "Alpine PCTG", nozzle=(260, 255), nozzle_range=(240, 275), beds=_BED_PCTG,
        fan=(30, 50, 70, 3, 20, 0), colour="#7FD1C8", flow=0.95, max_volumetric=10.0, density=1.23, cost=30.0,
        slow_down=(8.0, 15.0), fan_layer_time=80.0, dry=True,
        note="Copolyester plus tenace que le PETG : 10–15 °C plus chaud (240–275 °C), lit 75 °C sur PEI, ventilation "
             "modérée ; plaque froide déconseillée, sécher avant usage."),
    "PP": _fiche(
        "PP", "Alpine PP", nozzle=(245, 240), nozzle_range=(220, 260), beds=_BED_HOT,
        fan=(20, 50, 70, 3, 0, 0), colour="#E9E4D8", flow=0.97, max_volumetric=8.0, density=0.90, cost=40.0,
        slow_down=(10.0, 15.0), fan_layer_time=90.0, shrink=98.0,
        note="Polypropylène : adhère mal aux plaques PEI (ruban PP ou colle conseillés), lit chaud, ventilation faible "
             "pour limiter le gauchissement, retrait marqué (≈ 2 %), brim large conseillé ; chambre fermée."),
    "PPA-CF": _fiche(
        "PPA-CF", "Alpine PPA-CF", nozzle=(300, 295), nozzle_range=(280, 300), beds=_BED_HOT,
        fan=(0, 20, 40, 3, 0, 0), colour="#1F1F1F", flow=0.95, max_volumetric=6.0, density=1.20, cost=90.0,
        slow_down=(10.0, 15.0), fan_layer_time=100.0, hardened=True, dry=True, shrink=99.5,
        note="Polyphtalamide chargé carbone : au plafond de température de la P1S (300 °C), lit au maximum, ventilation "
             "quasi nulle, buse trempée et séchage indispensables ; prévu pour les machines à chambre chauffée, résultats "
             "limités sur P1S."),
}
TYPES = tuple(FILAMENTS)

# Alias explicites (clé normalisée → type). Les noms de profils de fabricants (« Bambu PLA Basic @BBL X1C »,
# « Generic PETG ») passent ensuite par la reconnaissance par jetons.
_ALIASES = {
    "pla": "PLA", "plabasic": "PLA", "plamatte": "PLA", "plaplus": "PLA", "plapro": "PLA", "platough": "PLA",
    "plametal": "PLA", "plamarble": "PLA", "plawood": "PLA", "plaglow": "PLA", "plasparkle": "PLA",
    "plasilk": "PLA Silk", "silkpla": "PLA Silk", "silk": "PLA Silk", "plasilkplus": "PLA Silk",
    "placf": "PLA-CF", "placarbon": "PLA-CF", "placarbonfiber": "PLA-CF", "placarbonfibre": "PLA-CF",
    "petg": "PETG", "petgbasic": "PETG", "pet": "PETG", "petgtranslucent": "PETG",
    "petghf": "PETG HF", "petghighflow": "PETG HF", "hfpetg": "PETG HF",
    "petgcf": "PETG-CF", "petgcarbon": "PETG-CF", "petcf": "PETG-CF",
    "abs": "ABS", "absgf": "ABS", "absplus": "ABS",
    "asa": "ASA", "asaaero": "ASA",
    "tpu": "TPU", "tpu95a": "TPU", "tpu95": "TPU", "tpuams": "TPU", "tpu90a": "TPU", "tpu98a": "TPU", "flex": "TPU",
    "tpuforams": "TPU",
    "pc": "PC", "pcabs": "PC", "polycarbonate": "PC", "pcfr": "PC",
    "pa": "PA", "nylon": "PA", "pa6": "PA", "pa12": "PA", "paht": "PA", "pa6gf": "PA",
    "pacf": "PA-CF", "pahtcf": "PA-CF", "nyloncf": "PA-CF", "pa6cf": "PA-CF", "pa12cf": "PA-CF", "pagf": "PA-CF",
    "pva": "PVA", "supportw": "PVA", "bvoh": "PVA",
    "hips": "HIPS",
    "pctg": "PCTG",
    "pp": "PP", "ppcf": "PP", "ppgf": "PP", "polypropylene": "PP",
    "ppa": "PPA-CF", "ppacf": "PPA-CF", "ppagf": "PPA-CF",
    "tpe": "TPU", "tpe95a": "TPU",
}


def canonical_type(material) -> str:
    """Type Alpine d'un libellé de matériau (alias, noms de profils de fabricants) ; ``MaterialUnknownError`` sinon."""
    raw = str(material or "").strip()
    key = _normalise(raw.split("@", 1)[0])
    if key in _ALIASES:
        return _ALIASES[key]
    if key in {_normalise(name) for name in TYPES}:
        return next(name for name in TYPES if _normalise(name) == key)
    tokens = set(re.findall(r"[a-z0-9]+", raw.lower().split("@", 1)[0].replace("-", " ").replace("_", " ")))
    cf = bool(tokens & {"cf", "carbon", "carbone", "gf", "glass"})
    if "petg" in tokens or "pet" in tokens:
        if cf:
            return "PETG-CF"
        if tokens & {"hf", "highflow", "rapid", "fast", "speed"}:
            return "PETG HF"
        return "PETG"
    if "pla" in tokens:
        if cf:
            return "PLA-CF"
        if "silk" in tokens:
            return "PLA Silk"
        return "PLA"
    if tokens & {"tpu", "tpe", "flex", "flexible"}:
        return "TPU"
    if "asa" in tokens:
        return "ASA"
    if "abs" in tokens:
        return "ABS"
    if "pva" in tokens or "bvoh" in tokens:
        return "PVA"
    if "hips" in tokens:
        return "HIPS"
    if "pctg" in tokens:
        return "PCTG"
    if "ppa" in tokens:
        return "PPA-CF"
    if tokens & {"pa", "pa6", "pa12", "paht", "nylon"}:
        return "PA-CF" if cf else "PA"
    if tokens & {"pc", "polycarbonate"}:
        return "PC"
    if tokens & {"pp", "polypropylene"}:
        return "PP"
    raise MaterialUnknownError(f"Matériau inconnu ou non pris en charge par le trancheur Alpine : « {raw[:60] or '(vide)'} ». "
                               f"Types connus : {', '.join(TYPES)}.")


def resolve_filament(material) -> dict:
    """Copie de la fiche Alpine du matériau (tolérant aux alias : « PLA Basic », « petg-hf », « TPU-AMS »…).

    La copie porte en plus ``requested`` (libellé reçu) ; lever ``MaterialUnknownError`` si rien ne correspond.
    """
    type_ = canonical_type(material)
    fiche = copy.deepcopy(FILAMENTS[type_])
    fiche["requested"] = str(material or "").strip()
    return fiche


def plate_temperatures(fiche: dict, bed_type) -> tuple[int, int] | None:
    """(première couche, suivantes) du lit pour une plaque ; None si la plaque est inconnue ou déconseillée."""
    plate = normalise_plate(bed_type)
    if not plate:
        return None
    key = PLATE_TEMP_KEYS[plate]
    initial, following = int(fiche.get(f"{key}_initial_layer") or 0), int(fiche.get(key) or 0)
    if initial <= 0 and following <= 0:
        return None
    return initial, following


def suitable_plates(fiche: dict) -> list[str]:
    """Plaques conseillées pour une fiche (température de lit renseignée)."""
    return [plate for plate in PLATES if plate_temperatures(fiche, plate)]


def list_types() -> list[dict]:
    """Résumé des fiches pour une liste de choix : type, nom, températures, plaques conseillées."""
    return [{"type": name, "name": fiche["name"], "nozzle_temperature": fiche["nozzle_temperature"],
             "nozzle_temperature_initial_layer": fiche["nozzle_temperature_initial_layer"],
             "plates": suitable_plates(fiche), "ams_compatible": fiche["ams_compatible"],
             "nozzle_hardened_required": fiche["nozzle_hardened_required"]} for name, fiche in FILAMENTS.items()]


__all__ = ["BED_TEMP_MAX_C", "FILAMENTS", "FILAMENT_DIAMETER_MM", "MaterialUnknownError", "NOZZLE_TEMP_MAX_C",
           "NOZZLE_TEMP_MIN_C", "PLATES", "PLATE_SHORT", "PLATE_TEMP_KEYS", "TYPES", "VENDOR", "canonical_type",
           "list_types", "normalise_plate", "plate_temperatures", "resolve_filament", "suitable_plates"]
