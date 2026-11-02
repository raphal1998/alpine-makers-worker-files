"""Calcul d'une estimation : port de Calcul.kt d'Alpine Prism, lui-même transposition exacte de
utils/estimation3d.js (calculerEstimation) de la boutique. Vérifié par tests/test_atool_estimation.py
sur les vecteurs du serveur (mêmes entrées → mêmes montants).

Les clés des saisies, paramètres et résultats gardent les noms de la boutique (camelCase).
Bibliothèque standard seulement.
"""
import copy
import math
from decimal import ROUND_HALF_UP, Decimal

# Les cinq outils d'estimation de la boutique (utils/matieres.js).
MATIERES = {
    "filament": {"cle": "filament", "libelle": "Impression 3D filament", "court": "Filament", "unite": "kg", "plaque": False},
    "resine": {"cle": "resine", "libelle": "Impression 3D résine", "court": "Résine", "unite": "kg", "plaque": False},
    "gravure": {"cle": "gravure", "libelle": "Gravure laser", "court": "Gravure", "unite": "m2", "plaque": True},
    "decoupe": {"cle": "decoupe", "libelle": "Découpe laser", "court": "Découpe", "unite": "m2", "plaque": True},
    "cnc": {"cle": "cnc", "libelle": "Fraisage CNC", "court": "CNC", "unite": "m2", "plaque": True},
}

# Valeurs par défaut des classes Kotlin (Model.kt) : bloc absent du JSON → ces valeurs.
_MACHINE = {"amortissementHoraire": 1.2, "entretienHoraire": 0.6, "puissanceW": 110.0, "prixKwh": 0.29}
_MAIN = {"tauxHoraire": 45.0, "preparationMin": 10.0}
_POST = {"coutConsommablesParPiece": 0.6, "puissanceCureW": 40.0, "dureeCureMin": 10.0, "dureeLavageMin": 5.0}
_LASER = {"vitesseGravureCm2Min": 6.0, "passesDefaut": 1, "vitesseCoupeMmMin": 600.0, "epaisseurReferenceMm": 3.0,
          "tempsPercageS": 0.5, "coutConsommablesParPiece": 0.3, "prixM2Defaut": 60.0, "prereglages": []}
_CNC = {"debitCm3Min": 8.0, "usureOutilHoraire": 3.0, "tempsChangementOutilMin": 2.0, "coutConsommablesParPiece": 0.5,
        "prixM2Defaut": 150.0, "prereglages": []}
_PREREGLAGE = {"famille": "", "vitesseGravureCm2Min": 0.0, "vitesseCoupeMmMin": 0.0, "debitCm3Min": 0.0}
_PALIER = {"quantiteMin": 0, "remisePct": 0.0}
_PARAMETRES = {"tauxEurChf": 0.94, "tauxUsdChf": 0.8, "densite": None, "tauxEchec": 5.0, "dechetsDefaut": 8.0,
               "margeDefaut": 40.0, "tva": 8.1, "fraisFixes": 0.0, "arrondi": 0.05, "prixPlancher": 0.0}

# Préréglages par famille de matériau du serveur (utils/estimation3d.js, familles de utils/fournisseurs/plaques.js).
PREREGLAGES = {
    "gravure": [
        {"famille": "LAMINÉS & FILMS", "vitesseGravureCm2Min": 10}, {"famille": "BOIS", "vitesseGravureCm2Min": 8},
        {"famille": "PLASTIQUES", "vitesseGravureCm2Min": 7}, {"famille": "MÉTAUX", "vitesseGravureCm2Min": 6},
        {"famille": "MINÉRAUX & VERRE", "vitesseGravureCm2Min": 6}, {"famille": "CUIR, TEXTILES & PAPIER", "vitesseGravureCm2Min": 10},
        {"famille": "SUPPORTS À GRAVER", "vitesseGravureCm2Min": 7}, {"famille": "MOUSSES & CIRES", "vitesseGravureCm2Min": 8},
    ],
    "decoupe": [
        {"famille": "BOIS", "vitesseCoupeMmMin": 900, "vitesseGravureCm2Min": 8},
        {"famille": "PLASTIQUES", "vitesseCoupeMmMin": 600, "vitesseGravureCm2Min": 7},
        {"famille": "CUIR, TEXTILES & PAPIER", "vitesseCoupeMmMin": 1500, "vitesseGravureCm2Min": 10},
        {"famille": "MOUSSES & CIRES", "vitesseCoupeMmMin": 1200, "vitesseGravureCm2Min": 8},
        {"famille": "LAMINÉS & FILMS", "vitesseCoupeMmMin": 1000, "vitesseGravureCm2Min": 10},
    ],
    "cnc": [
        {"famille": "MÉTAUX", "debitCm3Min": 3}, {"famille": "PLASTIQUES", "debitCm3Min": 10},
        {"famille": "BOIS", "debitCm3Min": 15}, {"famille": "MOUSSES & CIRES", "debitCm3Min": 40},
    ],
}

# Paramètres d'atelier par défaut du serveur (PARAMETRES_DEFAUT_* de utils/estimation3d.js) : le Worker n'a pas
# accès aux paramètres enregistrés de la boutique, il calcule avec ceux-ci.
_DEFAUTS_SERVEUR = {
    "filament": {},
    "resine": {"densite": 1.1, "machine": {"amortissementHoraire": 0.9, "entretienHoraire": 0.5, "puissanceW": 70, "prixKwh": 0.29},
               "postTraitement": dict(_POST), "tauxEchec": 8, "dechetsDefaut": 15},
    "gravure": {"machine": {"amortissementHoraire": 6, "entretienHoraire": 2, "puissanceW": 600, "prixKwh": 0.29},
                "laser": dict(_LASER, prixM2Defaut=60, prereglages=PREREGLAGES["gravure"]), "tauxEchec": 3, "dechetsDefaut": 15},
    "decoupe": {"machine": {"amortissementHoraire": 7, "entretienHoraire": 2.5, "puissanceW": 800, "prixKwh": 0.29},
                "laser": dict(_LASER, prixM2Defaut=45, prereglages=PREREGLAGES["decoupe"]), "tauxEchec": 4, "dechetsDefaut": 25},
    "cnc": {"machine": {"amortissementHoraire": 12, "entretienHoraire": 4, "puissanceW": 2200, "prixKwh": 0.29},
            "cnc": dict(_CNC, prereglages=PREREGLAGES["cnc"]), "mainOeuvre": {"tauxHoraire": 45, "preparationMin": 20},
            "tauxEchec": 5, "dechetsDefaut": 30},
}

# Saisie d'une pièce (Saisie de Model.kt) : None = champ vide (valeur des paramètres).
SAISIE = {
    "prixKg": 0.0, "poidsG": 0.0, "volumeMl": 0.0, "dechetsPct": None, "tempsImpressionMin": 0.0, "tempsAssemblageMin": 0.0,
    "tempsPostTraitementMin": 0.0, "quantite": 1, "tauxEchecPct": None, "margePct": None, "tvaPct": None, "fraisFixes": None,
    "remisePct": None, "dureeCureMin": None, "consommables": None, "prixM2": 0.0, "longueurMm": 0.0, "largeurMm": 0.0,
    "epaisseurMm": None, "matiereClient": False, "famille": "", "surfaceGraveeCm2": 0.0, "passes": None,
    "vitesseGravureCm2Min": None, "longueurCoupeM": 0.0, "nbPercages": 0, "vitesseCoupeMmMin": None, "volumeEnleveCm3": 0.0,
    "nbOutils": 0, "debitCm3Min": None,
}


def matiere_de(cle):
    return cle if cle in MATIERES else "filament"


def _bloc(brut, base):
    """Bloc de paramètres complété par les valeurs de la classe Kotlin (coerceInputValues : null → défaut)."""
    out = dict(base)
    for k, v in (brut or {}).items():
        if k in base and v is not None:
            out[k] = v
    for k, v in out.items():
        if isinstance(base.get(k), float) and v is not None:
            out[k] = float(v)
    return out


def lire_parametres(brut):
    """Paramètres (JSON du serveur ou défauts) complétés comme la désérialisation Kotlin de Parametres."""
    brut = brut or {}
    p = _bloc(brut, _PARAMETRES)
    p["densite"] = None if brut.get("densite") is None else float(brut["densite"])
    p["machine"] = _bloc(brut.get("machine"), _MACHINE)
    p["mainOeuvre"] = _bloc(brut.get("mainOeuvre"), _MAIN)
    p["postTraitement"] = _bloc(brut["postTraitement"], _POST) if brut.get("postTraitement") else None
    p["laser"] = None
    p["cnc"] = None
    for cle, base in (("laser", _LASER), ("cnc", _CNC)):
        if brut.get(cle):
            bloc = _bloc(brut[cle], base)
            bloc["prereglages"] = [_bloc(r, _PREREGLAGE) for r in (brut[cle].get("prereglages") or [])]
            p[cle] = bloc
    p["remisesQuantite"] = [_bloc(r, _PALIER) for r in (brut.get("remisesQuantite") or [])]
    return p


def parametres_defaut(matiere):
    """Paramètres d'atelier par défaut de la boutique pour une matière."""
    return lire_parametres(copy.deepcopy(_DEFAUTS_SERVEUR[matiere_de(matiere)]))


def _defauts_kotlin(m):
    """Defauts.de(m) de Model.kt : repli quand un bloc manque (préréglages vides)."""
    d = copy.deepcopy(_DEFAUTS_SERVEUR[m])
    for cle in ("laser", "cnc"):
        if cle in d:
            d[cle]["prereglages"] = []
    if m == "cnc":
        d.pop("laser", None)
    return lire_parametres(d)


def _js_round(v):
    """Math.round de JavaScript (moitié vers +∞)."""
    return float(math.floor(v + 0.5))


def _r2(v):
    return _js_round(v * 100) / 100


def _pos(v):
    return max(0.0, float(v) if v is not None else 0.0)


def arrondi1(v):
    """Math.round(v * 10) / 10.0 de Java."""
    return math.floor(v * 10 + 0.5) / 10.0


def saisie_champ(v):
    """Formats.saisie puis Formats.lire : nombre tel que l'appli le remet dans un champ (deux décimales au plus)."""
    if v == math.floor(v):
        return float(int(v))
    return float(Decimal(v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def arrondir(valeur, pas):
    p = pas if pas > 0 else 0.01
    quotient = float(Decimal(valeur / p).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))
    return _js_round(_js_round(quotient) * p * 100) / 100


def _palier(paliers, q):
    retenus = [x for x in paliers if x["quantiteMin"] >= 2 and x["remisePct"] > 0 and q >= x["quantiteMin"]]
    return max(retenus, key=lambda x: x["quantiteMin"]) if retenus else None


def normaliser(saisie, p, m):
    s = dict(SAISIE)
    s.update({k: v for k, v in (saisie or {}).items() if k in SAISIE})
    q = max(1, int(s["quantite"] or 1))
    saisie_remise = s["remisePct"] is not None
    pal = None if saisie_remise else _palier(p["remisesQuantite"], q)

    def ou(v, defaut):
        return _pos(v) if v is not None else defaut

    e = {
        "prixKg": _pos(s["prixKg"]), "poidsG": 0.0, "volumeMl": 0.0, "dechetsPct": ou(s["dechetsPct"], p["dechetsDefaut"]),
        "tempsImpressionMin": _pos(s["tempsImpressionMin"]), "tempsAssemblageMin": _pos(s["tempsAssemblageMin"]),
        "tempsPostTraitementMin": _pos(s["tempsPostTraitementMin"]), "quantite": q,
        "tauxEchecPct": ou(s["tauxEchecPct"], p["tauxEchec"]), "margePct": ou(s["margePct"], p["margeDefaut"]),
        "tvaPct": ou(s["tvaPct"], p["tva"]), "fraisFixes": ou(s["fraisFixes"], p["fraisFixes"]),
        "remisePct": min(100.0, _pos(s["remisePct"])) if saisie_remise else (pal["remisePct"] if pal else 0.0),
        "remiseQuantite": pal is not None, "remiseQuantiteMin": pal["quantiteMin"] if pal else 0,
        "dureeCureMin": 0.0, "consommables": 0.0, "prixM2": 0.0, "longueurMm": 0.0, "largeurMm": 0.0, "epaisseurMm": 0.0,
        "matiereClient": False, "famille": "", "surfaceGraveeCm2": 0.0, "passes": 1, "vitesseGravureCm2Min": 0.0,
        "longueurCoupeM": 0.0, "nbPercages": 0, "vitesseCoupeMmMin": 0.0, "volumeEnleveCm3": 0.0, "nbOutils": 0,
        "debitCm3Min": 0.0, "origineVitesses": {}, "tempsMachineMin": 0.0, "tempsEstime": False,
    }
    d = _defauts_kotlin(m)
    if m == "resine":
        densite = p["densite"] if (p["densite"] or 0) > 0 else 1.1
        pt = p["postTraitement"] or d["postTraitement"]
        vol = _pos(s["volumeMl"])
        e.update(volumeMl=vol, poidsG=_js_round(vol * densite * 10) / 10, dureeCureMin=ou(s["dureeCureMin"], pt["dureeCureMin"]),
                 consommables=ou(s["consommables"], pt["coutConsommablesParPiece"]))
    elif MATIERES[m]["plaque"]:
        laser = p["laser"] or d["laser"]
        cnc = p["cnc"] or d["cnc"]
        conso = cnc["coutConsommablesParPiece"] if m == "cnc" else laser["coutConsommablesParPiece"]
        famille = str(s["famille"] or "").strip().upper()[:40]
        prereglages = cnc["prereglages"] if m == "cnc" else laser["prereglages"]
        reglage = next((r for r in prereglages if r["famille"] == famille), None) if famille else None
        origines = {}

        def vitesse(champ, valeur, general):
            v = _pos(valeur)
            pre = reglage.get(champ) if reglage else None
            if v > 0:
                origines[champ] = "saisie"
                return v
            if pre is not None and pre > 0:
                origines[champ] = "famille"
                return float(pre)
            origines[champ] = "parametres"
            return float(general)

        e.update(poidsG=0.0, prixM2=_pos(s["prixM2"]), longueurMm=_pos(s["longueurMm"]), largeurMm=_pos(s["largeurMm"]),
                 epaisseurMm=_pos(s["epaisseurMm"]), matiereClient=bool(s["matiereClient"]), consommables=ou(s["consommables"], conso),
                 famille=famille)
        if m == "gravure":
            passes = laser["passesDefaut"] if s["passes"] is None else _pos(float(s["passes"]))
            e.update(surfaceGraveeCm2=_pos(s["surfaceGraveeCm2"]), passes=max(1, int(_js_round(passes))),
                     vitesseGravureCm2Min=vitesse("vitesseGravureCm2Min", s["vitesseGravureCm2Min"], laser["vitesseGravureCm2Min"]))
        elif m == "decoupe":
            e.update(longueurCoupeM=_pos(s["longueurCoupeM"]), nbPercages=max(0, int(s["nbPercages"] or 0)),
                     surfaceGraveeCm2=_pos(s["surfaceGraveeCm2"]),
                     vitesseCoupeMmMin=vitesse("vitesseCoupeMmMin", s["vitesseCoupeMmMin"], laser["vitesseCoupeMmMin"]))
            e["vitesseGravureCm2Min"] = vitesse("vitesseGravureCm2Min", s["vitesseGravureCm2Min"], laser["vitesseGravureCm2Min"])
        else:
            e.update(volumeEnleveCm3=_pos(s["volumeEnleveCm3"]), nbOutils=max(0, int(s["nbOutils"] or 0)),
                     debitCm3Min=vitesse("debitCm3Min", s["debitCm3Min"], cnc["debitCm3Min"]))
        e["origineVitesses"] = origines
    else:
        e["poidsG"] = _pos(s["poidsG"])
    return e


def _temps_estime(e, p, m):
    d = _defauts_kotlin(m)
    if m == "gravure":
        return e["surfaceGraveeCm2"] / e["vitesseGravureCm2Min"] * e["passes"] if e["vitesseGravureCm2Min"] > 0 else 0.0
    if m == "decoupe":
        laser = p["laser"] or d["laser"]
        ep = e["epaisseurMm"] if e["epaisseurMm"] > 0 else laser["epaisseurReferenceMm"]
        v = e["vitesseCoupeMmMin"] * min(1.0, laser["epaisseurReferenceMm"] / ep)
        coupe = e["longueurCoupeM"] * 1000 / v if v > 0 else 0.0
        gravure = e["surfaceGraveeCm2"] / e["vitesseGravureCm2Min"] if e["vitesseGravureCm2Min"] > 0 else 0.0
        return coupe + e["nbPercages"] * laser["tempsPercageS"] / 60 + gravure
    usinage = e["volumeEnleveCm3"] / e["debitCm3Min"] if e["debitCm3Min"] > 0 else 0.0
    return usinage + e["nbOutils"] * (p["cnc"] or d["cnc"])["tempsChangementOutilMin"]


def calculer(saisie, parametres=None, matiere="filament"):
    """Estimation complète : {"entrees", "unitaire", "total", "indicateurs"} (noms de la boutique)."""
    m = matiere_de(matiere)
    p = parametres if parametres is not None else parametres_defaut(m)
    plaque = MATIERES[m]["plaque"]
    resine = m == "resine"
    e = normaliser(saisie, p, m)
    q = e["quantite"]
    temps_machine, estime = 0.0, False
    if plaque:
        estime = not (e["tempsImpressionMin"] > 0)
        temps_machine = _js_round(_temps_estime(e, p, m) * 10) / 10 if estime else e["tempsImpressionMin"]
        e.update(tempsImpressionMin=temps_machine, tempsMachineMin=temps_machine, tempsEstime=estime)
    heures = e["tempsImpressionMin"] / 60
    surface_m2 = e["longueurMm"] * e["largeurMm"] / 1e6
    surface_achetee = surface_m2 * (1 + e["dechetsPct"] / 100)
    grammes = e["volumeMl"] * (p["densite"] if p["densite"] is not None else 1.1) if resine else e["poidsG"]
    if plaque:
        matiere_cout = 0.0 if e["matiereClient"] else surface_achetee * e["prixM2"]
    else:
        matiere_cout = grammes * (1 + e["dechetsPct"] / 100) / 1000 * e["prixKg"]
    mach = p["machine"]
    machine = heures * (mach["amortissementHoraire"] + mach["entretienHoraire"])
    cure = e["dureeCureMin"] / 60 * ((p["postTraitement"] or _POST)["puissanceCureW"] / 1000) * mach["prixKwh"] if resine else 0.0
    electricite = heures * (mach["puissanceW"] / 1000) * mach["prixKwh"] + cure
    outillage = heures * (p["cnc"] or _CNC)["usureOutilHoraire"] if m == "cnc" else 0.0
    production = matiere_cout + machine + electricite + outillage
    echec = production * (e["tauxEchecPct"] / 100)
    consommables = e["consommables"] if (resine or plaque) else 0.0
    main_oeuvre = (e["tempsAssemblageMin"] + e["tempsPostTraitementMin"]) / 60 * p["mainOeuvre"]["tauxHoraire"]
    preparation = p["mainOeuvre"]["preparationMin"] / 60 * p["mainOeuvre"]["tauxHoraire"] / q
    frais = e["fraisFixes"] / q
    cout = production + echec + consommables + main_oeuvre + preparation + frais
    avant_remise = cout + cout * (e["margePct"] / 100)
    prix_ht = avant_remise
    if e["remisePct"] > 0:
        prix_ht *= 1 - e["remisePct"] / 100
    remise = avant_remise - prix_ht
    plancher = p["prixPlancher"] > 0 and prix_ht < p["prixPlancher"]
    if plancher:
        prix_ht = p["prixPlancher"]
    ht = arrondir(prix_ht, p["arrondi"])
    ttc = arrondir(ht + ht * (e["tvaPct"] / 100), p["arrondi"])
    unitaire = {
        "matiere": _r2(matiere_cout), "machine": _r2(machine), "electricite": _r2(electricite), "echec": _r2(echec),
        "consommables": _r2(consommables), "outillage": _r2(outillage), "mainOeuvre": _r2(main_oeuvre),
        "preparation": _r2(preparation), "fraisFixes": _r2(frais), "cout": _r2(cout), "marge": _r2(prix_ht - cout),
        "remise": _r2(remise), "prixHT": ht, "tva": _r2(ttc - ht), "prixTTC": ttc, "plancherApplique": plancher,
    }
    total = {
        "cout": _r2(cout * q), "prixHT": _r2(ht * q), "tva": _r2((ttc - ht) * q), "prixTTC": _r2(ttc * q),
        "heuresImpression": _r2(heures * q), "heuresMachine": _r2(heures * q) if plaque else 0.0,
        "poidsG": 0.0 if plaque else _r2(grammes * (1 + e["dechetsPct"] / 100) * q),
        "surfaceM2": _js_round(surface_achetee * q * 10000) / 10000 if plaque else 0.0,
        "volumeMl": _r2(e["volumeMl"] * (1 + e["dechetsPct"] / 100) * q) if resine else 0.0,
    }

    def pct(x):
        return int(_js_round(x / cout * 100)) if cout > 0 else 0

    indicateurs = {
        "coutParGramme": _r2(cout / grammes) if (not plaque and grammes > 0) else 0.0,
        "coutParDm2": _r2(cout / (surface_m2 * 100)) if (plaque and surface_m2 > 0) else 0.0,
        "coutParMl": _r2(cout / e["volumeMl"]) if (resine and e["volumeMl"] > 0) else 0.0,
        "prixParHeureMachine": _r2(ht / heures) if heures > 0 else 0.0,
        "partMatierePct": pct(matiere_cout), "partMachinePct": pct(machine + electricite + outillage),
        "partMainOeuvrePct": pct(main_oeuvre + preparation), "margeEffectivePct": pct(ht - cout),
        "tempsMachineMin": temps_machine, "tempsEstime": estime,
    }
    return {"entrees": e, "unitaire": unitaire, "total": total, "indicateurs": indicateurs}


# ------------------------------------------------------------------------------------------------ formats

def _groupes(texte):
    return texte.replace(",", " ").replace(".", ",")


def chf(v):
    return "—" if v is None else _groupes(f"{v:,.2f}") + " CHF"


def nombre(v):
    if v == math.floor(v):
        return f"{v:.0f}"
    return _groupes(f"{v:,.2f}").rstrip("0").rstrip(",")


def unitaire_prix(v, unite):
    return _groupes(f"{v:,.2f}") + " CHF/" + ("m²" if unite == "m2" else unite)


def duree(minutes):
    m = int(math.floor(minutes + 0.5))
    if m <= 0:
        return "—"
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h {m % 60:02d}"
