---
license: creativeml-openrail-m
language:
- en
library_name: diffusers
pipeline_tag: text-to-image
base_model: stabilityai/stable-diffusion-xl-base-1.0
tags:
- stable-diffusion
- stable-diffusion-xl
- sdxl
- sdxl-lightning
- text-to-image
- photorealistic
- fast
- low-step
- juggernaut
- rundiffusion
- kandooai
---

<div align="center">

<a href="https://www.rundiffusion.com/juggernaut?utm_source=huggingface&utm_medium=model_card&utm_campaign=juggernaut_xl_lightning&utm_content=header_banner">
  <img src="https://huggingface.co/RunDiffusion/Juggernaut-XL-Lightning/resolve/main/assets/Juggernaut_Banner.webp" alt="Juggernaut — by RunDiffusion" />
</a>

<h1>Juggernaut XL Lightning by RunDiffusion</h1>

<p><i>Juggernaut quality at SDXL Lightning speed — 5–7 steps, photo-grade output.</i></p>

<p>
  <a href="https://app.rundiffusion.com/login?utm_source=huggingface&utm_medium=model_card&utm_campaign=juggernaut_xl_lightning&utm_content=hero_cta"><img alt="Try Juggernaut Lightning — Free" src="https://img.shields.io/badge/%E2%96%B6%20Try%20Juggernaut%20Lightning%20%E2%80%94%20Free-7C3AED?style=for-the-badge&labelColor=7C3AED"></a>
</p>

<p>
  <a href="https://www.rundiffusion.com/prompting?utm_source=huggingface&utm_medium=model_card&utm_campaign=juggernaut_xl_lightning&utm_content=prompting_resource"><img alt="Prompting Guides" src="https://img.shields.io/badge/%F0%9F%93%96%20Prompting%20Guides-1f1f23?style=for-the-badge"></a>&nbsp;<a href="https://www.rundiffusion.com/juggernaut?utm_source=huggingface&utm_medium=model_card&utm_campaign=juggernaut_xl_lightning&utm_content=lineup_compare"><img alt="Compare the Lineup" src="https://img.shields.io/badge/Compare%20the%20Lineup-1971c2?style=for-the-badge&labelColor=1f1f23"></a>&nbsp;<img alt="5-7 steps" src="https://img.shields.io/badge/Inference-5%E2%80%937%20steps-EAB308?style=for-the-badge&labelColor=1f1f23">&nbsp;<img alt="License: CreativeML Open RAIL-M" src="https://img.shields.io/badge/License-OpenRAIL--M-2ea44f?style=for-the-badge">
</p>

</div>

> Juggernaut XL Lightning is the **speed-optimized** member of the Juggernaut family — built on **SDXL Lightning** distillation, it produces photo-grade Juggernaut output in just **5–7 inference steps** instead of the usual 30–40. Same aesthetic, ~5× faster, ideal for batch generation, real-time iteration, and live demos.

## When to Use It

- ⚡ **Batch generation** — render hundreds of variations in the time you'd usually generate a few.
- 🔁 **Real-time iteration** — preview prompt changes in seconds, not minutes.
- 💻 **Lower-end hardware** — fewer steps means lower VRAM pressure during inference.
- 🎤 **Live demos and showcases** — instant turnaround when latency is the bottleneck.

If you need the very best per-image quality and don't mind the wait, use [**Juggernaut XL v9**](https://huggingface.co/RunDiffusion/Juggernaut-XL-v9) instead — full-step inference holds slightly more detail.

---

<div align="center">

<h3>⚡ Want speed without setup? Run Lightning on RunDiffusion.</h3>

<p>Pre-loaded with the right scheduler, sampler, and CFG. <b>No installs. No GPU rental.</b></p>

<a href="https://app.rundiffusion.com/login?utm_source=huggingface&utm_medium=model_card&utm_campaign=juggernaut_xl_lightning&utm_content=midpage_cta"><img alt="Launch on RunDiffusion" src="https://img.shields.io/badge/%E2%96%B6%20Launch%20on%20RunDiffusion-7C3AED?style=for-the-badge&labelColor=7C3AED"></a>

<p><i>Pre-loaded into ComfyUI · Forge · Automatic1111 · Fooocus · InvokeAI · SwarmUI</i></p>

</div>

---

## Recommended Settings

> **These are different from standard Juggernaut.** Lightning needs fewer steps and lower CFG.

| Parameter | Value |
| --- | --- |
| Sampler | `DPM++ SDE` or `DPM++ SDE Karras` |
| Steps | `5 – 7` |
| CFG scale | `1.5 – 2.0` |
| Resolution | `≥ 1024 × 1024` (use SDXL-native sizes) |
| VAE | **Already baked in** — no external VAE required |

Standard Juggernaut prompts work as-is. Don't adjust your prompt for Lightning — just adjust the sampler, steps, and CFG.

📖 **Want to go deeper on prompting?** See the [**RunDiffusion prompting library**](https://www.rundiffusion.com/prompting?utm_source=huggingface&utm_medium=model_card&utm_campaign=juggernaut_xl_lightning&utm_content=prompting_inline). Free to read, no signup.

## Two Ways to Run Juggernaut XL Lightning

### 🚀 Easiest — Run it on RunDiffusion (recommended)

One-click access inside ComfyUI, Forge, Automatic1111, Fooocus, InvokeAI, or SwarmUI. **Free trial included**, plus access to every other Juggernaut model on the same account.

<div align="center">
  <a href="https://app.rundiffusion.com/login?utm_source=huggingface&utm_medium=model_card&utm_campaign=juggernaut_xl_lightning&utm_content=quickstart_try"><img alt="Get Started Free" src="https://img.shields.io/badge/Get%20Started%20Free%20%E2%86%92-7C3AED?style=for-the-badge&labelColor=7C3AED"></a>
</div>

### 💻 Or run it locally — Diffusers

```python
import torch
from diffusers import DiffusionPipeline, DPMSolverSinglestepScheduler

pipe = DiffusionPipeline.from_pretrained(
    "RunDiffusion/Juggernaut-XL-Lightning",
    torch_dtype=torch.float16,
    use_safetensors=True,
).to("cuda")

# DPM++ SDE Karras
pipe.scheduler = DPMSolverSinglestepScheduler.from_config(
    pipe.scheduler.config, use_karras_sigmas=True
)

prompt = "Cinematic mid shot photo of an astronaut walking through a neon-lit Tokyo alley at night, hyperdetailed photography, skin details, shallow depth of field"

image = pipe(prompt, width=1024, height=1024, num_inference_steps=6, guidance_scale=1.8).images[0]
image.save("juggernaut_xl_lightning.png")
```

For ComfyUI / Forge / InvokeAI / SwarmUI: download the single-file checkpoint and drop it into your `models/checkpoints/` directory.

## The Rest of the Family

| Model | Best for |
| --- | --- |
| [**Juggernaut XL v9**](https://huggingface.co/RunDiffusion/Juggernaut-XL-v9) | The flagship SDXL — full-step photorealism workhorse. **6M+ downloads.** |
| [**Juggernaut XI v11**](https://huggingface.co/RunDiffusion/Juggernaut-XI-v11) | Latest SDXL-line refinement |
| [**Juggernaut XI Lightning**](https://huggingface.co/RunDiffusion/Juggernaut-XI-Lightning) | Speed variant on the XI base |
| [**Juggernaut Z**](https://huggingface.co/RunDiffusion/Juggernaut-Z-Image) | Lumina-Image-2 architecture — cinematic, presentation-ready |
| [**Juggernaut Pro Flux**](https://www.rundiffusion.com/juggernaut-pro-flux?utm_source=huggingface&utm_medium=model_card&utm_campaign=juggernaut_xl_lightning&utm_content=cross_promo_pro_flux) | FLUX.1 backbone — top-tier photo quality |

## Commercial Use

This model **may not be deployed behind paid API services** without explicit licensing. For commercial licensing, custom models, business inquiries, or consultation, contact **[juggernaut@rundiffusion.com](mailto:juggernaut@rundiffusion.com)**.

You are free to use this model for personal and creative work under the terms of the [CreativeML Open RAIL-M license](https://huggingface.co/spaces/CompVis/stable-diffusion-license).

## Credits

Juggernaut XL Lightning was created by **[KandooAI](https://twitter.com/Juggernaut_AI)** in collaboration with **[RunDiffusion](https://www.rundiffusion.com/?utm_source=huggingface&utm_medium=model_card&utm_campaign=juggernaut_xl_lightning&utm_content=credits)**, distilled on top of **[SDXL Lightning](https://huggingface.co/ByteDance/SDXL-Lightning)**.

---

<div align="center">

<h3>Ready to generate at speed?</h3>

<p>Juggernaut Lightning is one click away — no setup, no GPU rental, no model downloads.</p>

<a href="https://app.rundiffusion.com/login?utm_source=huggingface&utm_medium=model_card&utm_campaign=juggernaut_xl_lightning&utm_content=footer_cta"><img alt="Try Juggernaut Lightning — Free" src="https://img.shields.io/badge/%E2%96%B6%20Try%20Juggernaut%20Lightning%20%E2%80%94%20Free-7C3AED?style=for-the-badge&labelColor=7C3AED"></a>

<p><sub><a href="https://www.rundiffusion.com/juggernaut?utm_source=huggingface&utm_medium=model_card&utm_campaign=juggernaut_xl_lightning&utm_content=footer_lineup">Compare the full Juggernaut lineup</a> · <a href="https://www.rundiffusion.com/prompting?utm_source=huggingface&utm_medium=model_card&utm_campaign=juggernaut_xl_lightning&utm_content=footer_prompting">Prompting guides</a> · <a href="mailto:juggernaut@rundiffusion.com">Commercial licensing</a></sub></p>

</div>
