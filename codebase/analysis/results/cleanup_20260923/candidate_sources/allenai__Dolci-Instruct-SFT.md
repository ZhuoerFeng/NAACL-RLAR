---
annotations_creators:
- crowdsourced
- expert-generated
- machine-generated
language:
- amh
- arb
- ary
- ars
- acq
- arz
- apc
- ben
- ceb
- dan
- deu
- ell
- eng
- eus
- fil
- fin
- fra
- gle
- guj
- hat
- hau
- hin
- hun
- ibo
- ind
- ita
- jav
- jpn
- kan
- kir
- kor
- kur
- lit
- mal
- mar
- mlg
- msa
- mya
- nep
- nld
- nso
- nya
- pan
- pes
- pol
- por
- pus
- rus
- sin
- sna
- snd
- som
- spa
- sqi
- srp
- sun
- swa
- swe
- tam
- tel
- tha
- tur
- ukr
- urd
- vie
- wol
- xho
- yor
- zho
- zul
license: odc-by
multilinguality:
- multilingual
task_categories:
- other
dataset_info:
  features:
  - name: id
    dtype: string
  - name: messages
    list:
    - name: content
      dtype: string
    - name: function_calls
      dtype: string
    - name: functions
      dtype: string
    - name: role
      dtype: string
  - name: source_dataset
    dtype: string
  - name: domain
    dtype: string
  splits:
  - name: train
    num_bytes: 7006978580
    num_examples: 2152112
  download_size: 3061427649
  dataset_size: 7006978580
configs:
- config_name: default
  data_files:
  - split: train
    path: data/train-*
---

# Dolci Instruct SFT Mixture

*Note that this collection licensed under ODC-BY. It is intended for research and educational use in accordance with Ai2's [Responsible Use Guidelines](https://allenai.org/responsible-use).*

The Dolci Instruct SFT mixture was used to train [Olmo 3 7B Instruct SFT](https://huggingface.co/allenai/Olmo-3-7B-Instruct-SFT).
It contains 2,152,112 samples from the following sets:

Sources include a mixture of existing prompts:
- [OpenThoughts 3](https://huggingface.co/datasets/open-thoughts/OpenThoughts3-1.2M) (Apache 2.0): Extended to 32K context length and downsampled code prompts to 16X multiple, to 941,166 total prompts, reasoning traces removed for instruct, 99,268 prompts.
- [CoCoNot](https://huggingface.co/datasets/allenai/coconot) (ODC-BY-1.0), 10,957 prompts (Brahman et al., 2024)
- [FLAN v2](https://github.com/google-research/FLAN/tree/main) via [`ai2-adapt-dev/flan_v2_converted`](https://huggingface.co/datasets/ai2-adapt-dev/flan_v2_converted), 89,981 prompts (Longpre et al., 2023)
- [OpenAssistant Guanaco](https://huggingface.co/datasets/OpenAssistant/oasst1) (Apache 2.0), 7,132 prompts (Kopf et al., 2024)
- [Tulu 3 Persona MATH](https://huggingface.co/datasets/allenai/tulu-3-personas-math) (ODC-BY-1.0), 149,958 prompts
- [Tulu 3 Persona GSM](https://huggingface.co/datasets/allenai/tulu-3-sft-personas-math-grade) (ODC-BY-1.0), 49,980 prompts
- [Tulu 3 Persona Python](https://huggingface.co/datasets/allenai/tulu-3-sft-personas-code) (ODC-BY-1.0), 34,999 prompts
- [Tulu 3 Persona Algebra](https://huggingface.co/datasets/allenai/tulu-3-personas-algebra) (ODC-BY-1.0), 19,999 prompts 
- [Tulu 3 WildGuardMix](https://huggingface.co/datasets/allenai/wildguardmix) (Apache 2.0), 49,373 prompts (Han et al., 2024)
- [Tulu 3 WildJailbreak](https://huggingface.co/datasets/allenai/wildjailbreak) (ODC-BY-1.0), 49,965 prompts (Wildteaming, 2024)
- [Aya](https://huggingface.co/datasets/CohereForAI/aya_dataset) (Apache 2.0), 99,987 prompts (Singh et al., 2024)
- [TableGPT](https://huggingface.co/datasets/LipengCS/Table-GPT) (MIT), 5,000 prompts (Zha et al., 2023)
- [SciRIFF](https://huggingface.co/datasets/allenai/SciRIFF) (ODC-BY-1.0), 4,557 prompts (Wadden et al., 2024)
- [Evol CodeAlpaca](https://huggingface.co/datasets/theblackcat102/evol-codealpaca-v1) (Apache 2.0), 107,270 prompts (Luo et al., 2023)

And new prompts from us:
- Dolci Tülu 3 Precise IF: 136,833 prompts.
- Dolci Instruct Python Algorithms:  186,345 
- WildChat with upgraded responses from GPT-4.1 (ODC-BY-1.0), 302,406 prompts (Zhao et al., 2024)
- Logic puzzles, 159,882 prompts.
- Verifiable reasoning, 310,572 prompts.
- New hardcoded data, 69 prompts.
- [Dolci Instruct Tool Use](https://huggingface.co/datasets/allenai/Dolci-Instruct-SFT-Tool-Use), 227,579 prompts.

The counts are smaller than the original prompt sources pulled from Tülu 3 / OLMo 2 due to more extensive filtering for data quality and by topics within the Azure API (blocked requests).

## Citation

```
@misc{olmo2025olmo3,
title={Olmo 3},
author={Team Olmo and Allyson Ettinger and Amanda Bertsch and Bailey Kuehl and David Graham and David Heineman and Dirk Groeneveld and Faeze Brahman and Finbarr Timbers and Hamish Ivison and Jacob Morrison and Jake Poznanski and Kyle Lo and Luca Soldaini and Matt Jordan and Mayee Chen and Michael Noukhovitch and Nathan Lambert and Pete Walsh and Pradeep Dasigi and Robert Berry and Saumya Malik and Saurabh Shah and Scott Geng and Shane Arora and Shashank Gupta and Taira Anderson and Teng Xiao and Tyler Murray and Tyler Romero and Victoria Graf and Akari Asai and Akshita Bhagia and Alexander Wettig and Alisa Liu and Aman Rangapur and Chloe Anastasiades and Costa Huang and Dustin Schwenk and Harsh Trivedi and Ian Magnusson and Jaron Lochner and Jiacheng Liu and Lester James V. Miranda and Maarten Sap and Malia Morgan and Michael Schmitz and Michal Guerquin and Michael Wilson and Regan Huff and Ronan Le Bras and Rui Xin and Rulin Shao and Sam Skjonsberg and Shannon Zejiang Shen and Shuyue Stella Li and Tucker Wilde and Valentina Pyatkin and Will Merrill and Yapei Chang and Yuling Gu and Zhiyuan Zeng and Ashish Sabharwal and Luke Zettlemoyer and Pang Wei Koh and Ali Farhadi and Noah A. Smith and Hannaneh Hajishirzi},
year={2025},
eprint={2512.13961},
archivePrefix={arXiv},
primaryClass={cs.CL},
url={https://arxiv.org/abs/2512.13961},
}
```
