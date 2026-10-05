---
license: apache-2.0
tags:
- comfyui
- diffusion-single-file
base_model:
- black-forest-labs/FLUX.1-schnell
---

# FLUX.1-schnell

Repackaged model files for ComfyUI.

Original model repository: https://huggingface.co/black-forest-labs/FLUX.1-schnell

Place the files in the following folders:

```
📂 ComfyUI/
├── 📂 models/
│   ├── 📂 checkpoints/
│   │   ├── flux1-schnell-fp8.safetensors
│   │   └── flux1-schnell.safetensors
```

---

This flux1-schnell model has weights in FP8, which makes running in ComfyUI much faster and use less memory.

## Workflows

<table>
<thead>
<tr><th align="center" valign="middle" style="text-align:center;vertical-align:middle">Workflow</th><th colspan="2" align="center" valign="middle" style="text-align:center;vertical-align:middle">Thumb</th></tr>
</thead>
<tbody>
<tr><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux_schnell.json">Flux.1 Schnell FP8</a></td><td colspan="2" align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux_schnell.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/flux_schnell-1.webp" width="200" height="200" alt="Flux.1 Schnell FP8"></a></td></tr>
<tr><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux_schnell_full_text_to_image.json">Flux.1 Schnell Full: Text to Image</a></td><td colspan="2" align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux_schnell_full_text_to_image.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/flux_schnell_full_text_to_image-1.webp" width="200" height="200" alt="Flux.1 Schnell Full: Text to Image"></a></td></tr>
</tbody>
</table>
