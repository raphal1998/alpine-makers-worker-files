# -*- coding: utf-8 -*-
r"""Diagnostic profond du Worker (« Réparer », entrée 24 du menu ; agent 1.50.33).

Cherche, SANS l'agent (il peut être arrêté ou figé), ce qui empêche le Worker de bien marcher, et répare seulement ce
qui se répare sans risque. Python standard uniquement (Python 3.9 ou plus), aucun réseau, aucun processus arrêté.

Contrôles :
  D-PYTHON            version de l'interpréteur du Worker ;
  D-CONFIG            config.json lisible (jamais affiché : ni jeton ni identité) ;
  D-IDENTITE-DEPS     bibliothèque de sécurité (cryptography) présente quand l'identité signée est active ;
  D-ETAT-CORROMPU     fichiers d'état de runtime\ illisibles (JSON cassé) : l'agent les recrée ;
  D-JOURNAL           journal durable des jobs (SQLite) : contrôle d'intégrité, en lecture seule ;
  D-BOITE-PERIMEE     demandes ou réponses de la boîte locale restées trop longtemps ;
  D-VERROU-ORPHELIN   fichiers de verrou d'installation que plus aucun processus ne tient ;
  D-COMPOSANT-ABSENT  moteur ou dossier déclaré (fiches runtime, storage.json) mais absent du disque ;
  D-DISQUE            espace libre du lecteur du Worker ;
  D-JOBS-ANCIENS      jobs restés actifs au journal depuis longtemps ;
  D-ERREURS-RECURRENTES / D-ERREUR-INCONNUE   passe « inconnu » : erreurs et traces qui reviennent dans les
                      journaux récents, regroupées, avec une piste quand elle est connue.

Réparations (``--repair D-…,D-…``), chacune refaite seulement si le contrôle la trouve encore :
  D-ETAT-CORROMPU, D-BOITE-PERIMEE, D-VERROU-ORPHELIN : fichiers DÉPLACÉS (jamais effacés) dans
  runtime\sauvegardes-reparation\<date>\ ; D-IDENTITE-DEPS : identity_setup.py (installation officielle de la
  bibliothèque dans runtime\identity-deps). Jamais une donnée utilisateur, un modèle, la configuration ni le journal.

Sortie ``--json`` : {"ok", "checked_at", "points": [{code, gravite, titre, detail, reparable, reparation, manuel}],
"repairs": [{code, ok, message}]}. Codes de sortie : 0 lu (et réparations réussies), 1 échec.
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

DISQUE_ALERTE_GIO = 10.0
DISQUE_ERREUR_GIO = 2.0
BOITE_DEMANDE_SECONDES = 600          # une demande que l'agent n'a pas lue en dix minutes ne sera plus lue
BOITE_RESULTAT_SECONDES = 3600        # une réponse que personne n'a relue en une heure ne sert plus
VERROU_SECONDES = 3600
JOB_ANCIEN_SECONDES = 6 * 3600
JOURNAL_OCTETS = 1536 * 1024          # fin de chaque journal analysée
JOURNAL_JOURS = 7
ERREURS_MIN_REPETITIONS = 3
REPARABLES = ("D-ETAT-CORROMPU", "D-BOITE-PERIMEE", "D-VERROU-ORPHELIN", "D-IDENTITE-DEPS")
TERMINAUX = {"completed", "failed", "cancelled"}

# Pistes connues pour la passe « inconnu » : motif (insensible à la casse) -> conseil.
PISTES = (
    (r"no module named|modulenotfounderror|importerror", "Un module Python manque : « Réparer » (24) réinstalle la bibliothèque de sécurité ; pour un moteur : Mes Workers > Installations > Réparer."),
    (r"permissionerror|access is denied|accès refusé|winerror 5\b", "Accès refusé à un fichier du Worker : « Rallumer le Worker » (12) rend la clé au compte ; sinon vérifie les droits du dossier (État complet, 1)."),
    (r"address already in use|winerror 10048|only one usage of each socket", "Un port est déjà pris : « Moteurs (état, arrêt, départ) » (9) montre quel programme le tient."),
    (r"cuda out of memory|outofmemoryerror|not enough memory|memoryerror", "Mémoire insuffisante (carte graphique ou PC) : « Libérer la mémoire du PC » (16), ou un seul moteur à la fois (9)."),
    (r"database is locked|database disk image is malformed|sqlite3\.", "Journal SQLite occupé ou abîmé : « Réparer » (24) le contrôle ; s'il est abîmé, garde une copie du dossier runtime et demande de l'aide (4)."),
    (r"connectionrefused|winerror 10061|connection refused", "Un moteur local ne répond pas (arrêté ou tombé) : « Moteurs (état, arrêt, départ) » (9), choix 3."),
    (r"mediamtx|hls upstream", "Le relais vidéo de PrintGuard redémarre en boucle : vérifie la caméra de l'imprimante, puis relance PrintGuard avec « Moteurs (état, arrêt, départ) » (9)."),
    (r"could not open|no such file|filenotfounderror|model allocation is null|introuvable",
     "Un fichier d'un moteur manque ou est abîmé : répare ce moteur (Mes Workers > Installations > Réparer, ou « Contrôle total et réparation », 18)."),
    (r"commandes worker momentanément indisponibles|connexion worker interrompue|urlerror|getaddrinfo|name or service not known|winerror 11001",
     "L'agent n'arrive pas à joindre le site : vérifie la connexion Internet du PC, puis « Reconnecter le Worker » (5)."),
    (r"certificate|\bssl\b|\btls\b", "Échec de connexion sécurisée : vérifie l'heure du PC (État complet, 1), puis le réseau."),
    (r"http error 401|http error 403|unauthorized|forbidden", "Le site refuse ce Worker : « Récupérer une inscription » (7), ou « Réassocier le Worker » (8) si sa fiche a été supprimée."),
    (r"no space left|disk full|espace disque|winerror 112", "Disque plein : « Nettoyage léger » (14), puis « Contrôler les doublons » (19)."),
    (r"jsondecodeerror|expecting value|json invalide", "Fichier de réglages ou d'état illisible : « Réparer » (24) déplace les fichiers d'état abîmés (sauvegardés)."),
    (r"timed out|timeout|délai dépassé", "Délais dépassés répétés : réseau lent ou moteur surchargé ; « Redémarrer l'agent » (10) si cela dure."),
)
_NOMBRES = re.compile(r"\b\d[\d.:,-]*\b")
_CHEMINS = re.compile(r"([A-Za-z]:\\|/)[^\s'\"]+")
_HEX = re.compile(r"\b[0-9a-f]{12,}\b", re.I)
# Ligne d'erreur : niveau ERROR / CRITICAL / FATAL des moteurs, ou message français de l'agent. Un avertissement
# (WARNING, [W:…]) n'en est pas une : les moteurs en écrivent des centaines sans gravité.
_LIGNE_ERREUR = re.compile(r"\b(ERROR|CRITICAL|FATAL)\b|(?i:\b(erreur|échec|impossible|indisponibles?|interrompue?s?)\b)")
_AVERTISSEMENT = re.compile(r"\bWARNING\b|\[W:|\bINFO\b|\bDEBUG\b")
_FAUX_POSITIFS = re.compile(r"(?i)uvicorn\.error|errors?\s*[=:]\s*(0|none|null|false|\[\])|ligne\(s\) d'erreur")
_FIN_TRACE = re.compile(r"^([A-Za-z_][\w.]*(Error|Exception|Exit|Interrupt))\b(:.*)?$")


def point(code, gravite, titre, detail="", reparable=False, reparation="", manuel=""):
    return {"code": code, "gravite": gravite, "titre": titre[:300], "detail": str(detail)[:600], "reparable": bool(reparable),
            "reparation": reparation[:300], "manuel": manuel[:400]}


def lire_json(chemin):
    """(valeur, erreur) : erreur vaut "" si le fichier se lit, "absent" s'il manque."""
    try:
        return json.loads(Path(chemin).read_text(encoding="utf-8-sig")), ""
    except FileNotFoundError:
        return None, "absent"
    except (OSError, ValueError) as erreur:
        return None, type(erreur).__name__


def controle_python():
    version = sys.version_info
    if version < (3, 9):
        return [point("D-PYTHON", "erreur", "Le Python du Worker est trop ancien (%d.%d) : 3.9 au moins est nécessaire." % version[:2],
                      manuel="Installe Python 3.11 depuis python.org, puis relance install_windows.bat (l'association est conservée).")]
    return []


def controle_config(racine):
    valeur, erreur = lire_json(racine / "config.json")
    if erreur == "absent":
        return [point("D-CONFIG", "erreur", "config.json est absent : ce Worker n'est associé à aucun compte.",
                      manuel="« Réassocier le Worker » (8), ou install_windows.bat pour une première installation.")], {}
    if erreur or not isinstance(valeur, dict):
        return [point("D-CONFIG", "erreur", "config.json est illisible (fichier abîmé) : l'agent ne peut pas démarrer.",
                      "Il contient l'association du Worker : cet outil n'y touche jamais.",
                      manuel="Restaure-le depuis une sauvegarde (« Sauvegarder le Worker », 13), sinon « Récupérer une inscription » (7).")], {}
    return [], valeur


def controle_identite(racine, config):
    if not config.get("identity_enabled"):
        return []
    dossier = racine / "runtime" / "identity-deps"
    if dossier.is_dir() and str(dossier) not in sys.path:
        sys.path.insert(0, str(dossier))
    try:
        __import__("cryptography.hazmat.primitives.asymmetric.ed25519")
        return []
    except Exception:
        pass
    setup = racine / "identity_setup.py"
    return [point("D-IDENTITE-DEPS", "erreur", "La bibliothèque de sécurité du Worker (cryptography) manque : l'agent ne peut pas signer ses demandes.",
                  reparable=setup.is_file(), reparation="réinstaller cryptography dans runtime\\identity-deps (identity_setup.py, quelques Mo téléchargés)",
                  manuel="" if setup.is_file() else "identity_setup.py manque : mets le Worker à jour (23) ou réinstalle-le par-dessus.")]


def fichiers_etat(racine):
    """Fichiers d'état JSON que l'agent recrée seul : fiches de runtime\\ et mémos de la boîte locale."""
    runtime = racine / "runtime"
    candidats = []
    if runtime.is_dir():
        candidats += [chemin for chemin in runtime.glob("*.json") if chemin.is_file()]
        boite = runtime / "local-control"
        if boite.is_dir():
            candidats += [chemin for chemin in boite.glob("*.json") if chemin.is_file()
                          and not chemin.name.endswith((".request.json", ".result.json"))]
    return candidats


def controle_etats(racine):
    abimes = []
    for chemin in fichiers_etat(racine):
        valeur, erreur = lire_json(chemin)
        if erreur and erreur != "absent":
            abimes.append(chemin)
        elif not erreur and not isinstance(valeur, (dict, list)):
            abimes.append(chemin)
    if not abimes:
        return [], []
    noms = ", ".join(str(chemin.relative_to(racine)) for chemin in abimes[:8])
    return [point("D-ETAT-CORROMPU", "alerte", "%d fichier(s) d'état illisible(s) : %s" % (len(abimes), noms),
                  "L'agent les recrée ; abîmés, ils faussent l'état des moteurs ou de la dernière extinction.",
                  reparable=True, reparation="les déplacer (sauvegardés) dans runtime\\sauvegardes-reparation")], abimes


def controle_journal(racine):
    journal = racine / "runtime" / "worker-journal.sqlite3"
    if not journal.is_file():
        return [], None
    try:
        connexion = sqlite3.connect(journal.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as erreur:
        return [point("D-JOURNAL", "alerte", "Journal des jobs impossible à ouvrir (%s)." % type(erreur).__name__,
                      manuel="Si l'agent tourne, il le tient peut-être : relance « Réparer » plus tard.")], None
    try:
        connexion.execute("PRAGMA query_only=ON")
        verdict = connexion.execute("PRAGMA quick_check").fetchone()
        lignes = connexion.execute("SELECT id, data FROM records WHERE kind='jobs'").fetchall()
    except sqlite3.Error as erreur:
        if "locked" in str(erreur).lower():
            return [point("D-JOURNAL", "info", "Journal des jobs occupé par l'agent : contrôle d'intégrité non fait.")], None
        return [point("D-JOURNAL", "erreur", "Journal des jobs illisible (%s)." % type(erreur).__name__,
                      "Il garde les reçus des calculs et des installations : cet outil n'y touche jamais.",
                      manuel="Garde une copie du dossier runtime, puis lance « Rapport de diagnostic » (4) pour l'aide.")], None
    finally:
        connexion.close()
    if not verdict or str(verdict[0]).lower() != "ok":
        return [point("D-JOURNAL", "erreur", "Le journal des jobs est abîmé (contrôle SQLite : %s)." % str(verdict and verdict[0])[:120],
                      "Il garde les reçus des calculs et des installations : cet outil n'y touche jamais.",
                      manuel="Garde une copie du dossier runtime, puis lance « Rapport de diagnostic » (4) pour l'aide.")], None
    return [], lignes


def controle_jobs(lignes, maintenant):
    anciens = []
    for job_id, brut in lignes or []:
        try:
            job = json.loads(brut)
        except ValueError:
            continue
        if not isinstance(job, dict):
            continue
        actif = str(job.get("state") or "") not in TERMINAUX or job.get("engine_stop_confirmed") is not True
        date = job.get("updated_at")
        if actif and isinstance(date, (int, float)) and maintenant - date > JOB_ANCIEN_SECONDES:
            anciens.append("%s (%s)" % (str(job.get("tool_id") or "?")[:30], str(job.get("state") or "?")[:20]))
    if not anciens:
        return []
    return [point("D-JOBS-ANCIENS", "alerte", "%d job(s) actif(s) au journal depuis plus de 6 heures : %s" % (len(anciens), ", ".join(anciens[:6])),
                  "Un job resté actif bloque l'arrêt des moteurs, la maintenance et les mises à jour.",
                  manuel="« Arrêter tous les jobs » (21), puis « Jobs bloquants » (2) si l'un d'eux reste.")]


def controle_boite(racine, maintenant):
    boite = racine / "runtime" / "local-control"
    perimes = []
    if boite.is_dir():
        for chemin in boite.iterdir():
            try:
                age = maintenant - chemin.stat().st_mtime
            except OSError:
                continue
            if chemin.name.endswith(".request.json") and age > BOITE_DEMANDE_SECONDES:
                perimes.append(chemin)
            elif chemin.name.endswith(".result.json") and age > BOITE_RESULTAT_SECONDES:
                perimes.append(chemin)
    if not perimes:
        return [], []
    return [point("D-BOITE-PERIMEE", "alerte", "%d demande(s) ou réponse(s) périmée(s) dans la boîte locale de l'agent." % len(perimes),
                  "Une demande jamais lue fait croire aux outils que l'agent est bloqué.",
                  reparable=True, reparation="les déplacer (sauvegardées) dans runtime\\sauvegardes-reparation")], perimes


def verrou_tenu(chemin):
    """Un processus tient-il ce verrou (octet 0 verrouillé) ? En cas de doute : oui."""
    try:
        with open(chemin, "a+b") as fichier:
            fichier.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fichier.fileno(), msvcrt.LK_NBLCK, 1)
                fichier.seek(0)
                msvcrt.locking(fichier.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fichier.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fichier.fileno(), fcntl.LOCK_UN)
        return False
    except OSError:
        return True


def controle_verrous(racine, maintenant):
    runtime = racine / "runtime"
    orphelins = []
    if runtime.is_dir():
        for chemin in runtime.glob("install-*.lock"):
            try:
                if maintenant - chemin.stat().st_mtime > VERROU_SECONDES and not verrou_tenu(chemin):
                    orphelins.append(chemin)
            except OSError:
                continue
    if not orphelins:
        return [], []
    return [point("D-VERROU-ORPHELIN", "info", "%d fichier(s) de verrou d'installation que plus aucun processus ne tient." % len(orphelins),
                  reparable=True, reparation="les déplacer (sauvegardés) dans runtime\\sauvegardes-reparation")], orphelins


def controle_composants(racine):
    points = []
    absents = []
    for chemin in sorted((racine / "runtime").glob("*.json")) if (racine / "runtime").is_dir() else []:
        valeur, erreur = lire_json(chemin)
        if erreur or not isinstance(valeur, dict):
            continue
        dossier = str(valeur.get("root") or "").strip()
        if dossier and valeur.get("enabled") is not False and not Path(dossier).is_dir():
            absents.append("%s (%s)" % (chemin.stem, dossier))
    if absents:
        points.append(point("D-COMPOSANT-ABSENT", "alerte", "Moteur noté par l'agent mais absent du disque : " + ", ".join(absents[:4]),
                            "Le dossier du moteur a disparu (désinstallé à la main, disque débranché ou déplacé) : il ne pourra pas démarrer.",
                            manuel="Rebranche le disque, ou réinstalle le moteur : Mes Workers > Installations > Réparer (ou « Contrôle total et réparation », 18)."))
    stockage, erreur = lire_json(racine / "storage.json")
    environnement = stockage.get("environment") if isinstance(stockage, dict) else None
    if (erreur and erreur != "absent") or (stockage is not None and not isinstance(environnement, dict)):
        points.append(point("D-STOCKAGE", "erreur", "storage.json (emplacements des modèles et caches) est illisible.",
                            "Il contient tes emplacements de stockage : cet outil n'y touche jamais.",
                            manuel="Règle de nouveau l'emplacement dans Mes Workers > Stockage, ou restaure une sauvegarde (13)."))
    elif isinstance(environnement, dict):
        # Un dossier pas encore créé n'est pas une panne ; un LECTEUR absent (disque débranché, lettre changée), si.
        manquants = []
        for cle, valeur in environnement.items():
            if isinstance(valeur, str) and re.match(r"^[A-Za-z]:\\", valeur) and not Path(Path(valeur).anchor).exists():
                manquants.append("%s (%s)" % (str(cle)[:40], valeur[:120]))
        if manquants:
            points.append(point("D-STOCKAGE", "erreur", "Lecteur de stockage introuvable : " + ", ".join(manquants[:4]),
                                "Les modèles ou caches rangés là sont invisibles pour le Worker (disque débranché ou lettre changée).",
                                manuel="Rebranche le disque, ou change l'emplacement dans Mes Workers > Stockage."))
    return points


def controle_disque(racine):
    try:
        usage = shutil.disk_usage(str(racine))
    except OSError:
        return []
    libre = usage.free / (1024 ** 3)
    if libre < DISQUE_ERREUR_GIO:
        gravite = "erreur"
    elif libre < DISQUE_ALERTE_GIO:
        gravite = "alerte"
    else:
        return []
    return [point("D-DISQUE", gravite, "Il reste %.1f Gio sur le lecteur du Worker." % libre,
                  "Téléchargements de modèles, générations et mises à jour échouent faute de place.",
                  manuel="« Nettoyage léger » (14), puis « Contrôler les doublons » (19) ; déplace des modèles vers un autre disque (Mes Workers > Stockage).")]


def journaux(racine, avec_service):
    trouves = []
    dossier = racine / "logs"
    if dossier.is_dir():
        # Les journaux des outils et de worker.exe recopient ce que les outils affichent : jamais analysés.
        trouves += [chemin for chemin in dossier.glob("*.log") if chemin.name not in ("outils-worker.log", "worker-manager.log")]
    if avec_service:
        commun = Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "AlpineMakers" / "logs"
        if commun.is_dir():
            service = sorted((chemin for chemin in commun.glob("AlpineWorker*.log")
                              if re.match(r"^AlpineWorker(\.err)?(-\d{8}T[\d.]+)?\.log$", chemin.name)),
                             key=lambda chemin: chemin.stat().st_mtime, reverse=True)
            trouves += service[:4]
    limite = time.time() - JOURNAL_JOURS * 86400
    return [chemin for chemin in trouves if chemin.is_file() and chemin.stat().st_mtime >= limite]


def lire_fin(chemin):
    with open(chemin, "rb") as fichier:
        fichier.seek(0, os.SEEK_END)
        taille = fichier.tell()
        fichier.seek(max(0, taille - JOURNAL_OCTETS))
        brut = fichier.read()
    if brut.startswith(b"\xff\xfe") or (len(brut) > 3 and brut[1:2] == b"\x00"):
        texte = brut.decode("utf-16-le", "replace")
    else:
        try:
            texte = brut.decode("utf-8")
        except UnicodeDecodeError:
            texte = brut.decode("cp1252", "replace")      # sortie console Windows d'un moteur
    return texte.splitlines()


def masquer(ligne, racine):
    """Chemins remplacés par des repères : le dossier du Worker et le profil Windows ne sont jamais recopiés."""
    for valeur, repere in ((str(racine), "<Worker>"), (os.environ.get("USERPROFILE") or "", "<profil>")):
        if len(valeur) > 3:
            ligne = re.sub(re.escape(valeur), repere, ligne, flags=re.I)
    ligne = re.sub(r"\x1b\[[0-9;]*m", "", ligne)               # couleurs de console
    return _CHEMINS.sub("<chemin>", ligne)


def normaliser(ligne, racine):
    ligne = masquer(ligne, racine)
    ligne = _HEX.sub("<id>", ligne)
    ligne = _NOMBRES.sub("#", ligne)
    return " ".join(ligne.split())[:160]


def piste(texte):
    for motif, conseil in PISTES:
        if re.search(motif, texte, re.I):
            return conseil
    return ""


def controle_journaux(racine, avec_service):
    """Passe « inconnu » : erreurs qui reviennent (au moins trois fois) et fins de trace Python, regroupées."""
    groupes = {}
    for chemin in journaux(racine, avec_service):
        try:
            lignes = lire_fin(chemin)
        except OSError:
            continue
        for index, brute in enumerate(lignes):
            ligne = brute.strip()
            if not ligne:
                continue
            fin = _FIN_TRACE.match(ligne)
            if fin is None and (not _LIGNE_ERREUR.search(ligne) or _AVERTISSEMENT.search(ligne) or _FAUX_POSITIFS.search(ligne)):
                continue
            if ligne.startswith("Traceback"):
                continue          # la dernière ligne de la trace porte la cause : elle est comptée à part
            cle = normaliser(ligne, racine)
            groupe = groupes.setdefault(cle, {"nombre": 0, "journal": chemin.name, "exemple": ligne, "trace": False})
            groupe["nombre"] += 1
            groupe["exemple"] = ligne
            groupe["trace"] = groupe["trace"] or bool(fin)
    retenus = sorted((item for item in groupes.values()
                      if item["nombre"] >= ERREURS_MIN_REPETITIONS or (item["trace"] and item["nombre"] >= 2)),
                     key=lambda item: -item["nombre"])[:6]
    points = []
    for item in retenus:
        exemple = " ".join(masquer(item["exemple"], racine).split())[:220]
        conseil = piste(item["exemple"])
        titre = "Erreur répétée %d fois dans %s : %s" % (item["nombre"], item["journal"], exemple)
        if conseil:
            points.append(point("D-ERREURS-RECURRENTES", "alerte", titre, manuel=conseil))
        else:
            points.append(point("D-ERREUR-INCONNUE", "alerte", titre,
                                "Erreur que cet outil ne sait pas encore expliquer.",
                                manuel="« Journaux » (3) montre son contexte ; « Rapport de diagnostic » (4) prépare un fichier sans secret pour l'aide."))
    return points


def diagnostiquer(racine, avec_service=False):
    maintenant = time.time()
    points, cibles = [], {}
    points += controle_python()
    trouves, config = controle_config(racine)
    points += trouves
    points += controle_identite(racine, config)
    trouves, cibles["D-ETAT-CORROMPU"] = controle_etats(racine)
    points += trouves
    trouves, lignes = controle_journal(racine)
    points += trouves
    points += controle_jobs(lignes, maintenant)
    trouves, cibles["D-BOITE-PERIMEE"] = controle_boite(racine, maintenant)
    points += trouves
    trouves, cibles["D-VERROU-ORPHELIN"] = controle_verrous(racine, maintenant)
    points += trouves
    points += controle_composants(racine)
    points += controle_disque(racine)
    points += controle_journaux(racine, avec_service)
    ordre = {"erreur": 0, "alerte": 1, "info": 2}
    points.sort(key=lambda item: ordre.get(item["gravite"], 3))
    return points, cibles


def deplacer(racine, fichiers, horodatage):
    """Déplace les fichiers (jamais effacés) sous runtime\\sauvegardes-reparation\\<date>\\, chemin relatif gardé."""
    coffre = racine / "runtime" / "sauvegardes-reparation" / horodatage
    deplaces = 0
    for chemin in fichiers:
        try:
            relatif = chemin.resolve().relative_to(racine.resolve())
        except ValueError:
            continue           # jamais un fichier hors du dossier du Worker
        cible = coffre / relatif
        cible.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(chemin), str(cible))
            deplaces += 1
        except OSError:
            continue
    return deplaces, coffre


def reparer(racine, codes, avec_service=False):
    points, cibles = diagnostiquer(racine, avec_service)
    presents = {item["code"] for item in points if item["reparable"]}
    horodatage = time.strftime("%Y%m%d-%H%M%S")
    resultats = []
    for code in codes:
        if code not in REPARABLES:
            resultats.append({"code": code, "ok": False, "message": "Réparation inconnue : rien n'a été fait."})
            continue
        if code not in presents:
            resultats.append({"code": code, "ok": True, "message": "Déjà en ordre : rien à faire."})
            continue
        if code == "D-IDENTITE-DEPS":
            try:
                fini = subprocess.run([sys.executable, str(racine / "identity_setup.py")], cwd=str(racine), timeout=300,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                ok = fini.returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
            resultats.append({"code": code, "ok": ok, "message": "Bibliothèque de sécurité réinstallée." if ok else
                              "Installation de la bibliothèque de sécurité impossible (réseau ?) : réessaie plus tard."})
            continue
        fichiers = cibles.get(code) or []
        deplaces, coffre = deplacer(racine, fichiers, horodatage)
        ok = deplaces == len(fichiers)
        resultats.append({"code": code, "ok": ok, "message": "%d fichier(s) déplacé(s) dans %s." % (deplaces, coffre.relative_to(racine))})
    return resultats


def main():
    analyseur = argparse.ArgumentParser(description="Diagnostic profond du Worker (lecture seule, sauf --repair).")
    analyseur.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    analyseur.add_argument("--json", action="store_true")
    analyseur.add_argument("--service-logs", action="store_true", help="analyser aussi les journaux du service AlpineWorker")
    analyseur.add_argument("--repair", default="", help="codes à réparer, séparés par des virgules")
    options = analyseur.parse_args()
    try:
        sys.stdout.reconfigure(errors="replace")   # console en page de code : jamais d'échec sur un accent
    except (AttributeError, ValueError):
        pass
    racine = Path(options.root).resolve()
    rapport = {"ok": True, "checked_at": time.time(), "points": [], "repairs": []}
    try:
        if options.repair:
            rapport["repairs"] = reparer(racine, [code.strip() for code in options.repair.split(",") if code.strip()], options.service_logs)
        rapport["points"], _ = diagnostiquer(racine, options.service_logs)
    except Exception as erreur:  # filet : l'outil appelant affiche une erreur propre
        rapport.update(ok=False, error=type(erreur).__name__)
    if options.json:
        sys.stdout.write(json.dumps(rapport, ensure_ascii=True) + "\n")
    else:
        for item in rapport["points"]:
            print("[%s] %s %s" % (item["code"], item["gravite"], item["titre"]))
        for item in rapport["repairs"]:
            print("réparation %s : %s" % (item["code"], item["message"]))
    return 0 if rapport["ok"] and all(item["ok"] for item in rapport["repairs"]) else 1


if __name__ == "__main__":
    sys.exit(main())
