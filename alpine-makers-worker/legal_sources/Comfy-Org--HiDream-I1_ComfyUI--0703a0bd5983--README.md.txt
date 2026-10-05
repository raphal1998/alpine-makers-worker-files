---
license: mit
tags:
- comfyui
- diffusion-single-file
base_model:
- HiDream-ai/HiDream-I1-Dev
- HiDream-ai/HiDream-I1-Fast
- HiDream-ai/HiDream-I1-Full
- HiDream-ai/HiDream-E1-1
- HiDream-ai/HiDream-E1-Full
---

# HiDream-I1

Repackaged model files for ComfyUI.

Original model repository: 

- https://huggingface.co/HiDream-ai/HiDream-E1-1
- https://huggingface.co/HiDream-ai/HiDream-E1-Full
- https://huggingface.co/HiDream-ai/HiDream-I1-Dev
- https://huggingface.co/HiDream-ai/HiDream-I1-Fast
- https://huggingface.co/HiDream-ai/HiDream-I1-Full

Place the files in the following folders:

```
📂 ComfyUI/
├── 📂 models/
│   ├── 📂 diffusion_models/
│   │   ├── hidream_e1_1_bf16.safetensors
│   │   ├── hidream_e1_full_bf16.safetensors
│   │   ├── hidream_i1_dev_bf16.safetensors
│   │   ├── hidream_i1_dev_fp8.safetensors
│   │   ├── hidream_i1_fast_bf16.safetensors
│   │   ├── hidream_i1_fast_fp8.safetensors
│   │   ├── hidream_i1_full_fp16.safetensors
│   │   └── hidream_i1_full_fp8.safetensors
│   ├── 📂 text_encoders/
│   │   ├── clip_g_hidream.safetensors
│   │   ├── clip_l_hidream.safetensors
│   │   ├── llama_3.1_8b_instruct_fp8_scaled.safetensors
│   │   └── t5xxl_fp8_e4m3fn_scaled.safetensors
│   ├── 📂 vae/
│   │   └── ae.safetensors
```

---

For how to use this in ComfyUI see: https://comfyanonymous.github.io/ComfyUI_examples/hidream/

## Workflows

<table>
<thead>
<tr><th align="center" valign="middle" style="text-align:center;vertical-align:middle">Workflow</th><th colspan="2" align="center" valign="middle" style="text-align:center;vertical-align:middle">Thumb</th></tr>
</thead>
<tbody>
<tr><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/hidream_e1_1.json">HiDream E1.1 Image Editing</a></td><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/hidream_e1_1.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/hidream_e1_1-2.webp" width="200" height="200" alt="HiDream E1.1 Image Editing"></a><div align="center">Before</div></td><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/hidream_e1_1.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/hidream_e1_1-1.webp" width="200" height="200" alt="HiDream E1.1 Image Editing"></a><div align="center">After</div></td></tr>
<tr><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/hidream_i1_dev.json">HiDream I1 Dev</a></td><td colspan="2" align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/hidream_i1_dev.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/hidream_i1_dev-1.webp" width="200" height="200" alt="HiDream I1 Dev"></a></td></tr>
<tr><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/hidream_e1_full.json">HiDream E1 Image Edit</a></td><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/hidream_e1_full.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/hidream_e1_full-2.webp" width="200" height="200" alt="HiDream E1 Image Edit"></a><div align="center">Before</div></td><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/hidream_e1_full.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/hidream_e1_full-1.webp" width="200" height="200" alt="HiDream E1 Image Edit"></a><div align="center">After</div></td></tr>
<tr><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/hidream_i1_fast.json">HiDream I1 Fast</a></td><td colspan="2" align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/hidream_i1_fast.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/hidream_i1_fast-1.webp" width="200" height="200" alt="HiDream I1 Fast"></a></td></tr>
<tr><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/hidream_i1_full.json">HiDream I1 Full</a></td><td colspan="2" align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/hidream_i1_full.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/hidream_i1_full-1.webp" width="200" height="200" alt="HiDream I1 Full"></a></td></tr>
</tbody>
</table>
