# Historique des mises à jour

## Version 1.41.1 — 2026-10-05

**Stable Diffusion 2 — 768 reconnu dans le fichier**

### Corrigé
- Un checkpoint Stable Diffusion 2 en prédiction v (768 px) était relevé « 512 » : l’agent lit désormais le même tenseur que ComfyUI (norm1.bias du dernier bloc), vérifié sur les fichiers officiels 2.1 Base et 2.1.

### Technique
- Empreinte du paquet publié : `34b33628210daedb…` (dossier du paquet).
