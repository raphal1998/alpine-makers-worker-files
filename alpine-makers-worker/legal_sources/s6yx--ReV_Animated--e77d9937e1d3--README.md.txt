---
license: creativeml-openrail-m
language:
  - en
library_name: diffusers
pipeline_tag: text-to-image
tags:
  - stable-diffusion
  - stable-diffusion-diffusers
---





# ReV Animated

| Website      | Model Name   | Link                                       |
| ------------ | ------------ | ------------------------------------------ |
| CivitAI      | ReV Animated | <https://civitai.com/models/7371>          |
| Hugging Face | ReV Animated | <https://huggingface.co/s6yx/ReV_Animated> |

<br><br>

<div id="top"></div>

## Table of Contents

- [Download links](#download-links)
- [Model Overview](#model-overview)
- [Video Features](#video-features)
- [Prompting](#prompting)
- [Negative Prompts TI](#negative-prompts-ti)
- [Release Notes](#release-notes)
- [Image Gallery](#image-gallery)
  <br><br>

## Download links

| [rev_1.2.2.safetensors](https://huggingface.co/s6yx/ReV_Animated/blob/main/rev_1.2.2/rev_1.2.2.safetensors)  
| [rev_1.2.2-fp16.safetensors](https://huggingface.co/s6yx/ReV_Animated/blob/main/rev_1.2.2/rev_1.2.2-fp16.safetensors)

<br><br>

## Model Overview

The idea behind the model was derived from my [ReV Mix](https://civitai.com/models/5216/rev-mix) model

- `rev` or `revision`: The concept of how the model generates images is likely to change as I see fit.
- `Animated`: The model has the ability to create 2.5 anime-like image generations.

This model is a checkpoint merge, meaning it is a product of other models to create a product that derives from the originals.
<br><br>

## Video Features

- [Olivio Sarikas - Why Is EVERYONE Using This Model?! - Rev Animated for Stable Diffusion / A1111](https://youtu.be/Nl43zR5dVuM?t=192)

- [Olivio Sarikas - ULTRA SHARP Upscale! - Don't miss this Method!!! / A1111 - NEW Model](https://www.youtube.com/watch?v=A6dQPMy_tHY)
<p align="right">(<a href="#top">back to top</a>)</p>
  <br><br>

## Prompting

- **Order matters** - words near the front of your prompt are weighted more heavily than the things in the back of your prompt.
- content type &rarr; description &rarr; style &rarr; composition
<p align="right">(<a href="#top">back to top</a>)</p>
  <br><br>

## Negative Prompts TI

- [EasyNegative](https://huggingface.co/embed/EasyNegative/tree/main)
- [Deep Negative](https://civitai.com/models/4629/deep-negative-v1x)
- [bad_prompt_version2](https://huggingface.co/embed/bad_prompt/blob/main/bad_prompt_version2.pt)
- [bad-artist](https://huggingface.co/nick-x-hacker/bad-artist/blob/main/bad-artist.pt)
- [bad-artist-anime](https://huggingface.co/nick-x-hacker/bad-artist/blob/main/bad-artist-anime.pt)
<p align="right">(<a href="#top">back to top</a>)</p>

<br><br>

## Release Notes

- **1.2.2** - 2023-04-15

  - **Fixes**: used latest update to [s1dlx](https://github.com/s1dlx/sd-webui-bayesian-merger) this fixes the clip, vae and sd weights missing from merges.

- Keeps 1.2.1 merge with animatrix
<p align="right">(<a href="#top">back to top</a>)</p>

<br><br><br><br><br><br>

## Image Gallery

|                                                          Image                                                          |      File name       |
| :---------------------------------------------------------------------------------------------------------------------: | :------------------: |
| <img src='https://huggingface.co/s6yx/ReV_Animated/resolve/main/assets/00000-1192013237.png' width='256' height='256'/> | 00000-1192013237.png |
| <img src='https://huggingface.co/s6yx/ReV_Animated/resolve/main/assets/00000-2055475037.png' width='256' height='384'/> | 00000-2055475037.png |
| <img src='https://huggingface.co/s6yx/ReV_Animated/resolve/main/assets/00000-2510945799.png' width='384' height='256'/> | 00000-2510945799.png |
| <img src='https://huggingface.co/s6yx/ReV_Animated/resolve/main/assets/00000-3337126850.png' width='256' height='384'/> | 00000-3337126850.png |
| <img src='https://huggingface.co/s6yx/ReV_Animated/resolve/main/assets/00001-3934503526.png' width='256' height='256'/> | 00001-3934503526.png |
| <img src='https://huggingface.co/s6yx/ReV_Animated/resolve/main/assets/00001-452633722.png' width='256' height='384'/>  | 00001-452633722.png  |
| <img src='https://huggingface.co/s6yx/ReV_Animated/resolve/main/assets/00002-1927910554.png' width='256' height='256'/> | 00002-1927910554.png |
| <img src='https://huggingface.co/s6yx/ReV_Animated/resolve/main/assets/00003-2652653862.png' width='384' height='256'/> | 00003-2652653862.png |
| <img src='https://huggingface.co/s6yx/ReV_Animated/resolve/main/assets/00003-2673198611.png' width='256' height='384'/> | 00003-2673198611.png |
| <img src='https://huggingface.co/s6yx/ReV_Animated/resolve/main/assets/00005-1514816117.png' width='256' height='384'/> | 00005-1514816117.png |
| <img src='https://huggingface.co/s6yx/ReV_Animated/resolve/main/assets/00005-3774228737.png' width='256' height='384'/> | 00005-3774228737.png |
| <img src='https://huggingface.co/s6yx/ReV_Animated/resolve/main/assets/00006-1307711212.png' width='384' height='256'/> | 00006-1307711212.png |
| <img src='https://huggingface.co/s6yx/ReV_Animated/resolve/main/assets/00007-3653604530.png' width='256' height='384'/> | 00007-3653604530.png |
| <img src='https://huggingface.co/s6yx/ReV_Animated/resolve/main/assets/00009-735323561.png' width='256' height='384'/>  | 00009-735323561.png  |
| <img src='https://huggingface.co/s6yx/ReV_Animated/resolve/main/assets/00010-3061578913.png' width='256' height='384'/> | 00010-3061578913.png |
| <img src='https://huggingface.co/s6yx/ReV_Animated/resolve/main/assets/00010-387766904.png' width='256' height='384'/>  | 00010-387766904.png  |
| <img src='https://huggingface.co/s6yx/ReV_Animated/resolve/main/assets/00018-2474129942.png' width='256' height='384'/> | 00018-2474129942.png |
| <img src='https://huggingface.co/s6yx/ReV_Animated/resolve/main/assets/00019-1876981351.png' width='256' height='384'/> | 00019-1876981351.png |
| <img src='https://huggingface.co/s6yx/ReV_Animated/resolve/main/assets/00020-108597497.png' width='256' height='384'/>  | 00020-108597497.png  |
| <img src='https://huggingface.co/s6yx/ReV_Animated/resolve/main/assets/00020-3276234046.png' width='256' height='384'/> | 00020-3276234046.png |
| <img src='https://huggingface.co/s6yx/ReV_Animated/resolve/main/assets/00021-2937362614.png' width='256' height='384'/> | 00021-2937362614.png |
| <img src='https://huggingface.co/s6yx/ReV_Animated/resolve/main/assets/00037-995647460.png' width='256' height='384'/>  | 00037-995647460.png  |
| <img src='https://huggingface.co/s6yx/ReV_Animated/resolve/main/assets/00044-1328103044.png' width='256' height='384'/> | 00044-1328103044.png |
| <img src='https://huggingface.co/s6yx/ReV_Animated/resolve/main/assets/00056-1463146050.png' width='384' height='256'/> | 00056-1463146050.png |

<p align="right">(<a href="#top">back to top</a>)</p>
