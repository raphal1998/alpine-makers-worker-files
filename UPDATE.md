# Historique des mises à jour

## Version 1.51.1 — 2026-10-30

**Installation Windows : un Bureau synchronisé par OneDrive ne bloque plus install_windows.bat**

### Corrigé
- Sur un PC neuf dont le Bureau est dans OneDrive (cas par défaut d'un compte Microsoft sous Windows 11), install_windows.bat s'arrêtait juste après la question du démarrage automatique : le dossier par défaut Bureau\alpine-makers-worker traverse un dossier OneDrive, marqué comme point d'analyse, et l'installateur le refusait comme un lien. Le Worker s'installe alors dans le dossier du compte (%USERPROFILE%\alpine-makers-worker), hors synchronisation ; un -InstallDir explicite dans OneDrive, un lien ou une jonction reste refusé, avec le dossier fautif et un exemple de dossier local.
- En cas d'échec, la fenêtre d'install_windows.bat reste ouverte (pause) avec le message d'erreur et l'emplacement du journal %TEMP%\alpine-makers-worker-installation.log (message et ligne seulement, sans le code d'association) ; elle se fermait aussitôt après un double-clic.

### Technique
- Empreinte du paquet publié : `5384fe430e860792…` (dossier du paquet).

## Version 1.51.0 — 2026-10-29

**Alpine Toile Creator : une image devient une toile 3D multicolore calculée par le Worker**

### Ajouté
- Alpine Toile Creator (outil toile-studio, menu Studio) : la carte des couleurs préparée dans le navigateur (PNG 8 bits, un index de couleur par pixel, 255 = vide) devient sur le Worker un modèle 3D de toile, une pièce par couleur plus la plaque de base, contours au pixel près, puis un 3MF multicolore avec les filaments attribués pour Bambu Studio / OrcaSlicer, un aperçu et un récapitulatif (dimensions, triangles, grammes estimés par couleur).
- Moteur toile-engine : Python isolé (.venv) aux paquets épinglés (numpy, Pillow), processeur seulement, installé depuis Mes Workers → Installations → Alpine Toile Creator.
- Module facultatif toile-detourage : séparation premier plan / arrière-plan par IA (BiRefNet lite en ONNX, licence MIT, ONNX Runtime sur le processeur), modèle épinglé vérifié par taille et SHA-256, téléchargé sur demande explicite seulement ; aucune image n'est envoyée à un service tiers.
- Capacité toile_engine_v1 : le site n'envoie un calcul de toile qu'à un agent qui l'annonce (1.51.0 ou plus) et un détourage qu'à un Worker dont le module est installé.

### Technique
- Empreinte du paquet publié : `e4d90d362b01bb69…` (dossier du paquet).

## Version 1.50.45 — 2026-10-27

### Technique
- Révision du paquet : contenu du paquet modifié sans changement de numéro de version.
- Empreinte du paquet publié : `228cc10eece358bc…` (dossier du paquet).

## Version 1.50.45 — 2026-10-27

**Préparer pour une CNC v2 : toutes les opérations FreeCAD CAM, un G-code par outil, aperçu et capacités détaillées**

### Ajouté
- Capacité freecad_cam_v2 : le moteur calcule le projet CAM v2 avec toutes les opérations réellement disponibles en console FreeCAD 1.1.3 (MillFace, Pocket, Profile, Drilling G81/G82/G83 traduits en G0/G1, Helix, Adaptive, Slot, Engrave, Vcarve, Deburr, Surface et Waterline avec OpenCAMLib, ThreadMilling avec diamètres et pas, bloc personnalisé borné), dressups Tags / DogboneII / Boundary, outils boule, torique, V, chanfrein, graveur et fraise à fileter.
- Politique de changement d'outil : pauses M0 (défaut), un fichier G-code par outil (<nom>_T<n>.nc, nommés et ordonnés, chacun complet et contrôlé) ou M6 réel seulement pour une machine à changeur hors GRBL ; manifeste avec empreinte SHA-256 par fichier, étapes manuelles et liste de ce que la simulation ne couvre pas.
- Aperçu : trajectoires rapides / coupe / plongées par opération et STL du modèle placé (borné à 8 Mio) pour la vue 3D du site, op « preview » (modèle, brut, origine) sans calcul.
- Capacités détaillées rapportées au site : version FreeCAD, OpenCAMLib et version, opérations importables et satisfaites, formes d'outils présentes, dressups, arguments réels de chaque post, politiques de changement d'outil, restrictions.

### Modifié
- État par opération (calculée, vide avec cause, erreur avec cause), forme d'outil réellement lue par FreeCAD, durée estimée par opération d'après longueurs et avances (trapèze si l'accélération de la machine est connue), progression réelle (opérations terminées / total) et annulation propre entre deux opérations.
- Contrôles de fabricabilité : outil trop court, porte-outil dans le brut, zones plus étroites que l'outil (écartées de la poche et signalées), dégagement hors course Z, parcours hors course, vérification G21/G17/G90, M3/M5 et absence de G43/G84/cycles bruts dans chaque fichier.

### Corrigé
- Diamètre d'outil ignoré : les propriétés du ToolBit (Diameter, CuttingEdgeHeight, Length, Flutes…) posées avant l'attache au document étaient écrasées par FreeCAD (Ø 5 mm par défaut quel que soit l'outil). Elles sont posées après l'attache et relues ; le moteur refuse un contrôleur dont le diamètre ne correspond pas.
- Un argument de post inconnu (ex. --no-tlo) tuait freecadcmd : la ligne d'options est filtrée par le parser réel du post ; --translate_drill explicite et --precision posés pour grbl.

### Technique
- Empreinte du paquet publié : `fa64c05ac315a76b…` (dossier du paquet).

## Version 1.50.44 — 2026-10-27

### Technique
- Révision du paquet : contenu du paquet modifié sans changement de numéro de version.
- Empreinte du paquet publié : `fd6e3540549ce453…` (dossier du paquet).

## Version 1.50.44 — 2026-10-27

### Technique
- Révision du paquet : contenu du paquet modifié sans changement de numéro de version.
- Empreinte du paquet publié : `a9bf8990852c87bd…` (dossier du paquet).

## Version 1.50.44 — 2026-10-26

**Broche d’une CNC : tr/min convertis sur l’échelle S de la carte, plafond S détecté**

### Ajouté
- Réglage « S maximal de la carte » dans Configurer (Mes équipements, CNC) : 0 = le $30 relu. Le Worker convertit les tr/min de la fiche (console, boutons M3/M4, programmes envoyés) sur cette échelle : Broche max 8000 tr/min et carte à S1000 → « M3 S8000 » part en « M3 S1000 ».

### Modifié
- Rapport GRBL : spindle_rpm (tr/min de la fiche) et spindle_scale (ratio, S maximal, plafond observé, $30) en plus de la valeur S brute ; CNC Studio et Mes machines affichent les tr/min et le S réel envoyé à la carte.

### Corrigé
- Broche bridée à 1000 tr/min malgré $30=8000 : certaines cartes (TwoTrees / LaserBox_E, GRBL 1.1h) plafonnent S à 1000 quel que soit $30. Après un M3/M4 S, le Worker compare la vitesse rapportée (FS, override 100 %) au S envoyé ; un plafond confirmé par trois lectures active la conversion et le signale avec le réglage à enregistrer.

### Technique
- Empreinte du paquet publié : `3a36f9f2bd2af2bc…` (dossier du paquet).

## Version 1.50.43 — 2026-10-26

**ARRÊT d’une CNC : origine de travail restaurée, Z compris**

### Corrigé
- ARRÊT d’une CNC : si la carte rapporte une autre position travail après le reset alors que la machine n’a pas bougé (carte qui redémarre et remet sa position machine à zéro, décalage volatil effacé), le Worker repose l’origine de travail par G10 L20 P1 aux valeurs d’avant, X, Y et Z compris,. Carte verrouillée après le reset : restauration au déblocage ($X) ; un homing l’annule.

### Technique
- Empreinte du paquet publié : `b237d6e7c384e653…` (dossier du paquet).

## Version 1.50.42 — 2026-10-26

**ARRÊT d’une CNC : l’usinage s’arrête, la position et l’origine de travail restent**

### Corrigé
- ARRÊT sur une CNC : la pause part tout de suite, puis le reset GRBL n’est émis qu’une fois l’arrêt du mouvement confirmé (Hold:0). Avant, le reset partait pendant la décélération : ALARM:3 (position perdue) et origine de travail à refaire. Seul l’usinage lancé s’arrête ; position machine et origine (G54) sont conservées. Homing, palpage et laser gardent le reset immédiat.

### Technique
- Empreinte du paquet publié : `585cc48e1b565c48…` (dossier du paquet).

## Version 1.50.41 — 2026-10-25

**Jobs et ressources, et un PC hôte qui ne se fige plus**

### Ajouté
- Jobs et ressources : dans Mes Workers, worker.exe (gestion complète) et le menu (entrée 26), règle les jobs simultanés et les limites de VRAM, RAM et processeur, par job et cumulées (Gio ou % du total). Le Worker refuse un job trop gros, fait attendre celui qui dépasse le cumul, plafonne RAM et processeur sous Windows et mesure son utilisation.
- Journal des ressources : logs\ressources.log reçoit toutes les 20 s la RAM, la mémoire engagée, le processeur, la VRAM et la RAM de chaque moteur (2 Mio au plus). Quand la RAM libre du PC devient rare et qu'aucun job ne tourne, ComfyUI et Hunyuan3D rendent leurs modèles.

### Modifié
- Moteurs IA et capture des caméras en priorité « inférieure à la normale » : le bureau et les services du PC gardent la main. ComfyUI reçoit --reserve-vram et Hunyuan3D une fraction de VRAM selon la limite ; un moteur lancé avant un changement est signalé « à redémarrer ».

### Corrigé
- PC hôte figé : ComfyUI démarre sans mémoire épinglée (il verrouillait jusqu'à 40 % de la RAM, non paginable) ; « comfyui_pinned_memory: true » dans config.json la remet. La VRAM par processus est lue sans PowerShell, y compris sur un Windows en français, et seulement pendant un job.

### Technique
- Empreinte du paquet publié : `20575f662a352079…` (dossier du paquet).

## Version 1.50.40 — 2026-10-25

**Licences et droits du Worker dans worker.exe et le menu**

### Ajouté
- Licences et droits : worker.exe (bouton « § Licences et droits ») et le menu (touche K) montrent, pour ton compte, l’état juridique des moteurs, modules et modèles de ce Worker, avec les états de la page Licences et justificatifs, un résumé chiffré et ce qu’il reste à faire (accepter les conditions, déclarer tes droits…). Lecture seule, connexion exigée.

### Technique
- Empreinte du paquet publié : `5f184dea8261e0ae…` (dossier du paquet).

## Version 1.50.39 — 2026-10-25

**Connexion : ne plus demander le code 2FA sur ce PC pendant 7 jours (worker.exe et menu)**

### Ajouté
- worker.exe et MENU-WORKER.bat : après un code 2FA réussi, case (ou question O/N) « Ne plus demander le code 2FA sur ce PC pendant 7 jours ». Le jeton rendu par le site est gardé chiffré par Windows (DPAPI, ton compte Windows seulement, accès fermé, nom de fichier sans le pseudo) et remplace ensuite le code ; pseudo et mot de passe restent demandés.
- « Oublier ce PC » : bouton de l’écran de connexion de worker.exe (ou case décochée), touche O du menu, question à la déconnexion. Un jeton échu ou retiré depuis le site (Gestion du compte → appareils de confiance) est effacé et le code redemandé. Le jeton ne passe ni par une ligne de commande ni par un journal.

## Version 1.50.38 — 2026-10-25

**Journal des jobs sur chaque Worker, résumé du tableau de bord dans worker.exe**

### Ajouté
- Journal des jobs sur chaque Worker : chaque job terminé (jobs du site, calculs de worker.exe, gravures et usinages) ajoute une ligne à data\journal-des-jobs.jsonl, au format de l’historique du site, sans secret ni chemin absolu. worker.exe → « Journal des jobs » : tri par date et heure, filtres jour, heure, outil et statut, aperçu de l’image, « Voir », suppression confirmée.
- Accueil de worker.exe : « État du Worker » résume le tableau de bord en dix tuiles (agent et mise à jour, session, liaison au site, jobs, partage, RAM, disque, processeur, GPU et VRAM, pilote NVIDIA et CUDA), relues toutes les 30 s. Menu du Worker : touche J pour les 20 derniers jobs.

### Modifié
- worker.exe : dans une fenêtre basse, la colonne de gauche se resserre (logo masqué) et « Tableau de bord » ne passe plus sous les liens du bas.

### Technique
- Empreinte du paquet publié : `a303c8996ccbe680…` (dossier du paquet).

## Version 1.50.37 — 2026-10-25

**Versions installées remontées au site : mises à jour des moteurs et modules signalées**

### Ajouté
- Le Worker communique au site la version réellement installée de chaque moteur et module (révision git, version de PrintGuard et d’OrcaSlicer, bibliothèques des moteurs laser, CNC et surveillance IA, FreeCAD, CUDA), sans chemin ni secret. Le site prévient le propriétaire et les co-propriétaires quand une version validée plus récente existe ; rien n’est mis à jour sans leur clic.

### Modifié
- PrintGuard et OrcaSlicer : l’installateur consigne la version posée et la garde si une nouvelle installation échoue ; pour une copie plus ancienne, elle est relue dans le paquet PrintGuard ou dans l’exécutable d’OrcaSlicer.

### Technique
- Empreinte du paquet publié : `aa72719e08098786…` (dossier du paquet).

## Version 1.50.36 — 2026-10-23

**Menu du Worker connecté comme worker.exe, tableau de bord complet**

### Ajouté
- Menu du Worker (MENU-WORKER.bat) : connexion du propriétaire comme worker.exe (2FA, navigateur/téléphone, co-propriétaires et leurs droits, verrou après 3 échecs, notifications), mode hors ligne sûr. Tableau de bord complet dans le menu (touche T) et dans worker.exe : jobs, file, partage, RAM, disque, GPU/VRAM, CUDA et pilote, place occupée, composants, IA locales, comptes IA.

### Corrigé
- Sécurité : la mise à jour, la réparation, l'arrêt des jobs et le partage demandés depuis le PC exigent la session du propriétaire. Mot de passe, code et jeton passent par un fichier à accès fermé, lu une fois puis effacé, jamais par la ligne de commande (le jeton de worker.exe n'apparaît plus dans la ligne de commande de Python).

### Technique
- Empreinte du paquet publié : `6c1705592f6045c7…` (dossier du paquet).

## Version 1.50.35 — 2026-10-23

**Installations : la dernière version validée est installée directement (PrintGuard 2.6.x)**

### Ajouté
- Installations : un moteur ou un module reçoit directement la dernière version validée par le site (PrintGuard 2.6.x au lieu de 2.4.0). L'agent la revérifie avant de l'installer : même dépôt et même archive officiels, taille et SHA-256 publiés, version au moins égale à sa version embarquée.

### Corrigé
- PrintGuard : « Installer » sur une copie plus ancienne passe directement à la version validée, et « Mettre à jour » n'installe plus la 2.4.0. Un paquet sans ses modèles ou annonçant une autre version est refusé, la version en place est conservée. Le mode TEST accepte PrintGuard 2.4.0 ou ultérieur.

### Technique
- Empreinte du paquet publié : `fadafcd48aeecd18…` (dossier du paquet).

## Version 1.50.34 — 2026-10-23

**Préparer pour une CNC : la cause d'un échec FreeCAD est enfin transmise**

### Modifié
- Le moteur CNC (freecad_cam.py) s'exécute aussi quand FreeCADCmd le charge comme module au lieu de le lancer : la lecture des capacités ne se termine plus en une seconde sans rien calculer.

### Corrigé
- Convertisseur 3D et Préparer pour une CNC : quand FreeCAD s'arrête sans message, le Worker transmet maintenant la cause (démarrage impossible, plantage, atelier CAM absent, moteur non exécuté), son code de sortie et la fin de sa sortie d'erreur, sans le nom du compte Windows. Le site en tire la marche à suivre.

### Technique
- Empreinte du paquet publié : `14c4cd5ffdbaf967…` (dossier du paquet).

## Version 1.50.33 — 2026-10-23

**Menu Worker et worker.exe : arrêter, éteindre et réparer vraiment tout**

### Ajouté
- Réparer (24) fait d'abord un diagnostic profond, même agent arrêté ou figé : agent figé ou en doublon, processus restés seuls, ports pris par un autre programme, fichiers d'état abîmés, journal des jobs, verrous, disque, moteurs absents, et les erreurs qui reviennent dans les journaux, avec une piste. Il répare ce qui est sûr (fichiers déplacés et sauvegardés, jamais effacés) et dit le reste.
- Touche D du menu : quelle entrée pour quel problème. Après un échec, le menu et worker.exe indiquent l'entrée à utiliser ensuite. Moteurs (9) : choix 4 pour forcer l'arrêt d'un moteur bloqué.

### Corrigé
- Arrêter tous les jobs (21) arrête aussi les calculs lancés depuis worker.exe (convertisseur, préparation CNC FreeCAD, Labo image, tranchage) et coupe, après 20 secondes, l'arbre de processus de ce qui résiste (FreeCAD et ses enfants), seulement ce que l'agent a lancé.
- Éteindre (11) arrête tous les processus du Worker (agent resté seul, moteurs, calculs, installateurs, orphelins) en trois passes et vérifie qu'il n'en reste aucun ; Rallumer (12) et Redémarrer (10) nettoient les restes et attendent le verrou du superviseur avant de relancer.
- ARRÊT CNC exécuté immédiatement, même pendant un homing ou un palpage ; flux coupé avant la pause et le reset ; usinage suivi jusqu'à l'arrêt réel de la machine ; nombres lus comme un contrôleur (« X0. », « .5 », « Z-3.5. » réparé avec avertissement).

### Technique
- Empreinte du paquet publié : `97fc5307c03abb20…` (dossier du paquet).

## Version 1.50.32 — 2026-10-23

### Technique
- Révision du paquet : contenu du paquet modifié sans changement de numéro de version.
- Empreinte du paquet publié : `3faea4dd898d8a06…` (dossier du paquet).

## Version 1.50.32 — 2026-10-23

### Technique
- Révision du paquet : contenu du paquet modifié sans changement de numéro de version.
- Empreinte du paquet publié : `5d54cd64cf5cfb77…` (dossier du paquet).

## Version 1.50.32 — 2026-10-20

**Impression depuis le site : nom lisible sur l'écran de la P1S**

### Modifié
- Le fichier envoyé sur la carte microSD porte le nom du travail (ex. « MERDE1_6.gcode.3mf ») au lieu d'un identifiant technique : c'est ce nom que la P1S affiche pendant l'impression.

### Technique
- Empreinte du paquet publié : `5aadcf19d9eba313…` (dossier du paquet).

## Version 1.50.31 — 2026-10-20

**Impression depuis le site : la P1S démarre enfin le fichier envoyé**

### Modifié
- Le nom du fichier est aussi transmis dans le champ « file » de la commande d'impression, comme le font les outils de référence.

### Corrigé
- Le fichier tranché est déposé à la racine de la carte microSD (et non plus dans /cache) : la P1S recopiait le fichier sur lui-même, restait en IDLE sans message et signalait l'erreur 0500C010.

### Technique
- Empreinte du paquet publié : `d4d984093bd3e946…` (dossier du paquet).

## Version 1.50.30 — 2026-10-20

**Connexion assistée par navigateur ou téléphone, avec 2FA obligatoire**

### Ajouté
- Option volontaire dans Gestion du compte, activable avec accord, mot de passe actuel et code 2FA.
- Connexion de worker.exe depuis le navigateur du PC ou un téléphone : QR code temporaire, code de comparaison et confirmation explicite.

### Modifié
- Les identifiants restent gérés par le navigateur, enregistrés manuellement par leur titulaire. Le déverrouillage biométrique utilise les fonctions compatibles de l'appareil.
- La connexion assistée exige toujours le mot de passe et la 2FA ; worker.exe reçoit uniquement sa session après validation des droits actuels.

### Technique
- Empreinte du paquet publié : `0bf33831bc3bcf8a…` (dossier du paquet).

## Version 1.50.29 — 2026-10-20

**Model Studio Desktop : réglages du site, filaments et imprimante choisie**

### Ajouté
- Les 160 paramètres du profil de processus du site, avec catégories, recherche et fenêtre de réglages dédiée.
- Gestion des filaments du projet à partir des 48 types du site : couleurs, attribution aux pièces, synchronisation AMS et correspondance des bacs à l'impression.
- Sélection explicite d'une imprimante de Mes équipements dans Model Studio, avec lecture de sa buse et de ses filaments.

### Modifié
- Le catalogue natif est construit depuis les sources du site ; les projets conservent les filaments, la machine, la buse et le plateau.

### Corrigé
- Deux filaments de même matière restent distincts au tranchage et dans le projet enregistré.
- Le lancement conserve l'identifiant de la machine choisie et les bacs AMS, via les autorisations et validations d'impression du site.

## Version 1.50.28 — 2026-10-19

**Desktop : états ON/OFF des caméras et machines corrigés**

### Modifié
- La même lecture de l'état est utilisée dans Mes équipements et la configuration des caméras, sans modifier les contrôles de permission.

### Corrigé
- Les états activés 0/1 du site sont reconnus comme les booléens locaux : les boutons affichent ON en vert et le clic demande la bonne action.
- Une image reçue après la désactivation ne réapparaît plus dans une caméra OFF.

## Version 1.50.27 — 2026-10-19

**Worker : salle de contrôle complète depuis le menu principal**

### Modifié
- Une fenêtre Desktop déjà ouverte est réutilisée et affichée sur Salle de contrôle. L'ancienne vue locale reste accessible sous le libellé lecture seule.

### Corrigé
- Le bouton Salle de contrôle du menu principal et le raccourci --salle-de-controle ouvrent la vue complète avec les commandes de chaque caméra.

## Version 1.50.26 — 2026-10-19

**Desktop : boutons des caméras alignés sur le site**

### Ajouté
- Vue d’ensemble dédiée à la caméra et sa machine, avec image et télémétrie actualisées.

### Modifié
- Cinq boutons ronds dans l’ordre du site : Caméra, Machine, Config. caméra, Config. machine, Vue d’ensemble. Agrandir et Plein écran se trouvent sur l’image.

### Corrigé
- Config. machine ouvre la gestion de la machine associée ; les commandes caméra et machine restent indépendantes.

## Version 1.50.25 — 2026-10-19

**Desktop : galeries des studios, équipements et gestion des machines**

### Ajouté
- Mes équipements dans Alpine Makers Desktop : connexions, configuration, association caméra / machine, ordre, export et import de profils.
- Gestion imprimante, graveuse laser et CNC : cinq onglets natifs par famille, avec les validations et permissions des routes du site.
- Salle de contrôle intégrée : sélection des caméras, colonnes, filtre connecté, commandes indépendantes caméra / machine, configuration et agrandissement.

### Modifié
- Bibliothèques en grille par défaut, aperçu 3D interactif STL / OBJ / 3MF, zoom, vues, grille, arêtes, couleur et fond.
- Les commandes physiques conservent les confirmations, bornes et listes de sécurité du site ; les arrêts restent disponibles pendant une demande en cours.

### Corrigé
- Les sélecteurs des studios affichent des vignettes et un aperçu du fichier sélectionné ; les images WEBP sont prévisualisées en PNG sans modifier l'original.
- Le cadrage 3D tient compte des fenêtres étroites et les pages de cent fichiers chargent tous leurs aperçus.

## Version 1.50.24 — 2026-10-16

**worker.exe : partage par compte plus fin (fonctions, modèles, styles prêtés)**

### Ajouté
- Onglet Partage de worker.exe : bouton « Régler l'accès du compte choisi… » pour ouvrir à un compte précis Génération et/ou Alpine Labo image, Conversion et/ou Préparer pour une CNC, les modèles d'IA image et d'IA 3D (tous ou une sélection) et prêter ses styles privés (aucun, tous ou une sélection) ; colonne « Fonctions et styles » dans la liste des comptes.

### Technique
- Empreinte du paquet publié : `79c0d247cd556dfb…` (dossier du paquet).

## Version 1.50.23 — 2026-10-15

**worker.exe : la connexion du propriétaire est de nouveau reconnue (Gestion complète, Alpine Makers Desktop)**

### Corrigé
- Après la connexion dans worker.exe, l'agent ne recopiait pas le jeton de session, le rôle ni les droits rendus par le site : worker.exe se croyait « non connecté » et le site refusait la Gestion complète et la Bibliothèque de Desktop (« session absente »). Ils sont maintenant rendus à worker.exe.

### Technique
- Empreinte du paquet publié : `e9474ef8e0146e49…` (dossier du paquet).

## Version 1.50.22 — 2026-10-15

**Alpine Makers Desktop : outils natifs de worker.exe (Model Studio, Laser Studio, CNC Studio, Labo image, Convertisseur) et Bibliothèque du compte**

### Ajouté
- worker.exe : fenêtre « Alpine Makers Desktop » (bouton du Gestionnaire, option --desktop [--outil <id>]) avec la navigation Salle de contrôle / Alpine Studio / Bibliothèque ; un outil pas encore intégré affiche une page « bientôt disponible ». Fermer la fenêtre n’arrête rien.
- Agent : action locale « desktop » (desktop_actions.py) qui sert tous les outils natifs — paramètres validés, gros paramètres par fichier d’arguments lu une fois puis effacé, gros résultats par runtime/desktop/out, tâches longues suivies (progression, annulation).
- Bibliothèque du compte depuis worker.exe (desktop_library.py, capacité desktop_library_v1) : lister (privée, partagée, reçue ; 3D, laser, CNC, image ; pages, filtres, recherche), télécharger dans le cache de l’agent, importer un fichier déposé par worker.exe, modifier la fiche (collection, favori, étiquettes…), copier un fichier reçu, mettre en corbeille (confirmation obligatoire). Routes signées self/library du site, session de connexion du propriétaire exigée.
- Alpine Model Studio natif (desktop_model_studio.py) : aperçu 3D, primitives, texte 3D, pièce paramétrique FreeCAD, transformations, réseaux, réparation et analyse de maillage, tranchage OrcaSlicer sur ce Worker (profils, réglages avancés, multi-filaments), export STL/3MF ; impression par la route signée self/model-studio/print (vue d’impression du site, confirmation explicite) et suivi du lancement.
- Alpine Laser Studio natif (desktop_laser_studio.py) : projets .alsproj, import SVG, DXF et images, calques et réglages de coupe, matériaux, calculs par le moteur laser du Worker (aperçu, G-code, simulation, cadrage laser coupé), export ; graveuse par la route signée self/laser-studio → /api/equipment/<id>/grbl (check-list nommée, confirmation, ni test laser ni jog).
- Alpine CNC Studio natif (desktop_cnc_studio.py) : analyse et simulation du G-code par le moteur CNC du Worker, aperçu dessus/face/côté/3D, projets, outils et matériaux, CAM FreeCAD sur ce Worker, supervision ; commandes GRBL par les routes signées self/cnc-studio (check-list, confirmations, pilotage manuel armé 5 min).
- Alpine Labo image natif (desktop_image_lab.py) : les 15 outils du Labo exécutés par les moteurs installés sur ce PC (ComfyUI, cartes, visages), mêmes réglages, précontrôles et lanceurs que les jobs du site, comparaison avant/après, enregistrement dans la Bibliothèque image.
- Convertisseur 2D/3D natif (desktop_converter.py, capacité desktop_converter_v1) : conversion de format FreeCAD, réparation et analyse de maillage, « Préparer pour une CNC » en 10 étapes avec G-code retenu tant qu’un avertissement n’est pas acquitté ; projets et préréglages CAM du site par la route signée self/converter.
- Bibliothèque : quatre vues natives (Fichiers 3D, Fichiers laser, Fichiers CNC, Bibliothèque image) avec liste ou grille, filtres, tracé G-code et rendu STL/OBJ locaux, fiche, déplacer, partager, corbeille confirmée, vue hors ligne des fichiers déjà téléchargés, et « Ouvrir dans » le Studio correspondant ; nouvelle action agent « thumbnails » (aperçus par lot).

### Modifié
- scripts/build_worker_exe.py compile automatiquement chaque worker_manager/Desktop*.cs et accepte --out pour une construction d’essai non signée.

### Corrigé
- Socle Desktop : les actions core/task, core/tasks et core/cancel ne bloquent plus l’agent quand aucune tâche n’existe encore (verrou repris en le tenant).

### Technique
- Empreinte du paquet publié : `ba45ef8902dc388e…` (dossier du paquet).

## Version 1.50.21 — 2026-10-15

**Tranchage OrcaSlicer : profils de toutes les marques, filament TPU for AMS, accents des messages (pont 19.28)**

### Ajouté
- Pont OrcaSlicer 19.28 : le tranchage accepte le profil d'imprimante OrcaSlicer de n'importe quelle marque présente dans l'OrcaSlicer du Worker (BBL, Creality, Prusa, Anycubic, Elegoo, Voron, Qidi…) ; process et filament sont choisis dans le dossier de ce vendeur (puis OrcaFilamentLibrary) selon compatible_printers ou compatible_printers_condition, chaîne d'héritage comprise.
- Liste des imprimantes que l'OrcaSlicer du Worker sait trancher (nouvelle demande en lecture seule « Profils du Worker » du panneau de tranchage).

### Modifié
- Le panneau de tranchage préremplit le profil d'imprimante OrcaSlicer depuis la fiche de Mes équipements (modèle, buse) et le résultat indique l'imprimante utilisée.

### Corrigé
- Le type de filament « TPU-AMS » (AMS, Alpine Model Studio) trouve le profil « Bambu TPU for AMS » sur la Bambu Lab P1S en buse 0.4 et 0.8 ; table d'alias (TPU-AMS, TPU for AMS, PLA-S, PLA Basic, PETG HF, PA-CF…) puis repli sur un profil Generic de la même famille ; sinon refus qui liste les types disponibles pour l'imprimante.
- Messages du pont en français sans mojibake (« Ã© », « Â« ») : le script est livré en UTF-8 avec BOM, que Windows PowerShell 5.1 exige pour ne pas le lire en cp1252.

## Version 1.50.20 — 2026-10-15

**Sécurité : jeton de lecture du lien MJPEG local, Gestion complète liée à la connexion de worker.exe**

### Ajouté
- Lien MJPEG local des caméras (relais loopback, composant 1.3.0) : chaque source exige un jeton de lecture aléatoire passé en paramètre « token » de l'adresse, conservé dans runtime/camera-relay du Worker (droits du dossier d'installation) et stable d'un redémarrage à l'autre. Le « Lien local » copié depuis Mes équipements le contient : OBS, LightBurn et PrintGuard continuent de fonctionner avec ce lien ; une adresse sans jeton reçoit 403. Le Worker complète lui-même ses liens sans jeton (webcam par lien, caméra qui lit le lien d'une autre, PrintGuard déjà configuré).

### Modifié
- Le composant relais posé par une version plus ancienne est recopié automatiquement au démarrage de l'agent, sans relancer « Installer ».

### Corrigé
- Un processus local quelconque ne lit plus les caméras du Worker sur 127.0.0.1 sans le lien copié depuis le site.
- Gestion complète depuis worker.exe : sans la session de connexion de worker.exe, le site refuse désormais la demande (WORKER_EXE_SESSION_REQUIRED) au lieu de la traiter comme venant du propriétaire ; la compatibilité reste pour les Workers d'avant 1.50.16.

## Version 1.50.19 — 2026-10-15

**worker.exe : Salle de contrôle native, caméras et machines du Worker sans passer par le site**

### Ajouté
- Bouton « Salle de contrôle » de worker.exe (ou worker.exe --salle-de-controle) : mur des caméras de ce Worker lues directement sur le relais MJPEG local de l'agent (127.0.0.1, adresse et jeton rendus par l'agent), état honnête par tuile (en direct, image figée, déconnectée, relais arrêté, désactivée), colonnes Auto/1 à 4, cadence d'affichage maximale réglable, agrandir une caméra, plein écran, caméras masquées, préférences gardées sur le PC.
- Panneau des machines (imprimantes, graveuses, CNC) : liaison, dernière télémétrie lue par l'agent, caméra reliée quand l'agent la connaît (surveillance incendie, PrintGuard). Aucune commande machine, aucun relais démarré ; fermer la fenêtre n'arrête rien ; réservé au propriétaire.
- Action locale « control-room » de l'agent (worker_agent/control_room.py) : lecture seule, sans paramètre, hors ligne, sans secret ; ce qui n'existe que sur le site (noms des équipements, liaison caméra ↔ machine de Mes équipements) est dit absent.

## Version 1.50.18 — 2026-10-15

**Tranchage OrcaSlicer : profil d'impression adapté à la buse choisie (pont 19.27)**

### Modifié
- Le code de sortie d'OrcaSlicer est lu correctement et, en cas d'échec, les lignes d'erreur du journal d'OrcaSlicer sont jointes au message.

### Corrigé
- Tranchage refusé par OrcaSlicer (« OrcaSlicer n'a produit aucun 3MF … Slic3r::CLI::run found error ») avec une buse de 0,2, 0,6 ou 0,8 mm : le pont choisissait toujours un profil d'impression prévu pour la buse 0,4. Il prend maintenant le profil Bambu déclaré compatible avec la P1S et la buse choisie, le plus proche de la hauteur de couche demandée, et refuse clairement s'il n'en existe aucun.

## Version 1.50.17 — 2026-10-15

### Technique
- Révision du paquet : contenu du paquet modifié sans changement de numéro de version.
- Empreinte du paquet publié : `d0615850b9e69136…` (dossier du paquet).

## Version 1.50.17 — 2026-10-15

### Technique
- Révision du paquet : contenu du paquet modifié sans changement de numéro de version.
- Empreinte du paquet publié : `1e57405d5f7b18ce…` (dossier du paquet).

## Version 1.50.17 — 2026-10-15

**Paramètres GRBL du contrôleur : modification contrôlée depuis Mes équipements**

### Ajouté
- Écriture groupée des réglages GRBL ($n=v) demandée depuis Mes équipements → Paramètres GRBL : machine au repos (Idle ou Alarm) et aucun travail en cours, $$ relu juste avant, seuls les réglages que la carte rapporte (réglages constructeur compris), paramètres critiques ($0-$6, $20-$23, $32, pas par mm, courses, réglages hors catalogue) acquittés un à un, écriture ligne par ligne avec attente du ok ou de l'error de la carte, arrêt au premier refus, relecture de $$ et vérification de chaque valeur. Jamais $RST, $X, $H, $N, $SLP ni mouvement. Capacité equipment_grbl_firmware_v1.

### Technique
- Empreinte du paquet publié : `2b68e2c76b00db8f…` (dossier du paquet).

## Version 1.50.16 — 2026-10-14

**worker.exe : co-propriétaire avec équipements choisis, connexion autorisée et droits précis**

### Ajouté
- Onglet Partage : pour chaque co-propriétaire, choix précis des équipements partagés (tous ou une sélection), case « Peut se connecter à worker.exe » et boîte des droits (état et journaux, installations, moteurs, jobs, équipements, maintenance, console). Un co-propriétaire connecté voit un menu limité à ses droits ; partage, co-propriété, révocation, désinstallation complète et extinction du PC restent au propriétaire. Chaque action de la Gestion complète porte le jeton de session remis à la connexion : le site vérifie les droits, jamais la machine.

### Technique
- Empreinte du paquet publié : `911652ddfcacf299…` (dossier du paquet).

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
