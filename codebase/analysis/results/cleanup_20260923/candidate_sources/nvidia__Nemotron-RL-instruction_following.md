---
license: odc-by
---

## Dataset Description:
The Nemotron-RL-instruction_following is a dataset created by combining prompts from the [WildChat-1M dataset](https://huggingface.co/datasets/allenai/WildChat-1M) (made available under the [ODC Attribution License](https://opendatacommons.org/licenses/by/1-0/)) with instructions from the [Open-Instruct code base](https://github.com/allenai/open-instruct). The instructions are designed to be easily verifiable, such as requiring responses under 200 words. This makes the dataset well-suited for evaluating and training models on objective instruction adherence.


This dataset is released as part of NVIDIA [NeMo Gym](https://github.com/NVIDIA-NeMo/Gym), a framework for building reinforcement learning environments to train large language models. NeMo Gym contains a growing collection of training environments and datasets to enable Reinforcement Learning from Verifiable Reward (RLVR).

NeMo Gym is an open-source library within the [NVIDIA NeMo framework](https://github.com/NVIDIA-NeMo/), NVIDIA's GPU accelerated, end-to-end training framework for large language models (LLMs), multi-modal models and speech models.

This dataset is part of the [Nemo Gym Collection](https://huggingface.co/collections/nvidia/nemo-gym).


This dataset is ready for commercial use.

## Dataset Owner(s):
NVIDIA Corporation

## Dataset Creation Date:
September 1st, 2025

## License/Terms of Use: 
ODC Attribution License

## Intended Usage:
To be used with [NeMo Gym](https://github.com/NVIDIA-NeMo/Gym) for post-training LLMs. 

## Dataset Characterization
Data Collection Method<br>
* [Automated]  <br>

Labeling Method<br>
* [Automated] <br>



## Dataset Format
Text Only, Compatible with [NeMo Gym](https://github.com/NVIDIA-NeMo/Gym)

## Dataset Quantification
Number of records: 46391 tuples of (question, verifiable instruction)
Features present in record count above: N/A
Total Data Storage: 93 MB

## Reference(s):
[NeMo Gym](https://github.com/NVIDIA-NeMo/Gym)
[PAPER LINK](https://github.com/allenai/IFBench/blob/main/Precise_IF_Generalization_Abilities.pdf)

## Ethical Considerations:
NVIDIA believes Trustworthy AI is a shared responsibility and we have established policies and practices to enable development for a wide array of AI applications.  When downloaded or used in accordance with our terms of service, developers should work with their internal model team to ensure this model meets requirements for the relevant industry and use case and addresses unforeseen product misuse.   
Please report model quality, risk, security vulnerabilities or NVIDIA AI Concerns [here](https://www.nvidia.com/en-us/support/submit-security-vulnerability/).