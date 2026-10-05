# Historique des mises à jour

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
