# Historique des mises à jour

## Version 1.50.15 — 2026-10-14

**Alpine CNC Studio : moteur CNC installable sur le Worker (cnc-engine)**

### Ajouté
- Nouvel outil « Alpine CNC Studio » dans Installations de Mes Workers : composant cnc-engine (environnement Python isolé, bibliothèque standard seulement) qui valide et analyse les programmes de fraisage pour le compte qui l'a installé : erreurs et avertissements, étendue, durée estimée, outils et pauses, broche, arrosage, contrôle de course. Démarrage et arrêt à la demande comme Alpine Laser Studio, pastille on/off partout sur le site. Capacité cnc_engine_v1.

### Corrigé
- Validateur de G-code CNC : un G53 ou G28 qui suivait un mouvement effaçait ce mouvement des bornes calculées (copie de la position corrigée).

### Technique
- Empreinte du paquet publié : `042ddd42a415b4e6…` (dossier du paquet).

## Version 1.50.14 — 2026-10-14

**Caméras : vue fluide sur le site, résolution et cadence libres (jusqu'à 3840×2160 et 120 images/s)**

### Ajouté
- Vue fluide de la page « Voir caméra » du site : dès qu'une vue est ouverte, le Worker pousse au site la dernière image disponible, réduite au profil de la fiche (« Vue du site » : fluide 960 px à 20 images/s, équilibrée 1280 px à 15, nette 1920 px à 10, ou cadence, largeur et qualité JPEG personnalisées), par une connexion gardée ouverte. Rien n'est mis en file : une image en retard est sautée, et quand le lien montant ne suit pas le Worker baisse la qualité, puis la taille. Sans vue ouverte, il n'envoie rien de plus qu'avant. Capacité equipment_camera_live_v1.
- Format libre des caméras USB et des webcams : résolution jusqu'à 3840×2160, capture et lien MJPEG local de 1 à 120 images par seconde (au-delà de 30, cette version est requise). La caméra fournit ce qu'elle peut : le message de la fiche indique la taille et la cadence réellement obtenues à côté de celles demandées.

### Modifié
- Une image de caméra peut peser jusqu'à 4 Mio sur le Worker (4K) ; l'image jointe au rapport du site reste sous 1 Mio, recompressée seulement quand elle le dépasse.
- Les images que le Worker vient d'encoder lui-même ne sont plus décodées en entier une seconde fois à chaque image (en-tête à chaque image, décodage complet une fois par seconde) : moins de processeur à cadence élevée.

### Corrigé
- Le lien MJPEG local servait environ 22 images/s pour 30 demandées : chaque image décalait la suivante d'une période entière plus le temps d'envoi. La cadence demandée est maintenant tenue (composant relais 1.2.0).

## Version 1.50.13 — 2026-10-14

**worker.exe : ajouter un co-propriétaire (Worker, équipements ou les deux)**

### Ajouté
- Onglet Partage de la Gestion complète : le propriétaire ajoute un co-propriétaire par son pseudo et choisit ce qu'il partage (le Worker seul, ses équipements seuls, ou les deux), le suspend, le reprend ou le retire. Le co-propriétaire voit le Worker et les équipements partagés comme les siens sur le site et peut presque tout faire, sauf révoquer, désinstaller complètement, régler le partage ou se connecter à worker.exe.

### Technique
- Empreinte du paquet publié : `24190287a3f0adf3…` (dossier du paquet).

## Version 1.50.12 — 2026-10-14

**worker.exe : connexion du propriétaire avec son compte du site (2FA, verrou après 3 échecs)**

### Ajouté
- À l'installation et à chaque lancement de worker.exe, le propriétaire du Worker se connecte avec son compte du dashboard (même base) ; le code 2FA n'est demandé que si le compte l'a activé. Une fois l'application ouverte, aucune reconnexion tant qu'elle n'est pas fermée. Un autre compte est refusé et le propriétaire, les admins et le propriétaire du site en sont notifiés. Après 3 échecs, l'application est bloquée : un e-mail avec un lien de déblocage part au propriétaire, qui peut aussi débloquer depuis Mes Workers.
- Capacité worker_exe_login_v1 ; le mot de passe ne transite que par un fichier local lu une seule fois, jamais dans les journaux.
- Une webcam branchée au PC du Worker peut être utilisée de deux façons : le connecteur « Caméra USB » (inchangé), ou le connecteur « Flux MJPEG » avec l'origine « Webcam de ce Worker ». Dans ce second mode, le Worker capture la webcam (même capture que le mode USB), la publie sur son lien MJPEG local (port, images par seconde, largeur et hauteur de la fiche), démarre ce lien à la connexion et après un redémarrage, et la caméra lit ses images par ce lien. Le lien reste utilisable par OBS, LightBurn ou PrintGuard sur ce PC. Capacité equipment_camera_webcam_link_v1.

### Modifié
- Une même webcam n'est plus ouverte par deux fiches à la fois (fiche USB et fiche par lien) : la seconde reçoit un refus explicite au lieu d'une image absente.
- « Installer » et « Tester » d'une fiche par lien posent la capture USB et vérifient toute la chaîne (capture, lien, lecture).

### Corrigé
- Une fiche « Flux MJPEG » qui prenait son propre lien local comme adresse restait sur « ouverture du flux MJPEG (HTTPError) » : rien n'alimentait ce lien. Le formulaire propose maintenant l'origine « Webcam de ce Worker » et le refus du site indique ce choix.

### Technique
- Empreinte du paquet publié : `9e3d308d81b61fb4…` (dossier du paquet).

## Version 1.50.10 — 2026-10-14

**Lien MJPEG local pour chaque caméra (cadence, largeur et hauteur réglables)**

### Ajouté
- Chaque caméra du Worker (P1S, flux MJPEG existant ou USB) obtient automatiquement un lien MJPEG local http://127.0.0.1:<port>/api/stream.mjpeg?src=camera_<id>, lisible uniquement sur le PC du Worker (OBS, LightBurn, PrintGuard…). La fiche de Mes équipements règle le port, les images par seconde, la largeur et la hauteur ; le relais recadre et ré-encode chaque image une seule fois pour tous les lecteurs. Capacité equipment_camera_relay_v2, composant relais 1.1.0 : relance « Installer » sur la fiche caméra après la mise à jour pour obtenir la cadence et la taille choisies.

### Modifié
- Le relais loopback n'est plus réservé au mode « relais » de la caméra P1S.

### Technique
- Empreinte du paquet publié : `1d5a3b5ad694ee65…` (dossier du paquet).

## Version 1.50.9 — 2026-10-12

**Capture USB : le processus de capture s'arrête avec l'agent (plus d'orphelin qui garde la caméra)**

### Corrigé
- Quand l'agent était arrêté net (mise à jour, redémarrage), un processus de capture sans image à envoyer restait vivant, caméra en main : toutes les ouvertures suivantes réussissaient sans jamais recevoir d'image (« reçues : 0 »). Le processus surveille maintenant son agent et sort dès qu'il disparaît. Les orphelins déjà présents disparaissent au redémarrage du PC.

### Technique
- Empreinte du paquet publié : `aa3a98bbf3afdf64…` (dossier du paquet).

## Version 1.50.8 — 2026-10-12

**Media Foundation : image MJPEG reçue compressée prise telle quelle, 640×480 par défaut**

### Ajouté
- Sans largeur ni hauteur sur la fiche, Media Foundation choisit 640×480 si la caméra le propose.

### Corrigé
- Certaines caméras livrent leur flux MJPEG compressé, sans image décodée : l'agent l'ignorait et annonçait « ne fournit pas d'images ». Le JPEG est maintenant transmis tel quel. Le message d'échec donne le format retenu et le compte des images reçues, décodées, JPEG et vides.

## Version 1.50.7 — 2026-10-12

**Caméra USB sans image en Media Foundation : repli DirectShow en automatique, noms des caméras sur la fiche**

### Ajouté
- La fiche connectée nomme la caméra ouverte (« Media Foundation · Logitech StreamCam · MJPG 640×480 ») : l'ordre des index de Media Foundation n'est pas celui de DirectShow.

### Corrigé
- Une caméra ouverte par Media Foundation mais sans image (tenue par un autre programme, ou mauvais index) faisait recommencer l'agent en boucle ; en automatique il passe maintenant à DirectShow, et en « Media Foundation » forcé la fiche liste les caméras dans l'ordre des index.

## Version 1.50.6 — 2026-10-12

**« Installer la capture USB » reconnaît les paquets Media Foundation une fois posés**

### Corrigé
- Les paquets WinRT de Media Foundation, une fois installés, étaient tenus pour absents (paquet sans __init__.py) : « reste introuvable après l’installation ». La présence est maintenant vérifiée sur les fichiers réels du paquet.

## Version 1.50.5 — 2026-10-12

**« Installer la capture USB » pose aussi Media Foundation sur un Worker qui avait déjà OpenCV**

### Corrigé
- Un Worker où OpenCV était déjà installé répondait « OpenCV présent » sans poser les paquets WinRT de Media Foundation : la caméra restait sur « dépendances WinRT absentes ». L'installation reprend tant qu'ils manquent.

## Version 1.50.4 — 2026-10-12

**Caméras USB : capture par Media Foundation (Windows), deux caméras sur un même PC**

### Ajouté
- Capture des caméras USB par Media Foundation, le mécanisme de Windows lui-même (application Caméra, Teams) : les paquets OpenCV n'en disposent pas, l'agent passe par WinRT (dépendances posées par « Installer la capture USB »). Windows partage alors les caméras entre programmes et négocie le format (MJPEG préféré : moins de bande passante USB, deux caméras possibles sur un même contrôleur).
- Mode Automatique : Media Foundation d'abord, DirectShow (OpenCV) si la caméra n'y est pas ; le choix « Media Foundation » l'impose. La fiche affiche le format retenu (« Media Foundation · MJPG 640×480 »).

### Modifié
- En cas d'échec, la fiche nomme les caméras que Media Foundation voit, dans l'ordre des index.

## Version 1.50.3 — 2026-10-11

**Deux caméras USB sur un même Worker : une capture par processus**

### Corrigé
- Sous Windows, chaque caméra USB est maintenant capturée dans son propre processus (usb_camera_capture.py) : deux captures DirectShow dans un même programme se faisaient tomber l'une l'autre avec certains pilotes (deux caméras qui marchaient chacune seule, jamais ensemble, et se reconnectaient à tour de rôle). Les images suivent le même chemin qu'avant ; l'agent n'ouvre plus OpenCV lui-même.
- Media Foundation : les paquets OpenCV installés par pip n'en livrent pas le module ; le choix « MSMF » de la fiche ne peut donc rien ouvrir, le mode Automatique le constate et passe à DirectShow.

## Version 1.50.2 — 2026-10-11

**Caméras USB : Media Foundation ouvre vraiment, et la fiche dit quelles caméras le Worker voit**

### Ajouté
- Quand une caméra USB ne s'ouvre pas en mode automatique, le message de la fiche liste les index que le Worker peut ouvrir, par backend (« MSMF : 0, 1 · DirectShow : 1 ») : plus besoin de deviner l'index.

### Corrigé
- Media Foundation : l'agent lui imposait le format MJPEG et une taille de tampon après l'ouverture, ce qui faisait échouer la première image ; l'automatique retombait alors sur DirectShow, qui refuse une seconde caméra. Ces réglages ne sont plus envoyés qu'à DirectShow, et la première image MSMF est attendue jusqu'à trois fois.

## Version 1.50.1 — 2026-10-11

**Caméras USB : choix du backend OpenCV et deux caméras sur un même Worker**

### Ajouté
- Fiche « Caméra USB branchée au Worker (UVC) » : champ « Backend OpenCV » (Automatique = Media Foundation puis DirectShow sous Windows ; MSMF ; DirectShow ; V4L2 sous Linux). Le backend retenu est affiché sur la fiche (« Caméra USB connectée (MSMF) — … »).

### Modifié
- Un agent plus ancien ignore le nouveau réglage et garde DirectShow : les fiches existantes ne changent pas.

### Corrigé
- Deux caméras USB d'un même PC (index 0 et 1) : DirectShow, imposé jusqu'ici, refusait la seconde ; en automatique l'agent ouvre maintenant chaque caméra avec Media Foundation et ne retombe sur DirectShow que pour celle qui n'ouvre pas ou ne livre pas d'image, sans toucher à l'autre capture.

## Version 1.50.0 — 2026-10-11

**worker.exe fait tout ce que fait Mes Workers (gestion complète, à activer par le propriétaire)**

### Ajouté
- Fenêtre « ★ Gestion complète » dans worker.exe : démarrer et arrêter tous les modules ou une sélection, associer ou dissocier le Worker de ses pages d'outils (sélection, tout associer, tout dissocier), tout le catalogue des installations avec installation et désinstallation par sélection (plan et avertissements calculés par le site), réparation d'un moteur, annulation des installations, limites de génération image, nom, jobs simultanés, Worker principal, partage, jobs en cours et demandes de jobs, console, révocation et suppression avec ou sans désinstallation.
- Sécurité : désactivée par défaut. Le propriétaire l'active Worker par Worker dans Mes Workers (bouton « worker.exe : gestion complète » de la carte) ; la machine ne peut jamais l'activer elle-même. Chaque action passe par une demande signée de l'agent, reste limitée à CE Worker et est notifiée au propriétaire.

### Modifié
- worker.exe --gestion-complete [--onglet installations] ouvre directement cette fenêtre.

## Version 1.49.2 — 2026-10-11

**Un Worker installé en administrateur démarre de nouveau par la tâche planifiée**

### Modifié
- Quand la clé est illisible, logs\agent.log l'explique en clair et indique l'outil qui la répare.

### Corrigé
- Un Worker installé depuis une fenêtre administrateur ne démarrait plus par la tâche planifiée, l'entrée de démarrage de session ni « Rallumer » : sa clé (config\identity) n'était lisible qu'en administrateur, et l'agent s'arrêtait sur « Accès refusé » à chaque relance. « Rallumer le Worker » (MENU-WORKER > 12) détecte maintenant ce cas et, avec ton accord Windows, rend ce dossier à ton compte avant de lancer le Worker ; l'installateur le fait aussi dès l'installation. La clé n'est ni lue ni modifiée.

## Version 1.49.1 — 2026-10-11

**worker.exe affiche les questions des outils, et l'agent tient un journal hors service**

### Ajouté
- Journal de l'agent (logs\agent.log) quand le Worker démarre par la tâche planifiée ou par l'entrée de démarrage de session : sa sortie, ses arrêts (avec leur code) et la trace d'un éventuel plantage, consultables dans MENU-WORKER > 3 (Journaux), clé « agent ». Avant, cette sortie partait dans une console cachée et un Worker qui s'arrêtait au démarrage ne laissait aucune trace.

### Corrigé
- worker.exe : les questions posées par les outils (par exemple « Rallumer ce Worker maintenant ? [O/N] ») n'apparaissaient pas, la fenêtre semblait figée en attendant une réponse. Elles s'affichent maintenant avant chaque saisie, pour tous les outils.

### Technique
- Empreinte du paquet publié : `5a57b5afb553c0fd…` (dossier du paquet).

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
