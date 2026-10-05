---
license: creativeml-openrail-m
language:
- en
library_name: diffusers
pipeline_tag: text-to-image
base_model: stabilityai/stable-diffusion-xl-base-1.0
tags:
- safetensors
- stable-diffusion
- sdxl
- visionix
- visionix-alpha
- realism
- hyperrealism
- photorealism
- photo
- cinematic
- nature
- human
- lighting
- trained
inference:
  parameters:
    num_inference_steps: 22
    guidance_scale: 5.5
    negative_prompt: >-
      cartoon, 3D, disfigured, bad, art, deformed, extra limbs, weird, blurry, duplicate, morbid, mutilated, out of frame, extra fingers, mutated hands, poorly drawn, hands, poorly drawn face, mutation, ugly, bad, anatomy, bad proportions, extra limbs, clone, clone-faced, cross proportions, missing arms, malformed limbs, missing legs, mutated, hands, fused fingers, too many fingers, photo shop, video game, ugly, tiling, cross-eye, mutation of eyes, long neck, bonnet, hat, beanie, cap, B&W
---

# **VisioniX** Alpha - the most powerful realism-model


![preview](images/preview.png)


[>>> Inpainting version <<<](https://huggingface.co/ehristoforu/Visionix-alpha-inpainting)

We present the best realism model at the moment - VisioniX.

## About this model

This model was created through complex training on huge, ultra-realistic datasets.

### Why is this model better than its competitors?

All, absolutely all realism models make one important mistake: they chase only super realism (super detailed skin and others) completely forgetting about general aesthetics, anatomy, etc.

### Who is this model for?

The main feature of this model is that the model can generate not only super realistic photos, but also realistic detailed art and much more, so the model is suitable for a large audience and can solve a wide range of problems. If this model still does not suit you, we recommend using FluentlyXL model.

### Optimal settings for this model

- **Sampler**: *DPM++ 3M SDE* (Karras), DPM++ SDE (Karras)

- **Inference Steps**: *22*-25

- **Guidance Scale (CFG)**: 5-7

- **Negative Prompt**: *not* or:
  ```
  cartoon, 3D, disfigured, bad, art, deformed, extra limbs, weird, blurry, duplicate, morbid, mutilated, out of frame, extra fingers, mutated hands, poorly drawn, hands, poorly drawn face, mutation, ugly, bad, anatomy, bad proportions, extra limbs, clone, clone-faced, cross proportions, missing arms, malformed limbs, missing legs, mutated, hands, fused fingers, too many fingers, photo shop, video game, ugly, tiling, cross-eye, mutation of eyes, long neck, bonnet, hat, beanie, cap, B&W
  ```

### End

After this model, you will not want to use the rest of the realism models, if you like the model, we ask you to leave a good review and a couple of your results in the review, thank you, this will greatly help in promoting this wonderful model 💖