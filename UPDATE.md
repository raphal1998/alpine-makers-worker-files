# Historique des mises à jour

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
