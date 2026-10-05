---
license: other
license_name: fair-ai-public-license-1.0-sd
license_link: https://freedevproject.org/faipl-1.0-sd/
datasets:
- KBlueLeaf/danbooru2023-webp-4Mpixel
- KBlueLeaf/danbooru2023-sqlite
- KBlueLeaf/danbooru2023-florence2-caption
- Amber-River/Pixiv-2.6M
language:
- en
library_name: diffusers
pipeline_tag: text-to-image
base_model:
- KBlueLeaf/Kohaku-XL-Epsilon-rev2
---

# Kohaku XL Zeta
join us: https://discord.gg/tPBsKDyRR5

![image/png](https://cdn-uploads.huggingface.co/production/uploads/630593e2fca1d8d92b81d2a1/rUeUdKYiUfi6LtTcpasgN.png)

<style>
.image-viewer {position: relative;width: 100%;margin: 0 auto;display: flex;flex-flow: wrap;align-items: center;justify-content: center;}
.image-viewer input[type="radio"] {display: none;}
.image-viewer label {border-radius: 10%;padding: 20px;background-color: #B398F5;background-size: cover;background-position: center;cursor: pointer;color: black;margin: 8px;}
.image-viewer label:hover {background-color: #4C88F5;padding: 24px;margin: 4px;}
.image-viewer input[type="radio"]:checked + label {background-color: #6296F5;padding: 28px;margin: 0px;}
.image-container {position: relative;width: 100%;height: 50vh;margin: 1rem 1rem 0 0;}
.inner-container {position: absolute;width:100%;height: 100%;display: flex;align-items: center;justify-content: center;}
.inner-container img {border-radius: 10px;max-height: 100%;max-width: 100%;height: 0;width: 0;opacity: 0;transition: opacity 0.5s ease, height 0.25s ease, width 0.25s ease;}
#image1:checked ~ .image-container img:nth-child(1),#image2:checked ~ .image-container img:nth-child(2),#image3:checked ~ .image-container img:nth-child(3),#image4:checked ~ .image-container img:nth-child(4),#image5:checked ~ .image-container img:nth-child(5),#image6:checked ~ .image-container img:nth-child(6),#image7:checked ~ .image-container img:nth-child(7),#image8:checked ~ .image-container img:nth-child(8),#image9:checked ~ .image-container img:nth-child(9),#image10:checked ~ .image-container img:nth-child(10),#image11:checked ~ .image-container img:nth-child(11),#image12:checked ~ .image-container img:nth-child(12) {height: auto; width:auto; opacity: 1;}
#image1l{background-image: url("/KBlueLeaf/Kohaku-XL-Zeta/resolve/main/sample-images/01_2085.jpg");}
#image2l{background-image: url("/KBlueLeaf/Kohaku-XL-Zeta/resolve/main/sample-images/02_02084.jpg");}
#image3l{background-image: url("/KBlueLeaf/Kohaku-XL-Zeta/resolve/main/sample-images/03_02086.jpg");}
#image4l{background-image: url("/KBlueLeaf/Kohaku-XL-Zeta/resolve/main/sample-images/04_02081.jpg");}
#image5l{background-image: url("/KBlueLeaf/Kohaku-XL-Zeta/resolve/main/sample-images/05_00015-3807569455.jpg");}
#image6l{background-image: url("/KBlueLeaf/Kohaku-XL-Zeta/resolve/main/sample-images/05_00096-1093286410.jpg");}
#image7l{background-image: url("/KBlueLeaf/Kohaku-XL-Zeta/resolve/main/sample-images/05_00117-2417076749.jpg");}
#image8l{background-image: url("/KBlueLeaf/Kohaku-XL-Zeta/resolve/main/sample-images/05_00118-2417076750.jpg");}
#image9l{background-image: url("/KBlueLeaf/Kohaku-XL-Zeta/resolve/main/sample-images/05_00123-2659559372.jpg");}
#image10l{background-image: url("/KBlueLeaf/Kohaku-XL-Zeta/resolve/main/sample-images/06_02082.jpg");}
#image11l{background-image: url("/KBlueLeaf/Kohaku-XL-Zeta/resolve/main/sample-images/06_02088.jpg");}
#image12l{background-image: url("/KBlueLeaf/Kohaku-XL-Zeta/resolve/main/sample-images/06_02091.jpg");}
</style>
<div class="image-viewer">
<input type="radio" id="image1" name="image-switcher" checked>
<label for="image1" id="image1l"></label>
<input type="radio" id="image2" name="image-switcher">
<label for="image2" id="image2l"></label>
<input type="radio" id="image3" name="image-switcher">
<label for="image3" id="image3l"></label>
<input type="radio" id="image4" name="image-switcher">
<label for="image4" id="image4l"></label>
<input type="radio" id="image5" name="image-switcher">
<label for="image5" id="image5l"></label>
<input type="radio" id="image6" name="image-switcher">
<label for="image6" id="image6l"></label>
<input type="radio" id="image7" name="image-switcher">
<label for="image7" id="image7l"></label>
<input type="radio" id="image8" name="image-switcher">
<label for="image8" id="image8l"></label>
<input type="radio" id="image9" name="image-switcher">
<label for="image9" id="image9l"></label>
<input type="radio" id="image10" name="image-switcher">
<label for="image10" id="image10l"></label>
<input type="radio" id="image11" name="image-switcher">
<label for="image11" id="image11l"></label>
<input type="radio" id="image12" name="image-switcher">
<label for="image12" id="image12l"></label>
<div class="image-container">
<div class="inner-container">
<img src="sample-images/01_2085.jpg" alt="image1" />
<img src="sample-images/02_02084.jpg" alt="image2" />
<img src="sample-images/03_02086.jpg" alt="image3" />
<img src="sample-images/04_02081.jpg" alt="image4" />
<img src="sample-images/05_00015-3807569455.jpg" alt="image5" />
<img src="sample-images/05_00096-1093286410.jpg" alt="image6" />
<img src="sample-images/05_00117-2417076749.jpg" alt="image7" />
<img src="sample-images/05_00118-2417076750.jpg" alt="image8" />
<img src="sample-images/05_00123-2659559372.jpg" alt="image9" />
<img src="sample-images/06_02082.jpg" alt="image10" />
<img src="sample-images/06_02088.jpg" alt="image11" />
<img src="sample-images/06_02091.jpg" alt="image12" />
</div>
</div>
</div>

---

## Highlights
- Resume from Kohaku-XL-Epsilon rev2
- More stable, long/detailed prompt is not a requirement now.
- Better fidelity on style and character, support more style.
  - CCIP metric surpass Sanae XL anime. have over 2200 character with CCIP score > 0.9 in 3700 character set.
- Trained on both danbooru tags and natural language, better ability on nl caption.
- Trained on combined dataset, not only danbooru
  - danbooru (7.6M images, last id 7832883, 2024/07/10)
  - pixiv (filtered from 2.6M special set, will release the url set)
  - pvc figure (around 30k images, internal source)
  - realbooru (around 90k images, for regularization)
  - 8.46M images in total
- Since the model is trained on both kind of caption, the ctx length limit is extended to 300.


![image/png](https://cdn-uploads.huggingface.co/production/uploads/630593e2fca1d8d92b81d2a1/2EpGwA8D1c0UnVGuPMFtY.png)


## Usage (PLEASE READ THIS SECTION)
### Recommended Generation Settings
- resolution: 1024x1024 or similar pixel count
- cfg scale: 3.5~6.5
- sampler/scheduler:
  - Euler (A) / any scheduler
  - DPM++ series / exponential scheduler
  - for other sampler, I personally recommend exponential scheduler.
- step: 12~50

<!-- ### Prompt Gen
DTG series prompt gen can still be used on KXL zeta.
A brand new prompt gen for cooperating both tag and nl caption is under developing.
|![image/png](https://cdn-uploads.huggingface.co/production/uploads/630593e2fca1d8d92b81d2a1/ixiBsWdO1sg6QUMqRUbHu.png)|![image/png](https://cdn-uploads.huggingface.co/production/uploads/630593e2fca1d8d92b81d2a1/Byv2Xg1g8zN9nuCURasK6.png)|
|-|-| -->

### Prompt Format
As same as Kohaku XL Epsilon or Delta, but you can replace "general tags" with "natural language caption".
You can also put both together.

### Special Tags
- Quality tags: masterpiece, best quality, great quality, good quality, normal quality, low quality, worst quality
- Rating tags: safe, sensitive, nsfw, explicit
- Date tags: newest, recent, mid, early, old

#### Rating tags
General: safe
Sensitive: sensitive
Questionable: nsfw
Explicit: nsfw, explicit

## Dataset
For better ability on some certain concepts, I use full danbooru dataset instead of filterd one.
Than use crawled Pixiv dataset (from 3~5 tag with popularity sort) as addon dataset.
Since Pixiv's search system only allow 5000 page per tag so there is not much meaningful image, and some of them are duplicated with danbooru set(but since I want to reinforce these concept I directly ignore the duplication)

As same as kxl eps rev2, I add realbooru and pvc figure images for more flexibility on concept/style.

## Training
- Hardware: Quad RTX 3090s
- Dataset
  - Num Images: 8,468,798
  - Resolution: 1024x1024
  - Min Bucket Resolution: 256
  - Max Bucket Resolution: 4096
  - Caption Tag Dropout: 0.2
  - Caption Group Dropout: 0.2 (for dropping tag/nl caption entirely)
- Training
  - Batch Size: 4
  - Grad Accumulation Step: 32
  - Equivalent Batch Size: 512
  - Total Epoch: 1
  - Total Steps: 16548
  - Training Time: 430 hours (wall time)
  - Mixed Precision: FP16
- Optimizer
  - Optimizer: Lion8bit
  - Learning Rate: 1e-5 for UNet / TE training disabled
  - LR Scheduler: Constant (with warmup)
  - Warmup Steps: 100
  - Weight Decay: 0.1
  - Betas: 0.9, 0.95
- Diffusion
  - Min SNR Gamma: 5
  - Debiased Estimation Loss: Enabled
  - IP Noise Gamma: 0.05


## Why do you still use SDXL but not any Brand New DiT-Based Models?
Unless any one give me reasonable compute resources or any team release efficient enough DiT or I will not train any DiT-based anime base model. <br>
But if you give me 8xH100 for an year, I can even train lot of DiT from scratch (If you want)


## License:
Fair-AI-public-1.0-sd