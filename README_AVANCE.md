# Worker Alpine Makers — documentation avancée (installation par fichiers)

Document technique. Pour installer et utiliser le Worker, commence par [README.md](README.md).
Ce document décrit le fonctionnement **public** du Worker ; il ne contient ni secret, ni clé, ni détail
d'administration du Dashboard.

## Sommaire

1. [Architecture](#1-architecture)
2. [Cycle de vie et processus](#2-cycle-de-vie-et-processus)
3. [Communication avec le Dashboard](#3-communication-avec-le-dashboard)
4. [Réseau et ports](#4-réseau-et-ports)
5. [Fichiers et dossiers](#5-fichiers-et-dossiers)
6. [Mise à jour](#6-mise-à-jour)
7. [Sécurité](#7-sécurité)
8. [EXE ou fichiers : les différences](#8-exe-ou-fichiers--les-différences)
9. [Diagnostic avancé](#9-diagnostic-avancé)
10. [Limites connues](#10-limites-connues)
11. [Maintenance pour Alpine Makers](#11-maintenance-pour-alpine-makers)

---

## 1. Architecture

```text
┌──────────────┐  HTTPS sortant (signé)  ┌───────────────────┐
│   Worker     │ ───────────────────────►│     Dashboard      │
│  agent.py    │◄─────────────────────── │ (réponses seulement)│
└──────┬───────┘                          └───────────────────┘
       │ lance, surveille, arrête
       ▼
  Moteurs locaux (processus séparés, écoute sur 127.0.0.1 uniquement)
  ComfyUI · Hunyuan3D · OrcaSlicer · FreeCAD · PrintGuard · surveillance graveuse · relais machines
```

| Module | Rôle |
| --- | --- |
| `agent.py` | Boucle principale : présence, réception des commandes et des travaux, inventaire, mise à jour. |
| `safety.py` | Validation stricte de tout ce qui vient du site (formes de workflows, noms de fichiers, archives). |
| `identity.py`, `identity_setup.py` | Clé d'identité du PC et signature des requêtes. |
| `job_state.py` | Journal durable des travaux (reprise après redémarrage sans double exécution). |
| `installers\` | Installation des moteurs et des modèles (téléchargement vérifié, reprise, annulation). |
| `runners\` | Exécution d'un travail par moteur. |
| `hardware_profiles.py` | Détection de la carte graphique et choix du profil CUDA. |
| `equipment_*.py` | Relais vers imprimantes, caméras et graveuses du réseau local. |
| `worker_legal.py`, `legal_policy.json`, `legal_evidence.json`, `legal_sources\` | Conditions et notices des composants tiers, vérifiées avant installation. |
| `outils\`, `MENU-WORKER.bat` | Menu d'outils local (diagnostic, connexion, cycle de vie, entretien). |
| `run_worker.ps1` | Superviseur : lance l'agent et le relance s'il s'arrête. |

## 2. Cycle de vie et processus

1. **Installation** (`install_windows.bat` → `install_windows.ps1`) : prérequis, copie, identité, association, démarrage automatique facultatif.
2. **Démarrage** : `run_worker.ps1` prend le verrou `supervisor.lock` (un seul superviseur par dossier) et lance `agent.py`.
3. **Marche** : l'agent envoie sa présence, relève son inventaire (moteurs, modèles, matériel), reçoit commandes et travaux.
4. **Moteurs** : démarrés à la demande, chacun dans son environnement Python isolé ; une fiche `runtime\<outil>.json` par moteur lancé.
5. **Arrêt** : volontaire (menu 11) ou fermeture de session ; les travaux en cours sont journalisés pour être repris ou clos proprement.
6. **Redémarrage** : le superviseur relance l'agent ; le code de sortie 75 signale une mise à jour appliquée.

Modes de lancement sous Windows : **tâche planifiée** à l'ouverture de session (choix O à l'installation), ou
**lancement manuel** (menu 5). Un PC peut aussi l'exécuter comme service Windows ; ce mode n'est pas créé par l'installateur.

## 3. Communication avec le Dashboard

| Mécanisme | Valeur |
| --- | --- |
| Sens | Toujours **du Worker vers le site**, HTTPS. |
| Présence (heartbeat) | Toutes les **20 s**. |
| Relève des commandes | Toutes les **3 s** environ. |
| Hors ligne | Après **75 s** sans présence, le site ne confie plus rien au Worker. |
| Reconnexion | Automatique : l'agent réessaie avec un délai progressif ; aucune action tant que le dossier et l'identité sont intacts. |
| Association | Code à usage unique, valable **10 minutes**, saisi à l'installation. |
| Authentification | Chaque requête est signée par la clé du PC ; l'adresse IP et le nom du PC ne sont jamais une identité. |

**Comportement hors ligne.** Un travail en cours continue localement ; son résultat est remis à la reconnexion.
Le journal durable empêche qu'un travail soit exécuté deux fois après une coupure. Aucune nouvelle demande n'arrive
tant que la présence n'est pas rétablie.

**Commandes.** Le site ne peut envoyer que des commandes d'une liste fermée (démarrer ou arrêter un moteur, installer
ou retirer un modèle, lancer un travail, mettre à jour l'agent…). Chacune est validée localement : un workflow d'image
est comparé nœud par nœud à une forme autorisée, un nom de fichier ne peut pas sortir de son dossier, un téléchargement
n'est accepté que depuis une source prévue et vérifié par empreinte SHA-256.

## 4. Réseau et ports

- **Aucun port entrant.** Rien à ouvrir dans le pare-feu Windows ni sur le routeur.
- Sortant : HTTPS (443) vers le Dashboard, et vers les sources officielles des moteurs et modèles lors d'une installation.
- Les moteurs écoutent **uniquement sur la boucle locale** (`127.0.0.1`), par exemple ComfyUI sur le port 8188. Ils ne sont pas joignables depuis le réseau.
- Les machines (imprimantes, caméras, graveuses) sont jointes **par le Worker** sur ton réseau local ; le site ne contacte jamais une adresse privée.

## 5. Fichiers et dossiers

Installation : `C:\Users\<compte>\Desktop\alpine-makers-worker`.

| Élément | Contenu | Remplacé par une mise à jour ? |
| --- | --- | --- |
| Programme (`*.py`, `*.ps1`, `*.bat`, `outils\`, `installers\`, `runners\`…) | Le contenu de ce dépôt. | Oui |
| `agent_manifest.json` | Version et empreinte de chaque fichier livré. | Oui |
| `config.json`, `config\` | Réglages et identité du PC (**secrets locaux**). | Jamais |
| `components\` | Moteurs installés, environnements Python, modèles. | Jamais |
| `cache\`, `temp\` | Caches de téléchargement et fichiers temporaires. | Jamais |
| `jobs\`, `outputs\`, `workspace\`, `data\` | Travaux, résultats, données des outils. | Jamais |
| `runtime\` | État vivant et journal durable. | Jamais |
| `logs\` | Journaux des moteurs et des outils. | Jamais |

Les fichiers que tu ajoutes toi-même dans le dossier ne sont pas touchés par une mise à jour.

## 6. Mise à jour

1. Le site prépare le paquet à partir de sa version courante (celle de ce dépôt).
2. Le Worker le télécharge par son canal authentifié, **vérifie l'empreinte de chaque fichier**, la version et les capacités annoncées.
3. Il remplace le programme, supprime les fichiers que le nouveau paquet ne livre plus, puis se relance.
4. La nouvelle version n'est considérée en service qu'au **heartbeat suivant** (elle s'affiche sur la carte du Worker).

Une mise à jour est refusée pendant un travail ou une installation en cours. Les versions suivent le schéma
`MAJEUR.MINEUR.CORRECTIF` ; l'historique est dans [UPDATE.md](UPDATE.md).

## 7. Sécurité

- **Identité par PC**, créée localement, protégée par le compte Windows qui a installé le Worker. Elle ne quitte jamais le PC et ne doit jamais être copiée.
- **Dossier réservé** à ton compte, au système et aux administrateurs.
- **Aucun terminal distant, aucune commande libre** : seules des actions connues, validées localement.
- **Téléchargements vérifiés** (source prévue, taille, SHA-256) ; un fichier incomplet n'est jamais déclaré installé.
- **Licences** : les conditions de chaque moteur et modèle sont présentées sur le site et vérifiées par le Worker avant installation ; les notices sont conservées dans le dossier.
- **Journaux** : jamais de clé, de jeton ni de code d'association.

Signaler un problème de sécurité : écris à l’équipe Alpine Makers (coordonnées sur <https://alpine-makers.ch>), sans publier le détail.

## 8. EXE ou fichiers : les différences

| | Fichiers (ce dépôt) | EXE ([dépôt jumeau](https://github.com/raphal1998/alpine-makers-worker-exe)) |
| --- | --- | --- |
| Forme | Dossier `alpine-makers-worker\` avec scripts lisibles | Un exécutable `alpine-makers-worker-setup.exe` |
| Contenu installé | Identique | Identique (l'exécutable contient ce même dossier) |
| Lancement | `install_windows.bat` | Double-clic sur le `.exe`, qui extrait puis lance `install_windows.bat` |
| Adresse du site | Demandée ou passée en paramètre | Déjà inscrite dans l'exécutable |
| Linux | Oui (`install_linux.sh`) | Non |
| Transparence | Tout est lisible avant exécution | Exécutable non signé : Windows SmartScreen avertit |

## 9. Diagnostic avancé

| Outil | Usage |
| --- | --- |
| Menu **1** (`outils\etat_worker.bat`) | État de l'agent, du superviseur, des moteurs, dernier contact. |
| Menu **2** (`outils\jobs_bloquants.bat`) | Travaux qui empêchent une mise à jour ou une installation. |
| Menu **3** / dossier `logs\` | Journaux par moteur. |
| Menu **4** (`outils\rapport_diagnostic.bat`) | Archive de diagnostic sans secret. |
| `nvidia-smi` | Vérifier que Windows voit la carte graphique. |

Codes de sortie des outils du menu : `0` succès, `1` échec, `2` refusé ou annulé, `3` fenêtre administrateur refusée.

Options communes des outils de `outils\` : `-Simulation` (montrer sans agir, pour ceux qui modifient), `-Oui` (ne pas
poser la question), `-SansPause`, `-Racine <dossier>`.

## 10. Limites connues

- Moteurs d'IA : carte **NVIDIA** uniquement ; la mémoire de plusieurs cartes n'est jamais additionnée.
- Sous Windows, le démarrage automatique standard attend l'ouverture de ta session.
- Les badges « compatible » du site sont des estimations matérielles, pas un essai de génération.
- Un dossier installé ne se déplace pas et ne se copie pas vers un autre PC.

## 11. Maintenance pour Alpine Makers

Cette partie décrit comment ce dépôt est tenu à jour. Elle ne contient aucune information sensible.

- **Source unique** : le dossier `worker_agent/` du projet Dashboard. Ce dépôt n'est jamais modifié à la main : il est
  **généré** depuis cette source par le script `sync-workers`.
- **Préparer une mise à jour** : modifier la source, augmenter le numéro de version de l'agent, ajouter l'entrée
  correspondante à l'historique des versions du Dashboard, tester.
- **Générer le paquet** : le script fabrique le paquet avec le code même du Dashboard, donc ce dépôt contient
  exactement ce que le site distribue.
- **Détecter un changement** : empreinte SHA-256 de la liste triée « chemin + SHA-256 du contenu » de tous les
  fichiers du paquet, comparée à `VERSION.json`. Les dates des fichiers ne comptent pas.
- **Synchroniser** : `sync-workers.bat` (ou `sync-workers.ps1`). Si l'empreinte est identique : rien. Sinon : la
  version publique actuelle est d'abord archivée dans le dépôt privé de sauvegarde et vérifiée, puis la nouvelle
  version est publiée ici, `UPDATE.md` est complété, et l'envoi est vérifié.
- **Vérifier Git** : `git status`, `git remote -v`, `git branch -vv` dans le dossier local du dépôt ; `sync-workers.ps1 -Check` montre l'état sans rien écrire.
- **Consulter une ancienne version** : dépôt privé de sauvegarde, branche `backup`, dossier `Backup/<date> (v<version>)/files/`.
- **Revenir à une ancienne version** : `restore-worker.ps1`, qui liste les versions, extrait celle choisie dans un
  dossier à part et ne republie qu'après une confirmation explicite.
- **UPDATE.md** : écrit par le script à partir de l'historique des versions du Dashboard ; aucune entrée si le paquet n'a pas changé.
