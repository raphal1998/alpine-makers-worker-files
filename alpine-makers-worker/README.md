# Worker Alpine Makers — mode d'emploi complet

## Copie locale de test conformité (non déployée)

Cette copie ajoute un précontrôle avant installation/mise à jour/réparation,
et avant un nouveau démarrage/calcul des composants soumis à restrictions.
Les contrôles d'arrêt, désinstallation et nettoyage ne dépendent pas d'un accord
juridique. ComfyUI/Orca déjà installés ne reçoivent pas de contrôle réseau global
au démarrage. Les téléchargements et chemins techniques ne sont pas réécrits.

Les fichiers `licenses/<identifiant>/NOTICE.txt` et `provenance.json`
indexent l'éditeur et les liens officiels. Le sous-dossier `evidence/` conserve
les textes documentaires embarqués et vérifiés par SHA-256, avec un index daté
de version. Aucune URL n'est téléchargée pour cette copie locale et les anciens
textes ne sont pas supprimés. Ce dossier ne remplace PAS les sources
correspondantes requises ou les autorisations écrites ; les textes amont non
épinglés à la version installée restent explicitement signalés comme tels.
Les fichiers LICENSE/NOTICE téléchargés avec les dépôts/archives sont conservés.
Le registre `legal_policy.json` doit être livré avec la même version que celui
du serveur ; une politique incompatible exige une mise à jour du Worker.

Le Worker TEST indépendant utilise `local_sandbox: true` dans
son `config.json`. Son lanceur fournit aussi `ALPINE_LOCAL_SANDBOX=1`,
`ALPINE_SANDBOX_WORKER_ROOT` (chemin exact de CE Worker) et
`ALPINE_TEST_DASHBOARD_PORT=5157`. La seule adresse dashboard admise est
`http://127.0.0.1:5157`. Il n'adopte ni moteurs ni caches externes ; les ports
ComfyUI/Hunyuan/Orca réservés sont 28188/28189/25064. PrintGuard utilise 28000,
et son MediaMTX une configuration privée sur 29997/28554/21935/28888 ; un port
occupé ou une configuration non reconnue refuse seulement ce démarrage.
Les calculs et commandes matérielles restent disponibles après les contrôles
normaux : ce mode n'est PAS un simulateur. CUDA/FreeCAD peuvent être installés
explicitement, mais leur installation système modifie le PC entier et n'est
pas confinée dans ce dossier. Aucun moteur lourd, installateur système,
impression ni génération réelle n'a été lancé pour valider cette copie.

Ne lance pas l'installateur Windows ni l'autostart standards pour préparer
ce test : ils sont destinés à l'emplacement Worker normal. Utiliser exclusivement
le lanceur isolé fourni avec la dashboard TEST. Ne pas recopier ce prototype sur
une installation en service sans validation de l'intégration et des droits.

> **Ne partage jamais ce dossier ni `config.json`.** Un dossier de Worker déjà associé
> contient l'identité du PC (`config.json` + `config\identity\`). Celui qui le possède
> peut se faire passer pour ton PC auprès du site. On ne le copie pas sur un autre PC,
> on ne l'envoie pas par e-mail, on ne le met pas dans un nuage.

**Tu as un problème, ou tu veux juste savoir si tout va bien ?** Double-clique sur
**`MENU-WORKER.bat`**, à la racine de ce dossier. C'est le menu qui regroupe tous les
outils, avec pour chacun une explication avant de lancer quoi que ce soit. Commence
toujours par l'entrée **1 · État complet du Worker** : elle ne modifie rien et te dit
quel outil utiliser ensuite.

La **version** installée n'est volontairement écrite nulle part dans ce document : lis-la
dans l'en-tête du menu, ou dans le fichier `agent_manifest.json` (ligne `agent_version`).

Sommaire : [1 Ce qu'est le Worker](#1-ce-quest-le-worker) ·
[2 Carte du dossier](#2-carte-du-dossier) ·
[3 Première installation](#3-première-installation) ·
[4 Comment le Worker démarre](#4-comment-le-worker-démarre) ·
[5 Le menu d'outils](#5-le-menu-doutils) ·
[6 Au quotidien depuis le site](#6-utilisation-quotidienne-depuis-le-site) ·
[7 Identité et sécurité](#7-identité-et-sécurité-expliquées-simplement) ·
[8 Matériel, GPU, modèles](#8-matériel-gpu-et-modèles) ·
[9 Mise à jour et maintenance](#9-mise-à-jour-et-maintenance) ·
[10 Problèmes connus](#10-problèmes-connus) ·
[11 Linux](#11-linux) ·
[12 Glossaire](#12-glossaire) ·
[13 Historique technique](#13-où-trouver-lhistorique-technique)

### Les cinq règles à ne jamais oublier

1. **Jamais** copier `config.json` ni le dossier du Worker d'un PC à un autre.
2. **Jamais** supprimer `runtime\worker-journal.sqlite3` pour « débloquer » quelque chose.
   Ce journal est la mémoire du Worker (jobs, reçus, intentions de démarrage des moteurs).
   L'effacer ne débloque rien proprement et peut faire perdre des résultats.
3. **Aucune commande matérielle pour tester** : pas d'impression, pas de déplacement de
   tête, pas de chauffe « pour voir si ça marche ». Un test se fait par l'état ou par une
   simulation.
4. **Attendre la fin des jobs** (calculs, installations) avant toute maintenance :
   mise à jour, redémarrage, extinction, nettoyage, réassociation.
5. **La sauvegarde du Worker contient son identité** : elle se garde sur ce PC ou sur un
   support à toi, elle ne se partage jamais.

---

## 1. Ce qu'est le Worker

- Le Worker est un petit programme (`agent.py`) qui prête la puissance de ce PC au site
  Alpine Makers : génération d'images (ComfyUI), modèles 3D (Hunyuan3D), conversion
  (FreeCAD), tranchage (OrcaSlicer, « Alpine Model Studio »), surveillance d'impression
  (PrintGuard), et relais vers les imprimantes et caméras **de son réseau local**.
- C'est **lui** qui appelle le site, en **HTTPS sortant** uniquement. Il n'ouvre **aucun
  port** vers Internet, n'expose aucun terminal distant, et tu n'as rien à régler sur ta box.
- Il envoie un signal de présence (**heartbeat**) toutes les **20 secondes**. Sans signal
  pendant **75 secondes**, le site l'affiche **hors ligne** et ne lui confie plus rien.
- Le site ne peut lui demander qu'une **liste fermée de commandes** connues (démarrer un
  moteur, installer un modèle, lancer un job…). Il n'existe ni commande libre, ni ligne de
  commande envoyée par le site, ni téléchargement depuis une adresse arbitraire.
- Chaque requête est **signée** par une clé propre à ce PC (voir la section 7). Ni
  l'adresse IP ni le nom du PC ne servent d'identité.

---

## 2. Carte du dossier

Un seul emplacement d'installation est prévu : **le dossier `alpine-makers-worker` du
Bureau** (`C:\Users\<ton compte>\Desktop\alpine-makers-worker`). Les anciens emplacements
`C:\ProgramData\AlpineMakersWorker` et `C:\Users\Public\AlpineMakersWorker` sont
**historiques** : si tu en croises encore un, c'est un reste d'une vieille installation
(voir « tâche planifiée orpheline », section 10).

### Ce que contient le dossier

| Dossier ou fichier | Ce qu'il contient | À ne jamais déplacer ? |
| --- | --- | --- |
| `components\` | Les moteurs installés : `comfyui`, `hunyuan3d`, `orca`, `orca-bridge`, `printguard`, `printer-link`, et les marqueurs `freecad`, `cuda-toolkit`. Environnements Python et **modèles** compris. C'est l'essentiel du poids (des dizaines à des centaines de Gio). | **Jamais.** Un environnement Python déplacé ne fonctionne plus. |
| `cache\` | Caches Hugging Face, Torch, pip, CUDA, compilation. Évite de retélécharger. | Jamais. |
| `data\` | Données durables des outils (réglages PrintGuard, etc.). | Jamais. |
| `jobs\` | Un sous-dossier par travail reçu : entrées, résultats, état de reprise. | Jamais. |
| `outputs\` | Résultats produits par les moteurs (géométries, textures…). | Jamais. |
| `workspace\` | Fichiers de travail 3D et de tranchage, reçus Orca (`_worker_jobs`). | Jamais. |
| `runtime\` | État vivant : `worker-journal.sqlite3` (journal durable), une fiche `<outil>.json` par moteur lancé, `local-control\` (boîte aux lettres des outils locaux), `identity-deps\`, éventuel `python-3.12\` privé. | **Jamais, et ne rien y supprimer.** |
| `config\` | Secrets locaux : `identity\` (clé privée du PC, chiffrée), jetons internes des moteurs. | **Jamais. Ne pas ouvrir, ne pas copier.** |
| `logs\` | Journaux des moteurs (`image_generation.log`, `ai3d.log`, `printguard.log`, `model-studio.log`) et journal des outils du menu (`outils-worker.log`). | Jamais (tu peux les lire). |
| `temp\` | Fichiers temporaires des installations et mises à jour. | Jamais (le nettoyage léger du menu sait quoi y retirer). |
| `storage.json` | Réglages des emplacements de cache et de temporaires. | Jamais. |
| `config.json` | Adresse du site, identifiant du Worker, nom, réglages. **Secret.** | **Jamais. Ne pas partager.** |
| `python_path.txt`, `git_path.txt` | Chemin du Python et du Git utilisés par l'agent. | Jamais. |
| `supervisor.lock` | Verrou qui empêche deux superviseurs dans le même dossier. | Jamais. |
| `agent_manifest.json` | Liste et empreintes des fichiers livrés par la dernière mise à jour, et version. | Jamais. |
| Scripts de la racine (`agent.py`, `run_worker.ps1`, `worker_tools.ps1`, `local_control.py`, `install_windows.*`, `configure_autostart.ps1`, `free_resources.ps1`, `orca_core_bridge.ps1`, les `.bat` historiques…) | Le programme lui-même. | **On ne les déplace pas et on ne les renomme pas** : ils sont référencés par le manifeste, par le service Windows ou la tâche planifiée, et par la mise à jour. |
| `runners\`, `installers\`, `ai3d_backends\` | Modules du programme (exécution des jobs, installateurs, adaptateurs 3D). | Jamais. |
| `MENU-WORKER.bat` | Le menu des outils. | Reste à la racine. |
| `outils\` | **Tous les nouveaux outils** du menu : un script `.ps1` et son lanceur `.bat` par outil, plus le socle commun `commun.ps1`. | Reste tel quel. |

Pourquoi ne pas avoir « rangé » la racine dans des sous-dossiers ? Parce que chaque script
de la racine est appelé par son chemin exact (service, tâche planifiée, manifeste de mise à
jour, autres scripts). Les déplacer casserait le démarrage ou la mise à jour. Le rangement a
donc été fait **sans rien déplacer** : les nouveautés vont dans `outils\`, et ce tableau sert
de plan.

### Fichiers livrés par le paquet, fichiers ajoutés à la main

| Fichier | Origine | Ce que « Mettre à jour » en fait |
| --- | --- | --- |
| Programme, `README.md`, `MENU-WORKER.bat`, `outils\*` | Livrés par le paquet (ils figurent dans `agent_manifest.json`). | **Remplacés** par la nouvelle version. Un fichier que l'ancien paquet livrait et que le nouveau ne livre plus est **supprimé**. |
| `config.json`, `storage.json`, `components`, `cache`, `temp`, `runtime`, `logs`, `data`, `outputs`, `workspace`, `jobs`, `config`, `python_path.txt`, `git_path.txt`, `supervisor.lock` | État local de ce PC. | **Toujours préservés**, jamais touchés. |
| `CLAUDE.md` (consignes pour l'assistant Claude sur ce PC) | Ajouté à la main. | **Jamais touché** : seuls les fichiers listés par l'ancien manifeste peuvent être retirés. |

---

## 3. Première installation

### Windows

Ce qu'il te faut : Windows 10 ou 11, une carte **NVIDIA** visible par `nvidia-smi` (pilote
installé), ton **compte Windows habituel** (celui qui utilisera le Worker ensuite — c'est
important, voir la section 7), et un accès au site.

1. Sur le site, **Mes Workers → Associer un PC** : tu obtiens un **code d'association**.
   Il est valable **10 minutes**, utilisable **une seule fois**.
2. Télécharge le paquet Worker depuis le site et extrais-le où tu veux (par exemple dans
   Téléchargements). Ce dossier extrait n'est **pas** l'installation : c'est le paquet.
3. Dans le dossier extrait, lance `install_windows.bat` (double-clic, ou depuis un terminal) :

   ```powershell
   .\install_windows.bat -ServerUrl https://dashboard.alpine-makers.ch -PairingCode 123-456
   ```

   Sans paramètres, l'installateur demande l'adresse du site et le code.
4. L'installateur (vérifié dans `install_windows.ps1`) :
   - installe **Python 3.12** et **Git** s'ils manquent (WinGet, sinon le site officiel) ;
   - crée `Desktop\alpine-makers-worker`, le **réserve** à ton compte, à SYSTEM et aux
     administrateurs, y copie le programme et crée l'arborescence ;
   - prépare la clé d'identité, associe le PC avec ton code ;
   - te demande **O/N** pour le démarrage automatique à l'ouverture de session. Cette
     question est posée **dès le début**, à chaque lancement de l'installateur, y compris
     par « Récupérer une inscription » (entrée 7 du menu).
5. **Aucun moteur ni modèle n'est téléchargé** à ce stade. Tu les choisis ensuite sur le
   site, dans **Mes Workers → Installations**.

Utilise bien le `.bat` : il autorise ce seul lancement de PowerShell sans changer la
politique d'exécution de Windows. Les droits administrateur ne sont pas obligatoires.

> **Sur le PC d'atelier (service `AlpineWorker`), réponds toujours N** à la question du
> démarrage automatique. Le service s'en charge déjà. Répondre **O** crée la tâche planifiée
> « Alpine Makers Worker <code> » **et lance tout de suite un second superviseur** dans ta
> session (`configure_autostart.ps1` : `Register-ScheduledTask` puis `Start-ScheduledTask`) :
> deux lanceurs pour le même dossier. Si c'est déjà fait : entrée **15** du menu, « retirer
> le doublon ». Pour ne pas avoir la question du tout, ajoute `-AutoStart No` :
> `.\install_windows.bat -AutoStart No` ou `outils\recuperer_inscription.ps1 -AutoStart No`.
> Répondre N ne touche **jamais** au service : cela ne retire que les tâches planifiées qui
> visent **ce** dossier.

> **Si le dossier contient déjà une identité**, l'installateur ne consomme pas ton code :
> il affiche « Identite existante conservee : aucune nouvelle association ». C'est voulu
> (il protège une inscription existante), et c'est aussi un piège : voir le cas (a) de la
> section 10.

### Linux (encart)

```bash
sudo ./install_linux.sh https://dashboard.alpine-makers.ch 123-456 "Mon PC GPU"
```

Installation dans `/opt/alpine-makers-worker`, service systemd `alpine-makers-worker`.
Détails et variante sans carte graphique : section 11.

---

## 4. Comment le Worker démarre

### Deux façons d'être lancé

| Mode | Où | Comment ça marche |
| --- | --- | --- |
| **Service Windows** `AlpineWorker` (outil `nssm`) | Le **PC d'atelier**. | Le service démarre **avec Windows, sans ouverture de session**, sous ton compte Windows (il faut ce compte pour lire la clé d'identité). Un second service, `AlpineSupervisor`, vérifie toutes les 30 secondes que les sources Alpine Makers en démarrage **Automatique** tournent, et relance celles qui sont arrêtées. Une source passée en démarrage **Manuel** est considérée comme **arrêtée volontairement** et n'est plus relancée. |
| **Tâche planifiée** `Alpine Makers Worker <code>` | Les autres PC (installation standard). | Créée par `configure_autostart.ps1 -AutoStart Yes`. Elle se déclenche **à l'ouverture de ta session Windows** (pas avant). Le `<code>` est une empreinte du dossier d'installation, pour distinguer plusieurs Workers. |
| Aucun | Si tu as répondu N à l'installation. | Lancement manuel par l'entrée **5 · Reconnecter** du menu. |

Les outils du menu reconnaissent eux-mêmes le mode de ce PC et s'adaptent.

> Sur le PC d'atelier, le fichier `C:\ProgramData\AlpineMakers\pause-supervision.flag` met
> en pause la surveillance de **toutes** les sources (boutique, courrier, dashboard…).
> Aucun outil du Worker ne le crée pour arrêter le seul Worker : l'arrêt volontaire du
> Worker passe par le démarrage **Manuel** de son service (entrée 11).

### Le superviseur `run_worker.ps1`

Dans les deux modes, c'est `run_worker.ps1` qui lance l'agent et le **relance** s'il s'arrête.

- **Verrou `supervisor.lock`** : le superviseur garde ce fichier ouvert tant qu'il vit. Un
  second superviseur lancé dans le même dossier voit le verrou, affiche « Le superviseur
  Worker est deja actif » et s'arrête. **Impossible d'avoir deux Workers dans un dossier.**
- **Codes de sortie de l'agent** lus par le superviseur :

  | Code | Sens | Réaction |
  | --- | --- | --- |
  | **75** | Mise à jour ou redémarrage demandé. | Relance immédiate (3 s). |
  | **64** | **Déconnecter** demandé depuis le site. | Le superviseur s'arrête **sans relancer**. |
  | autre | Arrêt inattendu. | Relance après 3 s, puis 6, 12… jusqu'à 60 s maximum. |

- **Codes de sortie du superviseur lui-même** : **0** = un autre superviseur est déjà actif,
  ou déconnexion demandée ; **3** = Python introuvable (`python_path.txt`) ou verrou
  impossible à poser (droits du compte).
- Les outils du menu n'utilisent **jamais** 64 ni 75. Leur convention : **0** réussi,
  **1** échec, **2** refusé ou annulé par toi, **3** élévation administrateur refusée.

### L'arbre des processus

```text
service AlpineWorker (nssm)        ou        tâche planifiée
        └─ powershell.exe  run_worker.ps1        (superviseur)
              └─ python.exe  agent.py            (agent)
                    ├─ python.exe  ComfyUI       (moteur images)
                    ├─ python.exe  Hunyuan3D     (moteur 3D)
                    ├─ PrintGuard
                    └─ powershell.exe  orca_core_bridge.ps1   (bridge Orca)
```

Ces processus s'appellent `python.exe` et `powershell.exe`… **comme ceux du dashboard, de
la boutique et du courrier**. C'est pourquoi aucun outil n'arrête un processus **par son
nom** : ils sont reconnus par leur ligne de commande et leur filiation. En mode service,
ces processus vivent dans une session à part : **sans fenêtre administrateur on ne peut ni
les identifier ni les arrêter**. Les outils concernés demandent donc l'élévation.

### Les ports des moteurs (tous locaux)

| Moteur | Outil | Adresse | Remarque |
| --- | --- | --- | --- |
| ComfyUI (images) | `image_generation` | `127.0.0.1:8188` | |
| Hunyuan3D (3D) | `ai3d` | `127.0.0.1:8189` | **Pas 18080** : 18080 est le moteur 3D local du dashboard, autre programme. |
| PrintGuard | `printguard` | `0.0.0.0:8000` | |
| Bridge Orca (Model Studio) | `model-studio` | `127.0.0.1:15064` | Servi par **HTTP.sys** : le port apparaît tenu par le **PID 4 (System)**. C'est **normal**. On ne touche jamais au PID 4. |
| Relais caméra | — | `127.0.0.1:1984` | Servi **par l'agent lui-même**. Présent = l'agent vit (et un relais est configuré). |
| FreeCAD (conversion) | `converter` | aucun | Pas de processus permanent : lancé à chaque conversion. |

Aucun de ces ports n'a à être ouvert sur Internet.

---

## 5. Le menu d'outils

Lance **`MENU-WORKER.bat`** (racine du dossier). Chaque entrée affiche, **avant d'agir**,
ce qu'elle fait, ce qu'elle ne fait pas et ce qu'il lui faut. Les scripts sont dans
`outils\` ; chacun a aussi son propre `.bat` pour un lancement direct.

- **[ADMIN]** : l'outil ouvre une fenêtre administrateur (Windows te demande d'accepter).
  Si tu refuses, rien n'est modifié.
- Options des outils de **`outils\`** (entrées 1 à 4, 8 à 16) : `-Racine <dossier>`
  (autre dossier de Worker) et `-SansPause` (ne pas attendre Entrée à la fin). Ceux qui
  **modifient** quelque chose acceptent en plus `-Simulation` (montre sans rien faire) et
  `-Oui` (ne pose pas la question). Chaque `outils\<nom>.bat` relaie ces options telles
  quelles ; la ligne « Lancement direct » de chaque entrée donne les options propres à l'outil.
- **Les entrées 5, 6, 7 et 17** (`outils\reconnecter.ps1`, `actualiser_ip.ps1`,
  `recuperer_inscription.ps1`, `supprimer_moteurs.ps1`) remplacent les anciens `.bat` de la
  racine (`reconnect worker.bat`, `update worker IP.bat`, `recover existing worker.bat`,
  `uninstall AI components.bat`), **retirés du paquet en 1.21.3** : « Mettre à jour » les
  supprime de l'installation. Elles acceptent `-Racine` et `-SansPause` et appellent
  `worker_tools.ps1` (5, 6, 17) ou `install_windows.ps1 -RecoverExisting` (7). L'entrée 17 n'a
  **pas de simulation** : ses seules protections sont le mot `ROUGE` demandé par le menu, puis
  la phrase `SUPPRIMER IA` demandée par l'outil.
- Les outils qui **modifient** quelque chose écrivent une trace dans
  `logs\outils-worker.log` (les outils de lecture seule n'écrivent rien). **Jamais** de
  secret ni de code d'association dans cette trace.
- Codes de sortie des outils : `0` succès, `1` échec, `2` refusé ou annulé, `3` fenêtre
  administrateur refusée.
- Plusieurs outils parlent à l'agent en marche par une « boîte aux lettres » locale
  (`runtime\local-control\`). Si ton agent est trop ancien pour une action, l'outil te le
  dit (« mets le Worker à jour »), se rabat sur ce qu'il peut lire localement, et **ne
  plante pas**.

### DIAGNOSTIC (1 à 4) — regarder sans rien changer

#### 1 · État complet du Worker

- **Quand l'utiliser** : toujours en premier. « Est-ce que tout va bien ? », « pourquoi il
  est hors ligne ? ».
- **Ce qu'il fait** : lit la version, le mode de lancement (service / tâche / aucun), l'état
  du service et de la supervision, l'association, les ports des moteurs, la présence de
  l'agent, les jobs en cours ; **contrôle l'horloge du PC** et la **joignabilité du site** ;
  termine par un **verdict** et le **numéro de l'outil conseillé**.
- **Ce qu'il ne fait PAS** : aucune modification, aucun redémarrage, aucune lecture de secret.
- **Résultat attendu** : une liste de lignes vertes, un verdict « tout va bien » ou un
  conseil précis.
- **Si ça échoue** : « Python introuvable » → l'installation est abîmée, relance
  `install_windows.bat` depuis un paquet récent (il conserve l'identité). **Sur le PC
  d'atelier, réponds N** à sa question sur le démarrage automatique, ou lance-le avec
  `-AutoStart No` (encart de la section 3). Lignes illisibles sur les processus → normal
  sans administrateur en mode service.
- **Lancement direct** : `outils\etat_worker.bat` ; `-Json` rend l'état en une ligne JSON
  (c'est ce que l'entrée 4 met dans le rapport).

#### 2 · Jobs bloquants

- **Quand** : le site ou un outil répond « Maintenance bloquée : un job est actif ou son
  arrêt n'est pas confirmé ».
- **Fait** : lit le journal durable **en lecture seule** et liste les jobs non terminés ou
  dont l'arrêt du moteur n'est pas confirmé. Il **distingue** les jobs de l'**identité
  courante** (ceux qui bloquent vraiment) de ceux d'une **identité précédente** (restes
  d'avant une réassociation, qui ne bloquent plus avec un agent à jour).
- **Ne fait PAS** : ne modifie ni ne supprime **jamais** le journal, n'annule aucun job.
- **Résultat attendu** : « aucun job bloquant », ou la liste avec pour chacun l'outil,
  l'état et si l'arrêt du moteur est confirmé.
- **Si ça échoue / s'il y a un bloquant** : annule le job depuis le site (**Arrêter** sur
  la carte du Worker, ou **Arrêter les jobs**) et attends la confirmation. S'il s'agit d'une
  identité précédente et que ça bloque encore : **Mettre à jour** le Worker (cas (b),
  section 10).
- **Lancement direct** : `outils\jobs_bloquants.bat` ; `-Json` pour une sortie JSON.

#### 3 · Journaux

- **Quand** : un moteur ne démarre pas, une génération échoue, tu veux voir ce qui se passe.
- **Fait** : montre la **fin** d'un journal, n'affiche que les **erreurs**, **ouvre** le
  dossier `logs\`, ou **suit** un journal en direct.
- **Ne fait PAS** : ne vide et ne supprime aucun journal.
- **Résultat attendu** : les dernières lignes utiles à l'écran.
- **Si ça échoue** : journal absent = ce moteur n'a jamais été lancé sur ce PC.
- **Lancement direct** : `outils\journaux.bat -Fichier <clé> -Mode fin|erreurs|ouvrir|suivre`.
  Clés : `images`, `3d`, `printguard`, `orca`, `orca-bridge`, `outils`, `service`,
  `service-erreurs`, `supervision`. `-Liste` montre les journaux présents, `-Lignes N`
  change le nombre de lignes de « fin » (60 par défaut). « suivre » se quitte par Ctrl+C.

#### 4 · Rapport de diagnostic à partager

- **Quand** : tu demandes de l'aide et on te dit « envoie-moi l'état du Worker ».
- **Fait** : crée sur le **Bureau** un fichier `rapport-worker-<date>_<heure>.zip` : état
  complet (sortie de l'entrée 1), jobs bloquants, **300 dernières lignes** de chaque journal,
  `storage.json`, `agent_manifest.json`, la liste des fichiers de la racine (noms, tailles,
  dates : aucun contenu), le mode de lancement et la sortie de `nvidia-smi`. Toute ligne
  parlant de jeton ou de mot de passe et tout **code au format 123-456** sont masqués ; un
  dernier contrôle **abandonne le rapport** plutôt que de laisser passer un fichier interdit.
- **Ne fait PAS** : n'inclut **jamais** `config.json`, ni `config\`, ni un fichier de jeton,
  ni le journal durable. Ne modifie rien dans le dossier du Worker (il n'y écrit même pas de
  trace), n'arrête et ne démarre rien. Aucune fenêtre administrateur.
- **Résultat attendu** : « Rapport créé », le nom du zip et sa taille, en 10 à 20 secondes.
  Le fichier `LISEZ-MOI.txt` du zip en décrit le contenu : tu peux l'ouvrir pour vérifier.
- **Si ça échoue** : « Rapport non créé » suivi de la raison (Bureau non accessible, disque
  plein, masquage incomplet). Aucun zip à moitié fait n'est laissé.
- **Lancement direct** : `outils\rapport_diagnostic.bat` ; `-Dossier "D:\Rapports"` pour
  une autre destination (elle doit être **hors** du dossier du Worker).

### CONNEXION (5 à 8) — le Worker n'apparaît plus en ligne

#### 5 · Reconnecter

- **Quand** : Worker hors ligne alors que le PC est allumé ; après un **Déconnecter** du
  site ; après une coupure réseau.
- **Fait** : efface le marqueur de déconnexion `.worker-disconnected`, lance le superviseur
  s'il est absent (le verrou empêche tout doublon), demande à l'agent un signal de présence
  et un inventaire. C'est l'ancien `reconnect worker.bat`.
- **Ne fait PAS** : **ne crée jamais une identité**, ne consomme aucun code, ne rétablit pas
  un Worker **révoqué** ou dont la **fiche est supprimée**.
- **ATTENTION, mode service (PC d'atelier)** : « lance le superviseur s'il est absent » veut
  dire qu'il démarre `run_worker.ps1` **dans ta session**, sans condition
  (`worker_tools.ps1`). Si le service `AlpineWorker` est **arrêté** (par exemple après
  l'entrée 11), cette entrée **rallume donc le Worker hors service**. Le service, lui, ne
  pourra plus démarrer : son superviseur trouve le verrou pris, sort aussitôt, et le service
  finit en « Paused » (section 10). **Service arrêté → entrée 12, jamais celle-ci.** Quand
  le service tourne, aucun risque : le verrou écarte le doublon.
- **Résultat attendu** : `ok: true` en local, puis le Worker repasse en ligne sur le site
  en moins d'une minute. `ok: true` veut dire « demande prise en compte », pas « accepté
  par le site ».
- **Si ça échoue** : toujours hors ligne → entrée 1 (horloge ? site joignable ?). Identité
  refusée → entrée 7 si la fiche existe encore, entrée 8 si elle a été supprimée.
- **Lancement direct** : `outils\reconnecter.bat` (ou `.ps1`) ; options `-Racine`, `-SansPause`.

#### 6 · Actualiser les adresses IP

- **Quand** : changement de Wi-Fi, de câble, de box, d'adresse du PC.
- **Fait** : redétecte les adresses IPv4 du PC et fait renvoyer l'inventaire au site
  (ancien `update worker IP.bat`).
- **Ne fait PAS** : ne **fixe** aucune adresse, ne touche ni à Windows, ni au DHCP, ni au
  pare-feu, ni aux adresses des imprimantes. L'adresse IP **n'est pas** l'identité.
- **ATTENTION, mode service** : comme l'entrée 5, elle démarre un superviseur **dans ta
  session** si aucun ne tourne. Si le service `AlpineWorker` est arrêté, fais d'abord
  l'entrée **12**, puis celle-ci.
- **Résultat attendu** : la liste des IPv4 détectées.
- **Si ça échoue** : liste vide ou `network_unavailable` → vérifie d'abord la connexion du PC.
- **Lancement direct** : `outils\actualiser_ip.bat` (ou `.ps1`) ; options `-Racine`, `-SansPause`.

#### 7 · Récupérer une inscription existante

- **Quand** : la **fiche du Worker existe encore** dans Mes Workers, mais le PC n'est plus
  accepté (Worker **révoqué**, identité supprimée par un administrateur, `config.json`
  perdu, clé refusée).
- **Fait** : lance l'assistant de récupération (ancien `recover existing worker.bat`, qui
  est `install_windows.ps1 -RecoverExisting`). Il te pose **d'abord** la question **O/N** du
  démarrage automatique, puis demande le chemin du Worker à récupérer. Ensuite il
  répare les fichiers du programme, vérifie l'identité auprès du site et, si nécessaire, te
  fait saisir un **code de MIGRATION** (saisie masquée). Le `worker_id`, les équipements,
  les moteurs et les modèles sont **conservés**.
- **Ne fait PAS** : ne crée pas de nouveau Worker ; **refuse `-PairingCode`** (un code
  d'association n'est **pas** un code de migration) ; ne contourne pas une suppression de
  fiche (marqueur `.worker-detached`).
- **ATTENTION, PC d'atelier (service `AlpineWorker`)** : à la question du démarrage
  automatique, **réponds N**. Le service s'en charge ; répondre O crée une tâche planifiée
  en double **et lance un second superviseur** tout de suite (à retirer par l'entrée 15).
  Si la récupération échoue, le choix O/N n'est pas appliqué.
- **Résultat attendu** : « Identite existante verifiee », puis retour en ligne. Après un
  code de migration, un administrateur du site doit redonner l'admission au partage.
- **Si ça échoue** : « Ce Worker a ete supprime du site » → entrée 8. Clé illisible →
  tu n'es pas sous le compte Windows d'origine (section 7). Code expiré ou déjà utilisé →
  redemande un code de migration pour **la même clé**.
- **Lancement direct** : `outils\recuperer_inscription.bat` (ou `.ps1`) ; options `-Racine`,
  `-SansPause`, `-AutoStart No` (pas de question O/N ; choisi tout seul quand le service Windows
  gère ce dossier). Toute autre option est transmise à l'installateur, dont `-InstallDir "<dossier>"`.

#### 8 · Réassocier après suppression de la fiche [ADMIN]

- **Quand** : la fiche du Worker a été **supprimée** du site (souvent après une
  révocation), et `install_windows.bat` répond « Identite existante conservee : aucune
  nouvelle association ». C'est le cas réel (a) de la section 10.
- **Fait** : te demande de confirmer que la fiche a **bien disparu**, puis de taper
  **`REASSOCIER`**, puis l'adresse du site et le nom du Worker (Entrée = valeurs actuelles).
  En mode service, il ouvre la fenêtre administrateur et **c'est dans cette fenêtre** que le
  **code d'association** est demandé (crée-le juste avant : **Mes Workers →
  Associer un PC** ; le message de l'outil dit « Ajouter un Worker », c'est le même bouton).
  Il passe le service en **Manuel** le temps de l'opération (sinon la supervision le relancerait au milieu),
  l'arrête, exécute `agent.py --pair`, puis **dans tous les cas, même en échec**, remet le
  service en **Automatique** et le redémarre. Les moteurs, modèles, réglages et travaux
  restent en place.
- **Ne fait PAS** : ne réutilise pas l'ancien `worker_id` (tu obtiens une **nouvelle
  fiche**) ; ne rétablit ni les équipements ni les partages (à refaire sur le site) ;
  n'affiche et ne journalise **jamais** le code saisi. Ne sert **pas** si la fiche existe
  encore : tu créerais un doublon (→ entrée 7).
- **Résultat attendu** : « WORKER RÉASSOCIÉ », la ligne « Identifiant du Worker : ancien →
  nouveau » (début de l'identifiant seulement), service « Running, démarrage Auto », puis
  la liste de ce qu'il te reste à refaire sur le site (outils listés, équipements, partages).
- **Si ça échoue** : l'**ancienne configuration reste en place** et le Worker est relancé.
  Code de plus de 10 minutes ou déjà utilisé → crée-en un nouveau (20 par jour au plus) et
  relance l'outil. Élévation refusée (code 3) → relance et accepte la fenêtre Windows.
  « Le service tourne sous un autre compte » → ouvre la session Windows **de ce compte** :
  l'identité est chiffrée pour lui. Service non revenu « Running / Auto » → entrée 12.
- **Hors PC d'atelier (mode tâche ou lancement manuel)** : pas de fenêtre administrateur en
  temps normal, et l'outil **refuse** de tourner depuis une fenêtre administrateur (le
  Worker relancé aurait des droits administrateur). Il arrête le superviseur par ses PID
  reconnus, associe, puis relance `run_worker.ps1` en arrière-plan.
- **Lancement direct** : `outils\reassocier_worker.bat` ; `-Simulation` déroule tout sans
  rien arrêter ni associer ; `-ServeurUrl`, `-Nom` pour préremplir.

### MOTEURS ET WORKER (9 à 12) — arrêter, relancer, libérer la carte graphique

#### 9 · Moteurs : état / arrêter / démarrer

- **Quand** : libérer la carte graphique pour autre chose sans éteindre le Worker ; relancer
  les moteurs ensuite ; voir ce qui tourne.
- **Fait** : demande à **l'agent** d'arrêter PrintGuard, le bridge Orca, Hunyuan3D et
  ComfyUI. C'est un arrêt **PROPRE** : l'agent enregistre l'intention « arrêté », arrête le
  moteur et **vérifie**. « Démarrer » relance ceux qui tournaient avant (sinon ceux qui sont
  installés et prêts) ; l'avancement se lit ensuite dans « état ». **Sans administrateur.**
- **Ne fait PAS** : ne tue aucun processus à la main ; **refuse** s'il y a un job actif ou
  une maintenance en cours ; ne lance **aucune génération**.
- **Résultat attendu** : la liste des moteurs arrêtés, puis les ports fermés.
- **Si ça échoue** : « agent trop ancien » → **Mettre à jour** le Worker depuis le site,
  puis réessaie ; en attendant, utilise les boutons Démarrer/Arrêter du site. Refus pour job
  actif → entrée 2.
- **Lancement direct** : `outils\moteurs.bat -Action etat|arreter|demarrer` (sans `-Action`,
  l'outil propose le choix) ; `-Simulation` et `-Oui` pour `arreter` et `demarrer`.

> Pourquoi c'est important : un arrêt **brutal** d'un moteur (fin de tâche dans le
> Gestionnaire des tâches) laisse l'intention « allumé » dans le journal, et l'agent le
> **relance 30 secondes** après son prochain démarrage. Seul l'arrêt propre tient.

#### 10 · Redémarrer l'agent [ADMIN]

- **Quand** : l'agent semble figé, ou après une réparation. Équivalent local du bouton
  **Redémarrer** du site quand le site ne peut plus le joindre.
- **Fait** : vérifie qu'aucun travail ni maintenance n'est en cours, redémarre le service
  `AlpineWorker` (`Restart-Service`, attente 45 s), arrête par PID les processus de
  l'ancienne instance qui auraient survécu, puis attend l'agent **90 secondes au plus** et
  affiche son état. En mode tâche ou manuel : il arrête l'arbre du superviseur et le relance
  **sans** droits administrateur (la fenêtre administrateur n'est demandée que si les
  processus sont illisibles).
- **Ne fait PAS** : ne touche ni à l'identité, ni à l'association, ni au mode de démarrage,
  ni à un autre service du PC. N'éteint pas durablement les moteurs : ceux qui étaient en
  marche **repartent seuls** environ 30 secondes après l'agent. **Refuse** si le service est
  en démarrage Manuel (Worker éteint volontairement → entrée 12), s'il y a un travail en
  cours ou une maintenance.
- **Résultat attendu** : « Service AlpineWorker redémarré », « L'agent répond », Worker en
  ligne sous une minute ; un gros modèle met plusieurs minutes à revenir (suivi : entrée 9).
- **Si ça échoue** : service en état « Paused » → un superviseur lancé hors service tient le
  verrou (voir section 10) ; entrée 11, puis entrée 12. « Aucun signe de l'agent en 90
  secondes » → entrée 1.
- **Lancement direct** : `outils\redemarrer_agent.bat` ; `-Simulation`, `-Oui` ; `-Forcer`
  passe outre un travail en cours (il sera **perdu**, mot `FORCER` exigé) ou une maintenance
  bloquée.

#### 11 · Éteindre le Worker, lui seul [ADMIN]

- **Quand** : tu veux le PC pour toi (jeu, montage…) **sans** couper la boutique, le
  dashboard ni le courrier.
- **Fait** : arrête les moteurs **proprement**, arrête l'agent et son superviseur, passe le
  service `AlpineWorker` en démarrage **Manuel** pour que la supervision ne le relance pas
  sous 30 secondes, puis **vérifie que les ports** 8188, 8189, 8000, 15064 et 1984 sont libres.
- **Ne fait PAS** : ne touche à **aucun autre** service Alpine Makers ; ne crée **pas** le
  fichier de pause de la supervision ; n'arrête rien par nom de processus ; ne désinstalle rien.
- **Résultat attendu** : « Worker éteint », tous les ports fermés, service en Manuel.
  Le site affichera le Worker hors ligne après 75 secondes.
- **Si ça échoue** : refus pour job actif → attends ou annule le job (entrée 2). Un port
  reste ouvert → l'outil nomme le processus ; relance l'outil en acceptant l'élévation.
- **À savoir** : le **service** ne repart pas seul, même après un redémarrage du PC.
  **ATTENTION** : les entrées **5**, **6** et **17** (les anciens `.bat`) relancent un
  superviseur **dans ta session** si aucun ne tourne : elles rallumeraient le Worker hors
  service, et l'entrée 12 trouverait ensuite le verrou pris (service « Paused », section 10).
  **Après l'entrée 11, n'utilise que l'entrée 12 pour rallumer.**
- **Lancement direct** : `outils\eteindre_worker.bat` ; `-Simulation`, `-Oui` ; `-Forcer`
  passe outre un travail en cours (il sera **perdu**, mot `FORCER` exigé).

#### 12 · Rallumer le Worker [ADMIN]

- **Quand** : après l'entrée 11.
- **Fait** : remet le service en démarrage **Automatique**, le démarre, et redemande le
  démarrage des **moteurs qui tournaient** au moment de l'extinction.
- **Ne fait PAS** : n'installe rien, ne change pas l'identité.
- **Résultat attendu** : service « Running / Auto », Worker en ligne, moteurs qui reviennent
  en quelques minutes (le chargement d'un moteur est long, c'est normal).
- **Si ça échoue** : entrée 1 pour le verdict ; moteurs absents → entrée 9 « démarrer ».
  Service « Paused » juste après → un superviseur tourne déjà dans ta session (entrée 5, 6
  ou 17 utilisée entre-temps) : entrée 11, puis de nouveau entrée 12.
- **Lancement direct** : `outils\rallumer_worker.bat` ; `-Simulation`, `-Oui` ;
  `-SansMoteurs` rallume l'agent **sans** redemander le démarrage des moteurs.

### ENTRETIEN (13 à 16) — sauvegarde, ménage, démarrage du PC

#### 13 · Sauvegarder le Worker

- **Quand** : avant une opération risquée (réassociation, réparation), et de temps en temps.
- **Fait** : crée une **archive locale** du programme, de la configuration, de l'identité et
  du journal (copie cohérente), **SANS les moteurs** ni les modèles (trop lourds, et
  réinstallables).
- **Ne fait PAS** : ne sauvegarde pas `components\`, `cache\`, `jobs\`, `outputs\` ; n'envoie
  rien nulle part.
- **Résultat attendu** : le chemin de l'archive `worker-<date>_<heure>.zip` et sa taille.
  Par défaut elle va dans `C:\ProgramData\AlpineMakers\sauvegardes-worker` (repli : le même
  dossier sous `%LOCALAPPDATA%`), jamais dans le dossier du Worker.
- **Si ça échoue** : disque plein ou dossier de destination protégé.
- **Lancement direct** : `outils\sauvegarder_worker.bat` ; `-Lister` montre les sauvegardes
  présentes **et la marche à suivre pour restaurer à la main** ; `-Destination "D:\..."`
  choisit un autre dossier ; `-Simulation`, `-Oui`.
- **ATTENTION** : cette archive **contient l'identité du PC**. Elle ne se partage **jamais**
  et ne se restaure **que sur ce PC, sous ce compte Windows** (la clé est liée au compte).

#### 14 · Nettoyage léger

- **Quand** : le disque se remplit, après beaucoup d'installations.
- **Fait** : **depuis le menu, uniquement une simulation.** Il liste les petits fichiers
  temporaires de **plus de 7 jours** qu'il pourrait retirer : réponses de la boîte aux
  lettres locale (`runtime\local-control\*.result.json`), restes d'installation
  (`temp\pip-*`), traces Orca / FreeCAD de `workspace\`. **Rien n'est supprimé.**
- **Pour supprimer réellement** : lance toi-même `outils\nettoyage_leger.bat -Appliquer`.
  L'outil refait la liste, vérifie qu'aucun travail ni maintenance n'est en cours, puis te
  demande confirmation. `-Jours N` change l'âge minimal (7 au minimum) ; `-Simulation`
  l'emporte sur `-Appliquer`. Le menu ne propose **pas** cette suppression (le message de
  l'outil qui parle d'une entrée « Appliquer le nettoyage » est en avance sur le menu).
- **Ne fait PAS** : ne touche ni aux moteurs, ni aux modèles, ni aux caches de modèles, ni
  aux jobs, ni aux résultats, ni au journal, ni à la configuration. Ne suit aucun raccourci
  ni jonction. Ne libère **pas** des dizaines de Gio : pour cela, **Installations** sur le
  site ou l'entrée 17.
- **Résultat attendu** : depuis le menu, le nombre d'éléments et le volume **libérable**.
  Avec `-Appliquer` et ta confirmation : le volume **libéré**.
- **Si ça échoue** : fichier verrouillé = une installation est en cours ; réessaie plus tard.

#### 15 · Démarrage automatique et tâches orphelines

- **Quand** : le Worker ne repart pas après un redémarrage du PC ; une erreur apparaît à
  chaque ouverture de session ; tu veux changer le choix O/N de l'installation.
- **Fait** : montre le mode (service / tâche / aucun), l'état et le démarrage du service,
  le dossier qu'il vise, et toutes les tâches « Alpine Makers Worker… ». Il propose ensuite,
  selon le cas : de retirer les **tâches orphelines** — celles qui visent un dossier
  **disparu** (cas (j), section 10) ; en mode service, de retirer la **tâche en doublon**
  qui vise aussi ce dossier (celle que crée un « O » à l'installateur ; mot `RETIRER`
  exigé) ; et, si **rien** ne lance ce Worker, d'**activer** la tâche planifiée de
  l'installateur — ce qui **lance aussi le Worker tout de suite**.
- **Ne fait PAS** : ne touche pas aux tâches d'un **autre** Worker valide ni aux autres
  tâches du PC ; n'arrête et ne redémarre pas le Worker ; **ne modifie pas le service**
  (Manuel / Automatique : entrées 11 et 12) ; refuse d'activer une tâche quand le service
  lance déjà ce dossier. Il ne **désactive** pas non plus la tâche d'un Worker en mode
  tâche : il t'indique la commande (`configure_autostart.ps1 -AutoStart No`).
- **Résultat attendu** : une seule source de démarrage pour ce dossier, aucune orpheline.
- **Si ça échoue** : Windows refuse de retirer une tâche → l'outil ouvre une fenêtre
  administrateur ; accepte-la (code 3 si tu refuses : les tâches restent en place).
- **Lancement direct** : `outils\demarrage_auto.bat` ; `-RetirerOrphelines`,
  `-RetirerDoublon`, `-ActiverTache` ; `-Simulation`, `-Oui`.

#### 16 · Libérer la mémoire du PC en gardant Alpine Makers

- **Quand** : PC lent, mémoire vive (RAM) saturée, avant une grosse génération.
- **Fait** : enveloppe expliquée de `free_resources.ps1` (le script du bouton **Libérer VRAM
  et RAM** du site). Il commence **toujours par une simulation** et te montre la liste de
  ce qui serait fermé dans **ta session** (navigateurs, lanceurs de jeux, utilitaires,
  consoles oubliées), puis demande confirmation. En option, dans une fenêtre
  administrateur : il arrête aussi des services Windows non essentiels (indexation,
  télémétrie, Xbox…). **Garde** Windows, la sécurité, le pare-feu, l'accès à distance, le
  refroidissement, Claude, et **tout Alpine Makers** : Worker et moteurs, ainsi que Docker/WSL.
- **Ne fait PAS** : ne désinstalle et ne désactive rien (un redémarrage du PC remet tout) ;
  n'arrête pas les moteurs et **ne libère presque pas la VRAM** de la carte graphique :
  pour « VRAM libre insuffisante », c'est l'entrée **9** (arrêter les moteurs).
- **Résultat attendu** : mémoire avant / après, nombre de programmes fermés, mémoire rendue.
- **Si ça échoue** : « free_resources.ps1 est absent » → **Mettre à jour** le Worker. « La
  simulation n'a rendu aucun rapport » → rien n'est fermé, par prudence.
- **ATTENTION** : **enregistre ton travail avant** ; les programmes listés sont fermés d'office.
- **Lancement direct** : `outils\liberer_memoire.bat` ; `-Simulation` s'arrête après la
  liste ; `-Oui` ; `-AvecServices` inclut les services Windows (fenêtre administrateur).

### ZONE ROUGE (17) — action lourde, lis bien avant de confirmer

#### 17 · Supprimer les moteurs IA installés

- **Quand** : tu veux récupérer beaucoup de place, ou repartir de zéro sur les moteurs.
- **Fait** : ancien `uninstall AI components.bat`. Affiche le Worker, le dossier, le nombre
  de fichiers, le volume et les lignes **CONSERVE** ; écrit la liste exacte dans
  `runtime\local-control\cleanup-preview.json` ; exige de taper exactement **`SUPPRIMER IA`**.
- **Ne fait PAS** : ne supprime ni le Worker, ni son identité, ni ses associations, ni les
  jobs, ni les résultats, ni les caches partagés, ni les logiciels installés dans Windows
  (FreeCAD, CUDA Toolkit), ni les dossiers `input`, `output`, `user`, `custom_nodes`. Un
  composant **sans preuve d'origine** est conservé et signalé.
- **Résultat attendu** : `removed_files`, et des listes `errors` / `skipped` à lire.
- **Si ça échoue** : refus pour job actif → entrée 2. Code 2 après 120 s = demande **encore
  en attente**, pas annulée : ne réinstalle rien, regarde le fichier `.result.json` indiqué.
- **ATTENTION** : pas de Corbeille. Il faudra **retélécharger** des dizaines de Gio.
  **Aucune simulation** et aucune option : c'est l'ancien `.bat`. Ce que tu lis avant de
  taper `SUPPRIMER IA` (et le fichier `cleanup-preview.json`) **est** l'aperçu.
- **ATTENTION, mode service** : comme les entrées 5 et 6, il démarre un superviseur dans ta
  session si aucun ne tourne. Ne l'utilise pas quand le service `AlpineWorker` est arrêté
  (entrée 12 d'abord) : c'est de toute façon l'agent en marche qui exécute la suppression.
- **Lancement direct** : `outils\supprimer_moteurs.bat` (ou `.ps1`) ; options `-Racine`, `-SansPause` ; la phrase `SUPPRIMER IA` reste demandée.

---

## 6. Utilisation quotidienne depuis le site

Tout se passe dans **Mes Workers**. Les boutons n'agissent que sur un Worker **en ligne**
(sauf mention) et appartiennent au propriétaire du Worker.

| Bouton | Ce qu'il fait | À savoir |
| --- | --- | --- |
| **Mettre à jour** | Télécharge le paquet, vérifie chaque fichier, remplace le programme, relance l'agent (code 75). | Préserve identité, moteurs, modèles, jobs. Accepté au repos ; refusé pendant un calcul réel. Le bouton **clignote** quand une version plus récente existe. Section 9. |
| **Redémarrer** | Relance l'agent. | Pas pendant un calcul. Si le site ne joint plus le PC : entrée 10 du menu. |
| **Déconnecter** | Arrête les moteurs, ferme imprimantes/caméras/relais, met le Worker hors ligne. | Dure jusqu'au prochain démarrage du PC ou jusqu'à l'entrée **5 · Reconnecter**. Refusé si un moteur ne peut pas être arrêté de façon vérifiée. |
| **Arrêter les jobs** | Annule ce que le site a lancé sur ce Worker (jobs, commandes pas encore transmises, installation en cours, contrôle des doublons). | **N'éteint rien.** Un job en cours garde sa place jusqu'à la **confirmation d'arrêt** du moteur. |
| **Arrêter** (sur un calcul) | Annule ce seul job. | Même attente de confirmation. |
| **Réparer** | Met en file, dans l'ordre : mise à jour, redémarrage, démarrage des moteurs tombés. | Les réparations **lourdes** (réinstaller un moteur, retélécharger un modèle) ne partent qu'après ta confirmation explicite. |
| **Libérer VRAM et RAM** | Fait rendre la VRAM au moteur image puis ferme les programmes non essentiels. | Même chose que l'entrée 16. Un Worker qui calcule refuse. |
| **Contrôler les doublons** | Inventorie les gros fichiers identiques dans les dossiers du Worker. | **Ne supprime rien.** Rapport JSON téléchargeable. |
| **Installations** | Installer, réparer, adapter ou désinstaller moteurs, modules et modèles. | Le Worker choisit lui-même le bon profil pour ta carte (section 8). Exige un Worker **au repos**. **Annuler l'installation en cours** existe. |
| **ON/OFF IA locales** | Met un modèle ou un module ON/OFF **sans le désinstaller** ; seul le moteur est un vrai processus. | Un modèle OFF n'est plus proposé pour ce Worker. « Tout arrêter » arrête aussi le moteur. |
| **Équipements connectés** | Imprimantes et caméras **du réseau local du Worker** : installer le relais, tester (lecture seule), connecter, utiliser. | Le Worker doit vraiment **joindre** l'imprimante (pas de Wi-Fi invité isolé). Le relais caméra écoute sur `127.0.0.1:1984` du Worker. |
| **Sécurité et autorisations** | Identifiant du Worker, état de l'identité, partage, **Migrer / remplacer l'identité**, **Révoquer**. | Section 7. |

Un job qui **attend** affiche la vraie raison (moteur pas en service, VRAM libre
insuffisante, Worker occupé, modèle OFF…) sur la page qui l'a lancé.

**Surveillance IA des graveuses (agent 1.34.0)** : l'agent analyse aussi, en local, la caméra d'une graveuse
(flammes et fumée) et peut l'arrêter lui-même, même page fermée et site indisponible. Elle se règle dans
**IA local/api → Surveillance IA (graveuse)** (module « Surveillance IA graveuse » d'Alpine Laser Studio dans
Installations). Un **Mettre à jour** ou **Redémarrer** est refusé pendant un travail laser : redémarrer l'agent
couperait la liaison série et la surveillance en pleine gravure. État local sans le site, depuis le dossier du
Worker : `& (Get-Content python_path.txt) local_control.py --root . --action fire-watch`. Mode d'emploi complet,
licences et procédure de validation : `docs/fire_watch.md` du dashboard.

**Comptes IA (agent 1.35.0)** : **Mes Workers → Comptes IA** montre et gère les comptes Claude Code (« Claude ») et
Codex (« ChatGPT ») avec lesquels ce Worker travaille (assistant de code d'Alpine Laser Studio). Le compte
**« Compte du PC »** est celui de la session Windows/Linux qui exécute l'agent : le déconnecter déconnecte aussi
Claude Code ou Codex pour cet utilisateur du PC. Un compte **ajouté** a son propre profil dans
`data\ai-accounts\<claude|codex>\<compte>\` (registre `data\ai-accounts.json`), conservé par les mises à jour.
Connexion : jeton d'abonnement (`claude setup-token` sur n'importe quel PC) ou clé API pour Claude ; code
d'appareil ou clé API pour ChatGPT ; ou navigateur ouvert sur le PC Worker. Un jeton ou une clé restent sur le
Worker (`alpine-auth.json` du compte, jamais renvoyés au site) ; « Déconnecter » les efface. Il faut que la CLI
soit installée pour l'utilisateur de l'agent (Claude Code : https://claude.com/claude-code ; Codex :
`npm i -g @openai/codex`).

Depuis **1.35.1** : « Annuler » arrête aussi une connexion qui attend encore son tour (sa CLI n'est alors jamais
lancée) ; une mise à jour, un redémarrage ou une déconnexion de l'agent n'annule les connexions qu'une fois
accepté (refusé pendant une gravure, il les laisse intactes) ; une CLI bloquée (état, déconnexion, clé API) est
arrêtée avec ses processus enfants à l'échéance, même installée par npm (`claude.cmd`).

---

## 7. Identité et sécurité, expliquées simplement

### La clé du PC

À la première association, le Worker fabrique **sa propre clé** (type **Ed25519**) dans
`config\identity\`. La partie privée **ne quitte jamais** le PC ; le site ne connaît que la
partie publique et son empreinte. Chaque requête est signée avec cette clé, avec l'heure et
un numéro à usage unique.

Sous Windows, la clé privée est chiffrée par **DPAPI au nom de ton compte Windows**.
Conséquences très concrètes :

- le Worker doit tourner **sous le compte qui l'a installé** (c'est pour ça que le service
  `AlpineWorker` tourne sous ton compte, pas sous SYSTEM) ;
- une copie du dossier sur **un autre PC** ou **un autre compte** donne une clé **illisible** ;
- si tu supprimes ton compte Windows ou réinstalles Windows, la clé est perdue : il faut
  alors une **migration** (entrée 7), pas une bidouille.

### Deux codes différents, à ne pas confondre

| | Code d'**ASSOCIATION** | Code de **MIGRATION** |
| --- | --- | --- |
| Sert à | Créer une **nouvelle fiche** de Worker. | Faire accepter une **nouvelle clé** pour une fiche **existante**. |
| Où l'obtenir | Mes Workers → **Associer un PC**. | Mes Workers → **Migrer / remplacer l'identité** sur la fiche (après avoir vérifié l'empreinte). |
| Durée / usage | **10 minutes**, **usage unique**, stocké **haché** côté site, **20 par jour** et par compte. | 10 minutes, usage unique, lié à **ce** Worker et à **cette** clé. |
| Outil local | `install_windows.bat`, ou entrée **8**. | Entrée **7** (saisie masquée). |
| Résultat | Nouvel identifiant. | **Même identifiant**, équipements et associations conservés. |

Ne colle **jamais** une clé privée dans le site, et ne transmets aucun de ces codes.

### Révoquer, supprimer l'identité, supprimer la fiche : trois choses différentes

| Action sur le site | Ce que ça fait | Ce qui reste | Voie de retour |
| --- | --- | --- | --- |
| **Révoquer** | Coupe l'accès du PC **immédiatement**, retire le partage et l'admission. | La **fiche**, l'identifiant, les moteurs sur le PC. | **Migration** : Migrer / remplacer l'identité → code de migration → entrée **7**. Même identifiant. Les associations retirées sont à remettre. |
| **Supprimer l'identité** (administrateur) | Efface la clé publique côté site ; l'accès est coupé. | La fiche, les composants. | Identique : **migration** (entrée 7). |
| **Supprimer le Worker** (Worker en ligne) | Arrête les moteurs, **efface `components\` et `runtime\` sur le PC** (modèles compris), puis retire la fiche. | Le programme, les résultats déjà produits. | Aucune vers l'ancienne fiche. **Nouvelle association** : entrée **8**, puis réinstaller les moteurs. |
| **Supprimer sans désinstallation** | Retire la fiche tout de suite, **sans toucher au PC**. | Tout le dossier local, moteurs et modèles compris. | **Nouvelle association** : entrée **8**. Les moteurs sont retrouvés. |

Retiens : **tant que la fiche existe → entrée 7. Fiche supprimée → entrée 8.**
« Reconnecter » (entrée 5) ne fait **ni l'un ni l'autre**.

### Autres protections

- **Horloge** : le site refuse toute requête dont l'heure diffère de plus de **60 secondes**
  de la sienne. Un PC mal à l'heure = **tout** est refusé (l'entrée 1 le contrôle).
- Le dossier est réservé à ton compte, à SYSTEM et aux administrateurs.
- Aucun code d'association, jeton complet ou clé privée n'est écrit dans les journaux.
- Le partage d'un Worker avec d'autres comptes exige une identité vérifiée, une bonne
  santé, l'admission par un administrateur de la plateforme et des comptes choisis
  explicitement. Partager le tien ne t'ouvre aucun droit sur ceux des autres.

---

## 8. Matériel, GPU et modèles

- **Carte NVIDIA obligatoire** pour l'installation standard, visible par `nvidia-smi`.
  AMD, Mac et ARM ne sont pas pris en charge. (Variante Linux sans carte : section 11.)
- **Le profil CUDA est choisi par le Worker, pas par toi ni par le navigateur** :

  | Carte | Profil installé |
  | --- | --- |
  | Anciennes générations — Maxwell, **Pascal (GTX 1070…)**, Volta | PyTorch 2.10.0 + **CUDA 12.6** |
  | **Turing et plus récent** (RTX 20, 30, 40, **50**) | PyTorch 2.10.0 + **CUDA 12.8** |
  | Carte ou pilote non reconnu | **Refus expliqué**, rien n'est installé au hasard |

  Pilote conseillé au minimum : 560.76 pour CUDA 12.6, 570.65 pour CUDA 12.8 (Windows).
- La ligne « CUDA » de `nvidia-smi` est le **maximum** que ton pilote accepte, pas une
  version à installer. Ne rétrograde pas ton pilote à cause de ce libellé.
- **Plusieurs cartes** : le Worker retient **la carte compatible qui a le plus de VRAM**.
  La VRAM de plusieurs cartes n'est **JAMAIS additionnée**.
- **Chaque modèle a un minimum de VRAM** (affiché dans Installations). En dessous, il est
  refusé : une carte de 8 Gio ne fera pas tourner un modèle qui en demande davantage
  (Paint, par exemple). Sous 10 000 Mio, ComfyUI passe en mode mémoire réduite.
- **Disque** : l'installation d'un moteur exige **au moins 8 Gio libres**, **hors modèles**
  (chaque modèle pèse ensuite plusieurs Gio). RAM et disque affichés sont des estimations.
- Les moteurs utilisent leur propre Python 3.12 (trouvé sur le PC ou installé en privé dans
  `runtime\python-3.12`). Le Python de l'agent n'est pas remplacé.
- **Adapter / réparer** n'apparaît que si le profil installé ne correspond pas à la carte.
  Les modèles déjà vérifiés sont réutilisés. Un ancien environnement incompatible est gardé
  sous `.venv-preserved-…` : il occupe de la place et n'est pas effacé tout seul.
- Un badge « compatible » est une **estimation**, pas un test de génération.
- FreeCAD et CUDA Toolkit sont des logiciels **Windows** installés hors du Worker (winget) ;
  le Worker n'en garde qu'un marqueur. OrcaSlicer et PrintGuard sont fournis pour Windows.

### Images : moteur, architectures et modèles (agent 1.41.0)

Trois notions distinctes : le **moteur** (ComfyUI, sur ce PC), l'**architecture** d'un modèle, et le
**modèle** lui-même (un ou plusieurs fichiers). Les six architectures de référence :

| Architecture | Identifiant | Fichiers | Dossier(s) dans `components\comfyui\models` | Modèle de référence | VRAM min. |
| --- | --- | --- | --- | --- | --- |
| Stable Diffusion 1 | `stable_diffusion_1` | 1 checkpoint | `checkpoints` | Stable Diffusion 1.5 (3,97 Go) | 6 Go |
| Stable Diffusion 2 — 512 | `stable_diffusion_2_512` | 1 checkpoint | `checkpoints` | Stable Diffusion 2.1 Base (5,21 Go) | 6 Go |
| Stable Diffusion 2 — 768 | `stable_diffusion_2_768` | 1 checkpoint (prédiction v) | `checkpoints` | Stable Diffusion 2.1 (5,21 Go) | 6 Go |
| Stable Diffusion XL | `stable_diffusion_xl` | 1 checkpoint | `checkpoints` | SDXL Base 1.0 (6,94 Go) | 10 Go |
| Stable Cascade | `stable_cascade` | 2 checkpoints (étages C et B) | `checkpoints` | Stable Cascade (13,78 Go) | 10 Go |
| FLUX.1 | `flux_1` | 1 checkpoint tout-en-un (modèle + encodeurs + VAE) | `checkpoints` | FLUX.1 [schnell] fp8 (17,24 Go) | 12 Go |

- **Rien n'est téléchargé d'office.** Un modèle s'installe depuis le site : **Mes Workers → Installations →
  Installer**. Le Worker vérifie l'espace libre, la VRAM, la licence, télécharge avec reprise dans un fichier
  temporaire, contrôle la taille et le SHA-256, puis seulement le déclare prêt. Un fichier incomplet n'est
  jamais annoncé installé ; un ensemble (Stable Cascade) est prêt quand **tous** ses fichiers sont là.
- **Stable Cascade** n'est pas un checkpoint ordinaire : l'étage C compose l'image, l'étage B la décompresse,
  puis son VAE la décode. Texte → image uniquement (pas de LoRA, de ControlNet ni d'image de départ).
  Licence non commerciale de Stability AI.
- **FLUX.1** n'est pas un checkpoint SDXL : graphe propre (guidance FLUX, latent 16 canaux). La variante
  prise en charge est le fichier tout-en-un fp8 de Comfy-Org ([schnell] Apache-2.0, [dev] non commercial).
- **Un fichier posé à la main** dans `checkpoints` est reconnu par son contenu, pas par son nom : l'agent lit
  l'en-tête `.safetensors` (aucun poids chargé) et relève son architecture. Un `.ckpt` n'est jamais ouvert.
- Le Worker n'annonce une architecture que s'il sait en valider le graphe : Stable Cascade exige un agent
  1.41.0 (« Mettre à jour » dans Mes Workers suffit, aucune réinstallation).
- **Dépannage** : « agent trop ancien pour l'architecture … » → Mettre à jour ; « VRAM libre insuffisante »
  → un autre moteur occupe la carte (menu 11 « Libérer la carte graphique ») ; modèle « incomplet » →
  relancer l'installation (la reprise continue le fichier partiel) ; image noire en SD 2 — 768 → viser
  768 × 768, cette variante n'est pas faite pour 512 px.

---

## 9. Mise à jour et maintenance

### Ce que fait « Mettre à jour »

Le site construit le paquet à partir de ses **sources** (`worker_agent\` du dashboard) :
fichiers `.py .ps1 .bat .sh .md .txt .json .cmd`, jusqu'à trois niveaux de dossiers. Le
Worker le télécharge, vérifie l'empreinte **de chaque fichier**, la version et les
capacités, remplace le programme, puis se relance (code 75). Rien n'est tiré de GitHub.

| Préservé, toujours | Remplacé | Supprimé |
| --- | --- | --- |
| `config.json`, `storage.json`, `components`, `cache`, `temp`, `runtime`, `logs`, `data`, `outputs`, `workspace`, `jobs`, `config`, `python_path.txt`, `git_path.txt`, `supervisor.lock` ; et **tout fichier que tu as ajouté à la main** | Tous les fichiers du programme, ce `README.md`, `MENU-WORKER.bat`, `outils\*` | Uniquement les fichiers que l'**ancien** paquet livrait et que le nouveau ne livre plus (liste `removed_files` dans le résultat) |

- **Les outils du menu vivent dans les sources** `worker_agent\` et **arrivent par « Mettre
  à jour »**. Modifier un outil directement dans le dossier installé ne sert à rien : la
  prochaine mise à jour l'écrase. Une amélioration se fait dans les sources.
- La mise à jour **ne change pas** le choix de démarrage automatique et **ne réapplique
  pas** les permissions du dossier sur une vieille installation.
- La nouvelle version n'est **prouvée** que par le heartbeat suivant : regarde la version
  sur la carte du Worker, ou dans le menu.
- Une installation de moteur **en cours** n'est pas interrompue par la mise à jour : attends
  sa fin ou annule-la et attends la confirmation.

### Bonnes habitudes

- Avant toute maintenance : entrée **1**, puis entrée **2** (aucun job bloquant).
- Avant une opération sur l'identité : entrée **13** (sauvegarde).
- Disque : entrée **14** d'abord ; **Contrôler les doublons** sur le site pour comprendre ;
  entrée **17** seulement en dernier recours.
- Un moteur qui tombe est relancé par l'agent **au plus 3 fois par heure**, jamais dans les
  30 secondes qui suivent la chute. Au-delà, il reste à démarrer à la main (site, ou entrée 9).

---

## 10. Problèmes connus

Les cas **(a) à (j)** sont réels (le cas (i), propre au PC du propriétaire, n'est pas repris ici) : ils ont été rencontrés et résolus le **20.09.2026**.

| # | Symptôme | Cause | Comment le voir | Résolution |
| --- | --- | --- | --- | --- |
| **(a)** | Worker révoqué, puis fiche supprimée. `install_windows.bat` avec un nouveau code répond « **Identite existante conservee : aucune nouvelle association** » et rien ne change. | Le dossier contient encore l'ancien identifiant : l'installateur le protège et **ne consomme pas** le code. | Entrée **1** : « associé », mais le site ne connaît plus ce Worker. | Entrée **8 · Réassocier** (arrête le service, puis `agent.py --pair` avec un code neuf, puis remet le service en Automatique). Ne relance **pas** `install_windows.bat` pour ça ; si tu l'as fait sur le PC d'atelier en répondant O, passe aussi par l'entrée **15**. |
| **(b)** | « **Maintenance bloquée : un job est actif ou son arrêt n'est pas confirmé** » à chaque démarrage de moteur. | Le moteur 3D est **tombé en pleine génération** : le job est resté « uncertain » avec `engine_stop_confirmed=False`. **Cause aggravante corrigée le 20.09.2026** : le garde ne filtrait pas les jobs d'une **identité précédente** ; après une réassociation, ces jobs — que le site ne connaît plus — bloquaient **à vie**. | Entrée **2** : le job, son état, et s'il appartient à l'identité courante ou précédente. | Identité précédente : **Mettre à jour** le Worker (le garde corrigé les ignore). Identité courante : annuler le job sur le site et attendre la confirmation. **Ne jamais effacer le journal.** |
| **(c)** | « Reconnecter le Worker » (5) répond `ok` mais le Worker reste refusé. | Reconnecter **n'efface qu'un marqueur de déconnexion** et redemande un heartbeat. Il **ne recrée jamais une identité**. | Entrée **1**. | Fiche présente → entrée **7**. Fiche supprimée → entrée **8**. |
| **(d)** | « Récupérer une inscription » (7) avec `-PairingCode …` : « N'utilise pas -PairingCode ni -Name ». | La récupération veut un **code de MIGRATION** et une **fiche encore présente**. | Le message de l'outil. | Fiche présente → code de migration, entrée **7**. Sinon entrée **8**. |
| **(e)** | « Code d'association invalide ou expiré », ou « trop de tentatives ». | Un code vit **10 minutes**, sert **une fois**, est stocké **haché** (le site ne peut pas te le relire), **20 par jour** par compte. | Message du site ou de l'outil. | Crée un code neuf **juste avant** de lancer l'outil. Si quota atteint : attendre. |
| **(f)** | Tu cherches le moteur 3D sur le port 18080 : rien. | Le moteur 3D **du Worker** écoute sur **8189**. 18080 est le moteur local **du dashboard**. | Entrées **1** et **9**. | Regarder 8189. |
| **(g)** | Le port **15064** est « tenu par le PID 4 (System) ». | Normal : le bridge Orca passe par **HTTP.sys**, composant de Windows. | Entrée **9** (l'outil lit la file HTTP.sys). | Rien à faire. **Ne jamais viser le PID 4.** |
| **(h)** | Worker hors ligne, **toutes** les requêtes refusées, alors que le réseau marche. | **Horloge** du PC décalée de plus de **60 secondes**. | Entrée **1** (contrôle de l'horloge). | Paramètres Windows → Heure → **Synchroniser maintenant**, puis entrée **5**. |
| **(j)** | Une erreur à chaque ouverture de session ; tâche « **Alpine Makers Worker <identifiant>** ». | Tâche planifiée **orpheline** : elle vise un ancien dossier du Worker (par exemple `C:\Users\Public\AlpineMakersWorker`), **disparu**. | Entrée **15**. | Entrée **15** : retirer l'orpheline. Sans danger : le service `AlpineWorker` assure le démarrage. |
| | **Hors ligne après un redémarrage du PC.** | Mode tâche : il faut **ouvrir la session** Windows. Mode service : service en **Manuel** (entrée 11 faite, entrée 12 oubliée) ou mot de passe du compte changé. | Entrées **1** et **15**. | Entrée **12**, ou ouvrir la session. Mot de passe changé : le ressaisir dans `services.msc` (toi-même, aucun script ne le fait). |
| | **Installation figée à 50 %.** | Cas connu : une roue pip illisible dans un ancien cache global, et un compte rendu d'erreur trop long refusé par le site. Corrigé (cache `cache\pip`). | Console du Worker sur le site ; entrée **3**. | **Mettre à jour**, **Annuler l'installation en cours**, attendre la confirmation, relancer l'installation. |
| | **« VRAM libre insuffisante ».** | Un modèle reste chargé, un autre programme occupe la carte, ou le modèle est trop gros. | Carte du Worker sur le site ; `nvidia-smi`. | **Libérer VRAM et RAM** ou entrée **16** ; fermer jeux et navigateurs ; choisir un modèle plus léger (section 8). |
| | **Clé DPAPI illisible / « accès refusé ».** | Tu n'es pas sous le compte Windows d'origine, ou le dossier vient d'un autre PC. | Entrée **7**. | Reviens sous le bon compte. Sinon migration (entrée 7). Le programme **n'écrase jamais** une clé pour contourner. |
| | **Changement de réseau** (box, Wi-Fi, câble). | L'adresse a changé ; l'identité, elle, n'a pas bougé. | Entrée **1**. | Entrée **6**, puis entrée **5** si besoin. Imprimante injoignable : vérifier qu'elle est sur le **même réseau**, pas un Wi-Fi invité. |
| | **Un moteur repart tout seul 30 s après le démarrage.** | Il a été arrêté **brutalement** : son intention est restée « allumé ». | Entrée **9**. | L'arrêter **proprement** (entrée 9 ou bouton du site). |
| | **Le Worker repart tout seul après que je l'ai arrêté.** | La supervision relance sous 30 s une source en démarrage Automatique. | Entrée **1**. | Entrée **11** (passe le service en Manuel). |
| | **Service `AlpineWorker` en état « Paused ».** | Un superviseur lancé **hors service** tient le verrou `supervisor.lock` : le superviseur du service sort aussitôt avec 0, et nssm le relance en boucle puis le met en pause. Cause la plus fréquente : entrée **5**, **6** ou **17** (ou un ancien `.bat`, ou un « O » à l'installateur) utilisée **pendant que le service était arrêté**, typiquement entre l'entrée 11 et l'entrée 12. | Entrée **1** : service « Paused » alors que l'agent répond. | Entrée **11** (elle arrête aussi les processus lancés hors service), puis **12**. Ensuite, service arrêté = entrée 12 uniquement. |
| | **Deux lanceurs pour le même dossier** (service **et** tâche planifiée). | Tu as répondu **O** à la question du démarrage automatique de `install_windows.bat` ou de « Récupérer une inscription » (7) sur un PC déjà en service. | Entrée **15** : section « Doublon ». | Entrée **15** : retirer le doublon (mot `RETIRER`). La prochaine fois : répondre **N** ou passer `-AutoStart No`. |

---

## 11. Linux

- **Standard (carte NVIDIA)** : `sudo ./install_linux.sh <url> <code> "<nom>"`. Installation
  dans `/opt/alpine-makers-worker`, service systemd `alpine-makers-worker` qui démarre avec
  le PC et se relance seul (il ne se relance pas après un **Déconnecter** : code 64).
- **Sans carte graphique** : variante réservée au compte propriétaire de la plateforme.
  Guide complet dans **`README_LINUX_NO_GPU.md`**.
- Le menu, les `.bat` et les outils `outils\` sont **Windows**. Sous Linux :

  ```bash
  systemctl status alpine-makers-worker --no-pager
  journalctl -u alpine-makers-worker -n 50 --no-pager
  sudo python3 /opt/alpine-makers-worker/local_control.py --root /opt/alpine-makers-worker --action reconnect
  sudo python3 /opt/alpine-makers-worker/local_control.py --root /opt/alpine-makers-worker --action network
  ```

- La clé est protégée par les permissions du compte de service (fichier en 600).
- OrcaSlicer et PrintGuard ne sont distribués que pour Windows. Un CUDA Toolkit manquant
  s'installe à la main.

---

## 12. Glossaire

- **Agent** : le programme `agent.py`, cœur du Worker.
- **Superviseur** : `run_worker.ps1`, qui lance l'agent et le relance s'il s'arrête.
- **Supervision** : le service `AlpineSupervisor` du PC d'atelier, qui surveille **toutes**
  les sources Alpine Makers. À ne pas confondre avec le superviseur.
- **Moteur** : un programme de calcul piloté par l'agent (ComfyUI, Hunyuan3D, PrintGuard,
  bridge Orca). **Module** / **modèle** : ce qu'un moteur charge pour travailler.
- **Heartbeat** : le signal de présence, toutes les 20 secondes.
- **Job** : un travail confié par le site (une image, un modèle 3D, un tranchage…).
- **Job « uncertain »** : job dont on ne sait pas s'il s'est vraiment arrêté. Il bloque la
  maintenance tant que l'arrêt du moteur n'est pas **confirmé** (`engine_stop_confirmed`).
- **Journal durable** : `runtime\worker-journal.sqlite3`, la mémoire du Worker.
- **Intention de démarrage** : ce que l'agent retient pour chaque moteur (« doit tourner »
  ou « arrêté »). C'est elle qui relance un moteur après un redémarrage.
- **Fiche** : la ligne de ton Worker dans Mes Workers, avec son identifiant (`worker_id`).
- **Identité** : la clé Ed25519 du PC. **DPAPI** : le chiffrement Windows lié à ton compte.
- **Association** / **migration** : voir le tableau des deux codes, section 7.
- **Révoquer** : couper l'accès en gardant la fiche.
- **VRAM** : la mémoire de la carte graphique. **CUDA** : la couche NVIDIA dont PyTorch a besoin.
- **HTTP.sys** : composant de Windows qui sert le port du bridge Orca (d'où le PID 4).
- **nssm** : le petit outil qui transforme un programme en service Windows.
- **Élévation / [ADMIN]** : la fenêtre Windows qui demande les droits administrateur.
- **Tâche orpheline** : tâche planifiée qui vise un dossier qui n'existe plus.
- **Boîte aux lettres locale** : `runtime\local-control\`, par où les outils parlent à
  l'agent en marche, sans réseau.

---

## 13. Où trouver l'historique technique

Ce document explique **comment se servir** du Worker. Le **pourquoi** de chaque décision,
les audits et les validations sont dans le dossier `docs\` du dépôt du dashboard
(`P1S_Local_Dashboard\docs\`) :

| Document | Sujet |
| --- | --- |
| `worker_windows_lifecycle.md` | Démarrage, déconnexion, mise à jour, réparation, libération de mémoire, outils locaux. **Le plus à jour.** |
| `supervision_sources.md` | Services Windows du PC d'atelier, `AlpineSupervisor`, interrupteur Automatique/Manuel, fichier de pause. |
| `worker_identity_security.md` | Clé Ed25519, signatures, révocation, migration, quotas. |
| `JOBS_DURABLES.md` | Journal durable, jobs incertains, ce qu'il faut sauvegarder. |
| `worker_storage.md` | Arborescence, caches, `storage.json`. |
| `worker_installations.md`, `worker_hardware_installations.md` | Installations, profils CUDA, minima. |
| `worker_installation_50_percent_fix.md` | L'installation figée à 50 %. |
| `WORKERS_EQUIPEMENTS.md` | Imprimantes, caméras, relais, contenu du paquet de mise à jour. |
| `DISTRIBUTED_GPU_WORKERS.md` | Architecture générale, partage, contrôle des doublons. |

Le code fait foi : `install_windows.ps1`, `run_worker.ps1`, `worker_tools.ps1`,
`local_control.py`, `configure_autostart.ps1`, `agent.py`.
# Version 1.35.13 — dossiers maintenus

Les distributions sous forme de dossiers ZIP restent maintenues pour Windows/Linux.
Le téléchargement manuel est réservé aux owners du dashboard. Les workers déjà
associés gardent leur protocole de mise à jour, quel que soit le rôle du compte.
Aucun EXE n'est livré par cette version. Les réglages, modèles et fichiers installés
ne sont pas supprimés. Avant de nouvelles installations/utilisations, accepter les
CGU et licences dans le dashboard. Une demande éditeur en cours est un avertissement,
pas une autorisation ; respecter les restrictions affichées.
