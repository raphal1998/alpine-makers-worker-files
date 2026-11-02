"""Exécution commune des cinq estimations d'Alpine Prism sur le Worker (filament, résine, gravure, découpe, CNC).

Chaque paquet ``alpine_tools.estimation_<matière>`` déclare son TOOL (littéral) et délègue ``run`` à ``executer``.
Comme l'application : le fichier importé remplit les champs de la pièce (valeurs de valeursImport d'Ecrans.kt),
un champ saisi (> 0) remplace la valeur tirée du fichier, puis le calcul de la boutique (calcul.py) chiffre la pièce
avec les paramètres d'atelier par défaut de la boutique (le Worker n'a pas accès aux paramètres enregistrés).
"""
import math
from pathlib import Path

from . import calcul

TAILLE_MAX = 40 * 1024 * 1024
MODELES = ("stl", "3mf", "obj")
EXTENSIONS = {
    "filament": MODELES, "resine": MODELES, "cnc": MODELES,
    "gravure": ("svg", "dxf", "png", "jpg", "jpeg", "webp"),
    "decoupe": ("svg", "dxf"),
}
CLE_FICHIER = {"filament": "modele", "resine": "modele", "cnc": "modele", "gravure": "fichier", "decoupe": "dessin"}

# Parts du prix dans l'ordre et les teintes de la carte de résultat de l'application.
_PARTS = (("Matière", ("matiere",)), ("Machine", ("machine", "outillage")), ("Électricité", ("electricite",)),
          ("Échec", ("echec",)), ("Consommables", ("consommables",)), ("Main-d'œuvre", ("mainOeuvre", "preparation")),
          ("Frais", ("fraisFixes",)), ("Marge", ("marge",)), ("TVA", ("tva",)))
_TEINTES = ((0xFF, 0x5E, 0x7E), (0xFF, 0xB6, 0x48), (0xFF, 0xE6, 0x6B), (0xFF, 0x6B, 0x7A), (0x7C, 0xF2, 0x9A),
            (0x4F, 0xC3, 0xFF), (0x5B, 0x8C, 0xFF), (0xB9, 0x8C, 0xFF), (0xA9, 0xA3, 0xC9))
_ORIGINES = {"saisie": "saisie", "famille": "préréglage de la famille", "parametres": "paramètres de l'atelier"}
_VITESSES = {"vitesseGravureCm2Min": "gravure", "vitesseCoupeMmMin": "coupe", "debitCm3Min": "débit"}


def _n(v):
    return calcul.nombre(float(v))


def _a1(v):
    return calcul.saisie_champ(calcul.arrondi1(v))


def _p(params, cle, defaut=0.0):
    v = params.get(cle)
    return defaut if v is None else v


def _lire_fichier(matiere, inputs):
    chemin = inputs.get(CLE_FICHIER[matiere])
    if not chemin:
        return None, None, None
    chemin = Path(chemin)
    if not chemin.is_file():
        raise ValueError("Fichier introuvable sur le Worker.")
    ext = chemin.suffix.lower().lstrip(".")
    if ext not in EXTENSIONS[matiere]:
        raise ValueError("Format non accepté pour cet outil : " + ", ".join(e.upper() for e in EXTENSIONS[matiere]) + ".")
    if chemin.stat().st_size > TAILLE_MAX:
        raise ValueError("Fichier trop volumineux (40 Mo au plus).")
    return chemin, ext, chemin.name


def _analyse_modele(chemin, progress):
    from . import maillage
    progress(0.1, "Lecture du modèle 3D")
    triangles = maillage.lire(chemin.read_bytes(), chemin.name)
    progress(0.35, f"Mesure de {len(triangles)} triangles (volume, surface, supports)")
    return maillage.mesurer(triangles)


def _lignes_modele(x):
    lignes = [f"Modèle : {_n(calcul.arrondi1(x['dx']))} × {_n(calcul.arrondi1(x['dy']))} × {_n(calcul.arrondi1(x['dz']))} mm · "
              f"volume {_n(calcul.arrondi1(x['volumeMm3'] / 1000))} cm³ · {x['nbTriangles']} triangles"]
    if x["etanche"] is False:
        lignes.append("Maillage non fermé : volume approximatif, vérifie le modèle.")
    return lignes


def _lignes_dessin(d):
    lignes = [f"Dessin {d['format'].upper()} : {_n(calcul.arrondi1(d['largeurMm']))} × {_n(calcul.arrondi1(d['hauteurMm']))} mm · "
              f"tracés {_n(calcul.arrondi1(d['longueurMm'] / 1000 * 100) / 100)} m · {d['nbContoursFermes']} contours fermés, "
              f"{d['nbCheminsOuverts']} ouverts · surface {_n(calcul.arrondi1(d['surfaceMm2'] / 100))} cm²"]
    if d.get("avertissement"):
        lignes.append("Attention : " + d["avertissement"])
    return lignes


def _prix(params, matiere):
    """Champs de prix communs (CHAMPS_PRIX de l'application)."""
    return {
        "quantite": max(1, min(100000, int(_p(params, "quantite", 1)))), "margePct": _p(params, "marge_pct", None),
        "remisePct": _p(params, "remise_pct", None), "tauxEchecPct": _p(params, "taux_echec_pct", None),
        "dechetsPct": _p(params, "dechets_pct", None), "tempsAssemblageMin": _p(params, "temps_assemblage_min"),
        "tempsPostTraitementMin": _p(params, "temps_finition_min"), "fraisFixes": _p(params, "frais_fixes", None),
        "tvaPct": _p(params, "tva_pct", None),
    }


def _surcharger(valeurs, params, correspondances):
    """Champ saisi (> 0) : remplace la valeur tirée du fichier."""
    for cle_param, champ in correspondances:
        v = params.get(cle_param)
        if v is not None and v > 0:
            valeurs[champ] = float(v)


def _piece(matiere, params, inputs, progress):
    """Saisie de la pièce + lignes d'analyse + mesures brutes (pour le résumé)."""
    chemin, ext, nom = _lire_fichier(matiere, inputs)
    lignes, analyse, v = [], {}, {}
    if matiere in ("filament", "resine", "cnc") and chemin is not None:
        from . import maillage
        x = _analyse_modele(chemin, progress)
        analyse = {"type": "modele", **x}
        lignes += _lignes_modele(x)
        if matiere == "filament":
            type_f = str(params.get("type_filament") or "PLA")
            r = maillage.filament(x, int(_p(params, "remplissage_pct", 15)), float(params.get("hauteur_couche") or 0.2),
                                  int(params.get("parois") or 2), bool(params.get("supports", True)), maillage.densite(type_f))
            v["poidsG"] = _a1(r["totalG"])
            v["tempsImpressionMin"] = float(math.floor(r["minutes"] + 0.5))
            lignes.append(f"Filament {type_f} : pièce {_n(calcul.arrondi1(r['pieceG']))} g + supports {_n(calcul.arrondi1(r['supportsG']))} g"
                          f" = {_n(calcul.arrondi1(r['totalG']))} g · impression ≈ {calcul.duree(r['minutes'])} · {r['couches']} couches")
            analyse["impression"] = r
        elif matiere == "resine":
            r = maillage.resine(x, float(params.get("hauteur_couche") or 0.05), bool(params.get("supports", True)))
            v["volumeMl"] = _a1(r["volumeMl"])
            v["tempsImpressionMin"] = float(math.floor(r["minutes"] + 0.5))
            lignes.append(f"Résine : {_n(calcul.arrondi1(r['volumeMl']))} ml (supports {_n(calcul.arrondi1(r['supportsG'] / 1.1))} ml)"
                          f" · impression ≈ {calcul.duree(r['minutes'])} · {r['couches']} couches")
            analyse["impression"] = r
        else:
            enleve = max(0.0, x["dx"] * x["dy"] * x["dz"] - x["volumeMm3"]) / 1000
            v.update(longueurMm=_a1(x["dx"]), largeurMm=_a1(x["dy"]), epaisseurMm=_a1(x["dz"]), volumeEnleveCm3=_a1(enleve))
            lignes.append(f"Brut {_n(calcul.arrondi1(x['dx']))} × {_n(calcul.arrondi1(x['dy']))} × {_n(calcul.arrondi1(x['dz']))} mm"
                          f" · matière enlevée {_n(calcul.arrondi1(enleve))} cm³ · temps estimé au débit")
    elif chemin is not None and ext in ("svg", "dxf"):
        from . import vectoriel
        progress(0.15, "Analyse du dessin")
        d = vectoriel.analyser(chemin.read_bytes(), chemin.name)
        analyse = {"type": "dessin", **d}
        lignes += _lignes_dessin(d)
        reel = _p(params, "largeur_reelle_mm")
        k = reel / d["largeurMm"] if reel > 0 and d["largeurMm"] > 0 else 1.0
        if k != 1.0:
            lignes.append(f"Mis à l'échelle × {_n(round(k, 4))} (largeur réelle {_n(reel)} mm)")
        v.update(longueurMm=_a1(d["largeurMm"] * k), largeurMm=_a1(d["hauteurMm"] * k))
        if matiere == "decoupe":
            v["longueurCoupeM"] = calcul.saisie_champ(math.floor(d["longueurMm"] * k + 0.5) / 1000.0)
            v["nbPercages"] = d["nbContoursFermes"] + d["nbCheminsOuverts"]
        else:
            # Tracé sans surface fermée (gravure au trait) : largeur de ligne de 0,2 mm.
            surface = d["surfaceMm2"] * k * k if d["surfaceMm2"] > 0 else d["longueurMm"] * k * 0.2
            v["surfaceGraveeCm2"] = _a1(surface / 100)
    elif chemin is not None:
        from . import image
        progress(0.15, "Analyse de l'image")
        i = image.mesurer(chemin)
        analyse = {"type": "image", **i}
        lignes.append(f"Image {i['largeurPx']} × {i['hauteurPx']} px · {math.floor(i['ratioSombre'] * 100 + 0.5)} % de zones sombres (gravées)")
        reel = _p(params, "largeur_reelle_mm")
        l = max(1.0, reel if reel > 0 else 100.0)
        h = l * i["hauteurPx"] / max(1, i["largeurPx"])
        v.update(longueurMm=_a1(l), largeurMm=_a1(h), surfaceGraveeCm2=_a1(i["ratioSombre"] * l * h / 100))
    progress(0.6, "Calcul du prix")
    return nom, lignes, analyse, v


def _saisie(matiere, params, v, prix_defaut, avec_fichier):
    s = dict(_prix(params, matiere))
    s.update(v)
    if matiere == "filament":
        s["prixKg"] = _p(params, "prix_kg")
        _surcharger(s, params, (("poids_g", "poidsG"), ("temps_impression_min", "tempsImpressionMin")))
        if not s.get("poidsG"):
            raise ValueError("Importe un modèle 3D (STL, 3MF, OBJ) ou saisis le poids de la pièce.")
    elif matiere == "resine":
        s["prixKg"] = _p(params, "prix_kg")
        s["dureeCureMin"] = params.get("duree_cure_min")
        s["consommables"] = params.get("consommables")
        _surcharger(s, params, (("volume_ml", "volumeMl"), ("temps_impression_min", "tempsImpressionMin")))
        if not s.get("volumeMl"):
            raise ValueError("Importe un modèle 3D (STL, 3MF, OBJ) ou saisis le volume de résine.")
    else:
        s.update(prixM2=_p(params, "prix_m2", prix_defaut), matiereClient=bool(params.get("matiere_client")),
                 famille=str(params.get("famille") or ""), consommables=params.get("consommables"),
                 tempsImpressionMin=_p(params, "temps_machine_min"))
        _surcharger(s, params, (("longueur_mm", "longueurMm"), ("largeur_mm", "largeurMm")))
        if matiere == "gravure":
            s.update(epaisseurMm=params.get("epaisseur_mm"), passes=params.get("passes"),
                     vitesseGravureCm2Min=_p(params, "vitesse_gravure_cm2_min"))
            _surcharger(s, params, (("surface_gravee_cm2", "surfaceGraveeCm2"),))
            if not avec_fichier and not s.get("surfaceGraveeCm2") and not s["tempsImpressionMin"]:
                raise ValueError("Importe un dessin (SVG, DXF) ou une image (PNG, JPG), ou saisis la surface gravée.")
        elif matiere == "decoupe":
            s.update(epaisseurMm=params.get("epaisseur_mm"), surfaceGraveeCm2=_p(params, "surface_gravee_cm2"),
                     vitesseCoupeMmMin=_p(params, "vitesse_coupe_mm_min"), vitesseGravureCm2Min=_p(params, "vitesse_gravure_cm2_min"))
            _surcharger(s, params, (("longueur_coupe_m", "longueurCoupeM"), ("nb_percages", "nbPercages")))
            s["nbPercages"] = int(s.get("nbPercages") or 0)
            if not avec_fichier and not s.get("longueurCoupeM") and not s["tempsImpressionMin"]:
                raise ValueError("Importe un dessin de découpe (SVG, DXF) ou saisis la longueur de coupe.")
        else:
            s.update(nbOutils=int(_p(params, "nb_outils", 2)), debitCm3Min=_p(params, "debit_cm3_min"))
            _surcharger(s, params, (("longueur_mm", "longueurMm"), ("largeur_mm", "largeurMm"), ("epaisseur_mm", "epaisseurMm"),
                                    ("volume_enleve_cm3", "volumeEnleveCm3")))
            if not avec_fichier and not s.get("volumeEnleveCm3") and not s["tempsImpressionMin"]:
                raise ValueError("Importe un modèle 3D (STL, 3MF, OBJ) ou saisis le volume de matière enlevée.")
    return s


def _lignes_piece(matiere, e):
    if matiere == "filament":
        return [f"Pièce : {_n(e['poidsG'])} g · impression {calcul.duree(e['tempsImpressionMin'])}"]
    if matiere == "resine":
        return [f"Pièce : {_n(e['volumeMl'])} ml de résine ({_n(e['poidsG'])} g) · impression {calcul.duree(e['tempsImpressionMin'])}"
                f" · cuisson UV {_n(e['dureeCureMin'])} min"]
    base = f"Plaque : {_n(e['longueurMm'])} × {_n(e['largeurMm'])} mm" + (f" · épaisseur {_n(e['epaisseurMm'])} mm" if e["epaisseurMm"] > 0 else "")
    if e["famille"]:
        base += f" · famille {e['famille']}"
    if matiere == "gravure":
        return [base, f"Gravure : {_n(e['surfaceGraveeCm2'])} cm² · {e['passes']} passe(s) · {_n(e['vitesseGravureCm2Min'])} cm²/min"]
    if matiere == "decoupe":
        return [base, f"Découpe : {_n(e['longueurCoupeM'])} m de coupe · {e['nbPercages']} perçage(s) · {_n(e['vitesseCoupeMmMin'])} mm/min"
                + (f" · marquage {_n(e['surfaceGraveeCm2'])} cm²" if e["surfaceGraveeCm2"] > 0 else "")]
    return [base, f"Fraisage : {_n(e['volumeEnleveCm3'])} cm³ enlevés · {e['nbOutils']} changement(s) d'outil · débit {_n(e['debitCm3Min'])} cm³/min"]


def _lignes_calcul(matiere, r, p):
    e, u = r["entrees"], r["unitaire"]
    plaque = calcul.MATIERES[matiere]["plaque"]
    mach, mo = p["machine"], p["mainOeuvre"]
    lignes = []
    if plaque:
        if e["matiereClient"]:
            lignes.append("Calcul matière : fournie par le client (0 CHF)")
        else:
            surface = e["longueurMm"] * e["largeurMm"] / 1e6
            lignes.append(f"Calcul matière : {_n(round(surface, 4))} m² × (1 + {_n(e['dechetsPct'])} % de chutes) × "
                          f"{calcul.unitaire_prix(e['prixM2'], 'm2')} = {calcul.chf(u['matiere'])}")
        temps = calcul.duree(e["tempsMachineMin"]) + (" estimé" if e["tempsEstime"] else " saisi")
        origines = ", ".join(f"{_VITESSES.get(k, k)} : {_ORIGINES.get(o, o)}" for k, o in e["origineVitesses"].items())
        lignes.append(f"Calcul temps machine : {temps}" + (f" (vitesses — {origines})" if e["tempsEstime"] and origines else ""))
    else:
        grammes = e["poidsG"]
        lignes.append(f"Calcul matière : {_n(round(grammes, 2))} g × (1 + {_n(e['dechetsPct'])} % de déchets) × "
                      f"{calcul.unitaire_prix(e['prixKg'], 'kg')} = {calcul.chf(u['matiere'])}")
    lignes.append(f"Calcul machine : {_n(round(e['tempsImpressionMin'] / 60, 3))} h × ({_n(mach['amortissementHoraire'])} + "
                  f"{_n(mach['entretienHoraire'])} CHF/h) ; électricité {_n(mach['puissanceW'])} W à {_n(mach['prixKwh'])} CHF/kWh")
    lignes.append(f"Calcul main-d'œuvre : {_n(mo['tauxHoraire'])} CHF/h, préparation {_n(mo['preparationMin'])} min répartie sur "
                  f"{e['quantite']} pièce(s) ; échec {_n(e['tauxEchecPct'])} % ; marge {_n(e['margePct'])} % ; TVA {_n(e['tvaPct'])} % ; "
                  f"arrondi {_n(p['arrondi'])} CHF")
    lignes.append("Paramètres d'atelier : valeurs par défaut de la boutique Alpine Makers (estimation indicative, non contractuelle).")
    return lignes


def _parts(u):
    return [(nom, sum(u[k] for k in cles)) for nom, cles in _PARTS]


def _lignes_prix(matiere, r):
    u, t, e, i = r["unitaire"], r["total"], r["entrees"], r["indicateurs"]
    q = e["quantite"]
    lignes = [f"Prix par pièce : {calcul.chf(u['prixTTC'])} TTC · {calcul.chf(u['prixHT'])} HT · coût de revient {calcul.chf(u['cout'])}"
              + (" · prix plancher" if u["plancherApplique"] else "")]
    if q > 1:
        lignes.append(f"Total pour {q} pièces : {calcul.chf(t['prixTTC'])} TTC · {calcul.chf(t['prixHT'])} HT")
    for nom, valeur in _parts(u):
        if valeur > 0.004:
            lignes.append(f"  {nom} : {calcul.chf(valeur)}")
    if u["remise"] > 0:
        quand = f" (quantité dès {e['remiseQuantiteMin']})" if e["remiseQuantite"] else ""
        lignes.append(f"Remise {_n(e['remisePct'])} %{quand} : − {calcul.chf(u['remise'])}")
    if calcul.MATIERES[matiere]["plaque"]:
        ind = f"Temps machine {calcul.duree(i['tempsMachineMin'])}{' (estimé)' if i['tempsEstime'] else ''} · "
    else:
        ind = f"Impression {calcul.duree(e['tempsImpressionMin'])} · "
    ind += f"marge effective {i['margeEffectivePct']} %"
    if i["prixParHeureMachine"] > 0:
        ind += f" · {calcul.chf(i['prixParHeureMachine'])}/h"
    lignes.append(ind)
    return lignes


def _csv(chemin, matiere, nom, r, analyse):
    def cellule(v):
        texte = str(v)
        if isinstance(v, bool):
            texte = "oui" if v else "non"
        elif isinstance(v, float):
            texte = repr(round(v, 6)).replace(".", ",")
        if any(c in texte for c in ';"\n'):
            texte = '"' + texte.replace('"', '""') + '"'
        return texte

    lignes = ["Rubrique;Valeur", f"Outil;{cellule(calcul.MATIERES[matiere]['libelle'])}", f"Fichier;{cellule(nom or '—')}"]
    for cle, valeur in analyse.items():
        if isinstance(valeur, (int, float, str, bool)) and valeur is not None:
            lignes.append(f"analyse.{cle};{cellule(valeur)}")
    for bloc in ("entrees", "unitaire", "total", "indicateurs"):
        for cle, valeur in r[bloc].items():
            if isinstance(valeur, (int, float, str, bool)):
                lignes.append(f"{bloc}.{cle};{cellule(valeur)}")
    chemin.write_bytes(("﻿" + "\r\n".join(lignes) + "\r\n").encode("utf-8"))


def _apercu(chemin, u):
    """Barre spectrale du prix décomposé, comme la carte de résultat de l'application (sans texte)."""
    from PIL import Image, ImageDraw
    parts = [(t, max(0.0, v)) for t, (_, v) in zip(_TEINTES, _parts(u))]
    visibles = [(t, v) for t, v in parts if v > 0.004]
    hauteur = 120 + 44 * len(visibles)
    img = Image.new("RGB", (1000, hauteur), (0x05, 0x04, 0x0C))
    d = ImageDraw.Draw(img)
    total = max(sum(v for _, v in parts), 0.0001)
    x = 40.0
    for teinte, v in parts:
        w = v / total * 920
        if w > 0:
            d.rounded_rectangle((x, 40, x + max(0.0, w - 3), 80), radius=8, fill=teinte)
        x += w
    plus_grand = max([v for _, v in visibles] or [1.0])
    y = 110
    for teinte, v in visibles:
        d.rounded_rectangle((40, y, 64, y + 24), radius=5, fill=teinte)
        d.rounded_rectangle((80, y + 4, 80 + max(4.0, v / plus_grand * 880), y + 20), radius=6, fill=teinte)
        y += 44
    img.save(chemin, "PNG")


def _resume(matiere, nom, r, analyse):
    u, t, e, i = r["unitaire"], r["total"], r["entrees"], r["indicateurs"]
    resume = {
        "matiere": matiere, "fichier": nom, "quantite": e["quantite"], "prix_ttc_piece": u["prixTTC"], "prix_ht_piece": u["prixHT"],
        "cout_piece": u["cout"], "total_ttc": t["prixTTC"], "total_ht": t["prixHT"], "total_tva": t["tva"],
        "marge_effective_pct": i["margeEffectivePct"], "prix_plancher": u["plancherApplique"],
    }
    if calcul.MATIERES[matiere]["plaque"]:
        resume.update(temps_machine_min=i["tempsMachineMin"], temps_estime=i["tempsEstime"], surface_achetee_m2=t["surfaceM2"])
    else:
        resume.update(temps_impression_min=e["tempsImpressionMin"], poids_g=e["poidsG"], poids_total_g=t["poidsG"])
        if matiere == "resine":
            resume.update(volume_ml=e["volumeMl"], volume_total_ml=t["volumeMl"])
    resume.update(unitaire=u, total=t, indicateurs=i,
                  analyse={k: v for k, v in analyse.items() if isinstance(v, (int, float, str, bool)) or v is None})
    return resume


def executer(matiere, params, inputs, out_dir, progress):
    """Estimation complète d'une matière ; rend le dict du contrat Alpine Tools."""
    matiere = calcul.matiere_de(matiere)
    params = dict(params or {})
    inputs = dict(inputs or {})
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    p = calcul.parametres_defaut(matiere)
    prix_defaut = (p["laser"] or p["cnc"] or {}).get("prixM2Defaut", 0.0) if calcul.MATIERES[matiere]["plaque"] else 0.0
    nom, lignes_analyse, analyse, valeurs = _piece(matiere, params, inputs, progress)
    s = _saisie(matiere, params, valeurs, prix_defaut, nom is not None)
    r = calcul.calculer(s, p, matiere)
    progress(0.85, "Devis estimatif")
    details = [f"{calcul.MATIERES[matiere]['libelle']} — estimation"] + lignes_analyse + _lignes_piece(matiere, r["entrees"])
    details += _lignes_prix(matiere, r) + _lignes_calcul(matiere, r, p)
    nom_csv = f"devis-estimation-{matiere}.csv"
    _csv(out_dir / nom_csv, matiere, nom, r, analyse)
    _apercu(out_dir / "apercu.png", r["unitaire"])
    progress(1.0, "Estimation terminée")
    return {"files": [{"name": nom_csv, "label": "Devis estimatif (CSV)", "library": None}], "preview": "apercu.png",
            "details": details, "summary": _resume(matiere, nom, r, analyse)}
