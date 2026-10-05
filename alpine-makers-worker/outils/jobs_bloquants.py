# -*- coding: utf-8 -*-
"""Liste les jobs encore "actifs" du journal durable du Worker, en LECTURE SEULE.

Un job est actif si son etat n'est pas terminal (completed, failed, cancelled)
OU si l'arret du moteur n'est pas confirme (engine_stop_confirmed != True).
Tant qu'un job de l'identite COURANTE est actif, l'agent refuse l'arret des
moteurs, la maintenance et la mise a jour.

Garanties :
  - le journal est ouvert avec mode=ro : aucune ecriture n'est possible ;
  - config.json n'est lu que pour worker_id et server_url (jamais le token) ;
  - le worker_id n'est affiche que par ses 8 premiers caracteres.

Python standard uniquement. Codes de sortie : 0 lu, 1 lecture impossible.
"""
import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

TERMINAUX = {"completed", "failed", "cancelled"}


def identite_courante(racine):
    """Ne retient QUE worker_id et server_url de config.json."""
    try:
        brut = json.loads((racine / "config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return "", ""
    if not isinstance(brut, dict):
        return "", ""
    # Comparaison EXACTE, comme l'agent (agent.py, _job_identity_matches) : aucun "/" final retire.
    return str(brut.get("worker_id") or ""), str(brut.get("server_url") or "")


def anciens_jobs_ignores(racine):
    """L'agent INSTALLE ignore-t-il les jobs d'anciennes identites ?

    Le correctif (filtre _job_identity_matches dans _assert_no_active_jobs) est arrive
    sans changement d'AGENT_VERSION : la version ne dit rien, seul le code installe
    fait foi. True = filtre present ; False = absent (ces jobs bloquent encore la
    maintenance et l'arret des moteurs) ; None = agent.py illisible ou methode
    introuvable (on ne devine pas).
    """
    try:
        source = (racine / "agent.py").read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    debut = re.search(r"^([ \t]*)def _assert_no_active_jobs\(", source, re.M)
    if not debut:
        return None
    suite = source[debut.end():]
    # Le corps s'arrete a la prochaine definition de meme niveau d'indentation (ou moins).
    fin = re.search(r"^[ \t]{0,%d}(?:async[ \t]+def|def|class)[ \t]" % len(debut.group(1)), suite, re.M)
    corps = suite[:fin.start()] if fin else suite
    return bool(re.search(r"\b_job_identity_matches\(", corps))


def lire_jobs(racine):
    journal = racine / "runtime" / "worker-journal.sqlite3"
    if not journal.is_file():
        return None, "journal_absent"
    # as_uri() encode l'espace et l'accent du profil Windows ; mode=ro interdit toute ecriture.
    uri = journal.resolve().as_uri() + "?mode=ro"
    try:
        connexion = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error as erreur:
        return None, "ouverture_impossible: %s" % erreur
    try:
        connexion.execute("PRAGMA query_only=ON")
        lignes = connexion.execute("SELECT id, data FROM records WHERE kind='jobs'").fetchall()
    except sqlite3.Error as erreur:
        return None, "lecture_impossible: %s" % erreur
    finally:
        connexion.close()
    return lignes, ""


def analyser(racine):
    worker_id, server_url = identite_courante(racine)
    lignes, erreur = lire_jobs(racine)
    rapport = {
        "ok": lignes is not None,
        "error": erreur,
        "checked_at": time.time(),
        "identity_known": bool(worker_id),
        "jobs_total": 0,
        "active_current": 0,
        "active_previous": 0,
        "unreadable": 0,
        "previous_identity_ignored": anciens_jobs_ignores(racine),
        "jobs": [],
    }
    if lignes is None:
        return rapport
    rapport["jobs_total"] = len(lignes)
    for job_id, brut in lignes:
        try:
            job = json.loads(brut)
            if not isinstance(job, dict):
                raise ValueError
        except ValueError:
            rapport["unreadable"] += 1
            continue
        etat = str(job.get("state") or "")
        confirme = job.get("engine_stop_confirmed") is True
        if etat in TERMINAUX and confirme:
            continue
        dossier = None
        if job.get("workdir"):
            dossier = Path(str(job["workdir"]))
            if not dossier.is_absolute():
                dossier = racine / dossier
        verrou = annulation = existe = False
        if dossier is not None:
            try:
                existe = dossier.is_dir()
                verrou = (dossier / ".runner.lock").exists()
                annulation = (dossier / ".cancel.json").exists()
            except OSError:
                pass
        job_worker = str(job.get("worker_id") or "")
        job_serveur = str(job.get("server_url") or "")
        if not worker_id:
            identite = "inconnue"
        elif job_worker == worker_id and job_serveur == server_url:
            identite = "courante"
        else:
            identite = "precedente"
        if identite == "precedente":
            rapport["active_previous"] += 1
        else:
            rapport["active_current"] += 1
        rapport["jobs"].append({
            "job_id": str(job_id),
            "tool_id": str(job.get("tool_id") or ""),
            "state": etat,
            "engine_stop_confirmed": confirme,
            "cancel_requested": bool(etat == "cancel_requested" or job.get("cancel_requested") or annulation),
            "identity": identite,
            "worker_id_prefix": job_worker[:8],
            "server_url": job_serveur,
            "workdir_exists": existe,
            "runner_lock": verrou,
            "updated_at": job.get("updated_at"),
        })
    rapport["jobs"].sort(key=lambda j: (j["identity"] != "courante", -(j["updated_at"] or 0)))
    return rapport


def main():
    analyseur = argparse.ArgumentParser(description="Jobs actifs du journal du Worker (lecture seule).")
    analyseur.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    analyseur.add_argument("--json", action="store_true")
    options = analyseur.parse_args()
    rapport = analyser(Path(options.root).resolve())
    if options.json:
        sys.stdout.write(json.dumps(rapport, ensure_ascii=True) + "\n")
    elif not rapport["ok"]:
        print("Lecture impossible : %s" % rapport["error"])
    else:
        print("%d job(s) au journal ; actifs : %d (identite courante), %d (identite precedente)." % (
            rapport["jobs_total"], rapport["active_current"], rapport["active_previous"]))
        ignores = rapport["previous_identity_ignored"]
        if ignores is False:
            print("ATTENTION : cet agent n'ignore PAS encore les jobs d'anciennes identites ; mets le Worker a jour.")
        elif ignores is None:
            print("Impossible de verifier dans agent.py que l'agent ignore les jobs d'anciennes identites.")
        for job in rapport["jobs"]:
            print("  %s  %-16s etat=%-16s arret_confirme=%s annulation=%s identite=%s verrou=%s" % (
                job["job_id"], job["tool_id"], job["state"], job["engine_stop_confirmed"],
                job["cancel_requested"], job["identity"], job["runner_lock"]))
    return 0 if rapport["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
