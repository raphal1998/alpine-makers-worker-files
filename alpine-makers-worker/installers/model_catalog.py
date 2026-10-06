"""Fixed model files shared by installation, inventory and managed cleanup.

New snapshots are pinned to the official publisher revisions inspected on
2026-09-08. Only the chosen shape pipeline is downloaded, not every variant
and texture model in the upstream repository.
"""

COMFYUI_CATALOG = {
    ("image_generation", "sdxl-base-1.0"): ("sd_xl_base_1.0.safetensors", 6938078334, "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/462165984030d82259a11f4367a4eed129e94a7b/sd_xl_base_1.0.safetensors?download=true", "31e35c80fc4829d14f90153f4c74cd59c90b779f6afe05a74cd6120b893f7e5b"),
    ("image_generation", "sd-turbo"): ("sd_turbo.safetensors", 5214561328, "https://huggingface.co/stabilityai/sd-turbo/resolve/b261bac6fd2cf515557d5d0707481eafa0485ec2/sd_turbo.safetensors?download=true", "3f067a1b943cf162f2b8f8588f6cf5824bd5b4c7d1d88d87164b9ca123616549"),
    ("image_generation", "dreamshaper-8"): ("DreamShaper_8_pruned.safetensors", 2132625894, "https://huggingface.co/Lykon/DreamShaper/resolve/228d79cb20811466f5c5710aa91f05dabd0b8a14/DreamShaper_8_pruned.safetensors?download=true", "879db523c30d3b9017143d56705015e15a2cb5628762c11d086fed9538abd7fd"),
    ("image_generation", "sdxl-turbo"): ("sd_xl_turbo_1.0_fp16.safetensors", 6938081905, "https://huggingface.co/stabilityai/sdxl-turbo/resolve/71153311d3dbb46851df1931d3ca6e939de83304/sd_xl_turbo_1.0_fp16.safetensors?download=true", "e869ac7d6942cb327d68d5ed83a40447aadf20e0c3358d98b2cc9e270db0da26"),
    ("image_generation", "absolutereality-1.8.1"): ("AbsoluteReality_1.8.1_pruned.safetensors", 2132625432, "https://huggingface.co/Lykon/AbsoluteReality/resolve/df7c3efce8f54d02cf73a21fcecd908a0fde2898/AbsoluteReality_1.8.1_pruned.safetensors?download=true", "463d6a9fe8a4b56a4d69ef3692074c0617428dfd8e8f12f9efe3b1e9a71717ce"),
    ("image_generation", "sdxl-refiner-1.0"): ("sd_xl_refiner_1.0.safetensors", 6075981930, "https://huggingface.co/stabilityai/stable-diffusion-xl-refiner-1.0/resolve/5d4cfe854c9a9a87939ff3653551c2b3c99a4356/sd_xl_refiner_1.0.safetensors?download=true", "7440042bbdc8a24813002c09b6b69b64dc90fded4472613437b7f55f9b7d9c5f"),
    ("image_generation", "stable-diffusion-1.5"): ("v1-5-pruned-emaonly.safetensors", 4265146304, "https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-v1-5/resolve/451f4fe16113bff5a5d2269ed5ad43b0592e9a14/v1-5-pruned-emaonly.safetensors?download=true", "6ce0161689b3853acaa03779ec93eafe75a02f4ced659bee03f50797806fa2fa"),
    # Stable Diffusion 2.1 (OpenCLIP-H) : 512 px (prédiction du bruit) et 768 px (prédiction v), dépôts d'archive
    # sd2-community (les dépôts stabilityai d'origine ne sont plus publics), révisions épinglées, licence OpenRAIL++.
    ("image_generation", "stable-diffusion-2.1-base"): ("v2-1_512-ema-pruned.safetensors", 5214604494, "https://huggingface.co/sd2-community/stable-diffusion-2-1-base/resolve/4e63672c03103b6c636b8fb4119ba982469b2955/v2-1_512-ema-pruned.safetensors?download=true", "df955bdf6b682338ea9b55dfc0d8f3475aadf4836e204893d28b82355e0956d2"),
    ("image_generation", "stable-diffusion-2.1"): ("v2-1_768-ema-pruned.safetensors", 5214604494, "https://huggingface.co/sd2-community/stable-diffusion-2-1/resolve/bb2154823665391b4fb29b0b9cf82a198964ee05/v2-1_768-ema-pruned.safetensors?download=true", "dcd690123cfc64383981a31d955694f6acf2072a80537fdb612c8e58ec87a8ac"),
    ("image_generation", "ssd-1b"): ("SSD-1B.safetensors", 4465671506, "https://huggingface.co/segmind/SSD-1B/resolve/60987f37e94cd59c36b1cba832b9f97b57395a10/SSD-1B.safetensors?download=true", "0bf1ce6b065a6b969ab02dc8e8fa21eb20ee189b10935c49ce68c77a7e432c1c"),
    ("image_generation", "segmind-vega"): ("segmind-vega.safetensors", 3293395670, "https://huggingface.co/segmind/Segmind-Vega/resolve/7714c4363e5856ff974a4f4b068e8691f26d0b40/segmind-vega.safetensors?download=true", "94762e983e5942056be73c5c1d4464b8ffa1ada500b4fef1267550e2447953ce"),
    ("image_generation", "realistic-vision-6.0"): ("Realistic_Vision_V6.0_NV_B1.safetensors", 4265096996, "https://huggingface.co/SG161222/Realistic_Vision_V6.0_B1_noVAE/resolve/9a857a696b9aabbf509073e0aa55ec8200b6ef7d/Realistic_Vision_V6.0_NV_B1.safetensors?download=true", "5d814d2f9c489f9d3bffc56126a66c182eee7729bffacf71c048260aa366c269"),
    ("image_generation", "realvisxl-4.0"): ("RealVisXL_V4.0.safetensors", 6938040706, "https://huggingface.co/SG161222/RealVisXL_V4.0/resolve/26dfe44930964cd70d0a817b6d1cc945c130e38d/RealVisXL_V4.0.safetensors?download=true", "912c9dc74f5855175c31a7993f863a043ac8dcc31732b324cd05d75cd7e16844"),
    ("image_generation", "playground-2.5"): ("playground-v2.5-1024px-aesthetic.fp16.safetensors", 6938040576, "https://huggingface.co/playgroundai/playground-v2.5-1024px-aesthetic/resolve/1e032f13f2fe6db2dc49947dbdbd196e753de573/playground-v2.5-1024px-aesthetic.fp16.safetensors?download=true", "bcaa7dd6780974f000b17b5a6c63e6f867a75c51ffa85c67d6b196882c69b992"),
    ("image_generation", "juggernaut-xl-v9"): ("Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors", 7105348188, "https://huggingface.co/RunDiffusion/Juggernaut-XL-v9/resolve/cf419233522daa0b9ea36c3aff98fa2cab1fb0fb/Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors?download=true", "c9e3e68f89b8e38689e1097d4be4573cf308de4e3fd044c64ca697bdb4aa8bca"),
    ("image_generation", "juggernaut-xl-lightning"): ("Juggernaut_RunDiffusionPhoto2_Lightning_4Steps.safetensors", 7105348284, "https://huggingface.co/RunDiffusion/Juggernaut-XL-Lightning/resolve/9c35e7ca1112b7e567ae7b24400b83935909916d/Juggernaut_RunDiffusionPhoto2_Lightning_4Steps.safetensors?download=true", "c8df560d2992ac04299412be6a36fa53a4e7a1b74f27b94867ad3f84f4b425a5"),
    ("image_generation", "dreamshaper-xl-lightning"): ("DreamShaperXL_Lightning-SFW.safetensors", 6939220250, "https://huggingface.co/Lykon/dreamshaper-xl-lightning/resolve/561086c22aba3925b1e8803a73857f061635c7c3/DreamShaperXL_Lightning-SFW.safetensors?download=true", "832e7bb2302c3cd67c818ca4fe9dcbedf696d3070dab5463127263ec4db9899f"),
    ("image_generation", "dreamshaper-xl-turbo-v2"): ("DreamShaperXL_Turbo_V2-SFW.safetensors", 6939220250, "https://huggingface.co/Lykon/dreamshaper-xl-v2-turbo/resolve/f70ad5bce436c640d23fc02cd0a6ccc787280b6e/DreamShaperXL_Turbo_V2-SFW.safetensors?download=true", "6e718af03dcb651250ec8911554c4df8472a6d7027bcb3678a206c1170cc94fc"),
    ("image_generation", "animagine-xl-4.0"): ("animagine-xl-4.0.safetensors", 6938434056, "https://huggingface.co/cagliostrolab/animagine-xl-4.0/resolve/2b7c1b397761bf5bd3cc42e5b39ec99314a75a96/animagine-xl-4.0.safetensors?download=true", "1d5b43ff75b6ab598502d4c779d2fbfa3dceca51c60c3b609640a60772333916"),
    ("image_generation", "animagine-xl-3.1"): ("animagine-xl-3.1.safetensors", 6938325776, "https://huggingface.co/cagliostrolab/animagine-xl-3.1/resolve/483f0c322568ed13697ed01dd0be07204746d12b/animagine-xl-3.1.safetensors?download=true", "e3c47aedb06418c6c331443cd89f2b3b3b34b7ed2102a3d4c4408a8d35aad6b0"),
    ("image_generation", "sdxl-lightning-2step"): ("sdxl_lightning_2step.safetensors", 6938040682, "https://huggingface.co/ByteDance/SDXL-Lightning/resolve/c9a24f48e1c025556787b0c58dd67a091ece2e44/sdxl_lightning_2step.safetensors?download=true", "87e61f60b85d9f1c577bd9053872c7678b7d39f6f52d470081ff6ea3b49ddf98"),
    ("image_generation", "sdxl-lightning-4step"): ("sdxl_lightning_4step.safetensors", 6938040682, "https://huggingface.co/ByteDance/SDXL-Lightning/resolve/c9a24f48e1c025556787b0c58dd67a091ece2e44/sdxl_lightning_4step.safetensors?download=true", "e0d996ee0013e79d9d3561f50fcafb9a17e3ff07b780358e3b66d67932c4d490"),
    ("image_generation", "sdxl-lightning-8step"): ("sdxl_lightning_8step.safetensors", 6938040682, "https://huggingface.co/ByteDance/SDXL-Lightning/resolve/c9a24f48e1c025556787b0c58dd67a091ece2e44/sdxl_lightning_8step.safetensors?download=true", "43f0501ac4ffcef84f3fc32a47779f24c181647fa97ef7f5ec4428107d732ae9"),
    ("image_generation", "realvisxl-5.0"): ("RealVisXL_V5.0_fp16.safetensors", 6938065488, "https://huggingface.co/SG161222/RealVisXL_V5.0/resolve/ac93e0dda1f6d448cae19bbfab8c5e720a5e48bc/RealVisXL_V5.0_fp16.safetensors?download=true", "6a35a7855770ae9820a3c931d4964c3817b6d9e3c6f9c4dabb5b3a94e5643b80"),
    ("image_generation", "realvisxl-5.0-lightning"): ("RealVisXL_V5.0_Lightning_fp16.safetensors", 6938065512, "https://huggingface.co/SG161222/RealVisXL_V5.0_Lightning/resolve/f4454158cedaab9f0688c199561d6c92525f3a85/RealVisXL_V5.0_Lightning_fp16.safetensors?download=true", "fabcadd9330dcc4f9702063428d40b9d4d07168d8acefc819b8d1d9db466b3ec"),
    ("image_generation", "realvisxl-4.0-lightning"): ("RealVisXL_V4.0_Lightning.safetensors", 6939220250, "https://huggingface.co/SG161222/RealVisXL_V4.0_Lightning/resolve/39b41eff57c9d10c62119590901d6771863b426d/RealVisXL_V4.0_Lightning.safetensors?download=true", "d6a48d3e2025448f011c27f4667145286910696d744c50c8ba3c2fb31dc98ea1"),
    ("image_generation", "playground-v2"): ("playground-v2.fp16.safetensors", 6938042488, "https://huggingface.co/playgroundai/playground-v2-1024px-aesthetic/resolve/737a24ebe3ae4e5421110879168be188853d86c8/playground-v2.fp16.safetensors?download=true", "0411e988479884b1a3ecd184123efe38d051d8d0ef24270585a7d1d57499464a"),
    ("image_generation", "proteus-0.5"): ("proteusV0.5.safetensors", 7140738330, "https://huggingface.co/dataautogpt3/ProteusV0.5/resolve/d1f0b0334736a04e0a9431288b882b8cd0043d0b/proteusV0.5.safetensors?download=true", "8f511b9fa98a98d20c745025633c638759f61e276d5cb33cc49fec500d5c3396"),
    ("image_generation", "kohaku-xl-zeta"): ("kohaku-xl-zeta.safetensors", 6938040286, "https://huggingface.co/KBlueLeaf/Kohaku-XL-Zeta/resolve/c14affd9483ac7603cfff3f06b05da22a06deaa6/kohaku-xl-zeta.safetensors?download=true", "083cecef5b99c7eff9d6c572c6055abe978b645d78eb5cdb34e8661320651c12"),
    ("image_generation", "illustrious-xl-2.0"): ("Illustrious-XL-v2.0.safetensors", 6938040674, "https://huggingface.co/OnomaAIResearch/Illustrious-XL-v2.0/resolve/69459c1fe6f46db41ab31e6114f05acc0e06bcaa/Illustrious-XL-v2.0.safetensors?download=true", "c2a1a3eaa13d4c107dc7e00c3fe830cab427aa026362740ea094745b3422a331"),
    ("image_generation", "illustrious-xl-0.1"): ("Illustrious-XL-v0.1.safetensors", 6938040760, "https://huggingface.co/OnomaAIResearch/Illustrious-xl-early-release-v0/resolve/dca0dac303e6dc4b0c31d8001bc685b89b5d0204/Illustrious-XL-v0.1.safetensors?download=true", "3e15ba00387db678ab4a099f75771c4f5ac67fda9e7100a01d263eaf30145aa9"),
    ("image_generation", "noobai-xl-1.1"): ("NoobAI-XL-v1.1.safetensors", 7105349958, "https://huggingface.co/Laxhar/noobai-XL-1.1/resolve/814a274af2b8097c0828819d561ec74c7d0c6cea/NoobAI-XL-v1.1.safetensors?download=true", "6681e8e4b134c81f16533acedb0d406d7e5e366e1624b4105178c64d00b05d51"),
    ("image_generation", "aam-xl-animemix"): ("AAM_XL_Anime_Mix.safetensors", 6939220250, "https://huggingface.co/Lykon/AAM_XL_AnimeMix/resolve/693a8d0f4dde950c6ae9897000fe31be9eb93f6e/AAM_XL_Anime_Mix.safetensors?download=true", "d48c2391e04f7dbc83d8538b71be8a30991f3e4e2ff1a8a757b849381f769a8b"),
    ("image_generation", "sdxl-flash"): ("SDXL-Flash.safetensors", 7105349016, "https://huggingface.co/sd-community/sdxl-flash/resolve/e34c99ffd7d7e8da501fef94e7119f65dd8ee542/SDXL-Flash.safetensors?download=true", "e410205ff70b96cea83a09c920cc5f626b32258a99dcf2210a4dea1671e6c2cc"),
    ("image_generation", "realistic-vision-5.1"): ("Realistic_Vision_V5.1.safetensors", 4265097044, "https://huggingface.co/SG161222/Realistic_Vision_V5.1_noVAE/resolve/1e9f017a7b1eaefb63a1900ea6c5953d2739fd21/Realistic_Vision_V5.1.safetensors?download=true", "00445494c80979e173c267644ea2d7c67a37fe3c50c9f4d5a161d8ecdd96cb2f"),
    ("image_generation", "paragon-1.0"): ("Paragon_1.0_Beta.safetensors", 4244099148, "https://huggingface.co/SG161222/Paragon_V1.0/resolve/a14527663e4c65e4af9095008a9973db1b23d2f5/Paragon_1.0_Beta.safetensors?download=true", "5a6045090aafaea30529381ed6637d0072549c0379665f22aec952abe1be43a9"),
    ("image_generation", "reliberate-v3"): ("Reliberate_v3.safetensors", 2132625894, "https://huggingface.co/XpucT/Reliberate/resolve/75fbc70b46c6855c2312b04ec5f89a867a078523/Reliberate_v3.safetensors?download=true", "c536d070ef3c8430049d3dbb624d6aa97da9879e09831f686b77181eb1e7b092"),
    ("image_generation", "counterfeit-v3.0"): ("Counterfeit-V3.0_fix_fp16.safetensors", 2132651162, "https://huggingface.co/gsdf/Counterfeit-V3.0/resolve/d8ce63b3eb8e9102e54b3fcc07f9cb7a28ef30fc/Counterfeit-V3.0_fix_fp16.safetensors?download=true", "a54c944e4c04e9d9ca43468ef5b90ea0408bb6264829a4980de79a768df7179f"),
    ("image_generation", "counterfeit-v2.5"): ("Counterfeit-V2.5_fp16.safetensors", 2132626071, "https://huggingface.co/gsdf/Counterfeit-V2.5/resolve/93c5412baf37cbfa23a3278f7b33b0328db581fb/Counterfeit-V2.5_fp16.safetensors?download=true", "71e703a0fca0e284dd9868bca3ce63c64084db1f0d68835f0a31e1f4e5b7cca6"),
    ("image_generation", "neverending-dream"): ("NeverEndingDream_1.22_BakedVae_fp16.safetensors", 3455797598, "https://huggingface.co/Lykon/NeverEnding-Dream/resolve/239d0482dc703082d1b2b1a7b6051790ecd6d28c/NeverEndingDream_1.22_BakedVae_fp16.safetensors?download=true", "ecefb796ffcb7e4099df12f6419dffad671caa88780ff84fac9d680947173a0f"),
    ("image_generation", "aam-anylora-animemix"): ("AAM_anylora_animemix.safetensors", 3455824250, "https://huggingface.co/Lykon/AAM_Anylora_AnimeMix/resolve/2aac4e8fa7d29af5cb47818adb8134c96bdeae6f/AAM_anylora_animemix.safetensors?download=true", "354b8c571d3abe963e1520d3b4e0647be519841f4376fc5d16f7f5e7859f7d49"),
    ("image_generation", "openjourney"): ("mdjrny-v4.safetensors", 2132625462, "https://huggingface.co/prompthero/openjourney/resolve/f4572661b028c732b2b97c8fbdc32fa5db3afe03/mdjrny-v4.safetensors?download=true", "aba96b389d00943360a82d4b955c85a44baefec493a96bbd9d521258e6e467a0"),
    ("image_generation", "analog-diffusion"): ("analog-diffusion-1.0.safetensors", 2132625462, "https://huggingface.co/wavymulder/Analog-Diffusion/resolve/211449c273875dedc683fdb5a95d8a0ff9d76484/analog-diffusion-1.0.safetensors?download=true", "51f6fff5088a9c5f5aa7cefa0a5a859d0424fc68fdc440e0ee5608a2b82e5ff9"),
    ("image_generation", "protogen-x5.8"): ("ProtoGen_X5.8-pruned-fp16.safetensors", 1719136934, "https://huggingface.co/darkstorm2150/Protogen_x5.8_Official_Release/resolve/9e9ff27a0b5bbbaaa6aa911532d8cc3624587824/ProtoGen_X5.8-pruned-fp16.safetensors?download=true", "847da9eead08fa6dfc11f95e479ee4e12bc6da4747b006ccefcd1e0e498f62c1"),
    ("image_generation", "rev-animated-1.2.2"): ("rev_1.2.2-fp16.safetensors", 4244098720, "https://huggingface.co/s6yx/ReV_Animated/resolve/e77d9937e1d3772579eb9f94f415f2a69b517e49/rev_1.2.2/rev_1.2.2-fp16.safetensors?download=true", "f8bb2922e1dc877dc0d33ed9b9dbbdba612b8b37711cbe17d803dbc92dd65b78"),
    ("image_generation", "blue-pencil-xl-7.0"): ("blue_pencil-XL-v7.0.0.safetensors", 6938041144, "https://huggingface.co/bluepen5805/blue_pencil-XL/resolve/a9b51617185da2ab086af93564cb85710c59d9d3/blue_pencil-XL-v7.0.0.safetensors?download=true", "cff5a50ddeddad52b655a1786992b8f60ffa863b5678527722c1ce404d7f4ee7"),
    ("image_generation", "visionix-alpha"): ("Visionix-alpha.safetensors", 6938040682, "https://huggingface.co/ehristoforu/Visionix-alpha/resolve/8e2c24abace1c06d2008e2b21fe81d98be01f526/Visionix-alpha.safetensors?download=true", "4d3ea2c42e680b45c86d2847b3bb1b9a29352561b943430939574724081ff114"),
    ("image_generation", "illustrious-xl-1.1"): ("Illustrious-XL-v1.1.safetensors", 6938040728, "https://huggingface.co/OnomaAIResearch/Illustrious-XL-v1.1/resolve/8d966ec810874502d56a22ec9130dab6ef74c5ff/Illustrious-XL-v1.1.safetensors?download=true", "536863e9f0c13b0ce834e2f8a19ada425ee4f722c0ad3d0051ec7e6adaa8156c"),
    ("image_generation", "mann-e-dreams-0.0.4"): ("Mann-E_Dreams-0.0.4.safetensors", 6938041602, "https://huggingface.co/mann-e/Mann-E_Dreams/resolve/aeab11b103b9211559139b4039011f634722a758/Mann-E_Dreams-0.0.4.safetensors?download=true", "db43675ba5c83ae9a5c2afc7c5bcfdaaa9a01c72e4ff25d74100e452fb24db77"),
    ("image_generation", "sdxl-flash-mini"): ("SDXL-Flash_Mini.safetensors", 4465671322, "https://huggingface.co/sd-community/sdxl-flash-mini/resolve/7402868d57e5e72d029b56e5cf2dbad90f083c91/SDXL-Flash_Mini.safetensors?download=true", "2abc508f756ac79687d1cd7b520aa9e4b93b76dc9c7b9e9ace24da4762ea1381"),
    ("image_generation", 'flux1-schnell-fp8'): ('flux1-schnell-fp8.safetensors', 17236328572, 'https://huggingface.co/Comfy-Org/flux1-schnell/resolve/c2b683ea00713d6feadcd54b39e3725bbc78638b/flux1-schnell-fp8.safetensors?download=true', 'ead426278b49030e9da5df862994f25ce94ab2ee4df38b556ddddb3db093bf72'),
    ("image_generation", 'flux1-dev-fp8'): ('flux1-dev-fp8.safetensors', 17246524772, 'https://huggingface.co/Comfy-Org/flux1-dev/resolve/83c446ef27a6ac1e9e36ecf13257283aa12cf22a/flux1-dev-fp8.safetensors?download=true', '8e91b68084b53a7fc44ed2a3756d821e355ac1a7b6fe29be760c1db532f3d88a'),
    ("image_generation", 'sd35-medium'): ('sd3.5_medium_incl_clips_t5xxlfp8scaled.safetensors', 11638004202, 'https://huggingface.co/Comfy-Org/stable-diffusion-3.5-fp8/resolve/934251d1e6168bcb45a069abc5bd5cdd5f8fea00/sd3.5_medium_incl_clips_t5xxlfp8scaled.safetensors?download=true', '1778e8857679042c176c21cd8a0da7b29bded68be018557477f84419df79bacf'),
    ("image_generation", 'sd35-large-fp8'): ('sd3.5_large_fp8_scaled.safetensors', 14934922866, 'https://huggingface.co/Comfy-Org/stable-diffusion-3.5-fp8/resolve/934251d1e6168bcb45a069abc5bd5cdd5f8fea00/sd3.5_large_fp8_scaled.safetensors?download=true', '5ad94d6f951556b1ab6b75930fd4effbafaf3130fe9df440e7f2d05a220dd1be'),
    ("image_generation", 'lumina-image-2.0'): ('lumina_2.safetensors', 10620240765, 'https://huggingface.co/Comfy-Org/Lumina_Image_2.0_Repackaged/resolve/5b072540ef86570fecb8249c505f23d5bdeb88cd/all_in_one/lumina_2.safetensors?download=true', 'b703b020a8ff07994e06b3dfa1b28625192bdbfa39aa086a33ea6d6fd206b186'),
    ("image_generation", 'deliberate-v2'): ('Deliberate_v2.safetensors', 2132625431, 'https://huggingface.co/XpucT/Deliberate/resolve/740b11ddb4d7999a37069b4c5a3f3f3aeee97644/Deliberate_v2.safetensors?download=true', '9aba26abdfcd46073e0a1d42027a3a3bcc969f562d58a03637bf0a0ded6586c9'),
    ("image_generation", 'epic-diffusion-1.1'): ('epic-diffusion.safetensors', 2132625431, 'https://huggingface.co/johnslegers/epic-diffusion/resolve/1e092ea9beb780df1f504ddde34fe78a13cb0fb9/epic-diffusion.safetensors?download=true', '28b74d4f6871b7b2693c623fd78d1d8e9cc5ee9d92d13d9934125ec8a871a5ed'),
    ("image_generation", 'portraitplus-1.0'): ('portraitplus-1.0.safetensors', 2132625462, 'https://huggingface.co/wavymulder/portraitplus/resolve/edc3dc5233916dd716d1413dc2f8d427cb4f3e69/portrait+1.0.safetensors?download=true', '90674f4d8629e57da4dfe546e8b7db65a0ee72e2226e02cc595d7897895301a9'),
    ("image_generation", 'cyberpunk-anime-diffusion'): ('Cyberpunk-Anime-Diffusion.safetensors', 2132650554, 'https://huggingface.co/DGSpitzer/Cyberpunk-Anime-Diffusion/resolve/2b6407002b73374e6864d3647f4eb9659bca36a9/Cyberpunk-Anime-Diffusion.safetensors?download=true', 'ab55b3722e7484e2e11187e4d44ec83df46485fc683995488bbb97bf664286ff'),
}

# LoRA files are deliberately kept separate from checkpoints: they live in
# models/loras and can only augment a compatible base model.  The download is
# pinned to the publisher revision and verified before it replaces anything.
COMFYUI_LORA_CATALOG = {
    ("image_generation", "lora-3d-render-style-xl"): (
        "3d_render_style_xl.safetensors",
        85450700,
        "https://huggingface.co/goofyai/3d_render_style_xl/resolve/5ec74a57db5e244a2157173781a7b29045f88237/3d_render_style_xl.safetensors?download=true",
        "393de3e04d956b13ac1a41c3a932e196330ad03d7a70abc2a174d911376ec397",
    ),
}
# Ensembles multi-fichiers des familles DiT (ComfyUI models/<sous-dossier>/<fichier>) : chaque fichier est
# épinglé (révision, taille, SHA-256) ; un fichier partagé entre ensembles (encodeur T5, VAE FLUX) n'est
# téléchargé qu'une fois et n'est retiré que lorsque plus aucun ensemble installé ne s'en sert.
COMFYUI_BUNDLES = {
    # Stable Cascade : deux checkpoints ComfyUI (étage C = modèle + encodeur de texte, étage B = modèle + VAE), tous deux dans checkpoints/.
    ("image_generation", 'stable-cascade'): (('checkpoints', 'stable_cascade_stage_c.safetensors', 9224575566, 'https://huggingface.co/stabilityai/stable-cascade/resolve/a89f66d459ae653e3b4d4f992a7c3789d0dc4d16/comfyui_checkpoints/stable_cascade_stage_c.safetensors?download=true', '088ddf1e444abf399007b2da2bac87791df165c69f477994f6b3c745a20904b0'), ('checkpoints', 'stable_cascade_stage_b.safetensors', 4552331265, 'https://huggingface.co/stabilityai/stable-cascade/resolve/a89f66d459ae653e3b4d4f992a7c3789d0dc4d16/comfyui_checkpoints/stable_cascade_stage_b.safetensors?download=true', '6c218dc948575e3b14b03dffe2014d7870ac505005770ce3abdc28e920a03c05')),
    ("image_generation", 'z-image-turbo'): (('diffusion_models', 'z_image_turbo_bf16.safetensors', 12309866400, 'https://huggingface.co/Comfy-Org/z_image_turbo/resolve/6fc90a3b1b653e935a0d175e260736de25b84df5/split_files/diffusion_models/z_image_turbo_bf16.safetensors?download=true', '2407613050b809ffdff18a4ac99af83ea6b95443ecebdf80e064a79c825574a6'), ('text_encoders', 'qwen_3_4b.safetensors', 8044982048, 'https://huggingface.co/Comfy-Org/z_image_turbo/resolve/6fc90a3b1b653e935a0d175e260736de25b84df5/split_files/text_encoders/qwen_3_4b.safetensors?download=true', '6c671498573ac2f7a5501502ccce8d2b08ea6ca2f661c458e708f36b36edfc5a'), ('vae', 'ae.safetensors', 335304388, 'https://huggingface.co/Comfy-Org/z_image_turbo/resolve/6fc90a3b1b653e935a0d175e260736de25b84df5/split_files/vae/ae.safetensors?download=true', 'afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38'),),
    ("image_generation", 'qwen-image-fp8'): (('diffusion_models', 'qwen_image_fp8_e4m3fn.safetensors', 20430635136, 'https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/resolve/1f12b17be14c89b026c51a91d67c32f84bb047bc/split_files/diffusion_models/qwen_image_fp8_e4m3fn.safetensors?download=true', '98763a127701eb6fb59096f7742cb3aa7d64ed510b9f4e882d8351f8176e3ce3'), ('text_encoders', 'qwen_2.5_vl_7b_fp8_scaled.safetensors', 9384670680, 'https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/resolve/1f12b17be14c89b026c51a91d67c32f84bb047bc/split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors?download=true', 'cb5636d852a0ea6a9075ab1bef496c0db7aef13c02350571e388aea959c5c0b4'), ('vae', 'qwen_image_vae.safetensors', 253806246, 'https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/resolve/1f12b17be14c89b026c51a91d67c32f84bb047bc/split_files/vae/qwen_image_vae.safetensors?download=true', 'a70580f0213e67967ee9c95f05bb400e8fb08307e017a924bf3441223e023d1f'),),
    ("image_generation", 'hidream-i1-dev-fp8'): (('diffusion_models', 'hidream_i1_dev_fp8.safetensors', 17105946040, 'https://huggingface.co/Comfy-Org/HiDream-I1_ComfyUI/resolve/0703a0bd5983e85e5f5b007577263164938de251/split_files/diffusion_models/hidream_i1_dev_fp8.safetensors?download=true', '9a372d7384d56e34a8cc7fd77a0fa3d26d6b75d82c7582fd5347e2fd9e6f8664'), ('text_encoders', 'clip_l_hidream.safetensors', 247586528, 'https://huggingface.co/Comfy-Org/HiDream-I1_ComfyUI/resolve/0703a0bd5983e85e5f5b007577263164938de251/split_files/text_encoders/clip_l_hidream.safetensors?download=true', '706fdb88e22e18177b207837c02f4b86a652abca0302821f2bfa24ac6aea4f71'), ('text_encoders', 'clip_g_hidream.safetensors', 1389743104, 'https://huggingface.co/Comfy-Org/HiDream-I1_ComfyUI/resolve/0703a0bd5983e85e5f5b007577263164938de251/split_files/text_encoders/clip_g_hidream.safetensors?download=true', '3771e70e36450e5199f30bad61a53faae85a2e02606974bcda0a6a573c0519d5'), ('text_encoders', 't5xxl_fp8_e4m3fn_scaled.safetensors', 5157348688, 'https://huggingface.co/Comfy-Org/HiDream-I1_ComfyUI/resolve/0703a0bd5983e85e5f5b007577263164938de251/split_files/text_encoders/t5xxl_fp8_e4m3fn_scaled.safetensors?download=true', 'a498f0485dc9536735258018417c3fd7758dc3bccc0a645feaa472b34955557a'), ('text_encoders', 'llama_3.1_8b_instruct_fp8_scaled.safetensors', 9081258056, 'https://huggingface.co/Comfy-Org/HiDream-I1_ComfyUI/resolve/0703a0bd5983e85e5f5b007577263164938de251/split_files/text_encoders/llama_3.1_8b_instruct_fp8_scaled.safetensors?download=true', '9f86897bbeb933ef4fd06297740edb8dd962c94efcd92b373a11460c33765ea6'), ('vae', 'ae.safetensors', 335304388, 'https://huggingface.co/Comfy-Org/HiDream-I1_ComfyUI/resolve/0703a0bd5983e85e5f5b007577263164938de251/split_files/vae/ae.safetensors?download=true', 'afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38'),),
    ("image_generation", 'hidream-i1-fast-fp8'): (('diffusion_models', 'hidream_i1_fast_fp8.safetensors', 17105946040, 'https://huggingface.co/Comfy-Org/HiDream-I1_ComfyUI/resolve/0703a0bd5983e85e5f5b007577263164938de251/split_files/diffusion_models/hidream_i1_fast_fp8.safetensors?download=true', '2d471e82523234e157b9ddd44c0428c89907eeb4e7923b316d49362d17020236'), ('text_encoders', 'clip_l_hidream.safetensors', 247586528, 'https://huggingface.co/Comfy-Org/HiDream-I1_ComfyUI/resolve/0703a0bd5983e85e5f5b007577263164938de251/split_files/text_encoders/clip_l_hidream.safetensors?download=true', '706fdb88e22e18177b207837c02f4b86a652abca0302821f2bfa24ac6aea4f71'), ('text_encoders', 'clip_g_hidream.safetensors', 1389743104, 'https://huggingface.co/Comfy-Org/HiDream-I1_ComfyUI/resolve/0703a0bd5983e85e5f5b007577263164938de251/split_files/text_encoders/clip_g_hidream.safetensors?download=true', '3771e70e36450e5199f30bad61a53faae85a2e02606974bcda0a6a573c0519d5'), ('text_encoders', 't5xxl_fp8_e4m3fn_scaled.safetensors', 5157348688, 'https://huggingface.co/Comfy-Org/HiDream-I1_ComfyUI/resolve/0703a0bd5983e85e5f5b007577263164938de251/split_files/text_encoders/t5xxl_fp8_e4m3fn_scaled.safetensors?download=true', 'a498f0485dc9536735258018417c3fd7758dc3bccc0a645feaa472b34955557a'), ('text_encoders', 'llama_3.1_8b_instruct_fp8_scaled.safetensors', 9081258056, 'https://huggingface.co/Comfy-Org/HiDream-I1_ComfyUI/resolve/0703a0bd5983e85e5f5b007577263164938de251/split_files/text_encoders/llama_3.1_8b_instruct_fp8_scaled.safetensors?download=true', '9f86897bbeb933ef4fd06297740edb8dd962c94efcd92b373a11460c33765ea6'), ('vae', 'ae.safetensors', 335304388, 'https://huggingface.co/Comfy-Org/HiDream-I1_ComfyUI/resolve/0703a0bd5983e85e5f5b007577263164938de251/split_files/vae/ae.safetensors?download=true', 'afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38'),),
    ("image_generation", 'hidream-i1-full-fp8'): (('diffusion_models', 'hidream_i1_full_fp8.safetensors', 17105946040, 'https://huggingface.co/Comfy-Org/HiDream-I1_ComfyUI/resolve/0703a0bd5983e85e5f5b007577263164938de251/split_files/diffusion_models/hidream_i1_full_fp8.safetensors?download=true', '807e458dd2ea31e69e40db27afadde4a14ffd0c4c0a23b8c9a0833102e055337'), ('text_encoders', 'clip_l_hidream.safetensors', 247586528, 'https://huggingface.co/Comfy-Org/HiDream-I1_ComfyUI/resolve/0703a0bd5983e85e5f5b007577263164938de251/split_files/text_encoders/clip_l_hidream.safetensors?download=true', '706fdb88e22e18177b207837c02f4b86a652abca0302821f2bfa24ac6aea4f71'), ('text_encoders', 'clip_g_hidream.safetensors', 1389743104, 'https://huggingface.co/Comfy-Org/HiDream-I1_ComfyUI/resolve/0703a0bd5983e85e5f5b007577263164938de251/split_files/text_encoders/clip_g_hidream.safetensors?download=true', '3771e70e36450e5199f30bad61a53faae85a2e02606974bcda0a6a573c0519d5'), ('text_encoders', 't5xxl_fp8_e4m3fn_scaled.safetensors', 5157348688, 'https://huggingface.co/Comfy-Org/HiDream-I1_ComfyUI/resolve/0703a0bd5983e85e5f5b007577263164938de251/split_files/text_encoders/t5xxl_fp8_e4m3fn_scaled.safetensors?download=true', 'a498f0485dc9536735258018417c3fd7758dc3bccc0a645feaa472b34955557a'), ('text_encoders', 'llama_3.1_8b_instruct_fp8_scaled.safetensors', 9081258056, 'https://huggingface.co/Comfy-Org/HiDream-I1_ComfyUI/resolve/0703a0bd5983e85e5f5b007577263164938de251/split_files/text_encoders/llama_3.1_8b_instruct_fp8_scaled.safetensors?download=true', '9f86897bbeb933ef4fd06297740edb8dd962c94efcd92b373a11460c33765ea6'), ('vae', 'ae.safetensors', 335304388, 'https://huggingface.co/Comfy-Org/HiDream-I1_ComfyUI/resolve/0703a0bd5983e85e5f5b007577263164938de251/split_files/vae/ae.safetensors?download=true', 'afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38'),),
    ("image_generation", 'chroma1-hd'): (('diffusion_models', 'Chroma1-HD.safetensors', 17800038288, 'https://huggingface.co/lodestones/Chroma1-HD/resolve/0e0c60ece1e82b17cb7f77342d765ba5024c40c0/Chroma1-HD.safetensors?download=true', 'd446d9695d08276f61e53653e025289dd96f7a489c27982a1fa54ecabc06642a'), ('text_encoders', 't5xxl_fp8_e4m3fn_scaled.safetensors', 5157348688, 'https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/6af2a98e3f615bdfa612fbd85da93d1ed5f69ef5/t5xxl_fp8_e4m3fn_scaled.safetensors?download=true', 'a498f0485dc9536735258018417c3fd7758dc3bccc0a645feaa472b34955557a'), ('vae', 'ae.safetensors', 335304388, 'https://huggingface.co/Comfy-Org/z_image_turbo/resolve/6fc90a3b1b653e935a0d175e260736de25b84df5/split_files/vae/ae.safetensors?download=true', 'afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38'),),
}

COMFYUI_REQUIREMENTS = {
    "sdxl-base-1.0": (10000, 12000), "sd-turbo": (8000, 10000),
    "dreamshaper-8": (6000, 8000), "sdxl-turbo": (8000, 12000),
    "absolutereality-1.8.1": (6000, 8000),
    "sdxl-refiner-1.0": (10000, 12000),
    "stable-diffusion-1.5": (6000, 8000),
    "stable-diffusion-2.1-base": (6000, 8000), "stable-diffusion-2.1": (6000, 8000), "stable-cascade": (10000, 12000),
    "ssd-1b": (8000, 10000),
    "segmind-vega": (6000, 8000),
    "realistic-vision-6.0": (6000, 8000),
    "realvisxl-4.0": (10000, 12000),
    "playground-2.5": (10000, 12000),
    "juggernaut-xl-v9": (10000, 12000),
    "juggernaut-xl-lightning": (10000, 12000),
    "dreamshaper-xl-lightning": (10000, 12000),
    "dreamshaper-xl-turbo-v2": (10000, 12000),
    "animagine-xl-4.0": (10000, 12000),
    "animagine-xl-3.1": (10000, 12000),
    "sdxl-lightning-2step": (10000, 12000),
    "sdxl-lightning-4step": (10000, 12000),
    "sdxl-lightning-8step": (10000, 12000),
    "realvisxl-5.0": (10000, 12000),
    "realvisxl-5.0-lightning": (10000, 12000),
    "realvisxl-4.0-lightning": (10000, 12000),
    "playground-v2": (10000, 12000),
    "proteus-0.5": (10000, 12000),
    "kohaku-xl-zeta": (10000, 12000),
    "illustrious-xl-2.0": (10000, 12000),
    "illustrious-xl-0.1": (10000, 12000),
    "noobai-xl-1.1": (10000, 12000),
    "aam-xl-animemix": (10000, 12000),
    "sdxl-flash": (10000, 12000),
    "realistic-vision-5.1": (6000, 8000),
    "paragon-1.0": (6000, 8000),
    "reliberate-v3": (6000, 8000),
    "counterfeit-v3.0": (6000, 8000),
    "counterfeit-v2.5": (6000, 8000),
    "neverending-dream": (6000, 8000),
    "aam-anylora-animemix": (6000, 8000),
    "openjourney": (6000, 8000),
    "analog-diffusion": (6000, 8000),
    "protogen-x5.8": (6000, 8000),
    "rev-animated-1.2.2": (6000, 8000),
    "blue-pencil-xl-7.0": (10000, 12000),
    "visionix-alpha": (10000, 12000),
    "illustrious-xl-1.1": (10000, 12000),
    "mann-e-dreams-0.0.4": (10000, 12000),
    "sdxl-flash-mini": (8000, 10000),
    'flux1-schnell-fp8': (12000, 16000),
    'flux1-dev-fp8': (12000, 16000),
    'sd35-medium': (10000, 12000),
    'sd35-large-fp8': (16000, 24000),
    'lumina-image-2.0': (10000, 12000),
    'z-image-turbo': (12000, 16000),
    'qwen-image-fp8': (20000, 24000),
    'hidream-i1-dev-fp8': (20000, 24000),
    'hidream-i1-fast-fp8': (20000, 24000),
    'hidream-i1-full-fp8': (20000, 24000),
    'chroma1-hd': (12000, 16000),
    'deliberate-v2': (6000, 8000),
    'epic-diffusion-1.1': (6000, 8000),
    'portraitplus-1.0': (6000, 8000),
    'cyberpunk-anime-diffusion': (6000, 8000),
}
COMFYUI_REQUIREMENTS["lora-3d-render-style-xl"] = (10000, 12000)
# Modèles utilitaires du Labo image (agent 1.39.0, capacité comfyui_image_lab_v1) : agrandisseur, détourage, cartes
# de contrôle, description. Table DISTINCTE des modèles de génération : un fichier d'ici n'est jamais annoncé « text-to-image » ;
# chaque entrée porte ses propres capacités et l'outil du Labo qu'elle sert. Mêmes garanties que les ensembles DiT :
# dépôt officiel Comfy-Org, révision épinglée, taille et SHA-256 par fichier, safetensors uniquement (aucun pickle).
# « files » : (sous-dossier de ComfyUI/models, fichier, taille, URL épinglée, SHA-256). « vram » : (minimum,
# conseillé) en Mo — estimations non mesurées sur un vrai moteur.
COMFYUI_UTILITY_MODELS = {
    ("image_generation", "lab-realesrgan-x4plus"): {
        "tool": "upscale", "capabilities": ("image-upscale",), "vram": (2000, 4000),
        "files": (("upscale_models", "RealESRGAN_x4plus.safetensors", 66857836,
                   "https://huggingface.co/Comfy-Org/Real-ESRGAN_repackaged/resolve/5fd49b7b278836f48af63ecd314d0f98ab336105/RealESRGAN_x4plus.safetensors?download=true",
                   "37f9a931c215f040aa6d50f711f2cb115f713c46df1d0d6469a8bd7bfe9a60bb"),),
    },
    ("image_generation", "lab-birefnet"): {
        "tool": "remove_background", "capabilities": ("background-removal",), "vram": (4000, 6000),
        "files": (("background_removal", "birefnet.safetensors", 444473596,
                   "https://huggingface.co/Comfy-Org/BiRefNet/resolve/25511f8787e51912e1480706b4e47b8f467fbf72/background_removal/birefnet.safetensors?download=true",
                   "9ab37426bf4de0567af6b5d21b16151357149139362e6e8992021b8ce356a154"),),
    },
    ("image_generation", "lab-depth-anything-3-small"): {
        "tool": "control_depth", "capabilities": ("control-map-depth",), "vram": (2000, 4000),
        "files": (("geometry_estimation", "depth_anything_3_small.safetensors", 137254980,
                   "https://huggingface.co/Comfy-Org/Depth-Anything-3/resolve/1aaeea516f22fb2f4c7fae761c8ed470c6cf9f93/geometry_estimation/depth_anything_3_small.safetensors?download=true",
                   "9c0a53d157c5b315d4c82908a43d93eeaa3a9ac3de9d15c2b4242ef46b5508ad"),),
    },
    ("image_generation", "lab-depth-anything-3-base"): {
        "tool": "control_depth", "capabilities": ("control-map-depth",), "vram": (3000, 6000),
        "files": (("geometry_estimation", "depth_anything_3_base.safetensors", 541524124,
                   "https://huggingface.co/Comfy-Org/Depth-Anything-3/resolve/1aaeea516f22fb2f4c7fae761c8ed470c6cf9f93/geometry_estimation/depth_anything_3_base.safetensors?download=true",
                   "418c0d2ea857e2d1215fa51baa46833f499a62eb2400ec63d337aa20d326414f"),),
    },
    ("image_generation", "lab-sdpose-wholebody"): {
        "tool": "control_pose", "capabilities": ("control-map-pose",), "vram": (6000, 8000),
        "files": (("checkpoints", "sdpose_wholebody_fp16.safetensors", 1916645792,
                   "https://huggingface.co/Comfy-Org/SDPose/resolve/acb43fbe8142b54d9ebd52b720b81dd4e14c8d81/checkpoints/sdpose_wholebody_fp16.safetensors?download=true",
                   "63d01f9a7494560693b24767f4469d59c9d3266b31ff0a253e74d1e611442721"),),
    },
    # Description d'image : encodeur de texte Qwen3.5 2B (même empreinte que le fichier de Qwen/Qwen3.5-2B).
    ("image_generation", "lab-qwen35-2b"): {
        "tool": "caption", "capabilities": ("image-caption",), "vram": (6000, 8000),
        "files": (("text_encoders", "qwen3.5_2b_bf16.safetensors", 4548221488,
                   "https://huggingface.co/Comfy-Org/Qwen3.5/resolve/5d50a2252bf1bcd49e5fee9b5f296986d442682b/text_encoders/qwen3.5_2b_bf16.safetensors?download=true",
                   "aa33250c4fc64891ddfaba3a314fd9542ea371843c387178b425fbcc5ed680b1"),),
    },
    # Restauration des visages (agent 1.44.0, capacité image_lab_face_restore_v1) : deux fichiers .pth lus par
    # runners/face_restore_script.py, jamais par ComfyUI. Dossier propre « alpine_face_restore » : surtout pas
    # upscale_models/, où UpscaleModelLoader proposerait GFPGAN comme agrandisseur (spandrel l'appelle en 0–1 au lieu
    # de −1–1 : résultat faux). Le script relit les octets, vérifie le SHA-256, puis charge en weights_only=True.
    # GFPGAN v1.4 : publication officielle TencentARC (aucun safetensors officiel) ; SHA-256 mesuré sur le fichier
    # de cette source le 2026-10-06, identique à l'oid LFS de deux miroirs Hugging Face (gmk123/GFPGAN, leonelhs/gfpgan).
    ("image_generation", "lab-gfpgan-v14"): {
        "tool": "face_restore", "capabilities": ("face-restore",), "vram": (0, 2000),
        "files": (("alpine_face_restore", "GFPGANv1.4.pth", 348632874,
                   "https://github.com/TencentARC/GFPGAN/releases/download/v1.3.0/GFPGANv1.4.pth",
                   "e2cd4703ab14f4d01fd1383a8a8b266f9a5833dacee8e6a79d3bf21a1b6be5ad"),),
    },
    # Détecteur YuNet de Shiqi Yu, chez l'auteur (libfacedetection.train, révision 74f3aa77, tasks/task1/weights/) :
    # même fichier, octet pour octet, que la copie chargée par kornia (kornia/data@e541bd88 ; même SHA-1 de blob Git
    # d741e65a selon l'API GitHub, même SHA-256 mesuré sur les deux téléchargements le 2026-10-06).
    ("image_generation", "lab-yunet-face"): {
        "tool": "face_restore", "capabilities": ("face-detect",), "vram": (0, 500),
        "files": (("alpine_face_restore", "yunet_final.pth", 405729,
                   "https://github.com/ShiqiYu/libfacedetection.train/raw/74f3aa77c63234dd954d21286e9a60703b8d0868/tasks/task1/weights/yunet_final.pth",
                   "ee0a3035c5cbfc9484a31332223c9fd9c0ddcdb7c46b6a19e558ed393b6f2c67"),),
    },
}
for (_tool_id, _utility_id), _utility in COMFYUI_UTILITY_MODELS.items():
    COMFYUI_REQUIREMENTS[_utility_id] = tuple(_utility["vram"])
# Modules du Labo image SANS téléchargement (agent 1.46.0) : du code livré avec l'agent (runners/), exécuté par le Python du
# moteur image. « Installer » le module = vérifier ses prérequis dans l'environnement de ComfyUI (paquets importables,
# fonctions de Pillow utilisées) puis poser un marqueur JSON dans ComfyUI/models/<sous-dossier>/ ; l'inventaire relève ce
# marqueur, le désinstaller le retire. Aucun fichier tiers, aucune adresse, aucune empreinte de téléchargement.
# « marker » : (sous-dossier de ComfyUI/models, fichier) ; « imports » : modules exigés dans l'environnement du moteur ;
# « scripts » : fichiers de l'agent requis (relatifs à worker_agent/).
COMFYUI_LAB_MODULES = {
    ("image_generation", "lab-maps-tools"): {
        "tool": "image_maps", "capabilities": ("image-maps",), "vram": (0, 0),
        "marker": ("alpine_lab_maps", "lab-maps-tools.json"),
        "imports": ("PIL", "numpy"),
        "scripts": ("runners/image_maps.py", "runners/image_maps_script.py"),
    },
}
for (_tool_id, _module_id), _module in COMFYUI_LAB_MODULES.items():
    COMFYUI_REQUIREMENTS[_module_id] = tuple(_module["vram"])
HUNYUAN_CATALOG = {
    "hunyuan3d-2": ("tencent/Hunyuan3D-2mini", "hunyuan3d-2mini"),
    "hunyuan-dit": ("Tencent-Hunyuan/HunyuanDiT-v1.1-Diffusers-Distilled", "hunyuan-dit"),
    "hunyuan3d-2mv": ("tencent/Hunyuan3D-2mv", "hunyuan3d-2mv"),
    "hunyuan3d-2-paint": ("tencent/Hunyuan3D-2", "hunyuan3d-2"),
    "hunyuan3d-2-standard": ("tencent/Hunyuan3D-2", "hunyuan3d-2-standard"),
    "hunyuan3d-2mini-turbo": ("tencent/Hunyuan3D-2mini", "hunyuan3d-2mini-turbo"),
    "hunyuan3d-2-turbo": ("tencent/Hunyuan3D-2", "hunyuan3d-2-turbo"),
    "hunyuan3d-2-fast": ("tencent/Hunyuan3D-2", "hunyuan3d-2-fast"),
    "hunyuan3d-2mini-fast": ("tencent/Hunyuan3D-2mini", "hunyuan3d-2mini-fast"),
    "hunyuan3d-2mv-turbo": ("tencent/Hunyuan3D-2mv", "hunyuan3d-2mv-turbo"),
    "hunyuan3d-2mv-fast": ("tencent/Hunyuan3D-2mv", "hunyuan3d-2mv-fast"),
    "hunyuan3d-21-shape": ("tencent/Hunyuan3D-2.1", "hunyuan3d-21-shape"),
    "hunyuan3d-21-paint-pbr": ("tencent/Hunyuan3D-2.1", "hunyuan3d-21-paint"),
    "hunyuan3d-2-paint-turbo": ("tencent/Hunyuan3D-2", "hunyuan3d-2-paint-turbo"),
    "hunyuan-dit-1.2": ("Tencent-Hunyuan/HunyuanDiT-v1.2-Diffusers-Distilled", "hunyuan-dit-1.2"),
    "triposg": ("VAST-AI/TripoSG", "triposg"),
    "triposg-scribble": ("VAST-AI/TripoSG-scribble", "triposg-scribble"),
    "partcrafter": ("wgsxm/PartCrafter", "partcrafter"),
    "partcrafter-scene": ("wgsxm/PartCrafter-Scene", "partcrafter-scene"),
    "triposr": ("stabilityai/TripoSR", "triposr"),
}
HUNYUAN_REQUIREMENTS = {
    "hunyuan3d-2": (6000, 12000, ["image-to-3d"]),
    "hunyuan-dit": (8000, 12000, ["text-to-3d"]),
    "hunyuan3d-2mv": (8000, 12000, ["multi-view-to-3d"]),
    "hunyuan3d-2-paint": (12000, 16000, ["texture-3d"]),
    "hunyuan3d-2-standard": (8000, 12000, ["image-to-3d"]),
    "hunyuan3d-2mini-turbo": (6000, 12000, ["image-to-3d"]),
    "hunyuan3d-2-turbo": (8000, 12000, ["image-to-3d"]),
    "hunyuan3d-2-fast": (8000, 12000, ["image-to-3d"]),
    "hunyuan3d-2mini-fast": (6000, 12000, ["image-to-3d"]),
    "hunyuan3d-2mv-turbo": (8000, 12000, ["multi-view-to-3d"]),
    "hunyuan3d-2mv-fast": (8000, 12000, ["multi-view-to-3d"]),
    "hunyuan3d-21-shape": (10000, 12000, ["image-to-3d"]),
    "hunyuan3d-21-paint-pbr": (21000, 24000, ["texture-3d"]),
    "hunyuan3d-2-paint-turbo": (12000, 16000, ["texture-3d"]),
    "hunyuan-dit-1.2": (8000, 12000, ["text-to-3d"]),
    "triposg": (8000, 12000, ["image-to-3d"]),
    "triposg-scribble": (6000, 8000, ["image-to-3d"]),
    "partcrafter": (8000, 12000, ["image-to-3d"]),
    "partcrafter-scene": (10000, 16000, ["image-to-3d"]),
    "triposr": (6000, 8000, ["image-to-3d"]),
}
# Combined checkpoints include the DiT, VAE and image conditioner. FlashVDM
# decoder replacement is optional and is not enabled by the dashboard runtime.
HUNYUAN_SNAPSHOT_OPTIONS = {
    "hunyuan3d-2-standard": ("9cd649ba6913f7a852e3286bad86bfa9a2d83dcf", "hunyuan3d-dit-v2-0", "360bc281fc956d4acac0c3d36d5ec0ebf8cdddbf4b8892e894d12419388d479b"),
    "hunyuan3d-2mini-turbo": ("f90a0f7df7d5e6f71109cf333f6a95a0ae3194a6", "hunyuan3d-dit-v2-mini-turbo", "bdbcef30dd0149a281e17d5b5b1fdad1122c904e098a42f3100e04e03c247bc4"),
    "hunyuan3d-2-turbo": ("9cd649ba6913f7a852e3286bad86bfa9a2d83dcf", "hunyuan3d-dit-v2-0-turbo", "5ee5a81e4df08a1c65b79910bf5b145a90376e526794f4607a4d5d068d62f269"),
    "hunyuan3d-2-fast": ("9cd649ba6913f7a852e3286bad86bfa9a2d83dcf", "hunyuan3d-dit-v2-0-fast", "fcf91f29922270b4cce5f266099b306c6e140f2a2382a81fc5e47752d4d41e85"),
    "hunyuan3d-2mini-fast": ("f90a0f7df7d5e6f71109cf333f6a95a0ae3194a6", "hunyuan3d-dit-v2-mini-fast", "2bc48c8168874bb3f3d9bb6699af40517e498d2da3400c9c54dc0f5779875ab3"),
    "hunyuan3d-2mv-turbo": ("3a761b539b29fe4ff64714813aa9560fd66f5de0", "hunyuan3d-dit-v2-mv-turbo", "172d7a989b99f66af760da0080d8c6f6350c72609536cf0c55b87a4afa574e45"),
    "hunyuan3d-2mv-fast": ("3a761b539b29fe4ff64714813aa9560fd66f5de0", "hunyuan3d-dit-v2-mv-fast", "dcd6e2716ff13f2de1e4480bcfe779f330f6494af6f1bedbd4f1e503f198e785"),
}

# Models served by a backend module (its own environment) rather than the native hy3dgen
# engine; the engine service launches the module runner for them.
AI3D_BACKENDS = {
    "hunyuan3d-21-shape": "hunyuan3d-21",
    "hunyuan3d-21-paint-pbr": "hunyuan3d-21",
    "triposg": "open3d-lab",
    "triposg-scribble": "open3d-lab",
    "partcrafter": "open3d-lab",
    "partcrafter-scene": "open3d-lab",
    "triposr": "open3d-lab",
}
# Multi-file pinned snapshots: Hugging Face revision, allow/ignore patterns, per-file SHA-256
# (None = presence only), extra single-file downloads and Hub caches, optional post-install step.
AI3D_SNAPSHOTS = {
    "hunyuan-dit": {"revision": "527cf2ecce7c04021975938f8b0e44e35d2b1ed9", "allow_patterns": ["*"], "checks": {"model_index.json": None, "text_encoder/model.safetensors": "c6c6348af2cb4d5852fe51102ce39605903dbe7925c005cf8995506cc21ea914", "text_encoder_2/model-00001-of-00002.safetensors": "2c0c539ab8e8fba3877cc94bc483e427f74c525f817a809b028ebc8d96d75a94", "text_encoder_2/model-00002-of-00002.safetensors": "3b49976cb1fe40da28a600d783f4686024c97eb01f224c54305cce55ddcd8a5e", "transformer/diffusion_pytorch_model.safetensors": "7d31ac8fa389ff39dd0a81430010e52c43b59f15adc00c83625a47881e16830e", "vae/diffusion_pytorch_model.safetensors": "98a14dc6fe8d71c83576f135a87c61a16561c9c080abba418d2cc976ee034f88"}},
    "hunyuan3d-2-paint": {"revision": "9cd649ba6913f7a852e3286bad86bfa9a2d83dcf", "allow_patterns": ["hunyuan3d-paint-v2-0/*", "hunyuan3d-delight-v2-0/*", "LICENSE*", "NOTICE*", "Notice.txt", "README.md"], "checks": {"hunyuan3d-delight-v2-0/feature_extractor/preprocessor_config.json": None, "hunyuan3d-delight-v2-0/model_index.json": None, "hunyuan3d-delight-v2-0/scheduler/scheduler_config.json": None, "hunyuan3d-delight-v2-0/text_encoder/config.json": None, "hunyuan3d-delight-v2-0/text_encoder/model.safetensors": "bc1827c465450322616f06dea41596eac7d493f4e95904dcb51f0fc745c4e13f", "hunyuan3d-delight-v2-0/tokenizer/special_tokens_map.json": None, "hunyuan3d-delight-v2-0/tokenizer/tokenizer_config.json": None, "hunyuan3d-delight-v2-0/tokenizer/vocab.json": None, "hunyuan3d-delight-v2-0/unet/config.json": None, "hunyuan3d-delight-v2-0/unet/diffusion_pytorch_model.safetensors": "0ce61d15a43d11ba19079ab8f24dfce78b876d3f5291470079ef64b17e08ca58", "hunyuan3d-delight-v2-0/vae/config.json": None, "hunyuan3d-delight-v2-0/vae/diffusion_pytorch_model.safetensors": "3e4c08995484ee61270175e9e7a072b66a6e4eeb5f0c266667fe1f45b90daf9a", "hunyuan3d-paint-v2-0/feature_extractor/preprocessor_config.json": None, "hunyuan3d-paint-v2-0/model_index.json": None, "hunyuan3d-paint-v2-0/scheduler/scheduler_config.json": None, "hunyuan3d-paint-v2-0/text_encoder/config.json": None, "hunyuan3d-paint-v2-0/text_encoder/pytorch_model.bin": "c3e254d7b61353497ea0be2c4013df4ea8f739ee88cffa0ba58cd085459ed565", "hunyuan3d-paint-v2-0/tokenizer/special_tokens_map.json": None, "hunyuan3d-paint-v2-0/tokenizer/tokenizer_config.json": None, "hunyuan3d-paint-v2-0/tokenizer/vocab.json": None, "hunyuan3d-paint-v2-0/unet/config.json": None, "hunyuan3d-paint-v2-0/unet/diffusion_pytorch_model.safetensors": "1c5ce434ba976b30bbb51a080917ecd39f8d8761691887b5df3be765ef4bd9e9", "hunyuan3d-paint-v2-0/vae/config.json": None, "hunyuan3d-paint-v2-0/vae/diffusion_pytorch_model.safetensors": "abcec86e499e1ce9f05d1630725d386dc533b61fe0947ab034f07f89042e7a61"}, "ignore_patterns": ["hunyuan3d-paint-v2-0/unet/diffusion_pytorch_model.bin", "hunyuan3d-paint-v2-0/vae/diffusion_pytorch_model.bin"]},
    "hunyuan3d-21-shape": {"revision": "0b94677654c57bb9a6b6845cd7b704ccf551d327", "allow_patterns": ["hunyuan3d-dit-v2-1/*", "LICENSE*", "NOTICE*", "Notice.txt", "README.md"], "checks": {"hunyuan3d-dit-v2-1/config.yaml": None, "hunyuan3d-dit-v2-1/model.fp16.ckpt": "6b519fc7242f78e9b5f47ea4d55668fe3d944a2d27332f4ca68d29a6ff603f5e"}, "cache_repos": [{"repo_id": "facebook/dinov2-large", "revision": "47b73eefe95e8d44ec3623f8890bd894b6ea2d6c", "allow_patterns": ["config.json", "preprocessor_config.json", "model.safetensors"]}]},
    "hunyuan3d-21-paint-pbr": {"revision": "0b94677654c57bb9a6b6845cd7b704ccf551d327", "allow_patterns": ["hunyuan3d-paintpbr-v2-1/*", "LICENSE*", "NOTICE*", "Notice.txt", "README.md"], "checks": {"hunyuan3d-paintpbr-v2-1/feature_extractor/preprocessor_config.json": None, "hunyuan3d-paintpbr-v2-1/image_encoder/config.json": None, "hunyuan3d-paintpbr-v2-1/image_encoder/model.safetensors": "ae616c24393dd1854372b0639e5541666f7521cbe219669255e865cb7f89466a", "hunyuan3d-paintpbr-v2-1/model_index.json": None, "hunyuan3d-paintpbr-v2-1/scheduler/scheduler_config.json": None, "hunyuan3d-paintpbr-v2-1/text_encoder/config.json": None, "hunyuan3d-paintpbr-v2-1/text_encoder/pytorch_model.bin": "c3e254d7b61353497ea0be2c4013df4ea8f739ee88cffa0ba58cd085459ed565", "hunyuan3d-paintpbr-v2-1/tokenizer/special_tokens_map.json": None, "hunyuan3d-paintpbr-v2-1/tokenizer/tokenizer_config.json": None, "hunyuan3d-paintpbr-v2-1/tokenizer/vocab.json": None, "hunyuan3d-paintpbr-v2-1/unet/config.json": None, "hunyuan3d-paintpbr-v2-1/unet/diffusion_pytorch_model.bin": "675a1b5cd0098b2002637c443946529c03c5cd54427f40245263350feb3dd5b8", "hunyuan3d-paintpbr-v2-1/vae/config.json": None, "hunyuan3d-paintpbr-v2-1/vae/diffusion_pytorch_model.bin": "1b4889b6b1d4ce7ae320a02dedaeff1780ad77d415ea0d744b476155c6377ddc"}, "extras": [{"url": "https://huggingface.co/spaces/tencent/Hunyuan3D-2.1/resolve/61d23d8ce7d066d418ed501df7c3ee4329817f14/hy3dpaint/ckpt/RealESRGAN_x4plus.pth", "path": "RealESRGAN_x4plus.pth", "size": 67040989, "sha256": "4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1"}], "cache_repos": [{"repo_id": "facebook/dinov2-giant", "revision": "611a9d42f2335e0f921f1e313ad3c1b7178d206d", "allow_patterns": ["config.json", "preprocessor_config.json", "model.safetensors"]}], "post_install": "hunyuan3d-21-paint"},
    "hunyuan3d-2-paint-turbo": {"revision": "9cd649ba6913f7a852e3286bad86bfa9a2d83dcf", "allow_patterns": ["hunyuan3d-paint-v2-0-turbo/*", "hunyuan3d-delight-v2-0/*", "LICENSE*", "NOTICE*", "Notice.txt", "README.md"], "checks": {"hunyuan3d-delight-v2-0/feature_extractor/preprocessor_config.json": None, "hunyuan3d-delight-v2-0/model_index.json": None, "hunyuan3d-delight-v2-0/scheduler/scheduler_config.json": None, "hunyuan3d-delight-v2-0/text_encoder/config.json": None, "hunyuan3d-delight-v2-0/text_encoder/model.safetensors": "bc1827c465450322616f06dea41596eac7d493f4e95904dcb51f0fc745c4e13f", "hunyuan3d-delight-v2-0/tokenizer/special_tokens_map.json": None, "hunyuan3d-delight-v2-0/tokenizer/tokenizer_config.json": None, "hunyuan3d-delight-v2-0/tokenizer/vocab.json": None, "hunyuan3d-delight-v2-0/unet/config.json": None, "hunyuan3d-delight-v2-0/unet/diffusion_pytorch_model.safetensors": "0ce61d15a43d11ba19079ab8f24dfce78b876d3f5291470079ef64b17e08ca58", "hunyuan3d-delight-v2-0/vae/config.json": None, "hunyuan3d-delight-v2-0/vae/diffusion_pytorch_model.safetensors": "3e4c08995484ee61270175e9e7a072b66a6e4eeb5f0c266667fe1f45b90daf9a", "hunyuan3d-paint-v2-0-turbo/feature_extractor/preprocessor_config.json": None, "hunyuan3d-paint-v2-0-turbo/image_encoder/config.json": None, "hunyuan3d-paint-v2-0-turbo/image_encoder/model.safetensors": "ae616c24393dd1854372b0639e5541666f7521cbe219669255e865cb7f89466a", "hunyuan3d-paint-v2-0-turbo/image_encoder/preprocessor_config.json": None, "hunyuan3d-paint-v2-0-turbo/model_index.json": None, "hunyuan3d-paint-v2-0-turbo/scheduler/scheduler_config.json": None, "hunyuan3d-paint-v2-0-turbo/text_encoder/config.json": None, "hunyuan3d-paint-v2-0-turbo/text_encoder/pytorch_model.bin": "c3e254d7b61353497ea0be2c4013df4ea8f739ee88cffa0ba58cd085459ed565", "hunyuan3d-paint-v2-0-turbo/tokenizer/special_tokens_map.json": None, "hunyuan3d-paint-v2-0-turbo/tokenizer/tokenizer_config.json": None, "hunyuan3d-paint-v2-0-turbo/tokenizer/vocab.json": None, "hunyuan3d-paint-v2-0-turbo/unet/config.json": None, "hunyuan3d-paint-v2-0-turbo/unet/diffusion_pytorch_model.safetensors": "d6acffa4a22f4da61d87f446bfa83e7ac245481c1535fbf25b200fe4462d0b22", "hunyuan3d-paint-v2-0-turbo/vae/config.json": None, "hunyuan3d-paint-v2-0-turbo/vae/diffusion_pytorch_model.bin": "1b4889b6b1d4ce7ae320a02dedaeff1780ad77d415ea0d744b476155c6377ddc"}, "ignore_patterns": ["hunyuan3d-paint-v2-0-turbo/unet/diffusion_pytorch_model.bin"]},
    "hunyuan-dit-1.2": {"revision": "ba991d1546d8c50936c4c16398ed0a87b9b99fb1", "allow_patterns": ["*"], "checks": {"model_index.json": None, "scheduler/scheduler_config.json": None, "text_encoder/config.json": None, "text_encoder/model.safetensors": "c6c6348af2cb4d5852fe51102ce39605903dbe7925c005cf8995506cc21ea914", "text_encoder_2/config.json": None, "text_encoder_2/model-00001-of-00002.safetensors": "2c0c539ab8e8fba3877cc94bc483e427f74c525f817a809b028ebc8d96d75a94", "text_encoder_2/model-00002-of-00002.safetensors": "3b49976cb1fe40da28a600d783f4686024c97eb01f224c54305cce55ddcd8a5e", "text_encoder_2/model.safetensors.index.json": None, "tokenizer/special_tokens_map.json": None, "tokenizer/tokenizer_config.json": None, "tokenizer_2/special_tokens_map.json": None, "tokenizer_2/tokenizer_config.json": None, "transformer/config.json": None, "transformer/diffusion_pytorch_model.safetensors": "af0239f15b91e424160581963873466b0eb673993b952a16e5a54ae18d521a1c", "vae/config.json": None, "vae/diffusion_pytorch_model.safetensors": "98a14dc6fe8d71c83576f135a87c61a16561c9c080abba418d2cc976ee034f88"}},
    "triposg": {"revision": "2c1c516d22d58db486a058d98d31bb6177344e06", "allow_patterns": ["*"], "checks": {"feature_extractor_dinov2/preprocessor_config.json": None, "image_encoder_dinov2/config.json": None, "image_encoder_dinov2/model.safetensors": "399fba97a95f22c36834418bc69373364a99af3a1153da1c0fb31db567c92e23", "model_index.json": None, "scheduler/scheduler_config.json": None, "transformer/config.json": None, "transformer/diffusion_pytorch_model.safetensors": "9192b5923f7b605b394192809aa2ceb73bf0f4009674d8e3b999b45bb97d4bf2", "vae/config.json": None, "vae/diffusion_pytorch_model.safetensors": "a2e667c24927a5a35e5f19fcb4c75890e9399aa966b6db8131d7df733a750c8b"}},
    "triposg-scribble": {"revision": "e70b06bad2dece5b6a359aa8cd12f538cabd7095", "allow_patterns": ["*"], "checks": {"feature_extractor_dinov2/preprocessor_config.json": None, "image_encoder_dinov2/config.json": None, "image_encoder_dinov2/model.safetensors": "aa0b83921a3339259fb1ef684ce63c9d09b5a45f9998e0789a0ad4cba318b07b", "model_index.json": None, "scheduler/scheduler_config.json": None, "text_encoder/config.json": None, "text_encoder/model.safetensors": "549d39f40a16f8ef48ed56da60cd25a467bd2c70866f4d49196829881b13b7b2", "tokenizer/special_tokens_map.json": None, "tokenizer/tokenizer_config.json": None, "tokenizer/vocab.json": None, "transformer/config.json": None, "transformer/diffusion_pytorch_model.safetensors": "e5bd5765da5fa2f4790e8d299cda346df480e0370244dc64b0d91f5c0d35ae99", "vae/config.json": None, "vae/diffusion_pytorch_model.safetensors": "b11438a228ffdf13fcbc798c5197170f027eadf353138139cf6c1fa7f655e7e0"}},
    "partcrafter": {"revision": "69a0ffc1dad5e48e7e5ed91c0609f2b1276eb31f", "allow_patterns": ["*"], "checks": {"feature_extractor_dinov2/preprocessor_config.json": None, "image_encoder_dinov2/config.json": None, "image_encoder_dinov2/model.safetensors": "aa0b83921a3339259fb1ef684ce63c9d09b5a45f9998e0789a0ad4cba318b07b", "model_index.json": None, "scheduler/scheduler_config.json": None, "transformer/config.json": None, "transformer/diffusion_pytorch_model.safetensors": "915aba1d29c626d960305a0d36a6edb7798288a19c6724388b005c2fa53bcbf8", "vae/config.json": None, "vae/diffusion_pytorch_model.safetensors": "b43b006e5692223877427cdb568c2c1477f52a2d226db4d5eb354b4886c167a4"}},
    "partcrafter-scene": {"revision": "0454bb8e595a2765e8cb1f17ffacad9ba159777a", "allow_patterns": ["*"], "checks": {"feature_extractor_dinov2/preprocessor_config.json": None, "image_encoder_dinov2/config.json": None, "image_encoder_dinov2/model.safetensors": "aa0b83921a3339259fb1ef684ce63c9d09b5a45f9998e0789a0ad4cba318b07b", "model_index.json": None, "scheduler/scheduler_config.json": None, "transformer/config.json": None, "transformer/diffusion_pytorch_model.safetensors": "da5522acb9098797dc2ead2c42b448adb1f5f25b0acda58c2dc36356cea82b7e", "vae/config.json": None, "vae/diffusion_pytorch_model.safetensors": "b43b006e5692223877427cdb568c2c1477f52a2d226db4d5eb354b4886c167a4"}},
    "triposr": {"revision": "5b521936b01fbe1890f6f9baed0254ab6351c04a", "allow_patterns": ["config.yaml", "model.ckpt", "README.md", "LICENSE*"], "checks": {"config.yaml": None, "model.ckpt": "429e2c6b22a0923967459de24d67f05962b235f79cde6b032aa7ed2ffcd970ee"}},
}


# Modèles du référentiel public rendus installables : fichier image_model_sources.json livré à côté de ce
# module (copie dérivée du fichier racine du dépôt, vérifiée par un test). Fichiers Hugging Face .safetensors
# ou .ckpt (pickle : ComfyUI les charge par torch.load(weights_only=True), dépickleur restreint) des familles
# SD1.5/SDXL, épinglés par SHA-256 ; un identifiant déjà présent ci-dessus prime. Une taille absente (null)
# devient 0 : model_installer borne alors le téléchargement à la taille annoncée par le serveur (plafond 16 Gio).
EXTERNAL_CHECKPOINT_SUFFIXES = (".safetensors", ".ckpt")
# Agent 1.43.0 : un modèle du référentiel hébergé sur CivitAI (fichier réservé aux comptes) se télécharge avec la clé
# CivitAI PROPRE au propriétaire du Worker, remise avec la commande et jamais écrite sur disque. Seule l'adresse de
# téléchargement officielle d'une version est acceptée, en .safetensors, toujours épinglée par SHA-256.
import re as _re
CIVITAI_DOWNLOAD_URL = _re.compile(r"https://civitai\.com/api/download/models/[0-9]{1,12}")
COMFYUI_KEY_SOURCES = {}


def _external_image_sources():
    import json
    from pathlib import Path
    try:
        document = json.loads(Path(__file__).with_name("image_model_sources.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return document.get("models") or {} if isinstance(document, dict) else {}


for _model_id, _spec in _external_image_sources().items():
    if not isinstance(_spec, dict) or not str(_spec.get("filename", "")).lower().endswith(EXTERNAL_CHECKPOINT_SUFFIXES) or not _spec.get("sha256"):
        continue
    _url = str(_spec.get("url", ""))
    _key_source = "civitai" if CIVITAI_DOWNLOAD_URL.fullmatch(_url) else ""
    if _key_source and not str(_spec["filename"]).lower().endswith(".safetensors"):
        continue
    if not _key_source and not _url.startswith("https://huggingface.co/"):
        continue
    try:
        _size = max(0, int(_spec.get("size_bytes") or 0))
    except (TypeError, ValueError):
        _size = 0
    COMFYUI_CATALOG.setdefault(("image_generation", str(_model_id)), (str(_spec["filename"]), _size, str(_spec["url"]), str(_spec["sha256"])))
    COMFYUI_REQUIREMENTS.setdefault(str(_model_id), (int(_spec.get("min_vram_mb") or 6000), int(_spec.get("recommended_vram_mb") or 8000)))
    if _key_source and COMFYUI_CATALOG[("image_generation", str(_model_id))][2] == _url:
        COMFYUI_KEY_SOURCES[str(_model_id)] = _key_source
