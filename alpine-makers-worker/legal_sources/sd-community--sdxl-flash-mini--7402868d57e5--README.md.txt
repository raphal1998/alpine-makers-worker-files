---
license: creativeml-openrail-m
library_name: diffusers
pipeline_tag: text-to-image
base_model: stabilityai/stable-diffusion-xl-base-1.0
tags:
- safetensors
- stable-diffusion
- sdxl
- ssd-1b
- flash
- sdxl-flash
- sdxl-flash-mini
- distilled
- lightning
- turbo
- lcm
- hyper
- fast
- fast-sdxl
- sd-community
inference: false
---
# **SDXL Flash Mini** *in collaboration with [Project Fluently](https://hf.co/fluently)*

![preview](images/preview.png)

Introducing the new fast model SDXL Flash (Mini), we learned that all fast XL models work fast, but the quality decreases, and we also made a fast model, but it is not as fast as LCM, Turbo, Lightning and Hyper, but the quality is higher. Below you will see the study with steps and cfg.

`It weighs less, consumes less video memory and other resources, and the quality has not dropped much.`

### Steps and CFG (Guidance)

![steps_and_cfg_grid_test](images/steps_cfg_grid.png)

### Optimal settings
- **Steps**: 6-9
- **CFG Scale**: 2.5-3.5
- **Sampler**: DPM++ SDE

### Usage

We can use this model only in Auto111 or ComfyUI or Fooocus.