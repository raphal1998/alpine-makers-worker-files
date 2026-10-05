# Worker Alpine Makers — installation par fichiers

Ce dépôt contient la **version actuelle du Worker Alpine Makers sous forme de dossier** : le programme,
ses scripts `.bat` / PowerShell et son menu d'outils. C'est exactement le paquet que le site propose avec
« Télécharger le Worker ».

> Tu préfères un installateur en un seul fichier ? Voir le dépôt jumeau
> [alpine-makers-worker-exe](https://github.com/raphal1998/alpine-makers-worker-exe). Les deux installent
> **le même Worker**.

| Document | Pour qui |
| --- | --- |
| **README.md** (ce fichier) | Tout le monde : comprendre, installer, utiliser, dépanner. |
| [README_AVANCE.md](README_AVANCE.md) | Lecteurs techniques : architecture, sécurité, maintenance. |
| [UPDATE.md](UPDATE.md) | Historique des versions. |
| [`alpine-makers-worker/README.md`](alpine-makers-worker/README.md) | Le manuel complet livré avec le Worker (menu d'outils, entrée par entrée). |

---

## 1. Qu'est-ce qu'un Worker ?

Le **Dashboard Alpine Makers** est un site web (<https://dashboard.alpine-makers.ch>). Il sait préparer des
impressions 3D, générer des images et des modèles 3D par IA, convertir des fichiers, piloter une graveuse laser,
surveiller une impression… mais **il ne calcule rien lui-même**.

Le **Worker** est un petit programme que tu installes **sur ton propre PC**. Il prête la puissance de ton PC
(surtout sa carte graphique) à ton compte du Dashboard.

```text
   Ton navigateur ──────► Dashboard (site web) ◄────── Worker (sur ton PC)
                              donne le travail            fait le calcul, renvoie le résultat
```

### Pourquoi il tourne chez toi

- Les moteurs d'IA ont besoin d'une **carte graphique** : la tienne.
- Tes imprimantes, caméras et graveuses sont sur **ton réseau local** : seul un programme chez toi peut les joindre.
- Tes modèles et tes fichiers restent **sur ton disque**.

### Ce que le Worker permet au Dashboard de faire

| Outil du site | Moteur lancé par le Worker |
| --- | --- |
| Générateur d'image IA | ComfyUI (Stable Diffusion, FLUX…) |
| Générateur de modèle 3D IA | Hunyuan3D et moteurs associés |
| Convertisseur 2D / 3D | FreeCAD |
| Alpine Model Studio (tranchage) | OrcaSlicer |
| Surveillance d'impression | PrintGuard |
| Alpine Laser Studio, imprimantes, caméras | Relais vers les machines de ton réseau |

Aucun moteur n'est installé d'office : tu choisis ensuite sur le site, dans **Mes Workers → Installations**.

### Comment ils se parlent

- C'est **toujours le Worker qui appelle le site**, en HTTPS sortant. Il n'ouvre **aucun port** vers Internet :
  rien à régler sur ta box ni sur ton pare-feu.
- Il se signale au site toutes les **20 secondes**. Sans signe de vie pendant **75 secondes**, le site l'affiche
  « hors ligne » et ne lui confie plus rien.
- Le site ne peut lui demander qu'une **liste fermée d'actions** connues. Aucune commande libre.
- Chaque PC a sa propre **clé d'identité**, créée à l'installation et jamais envoyée.

---

## 2. Prérequis

| Élément | Exigence |
| --- | --- |
| Système | **Windows 10 ou 11** (64 bits). Linux : voir la fin de ce document. |
| Carte graphique | **NVIDIA** avec son pilote installé (`nvidia-smi` doit répondre). AMD, Intel et Mac ne sont pas pris en charge pour les moteurs d'IA. |
| Compte Windows | Ton compte habituel : le Worker lui est attaché. Les droits administrateur ne sont **pas** obligatoires pour installer. |
| Disque | Le Worker pèse quelques Mo. Chaque moteur demande au moins **8 Go libres**, chaque modèle plusieurs Go. |
| Compte Dashboard | Un compte sur <https://dashboard.alpine-makers.ch>. |
| Internet | Connexion sortante HTTPS. |

Python 3.12 et Git sont installés automatiquement s'ils manquent.

---

## 3. Installation pas à pas

1. **Récupère le dossier.** Sur cette page GitHub : bouton vert **Code → Download ZIP**, puis extrais l'archive.
   (Ou, depuis le site : **Mes Workers → Télécharger le Worker** — c'est le même contenu.)
2. **Demande un code d'association.** Sur le site : **Mes Workers → Associer un PC**. Le code est valable
   **10 minutes** et ne sert **qu'une fois**.
3. **Lance l'installateur.** Dans le dossier `alpine-makers-worker`, double-clique sur **`install_windows.bat`**.
   Il demande l'adresse du site et ton code. Tu peux aussi tout donner d'un coup :

   ```powershell
   .\install_windows.bat -ServerUrl https://dashboard.alpine-makers.ch -PairingCode 123-456
   ```

4. **Réponds à la question du démarrage automatique** (O = le Worker démarre à l'ouverture de ta session Windows).
5. **Vérifie sur le site** : ton PC apparaît dans **Mes Workers**, avec une pastille verte « en ligne ».

> **Utilise bien le `.bat`**, pas le `.ps1` directement : il autorise ce seul lancement sans modifier les réglages de Windows.

> **Le dossier extrait n'est pas l'installation.** L'installateur copie le programme dans
> `C:\Users\<toi>\Desktop\alpine-makers-worker`. Tu peux supprimer le dossier extrait ensuite.

### Ce que l'installateur fait

- installe Python 3.12 et Git s'ils manquent ;
- crée le dossier `alpine-makers-worker` sur ton Bureau et le **réserve à ton compte** ;
- crée la clé d'identité du PC et l'associe à ton compte avec le code ;
- crée, si tu as répondu O, une tâche planifiée « Alpine Makers Worker … » à l'ouverture de session.

Il n'installe **aucun moteur ni modèle** : cela se fait depuis le site.

---

## 4. Utilisation au quotidien

Presque tout se fait **depuis le site**. Sur le PC, un seul fichier à connaître :
**`MENU-WORKER.bat`**, dans le dossier installé. Chaque entrée explique ce qu'elle fait avant d'agir.

| Je veux… | Comment |
| --- | --- |
| Voir si tout va bien | Menu **1 · État complet du Worker** |
| Démarrer / reconnecter le Worker | Menu **5 · Reconnecter** |
| Arrêter le Worker | Menu **11 · Éteindre le Worker** |
| Le rallumer | Menu **12 · Rallumer le Worker** |
| Le redémarrer | Menu **10 · Redémarrer l'agent** |
| Changer le démarrage automatique | Menu **15 · Démarrage automatique** |
| Lire les journaux | Menu **3 · Journaux** |
| Préparer un rapport pour le support | Menu **4 · Rapport de diagnostic** |
| Installer un moteur ou un modèle | Site : **Mes Workers → Installations** |

### Vérifier qu'il fonctionne et qu'il est connecté

1. Sur le site, **Mes Workers** : pastille verte et version affichée sur la carte du PC.
2. Sur le PC, **menu 1** : il indique si l'agent tourne, depuis quand, et la date du dernier contact avec le site.

### Récupérer les journaux

- Dossier `logs\` de l'installation (un fichier par moteur, plus `outils-worker.log`).
- **Menu 4** fabrique une archive de diagnostic à partager : les secrets n'y figurent pas.

---

## 5. Mise à jour

Sur le site, **Mes Workers → Mettre à jour**. Le Worker télécharge la nouvelle version **depuis le Dashboard**,
vérifie l'empreinte de chaque fichier, se remplace et redémarre. Tes moteurs, modèles, réglages et ton identité
sont **conservés**. Rien n'est à réinstaller.

Ce dépôt GitHub te permet de **consulter** la version actuelle et son historique ([UPDATE.md](UPDATE.md)) ; la mise
à jour d'un Worker installé passe par le bouton du site.

---

## 6. Désinstallation propre

1. **Arrête le Worker** : menu **11**.
2. **Retire le démarrage automatique** : menu **15**.
3. (Facultatif) **Supprime les moteurs IA** pour libérer la place : menu **17** — plusieurs dizaines de Go, sans Corbeille.
4. Sur le site, **Mes Workers → Supprimer** la fiche du PC.
5. **Supprime le dossier** `Desktop\alpine-makers-worker`.

> FreeCAD et le CUDA Toolkit, s'ils ont été installés, sont des logiciels Windows classiques :
> ils se désinstallent depuis « Applications installées ».

---

## 7. Fichiers créés sur ton PC

| Emplacement | Contenu |
| --- | --- |
| `Desktop\alpine-makers-worker\` | Tout le Worker : programme, moteurs (`components\`), modèles, caches, journaux (`logs\`). |
| `…\config.json` et `…\config\` | Réglages et **clé d'identité du PC**. À ne jamais partager ni copier vers un autre PC. |
| Tâche planifiée « Alpine Makers Worker … » | Seulement si tu as choisi le démarrage automatique. |

Rien n'est écrit ailleurs, hormis Python/Git s'ils manquaient.

---

## 8. Dépannage courant

| Symptôme | À faire |
| --- | --- |
| Le PC est « hors ligne » sur le site | Menu **5 · Reconnecter**, puis menu **1** pour lire l'état. Vérifie ta connexion Internet et l'heure du PC. |
| « Code d'association invalide » | Le code a plus de 10 minutes ou a déjà servi : demandes-en un nouveau. |
| « Identité existante conservée » à l'installation | Le dossier contient déjà une inscription : c'est normal, ton code n'a pas été consommé. Menu **7** ou **8** si la fiche a été supprimée sur le site. |
| Installation d'un moteur refusée | Lis le message : carte NVIDIA absente, pilote trop ancien, VRAM ou disque insuffisant. |
| « VRAM libre insuffisante » | Un autre moteur occupe la carte : menu **9** (arrêter un moteur) ou **16** (libérer la mémoire). |
| Windows affiche un avertissement au lancement du `.bat` | Fichier téléchargé d'Internet : « Informations complémentaires → Exécuter quand même ». |
| Rien ne marche après une mise à jour | Menu **10 · Redémarrer l'agent**, puis menu **4** pour un rapport. |

---

## 9. FAQ

**Le Worker ouvre-t-il mon PC à Internet ?** Non. Il n'accepte aucune connexion entrante ; c'est lui qui appelle le site.

**Le site peut-il exécuter n'importe quoi sur mon PC ?** Non. Seulement une liste fermée d'actions, chacune vérifiée par le Worker.

**Faut-il laisser le PC allumé ?** Seulement quand tu veux utiliser ses moteurs ou ses machines depuis le site.

**Puis-je installer le Worker sur plusieurs PC ?** Oui, chacun avec son propre code d'association. Ne copie jamais un dossier installé d'un PC à l'autre.

**Mes fichiers partent-ils dans le cloud ?** Les calculs se font chez toi. Les résultats que tu demandes sont renvoyés au site pour s'afficher dans ton compte.

**Et sous Linux ?** Le paquet contient `install_linux.sh` (service systemd). Voir le manuel livré, section « Linux ».

---

*Alpine Makers — le Worker est fourni tel quel, sans garantie. Chaque moteur et chaque modèle installé garde sa propre licence, affichée sur le site avant installation.*
