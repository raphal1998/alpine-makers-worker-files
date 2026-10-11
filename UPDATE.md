# Historique des mises à jour

## Version 1.49.0 — 2026-10-09

**Préparer pour une CNC : FreeCAD CAM calcule les parcours d'usinage sur le Worker**

### Ajouté
- Capacité freecad_cam_v1 : le Worker lit les capacités CAM de son FreeCAD (version, opérations, post-processeurs et leurs options, formes d'outils) et calcule un projet CNC déclaratif : surfaçage, poches, perçages et contour avec ponts, pauses et changements d'outil, en 3 axes, avec fraise droite et foret.
- Le G-code produit est relu par un contrôle indépendant (unités, courses, broche, avances, outils, commandes refusées par la machine, code d'imprimante 3D) ; le Worker rend aussi le projet FreeCAD, le projet CAM, une fiche de réglage et une simulation.

### Modifié
- Machine à changement d'outil manuel : chaque changement d'outil arrête la broche et met le programme en pause (M0), même sans étape dédiée dans la séquence ; une pause arrête puis relance la broche.
- Le projet est revalidé par le runner et par le moteur avant tout calcul ; un fichier joint qui ne correspond pas au projet est refusé. Aucune commande n'est envoyée à une machine.

### Technique
- Empreinte du paquet publié : `d650dc975a9e7b55…` (dossier du paquet).

## Version 1.48.0 — 2026-10-08

**Gestion du Worker depuis le PC : rubrique Gestion du menu, état du site et mise à jour en deux temps**

### Ajouté
- MENU-WORKER : nouvelle rubrique « Gestion du Worker » — Contrôle total et réparation (18), Contrôler les doublons (19), Arrêter tous les jobs (21), Mode de partage (22), Mettre à jour le Worker (23), Réparer (24) ; « Supprimer les doublons » (20) en zone rouge, après un contrôle récent. Le bandeau du menu affiche un indicateur « À faire ».
- Le Worker lit son propre état vu par le site et peut, pour lui-même, demander une mise à jour, une réparation, l’arrêt de ses jobs ou un réglage du partage, par des requêtes signées par son identité (capacité worker_self_service_v1). Ouvrir le Worker à tous les comptes avec acceptation automatique exige de taper OUVRIR A TOUS.
- Doublons des modèles ComfyUI : contrôle, plan vérifié (taille, date, SHA-256 relus avant chaque opération) et remplacement d’une copie par un lien physique ; aucune suppression hors des copies manifestes du même dossier.
- Fichier ETAT-WORKER.txt écrit par l’agent, lisible sans réseau par le menu et par worker.exe.

### Modifié
- Au démarrage, les restes d’une mise à jour interrompue sont retirés (bornés aux fichiers livrés) et l’intégrité des fichiers de l’agent est contrôlée.
- Mes Workers, Installations et Mes équipements demandent de fermer worker.exe avant la première mise à jour d’un agent Windows antérieur à 1.48.0.

### Corrigé
- Mise à jour : worker.exe ouvert (WinError 5 « Accès refusé ») n’interrompt plus la mise à jour. Les fichiers sont préparés à côté de leur cible puis échangés par deux renommages ; un fichier chargé par l’agent qui reste verrouillé annule tout et remet l’ancienne version, un fichier non chargé (worker.exe) est écarté puis remplacé, ou laissé ancien et signalé.
- Le texte d’aide de l’outil Journaux (« … seulement ses lignes d’erreur, ou son défilement en direct ») n’est plus relayé au site comme une erreur du Worker.

## Version 1.47.1 — 2026-10-08

**Installation Windows : le démarrage automatique ne dépend plus des droits administrateur**

### Modifié
- MENU-WORKER > 15 (Démarrage automatique), le bandeau du menu, l’état du Worker et worker.exe reconnaissent le mode « entrée de démarrage de session » au lieu d’annoncer « aucun démarrage automatique » ; l’entrée 15 propose de passer à la tâche planifiée (fenêtre administrateur) ou de retirer ce démarrage.

### Corrigé
- L’installateur se terminait par « Register-ScheduledTask : Accès refusé » puis « L’installation du Worker a echoue. Code: 1 » sur un compte Windows sans droits administrateur (ou quand la tâche du même nom avait été créée depuis une session administrateur), alors que le Worker était installé et associé. Ce refus n’arrête plus l’installation : une tâche planifiée valide déjà présente pour ce compte est conservée ; sinon le Worker est inscrit dans le démarrage de session de Windows (registre du compte, sans privilège) et lancé tout de suite.
- Retirer le démarrage automatique retire aussi cette entrée de session, même quand Windows refuse de retirer la tâche planifiée ; le message renvoie alors vers MENU-WORKER > 15, qui demande les droits administrateur.

## Version 1.47.0 — 2026-10-07

**Générateur d’images local : CLIP skip et hires fix sur le Worker**

### Ajouté
- CLIP skip (capacité comfyui_clip_skip_hires_v1) : pour Stable Diffusion 1, 2 et XL, l’encodeur de texte s’arrête 1 à 12 couches avant la fin, comme chez AI Horde (nœud natif CLIPSetLastLayer de ComfyUI, placé après les LoRA).
- Hires fix en Texte → Image (toutes les architectures sauf Stable Cascade) : la première image est agrandie dans l’espace latent (×1,1 à ×2, interpolation bislerp, sans décodage intermédiaire), puis affinée par une seconde passe avec la même graine, la même CFG, le même échantillonneur et le même planificateur ; intensité 0,2 à 0,9 et 4 à 40 étapes, image finale de 2048 px de côté au plus.

### Modifié
- Les styles qui demandent CLIP skip ou hires fix les transmettent au générateur local au lieu de les marquer « non transposés ». Sur un Worker resté en 1.46.x, ces réglages sont retirés du calcul et la génération le signale ; « Mettre à jour » dans Mes Workers les rend disponibles.

## Version 1.46.1 — 2026-10-06

**Labo image : l’installation des « Outils de cartes du Labo » se termine sans erreur**

### Corrigé
- Installer le module « Outils de cartes du Labo » finissait en échec alors que le module était bien posé : il manquait sa notice documentaire dans le dossier juridique livré avec l’agent. La notice est ajoutée (code d’Alpine Makers, aucun composant tiers embarqué ; Pillow, licence MIT-CMU, et numpy, licence BSD-3-Clause, restent ceux du moteur ComfyUI, avec leurs titulaires et les liens vers leurs licences). Après la mise à jour du Worker, réinstalle le module depuis Mes Workers → Installations.

## Version 1.46.0 — 2026-10-06

**Labo image : propriétés, palette et cartes simples calculées sur le Worker**

### Ajouté
- Labo image, module « Outils de cartes du Labo » (capacité image_lab_maps_v1) : propriétés et palette d’une image (dimensions, rapport, transparence, niveaux de gris, couleur moyenne, empreinte visuelle, couleurs dominantes) et sept cartes — normales depuis une profondeur, trait, gribouillage, tuile / flou, grille de couleurs, niveaux de gris, masque depuis un détourage — calculées sur le Worker, avec les mêmes réglages et les mêmes résultats que l’ancien calcul du site.
- Le calcul est un script du Worker (Pillow seul) lancé avec le Python du moteur image (ComfyUI), sans réseau, avec délai maximal et annulation ; le site n’envoie qu’un type de calcul et des réglages bornés, revérifiés par le Worker.

### Modifié
- Ces outils ne tournent plus sur le site : sans Worker équipé du module (installé et ON dans le panneau ON/OFF du Labo), le Labo explique ce qui manque et refuse le calcul.

### Technique
- Empreinte du paquet publié : `786fd5af67beeb89…` (dossier du paquet).

## Version 1.45.0 — 2026-10-06

**Modèle ajouté par le responsable : installation par adresse, avec empreinte vérifiée**

### Ajouté
- Un checkpoint complet (modèle de génération) s’installe depuis une fiche ajoutée dans Panel Admin → Gestion des modèles (capacité comfyui_checkpoint_asset_v1) : le Worker télécharge le fichier à l’adresse https de la fiche, vérifie son empreinte SHA-256 annoncée et que son contenu est bien un checkpoint safetensors, puis le range dans models/checkpoints.

### Modifié
- Le modèle installé ainsi apparaît dans le relevé comme checkpoint « ajouté à la main », avec son architecture lue dans le fichier ; il ne passe ni par le LoRA Manager ni par l’index des ressources.

### Technique
- Empreinte du paquet publié : `284bac2133ce51b6…` (dossier du paquet).

## Version 1.44.0 — 2026-10-06

**Labo image : restauration des visages (GFPGAN v1.4, usage non commercial)**

### Ajouté
- Labo image, « Restaurer les visages » (capacité image_lab_face_restore_v1) : le Worker repère les visages de la photo avec le détecteur YuNet, les reconstruit un par un avec GFPGAN v1.4 puis les recolle dans l’image d’origine avec un bord progressif ; réglages : force (0 à 100 %) et nombre de visages (1 à 8). Sans visage, le résultat le dit et aucune image n’est produite.
- Le calcul est un script du Worker lancé avec le Python du moteur image (ComfyUI) : aucun paquet ni nœud ComfyUI n’est installé, seulement deux fichiers de poids. Il tourne sur la carte graphique quand elle a de la place, sinon sur le processeur.
- Le site n’envoie qu’un type de calcul et deux réglages bornés, revérifiés par le Worker : jamais un nom de fichier, un chemin ou une commande. Les poids sont relus en mémoire, comparés à leur SHA-256 épinglé, puis chargés sans exécution de code (weights_only) ; le script n’a pas accès au réseau.

## Version 1.43.0 — 2026-10-06

**Modèles à clé CivitAI, modèles retirés encore publiés, notices des modèles du référentiel**

### Ajouté
- Un modèle du catalogue hébergé par CivitAI se télécharge avec la clé CivitAI propre au propriétaire du Worker (Mes APIs). La clé arrive avec la commande, n’est envoyée qu’à CivitAI et n’est jamais écrite sur le disque du Worker ; le fichier reste épinglé par SHA-256.
- Sans clé ou avec une clé refusée, l’installation s’arrête avec la marche à suivre au lieu d’une erreur brute.

### Modifié
- La clé d’accès jointe à une commande n’est plus renvoyée au site lors du précontrôle des conditions.

### Corrigé
- L’installation d’un modèle du référentiel public se terminait par « Notice documentaire absente » après le téléchargement : le paquet livre désormais le dossier documentaire de chacun de ces modèles.

## Version 1.42.0 — 2026-10-05

**worker.exe : le menu du Worker devient une application, exécutables signés et à l’image d’Alpine Makers**

### Ajouté
- worker.exe, à la racine du Worker : application graphique du menu (état du Worker, connexion au Dashboard, toutes les entrées du menu par rubrique, journal copiable, réponses aux questions des outils), sans fenêtre de console. Elle pilote les outils existants ; MENU-WORKER.bat reste le menu de secours.
- Raccourci « Alpine Makers Worker » sur le Bureau et dans le menu Démarrer à l’installation.
- Icône Alpine Makers et informations de version en français sur worker.exe et sur l’installateur ; les deux sont signés (Authenticode SHA256, certificat autosigné « Alpine Makers »).

### Modifié
- Depuis worker.exe, la partie administrateur d’un outil tourne sans fenêtre : Windows demande toujours son accord, et ce que l’outil écrit est recopié dans le journal.
- menu_worker.ps1 -EtatJson rend l’état et le catalogue du menu en JSON, sans aucun secret.

### Technique
- Empreinte du paquet publié : `8b7e06b4d93aeae6…` (dossier du paquet).

## Version 1.41.1 — 2026-10-05

**Stable Diffusion 2 — 768 reconnu dans le fichier**

### Corrigé
- Un checkpoint Stable Diffusion 2 en prédiction v (768 px) était relevé « 512 » : l’agent lit désormais le même tenseur que ComfyUI (norm1.bias du dernier bloc), vérifié sur les fichiers officiels 2.1 Base et 2.1.

### Technique
- Empreinte du paquet publié : `34b33628210daedb…` (dossier du paquet).
