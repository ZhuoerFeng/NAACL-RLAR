---
dataset_info:
- config_name: Mid
  features:
  - name: messages
    list:
    - name: content
      dtype: string
    - name: role
      dtype: string
  - name: source
    dtype: string
  splits:
  - name: Llama_Nemotron_Post_Training_Dataset_reasoning_r1
    num_bytes: 61605080860
    num_examples: 3644790
  - name: OpenThoughts3_1.2M
    num_bytes: 56341153994
    num_examples: 1135104
  download_size: 53697026569
  dataset_size: 117946234854
- config_name: Preference
  features:
  - name: chosen
    list:
    - name: content
      dtype: string
    - name: role
      dtype: string
  - name: rejected
    list:
    - name: content
      dtype: string
    - name: role
      dtype: string
  - name: prompt
    dtype: string
  - name: chat_template_kwargs
    struct:
    - name: custom_instructions
      dtype: string
    - name: enable_thinking
      dtype: bool
    - name: python_tools
      sequence: string
    - name: xml_tools
      sequence: string
  - name: source
    dtype: string
  splits:
  - name: llama_3.1_tulu_3_8b_preference_mixture_no_think
    num_bytes: 1471659085
    num_examples: 230501
  - name: tulu_3_8b_pref_mix_Qwen3_32B_Qwen3_0.6B_think
    num_bytes: 4563920395
    num_examples: 216385
  download_size: 2599953933
  dataset_size: 6035579480
- config_name: SFT
  features:
  - name: messages
    list:
    - name: content
      dtype: string
    - name: role
      dtype: string
  - name: chat_template_kwargs
    struct:
    - name: custom_instructions
      dtype: string
    - name: enable_thinking
      dtype: bool
    - name: python_tools
      list: string
    - name: xml_tools
      list: string
  - name: source
    dtype: string
  splits:
  - name: LongAlign_64k_Qwen3_32B_yarn_131k_think
    num_bytes: 520907823
    num_examples: 7526
  - name: OpenThoughts3_1.2M_think
    num_bytes: 56273098407
    num_examples: 1133524
  - name: aya_dataset_Qwen3_32B_think
    num_bytes: 60172886
    num_examples: 15222
  - name: multi_turn_reasoning_if_think
    num_bytes: 421909719
    num_examples: 28217
  - name: s1k_1.1_think
    num_bytes: 25598191
    num_examples: 835
  - name: smolagents_toolcalling_traces_think
    num_bytes: 200401637
    num_examples: 9079
  - name: smoltalk_multilingual8_Qwen3_32B_think
    num_bytes: 1900647658
    num_examples: 244736
  - name: smoltalk_systemchats_Qwen3_32B_think
    num_bytes: 123542086
    num_examples: 27436
  - name: table_gpt_Qwen3_32B_think
    num_bytes: 77802435
    num_examples: 13201
  - name: LongAlign_64k_context_lang_annotated_lang_6_no_think
    num_bytes: 402317420
    num_examples: 6249
  - name: Mixture_of_Thoughts_science_no_think
    num_bytes: 130260467
    num_examples: 86110
  - name: OpenHermes_2.5_no_think
    num_bytes: 585433379
    num_examples: 384900
  - name: OpenThoughts3_1.2M_no_think_no_think
    num_bytes: 1215060567
    num_examples: 435193
  - name: hermes_function_calling_v1_no_think
    num_bytes: 45038786
    num_examples: 8961
  - name: smoltalk_multilingual_8languages_lang_5_no_think
    num_bytes: 564939737
    num_examples: 254047
  - name: smoltalk_smollm3_everyday_conversations_no_think
    num_bytes: 1955692
    num_examples: 2260
  - name: smoltalk_smollm3_explore_instruct_rewriting_no_think
    num_bytes: 14186608
    num_examples: 30391
  - name: smoltalk_smollm3_smol_magpie_ultra_no_think
    num_bytes: 2820888852
    num_examples: 406843
  - name: smoltalk_smollm3_smol_rewrite_no_think
    num_bytes: 89561602
    num_examples: 53262
  - name: smoltalk_smollm3_smol_summarize_no_think
    num_bytes: 229104725
    num_examples: 96061
  - name: smoltalk_smollm3_systemchats_30k_no_think
    num_bytes: 89680522
    num_examples: 33997
  - name: table_gpt_no_think
    num_bytes: 31127809
    num_examples: 13203
  - name: tulu_3_sft_personas_instruction_following_no_think
    num_bytes: 59890932
    num_examples: 29970
  - name: xlam_traces_no_think
    num_bytes: 96806049
    num_examples: 59962
  - name: smoltalk_everyday_convs_reasoning_Qwen3_32B_think
    num_bytes: 11738101
    num_examples: 2057
  download_size: 31451143641
  dataset_size: 65992072090
configs:
- config_name: Mid
  data_files:
  - split: Llama_Nemotron_Post_Training_Dataset_reasoning_r1
    path: Mid/Llama_Nemotron_Post_Training_Dataset_reasoning_r1-*
  - split: OpenThoughts3_1.2M
    path: Mid/OpenThoughts3_1.2M-*
- config_name: Preference
  data_files:
  - split: llama_3.1_tulu_3_8b_preference_mixture_no_think
    path: Preference/llama_3.1_tulu_3_8b_preference_mixture_no_think-*
  - split: tulu_3_8b_pref_mix_Qwen3_32B_Qwen3_0.6B_think
    path: Preference/tulu_3_8b_pref_mix_Qwen3_32B_Qwen3_0.6B_think-*
- config_name: SFT
  data_files:
  - split: LongAlign_64k_Qwen3_32B_yarn_131k_think
    path: SFT/LongAlign_64k_Qwen3_32B_yarn_131k_think-*
  - split: OpenThoughts3_1.2M_think
    path: SFT/OpenThoughts3_1.2M_think-*
  - split: aya_dataset_Qwen3_32B_think
    path: SFT/aya_dataset_Qwen3_32B_think-*
  - split: multi_turn_reasoning_if_think
    path: SFT/multi_turn_reasoning_if_think-*
  - split: s1k_1.1_think
    path: SFT/s1k_1.1_think-*
  - split: smolagents_toolcalling_traces_think
    path: SFT/smolagents_toolcalling_traces_think-*
  - split: smoltalk_everyday_convs_reasoning_Qwen3_32B_think
    path: SFT/smoltalk_everyday_convs_reasoning_Qwen3_32B_think-*
  - split: smoltalk_multilingual8_Qwen3_32B_think
    path: SFT/smoltalk_multilingual8_Qwen3_32B_think-*
  - split: smoltalk_systemchats_Qwen3_32B_think
    path: SFT/smoltalk_systemchats_Qwen3_32B_think-*
  - split: table_gpt_Qwen3_32B_think
    path: SFT/table_gpt_Qwen3_32B_think-*
  - split: LongAlign_64k_context_lang_annotated_lang_6_no_think
    path: SFT/LongAlign_64k_context_lang_annotated_lang_6_no_think-*
  - split: Mixture_of_Thoughts_science_no_think
    path: SFT/Mixture_of_Thoughts_science_no_think-*
  - split: OpenHermes_2.5_no_think
    path: SFT/OpenHermes_2.5_no_think-*
  - split: OpenThoughts3_1.2M_no_think_no_think
    path: SFT/OpenThoughts3_1.2M_no_think_no_think-*
  - split: hermes_function_calling_v1_no_think
    path: SFT/hermes_function_calling_v1_no_think-*
  - split: smoltalk_multilingual_8languages_lang_5_no_think
    path: SFT/smoltalk_multilingual_8languages_lang_5_no_think-*
  - split: smoltalk_smollm3_everyday_conversations_no_think
    path: SFT/smoltalk_smollm3_everyday_conversations_no_think-*
  - split: smoltalk_smollm3_explore_instruct_rewriting_no_think
    path: SFT/smoltalk_smollm3_explore_instruct_rewriting_no_think-*
  - split: smoltalk_smollm3_smol_magpie_ultra_no_think
    path: SFT/smoltalk_smollm3_smol_magpie_ultra_no_think-*
  - split: smoltalk_smollm3_smol_rewrite_no_think
    path: SFT/smoltalk_smollm3_smol_rewrite_no_think-*
  - split: smoltalk_smollm3_smol_summarize_no_think
    path: SFT/smoltalk_smollm3_smol_summarize_no_think-*
  - split: smoltalk_smollm3_systemchats_30k_no_think
    path: SFT/smoltalk_smollm3_systemchats_30k_no_think-*
  - split: table_gpt_no_think
    path: SFT/table_gpt_no_think-*
  - split: tulu_3_sft_personas_instruction_following_no_think
    path: SFT/tulu_3_sft_personas_instruction_following_no_think-*
  - split: xlam_traces_no_think
    path: SFT/xlam_traces_no_think-*
---

# SmolTalk2

![image/png](https://cdn-uploads.huggingface.co/production/uploads/61c141342aac764ce1654e43/IxKwk-Jqi1qftWTj-0Tid.png)

## Dataset description

This dataset contains three subsets (Mid, SFT, Preference) that correspond to the three phases of Post-Training for [SmolLM3-3B](https://huggingface.co/HuggingFaceTB/SmolLM3-3B). You can find more details in our [blog post](https://huggingface.co/blog/smollm3) about how we used the data in each of the stages [SmolLM3](https://huggingface.co/HuggingFaceTB/SmolLM3-3B).

The specific weight of each subset is available in the training recipe in SmolLM's repository.

You can load a dataset using
```python
from datasets import load_dataset

# To load the train split of a specific subset, such as Mixture-of-Thoughts, you can do
ds = load_dataset("HuggingFaceTB/smoltalk2", "SFT", split=["Mixture-of-Thoughts_science", "table_gpt_no_think"])
```

## Dataset Composition

### Mid-Training (`Mid`)

The mid-training dataset has a total of 4.8M rows and is composed of 2 datasets that we decontaminate to remove samples present in the benchmarks used for evaluation.

The datasets are:
- [Llama-Nemotron-Post-Training-Dataset](https://huggingface.co/datasets/nvidia/Llama-Nemotron-Post-Training-Dataset): 3.64M rows
- [OpenThoughts3-1.2M](https://huggingface.co/datasets/open-thoughts/OpenThoughts3-1.2M): 1.14M rows.

### SFT (`SFT`)
The total mix consists of 25 datasets, which we decontaminated to remove samples present in the benchmarks used for evaluation and remove samples containing emojis. We also created the `chat_template_kwargs` column by extracting any system message or tool descriptions already present in the dataset. 

We make a distinction between datasets with and without reasoning traces, denoted by the suffixes `think` and `no_think`, respectively. The 10 `think` datasets have a total of 1.5M rows, and the 15 `no_think` datasets have a total of 1.9M rows.

The `think` datasets are:
- [SmolTalk](https://huggingface.co/datasets/HuggingFaceTB/smoltalk) (multilingual-8languages): 244736 rows generated with Qwen3-32B with the prompts in SmolTalk.
- [SmolTalk](https://huggingface.co/datasets/HuggingFaceTB/smoltalk) (everyday-conversations): 244736 rows generated with Qwen3-32B with the prompts in SmolTalk. 
- [SmolTalk](https://huggingface.co/datasets/HuggingFaceTB/smoltalk) (systemchats-30k): 244736 rows generated with Qwen3-32B with the prompts in SmolTalk. 
- LongAlign-64k-context-lang-annotated: 7526 rows generated with Qwen3-32B with the prompts in LongAlign-64k.
- [NEW] smolagents-toolcalling-traces: 9079 rows.
  - We generate tool calling data with reasoning traces using `deepseek-ai/DeepSeek-V3-0324`.
- [NEW] Multi-Turn IF: 28217 rows.
  - We follow [Multi-IF's approach](https://arxiv.org/abs/2410.15553) to generate multi turn data. We source prompts from [Tulu 3 Personas IF](https://huggingface.co/datasets/allenai/tulu-3-sft-personas-instruction-following), generate 2 verifiable turns using Qwen3-235B-A22B, and generate responses with Qwen3-32B in reasoning mode.
- [s1k-1.1](https://huggingface.co/datasets/open-r1/s1K-1.1): 835 rows.
- [OpenThoughts3-1.2M](https://huggingface.co/datasets/open-thoughts/OpenThoughts3-1.2M): 1133524 rows.
- [Aya](https://huggingface.co/datasets/CohereLabs/aya_dataset): 15222 rows generated with Qwen3-32B with the prompts in Aya.
- [Table-GPT](https://huggingface.co/datasets/LipengCS/Table-GPT): 13201 rows generated with Qwen3-32B with the prompts in Table-GPT.

The `no_think` datasets are:
- [NEW] [SmolTalk](https://huggingface.co/datasets/HuggingFaceTB/smoltalk) (multilingual-8languages): 254047 rows.
  - Following [Qwen 2.5 report](https://arxiv.org/pdf/2412.15115), we first translate the prompts in Smol-Magpie-Ultra and Smol-Constraints using Qwen to 8 languages (fr, es, it, pt, de, ar, ru, zh) while respecting local conventions (units, currency, etc.). We then use the model to generate answers for each translated instruction in the target language.
- [SmolTalk](https://huggingface.co/datasets/HuggingFaceTB/smoltalk) (everyday-conversations): 2260 rows.
- [SmolTalk](https://huggingface.co/datasets/HuggingFaceTB/smoltalk) (systemchats-30k): 33997 rows.
- [SmolTalk](https://huggingface.co/datasets/HuggingFaceTB/smoltalk) (smollm3_smol-magpie-ultra): 406843 rows.
- [SmolTalk](https://huggingface.co/datasets/HuggingFaceTB/smoltalk) (smollm3_explore-instruct-rewriting): 30391 rows.
- [SmolTalk](https://huggingface.co/datasets/HuggingFaceTB/smoltalk) (smollm3_smol-rewrite): 53262 rows.
- [SmolTalk](https://huggingface.co/datasets/HuggingFaceTB/smoltalk) smollm3_smol-summarize: 96061 rows.
- [Mixture of Thoughts](https://huggingface.co/datasets/open-r1/Mixture-of-Thoughts) (science): 86110 rows where we remove the reasoning trace.
- [Tulu 3 SFT Personas IF](https://huggingface.co/datasets/allenai/tulu-3-sft-personas-instruction-following): 29970 rows.
- [hermes-function-calling-v1](https://huggingface.co/datasets/NousResearch/hermes-function-calling-v1): 8961 rows.
- [Table-GPT](https://huggingface.co/datasets/LipengCS/Table-GPT): 13203 rows.
- [OpenHermes-2.5](https://huggingface.co/datasets/teknium/OpenHermes-2.5): 384900 rows.
- [OpenThoughts3-1.2M](https://huggingface.co/datasets/open-thoughts/OpenThoughts3-1.2M): 435193 rows where we remove the reasoning trace.
- LongAlign-64k-context-lang-annotated (lang_6): 6249 examples. We filter [LongAlign](https://huggingface.co/datasets/THUDM/LongAlign-10k) for samples up to 64k tokens.

### Preference Data (`Preference`)

We used two datasets to train SmolLM3-3B with APO, which has a total of 447k rows. We generated the `think` equivalent using the prompts of the `no_think` counterpart and decontaminated using the same methods from the other two stages. The datasets are:

- [Tulu 3 8B Preference Mixture (`no_think`)](https://huggingface.co/datasets/allenai/llama-3.1-tulu-3-8b-preference-mixture): 231k rows.
- Tulu 3 8B Preference Mixture (`think`): 216k rows where we generate the chosen responses with Qwen3-32B and the rejected responses with Qwen3-0.6B.

## Dataset Stats

The dataset stats contain a more granular level of the training mix by dataset. We also include the `Weight` column that controls the number of examples we take from each dataset for training. You can find the full configuration files [here](https://github.com/huggingface/alignment-handbook/tree/main/recipes/smollm3).

### Mid-Training

| Dataset                                           |   Weight |   # examples |   % of examples |   # tokens (M) |   % of tokens |   Avg. # turns |   Avg. # tokens per example |   Avg. # tokens in context |   Avg. # tokens in response |
|---------------------------------------------------|----------|--------------|-----------------|----------------|---------------|----------------|-----------------------------|----------------------------|-----------------------------|
| Llama-Nemotron-Post-Training-Dataset_reasoning_r1 |        1 |      3644790 |           76.25 |        18707.9 |         53.19 |              2 |                     5132.79 |                     145    |                     4987.79 |
| OpenThoughts3-1.2M                                |        1 |      1135104 |           23.75 |        16464.2 |         46.81 |              2 |                    14504.5  |                     219.68 |                    14284.9  |
| Total                                             |        - |      4779894 |          100    |        35172.1 |        100    |              2 |                     7358.34 |                     162.73 |                     7195.61 |

### SFT

| Dataset                                     |   Weight |   # examples |   % of examples |   # tokens (M) |   % of tokens |   Avg. # turns |   Avg. # tokens per example |   Avg. # tokens in context |   Avg. # tokens in response |
|---------------------------------------------|----------|--------------|-----------------|----------------|---------------|----------------|-----------------------------|----------------------------|-----------------------------|
| smoltalk-smollm3_everyday-conversations_no_think     |     1    |         2260 |            0.07 |           0.63 |          0    |           7.75 |                      277.24 |                     239.23 |                      111.01 |
| smoltalk-smollm3_systemchats-30k_no_think            |     1    |        33997 |            1    |          22.06 |          0.11 |           6.27 |                      648.91 |                     439.76 |                      284.74 |
| tulu-3-sft-personas-instruction-following_no_think   |     1    |        29970 |            0.89 |          13.83 |          0.07 |           2    |                      461.46 |                     136.72 |                      397.74 |
| hermes-function-calling-v1_no_think                  |     1    |         8961 |            0.26 |          11.38 |          0.06 |           5.35 |                     1270.06 |                    1163.93 |                      468.37 |
| smoltalk-smollm3_smol-magpie-ultra_no_think          |     0.5  |       406843 |           12.03 |         619.05 |          3.21 |           6    |                     1521.59 |                    1072.52 |                      522.07 |
| smoltalk-multilingual-8languages_lang_5_no_think     |     1    |       254047 |            7.51 |         166.79 |          0.86 |           2    |                      656.54 |                     179.41 |                      550.13 |
| table-gpt_no_think                                   |     1    |        13203 |            0.39 |          11.49 |          0.06 |           2    |                      870.39 |                     787.81 |                      155.58 |
| OpenHermes-2.5_no_think                              |     0.5  |       384900 |           11.38 |         158.23 |          0.82 |           2    |                      411.1  |                     269.39 |                      214.71 |
| OpenThoughts3-1.2M_no_think_no_think                 |     0.4  |       435193 |           12.86 |         379.82 |          1.97 |           2    |                      872.76 |                     288.03 |                      657.73 |
| Mixture-of-Thoughts_science_no_think                 |     1    |        86110 |            2.55 |          37.51 |          0.19 |           2    |                      435.61 |                     135.64 |                      372.97 |
| smoltalk-smollm3_explore-instruct-rewriting_no_think |     1    |        30391 |            0.9  |           4.63 |          0.02 |           2    |                      152.29 |                     119.44 |                      110.87 |
| smoltalk-smollm3_smol-rewrite_no_think               |     1    |        53262 |            1.57 |          20.34 |          0.11 |           2    |                      381.86 |                     235.05 |                      229.28 |
| smoltalk-smollm3_smol-summarize_no_think             |     1    |        96061 |            2.84 |          51.82 |          0.27 |           2    |                      539.47 |                     442.18 |                      182.86 |
| LongAlign-64k-context-lang-annotated_lang_6_no_think |     1    |         6249 |            0.18 |          95.78 |          0.5  |           2    |                    15327.7  |                   15126.2  |                      274.55 |
| multi-turn-reasoning-if_think                     |     1    |        28217 |            0.83 |          97.62 |          0.51 |           6    |                     3459.66 |                    2404.17 |                     1312.48 |
| smoltalk-everyday-convs-reasoning-Qwen3-32B_think |     1    |         2057 |            0.06 |           3.17 |          0.02 |           4    |                     1539.37 |                     393.76 |                     1402.6  |
| smoltalk-systemchats-Qwen3-32B_think              |     1    |        27436 |            0.81 |          29.84 |          0.15 |           2    |                     1087.79 |                     101.63 |                     1059.73 |
| xlam-traces_no_think                                 |     1    |        59962 |            1.77 |          29.4  |          0.15 |           2    |                      490.25 |                     431.42 |                      455.84 |
| smolagents-toolcalling-traces_think               |     1    |         9079 |            0.27 |          63.81 |          0.33 |           5.34 |                     7028.12 |                    6934.23 |                      681.89 |
| s1k-1.1_think                                     |     1    |          835 |            0.02 |           8.25 |          0.04 |           2    |                     9876.31 |                     387.87 |                     9745.45 |
| LongAlign-64k-Qwen3-32B-yarn-131k_think           |     1    |         7526 |            0.22 |         136.21 |          0.71 |           2    |                    18099.2  |                   16220.5  |                     2135.73 |
| aya_dataset-Qwen3-32B_think                       |     1    |        15222 |            0.45 |          18.92 |          0.1  |           2    |                     1242.73 |                     301.34 |                     1198.4  |
| smoltalk-multilingual8-Qwen3-32B_think            |     0.3  |       244736 |            7.23 |         551.97 |          2.86 |           2    |                     2255.38 |                     363.63 |                     2148.74 |
| OpenThoughts3-1.2M_think                          |     0.02 |      1133524 |           33.5  |       16734    |         86.74 |           2    |                    14762.8  |                     476.17 |                    14543.6  |
| table-gpt-Qwen3-32B_think                         |     1    |        13201 |            0.39 |          25.92 |          0.13 |           2    |                     1963.49 |                     971.89 |                     1248.6  |
| Total                                       |     -    |      3383242 |          100    |       19292.4  |        100    |           2.58 |                     5702.35 |                     545.35 |                     5317.08 |



### Preference Data


| Dataset                                                        |   Weight |   # examples |   % of examples |   Avg. # turns |   Avg. # tokens in context |   # tokens (M) (Chosen) |   % of tokens (Chosen) |   Avg. # tokens per example (Chosen) |   Avg. # tokens in response (Chosen) |
|----------------------------------------------------------------|----------|--------------|-----------------|-------------------------|-------------------------------------|-------------------------|------------------------|--------------------------------------|--------------------------------------|
| llama_3.1_tulu_3_8b_preference_mixture_no_think               |     0.5  |       230501 |           51.58 |                       2 |                              283.34 |                  168.3  |                  19.79 |                               730.14 |                               519.8  |
| tulu_3_8b_pref_mix_Qwen3_32B_Qwen3_0.6B_think                 |     0.25 |       216385 |           48.42 |                       2 |                              469.94 |                  682.32 |                  80.21 |                              3153.27 |                              2940.33 |
| Total                                                          |     -    |       446886 |          100    |                       2 |                              373.69 |                  850.62 |                 100    |                              1903.44 |                              1691.84 |


## License

All the new datasets (aya_dataset-Qwen3-32B, multi-turn-reasoning-if, smolagents-toolcalling-traces, smoltalk-everyday-convs-reasoning-Qwen3-32B, smoltalk-multilingual8-Qwen3-32B, smoltalk-systemchats-Qwen3-32B, table-gpt-Qwen3-32B, tulu_3_8b_pref_mix_qwen3_32b_qwen3_06b_think) are licensed under Apache 2.0. For the existing public datasets, please refer to the original dataset for the license.