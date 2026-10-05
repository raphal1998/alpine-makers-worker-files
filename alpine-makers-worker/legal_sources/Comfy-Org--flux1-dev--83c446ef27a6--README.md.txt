---
license: other
license_name: flux-1-dev-non-commercial-license
license_link: https://huggingface.co/black-forest-labs/FLUX.1-dev/resolve/main/LICENSE.md
tags:
- comfyui
- diffusion-single-file
base_model:
- black-forest-labs/FLUX.1-dev
- black-forest-labs/FLUX.1-Canny-dev
- black-forest-labs/FLUX.1-Depth-dev
- black-forest-labs/FLUX.1-Fill-dev
---

# FLUX.1-dev

Repackaged model files for ComfyUI.

Original model repository: https://huggingface.co/black-forest-labs/FLUX.1-dev

Place the files in the following folders:

```
📂 ComfyUI/
├── 📂 models/
│   ├── 📂 checkpoints/
│   │   ├── flux1-dev-fp8.safetensors
│   │   └── flux1-dev.safetensors
│   ├── 📂 diffusion_models/
│   │   ├── flux1-canny-dev.safetensors
│   │   ├── flux1-depth-dev-nvfp4.safetensors
│   │   └── flux1-fill-dev.safetensors
│   ├── 📂 loras/
│   │   └── flux1-depth-dev-lora.safetensors
```

---

This is a smaller checkpoint for flux1-dev that will work better for ComfyUI users with less VRAM (under 24gb). 

The two text encoders used by Flux are already included in this one safetensor.

Use it with the `Load Checkpoint` node in ComfyUI.

## Workflows

<table>
<thead>
<tr><th align="center" valign="middle" style="text-align:center;vertical-align:middle">Workflow</th><th colspan="2" align="center" valign="middle" style="text-align:center;vertical-align:middle">Thumb</th></tr>
</thead>
<tbody>
<tr><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux_fill_inpaint_example.json">Flux.1 Inpaint</a></td><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux_fill_inpaint_example.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/flux_fill_inpaint_example-2.webp" width="200" height="200" alt="Flux.1 Inpaint"></a><div align="center">Before</div></td><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux_fill_inpaint_example.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/flux_fill_inpaint_example-1.webp" width="200" height="200" alt="Flux.1 Inpaint"></a><div align="center">After</div></td></tr>
<tr><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux1_dev_uso_reference_image_gen.json">Flux.1 Dev USO Reference Image Generation</a></td><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux1_dev_uso_reference_image_gen.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/flux1_dev_uso_reference_image_gen-2.webp" width="200" height="200" alt="Flux.1 Dev USO Reference Image Generation"></a><div align="center">Before</div></td><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux1_dev_uso_reference_image_gen.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/flux1_dev_uso_reference_image_gen-1.webp" width="200" height="200" alt="Flux.1 Dev USO Reference Image Generation"></a><div align="center">After</div></td></tr>
<tr><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux_fill_outpaint_example.json">Flux.1 Outpaint</a></td><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux_fill_outpaint_example.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/flux_fill_outpaint_example-2.webp" width="200" height="200" alt="Flux.1 Outpaint"></a><div align="center">Before</div></td><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux_fill_outpaint_example.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/flux_fill_outpaint_example-1.webp" width="200" height="200" alt="Flux.1 Outpaint"></a><div align="center">After</div></td></tr>
<tr><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux_dev_checkpoint_example.json">Flux.1 Dev fp8: Text to Image</a></td><td colspan="2" align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux_dev_checkpoint_example.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/flux_dev_checkpoint_example-1.webp" width="200" height="200" alt="Flux.1 Dev fp8: Text to Image"></a></td></tr>
<tr><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux_dev_full_text_to_image.json">Flux.1 Dev: Text to Image</a></td><td colspan="2" align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux_dev_full_text_to_image.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/flux_dev_full_text_to_image-1.webp" width="200" height="200" alt="Flux.1 Dev: Text to Image"></a></td></tr>
<tr><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux_canny_model_example.json">Flux.1 Canny Model</a></td><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux_canny_model_example.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/flux_canny_model_example-2.webp" width="200" height="200" alt="Flux.1 Canny Model"></a><div align="center">Before</div></td><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux_canny_model_example.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/flux_canny_model_example-1.webp" width="200" height="200" alt="Flux.1 Canny Model"></a><div align="center">After</div></td></tr>
<tr><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux_depth_lora_example.json">Flux.1 Depth Lora</a></td><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux_depth_lora_example.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/flux_depth_lora_example-2.webp" width="200" height="200" alt="Flux.1 Depth Lora"></a><div align="center">Before</div></td><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux_depth_lora_example.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/flux_depth_lora_example-1.webp" width="200" height="200" alt="Flux.1 Depth Lora"></a><div align="center">After</div></td></tr>
<tr><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux_redux_model_example.json">Flux.1 Redux Model</a></td><td colspan="2" align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/flux_redux_model_example.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/flux_redux_model_example-1.webp" width="200" height="200" alt="Flux.1 Redux Model"></a></td></tr>
</tbody>
</table>
