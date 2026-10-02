# coding=utf-8
# Copyright 2023 The HuggingFace Datasets Authors and the current dataset script contributor.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""TACO dataset."""

import json
import datasets


_REPO_NAME = "BAAI/TACO"

_CITATION = """
"""

_DESCRIPTION = """
TACO is a benchmark for Python code generation, it includes 25443 problems and 1000 problems for train and test splits.
"""

_HOMEPAGE = "https://github.com/FlagOpen/TACO"
_DIFFICULTY = ["EASY", "MEDIUM", "MEDIUM_HARD", "HARD", "VERY_HARD"]
_DIFFICULTY_CONFIGS = ["ALL"] + _DIFFICULTY
_SKILL = ['Data structures', 'Sorting', 'Range queries', 'Complete search', 'Amortized analysis', 'Dynamic programming', 'Bit manipulation', 'Greedy algorithms']
_SKILL_CONFIGS = ["ALL"] + _SKILL
_URLS = {
    "train": ['train/data-00000-of-00009.arrow', 'train/data-00001-of-00009.arrow', 'train/data-00002-of-00009.arrow', 'train/data-00003-of-00009.arrow', 'train/data-00004-of-00009.arrow', 'train/data-00005-of-00009.arrow', 'train/data-00006-of-00009.arrow', 'train/data-00007-of-00009.arrow', 'train/data-00008-of-00009.arrow'],  
    "test": ['test/data-00000-of-00001.arrow'],
}

    
class TACOConfig(datasets.BuilderConfig):
    """BuilderConfig for the TACO dataset."""

    def __init__(self, *args, difficulties=["ALL"], skills=["ALL"], **kwargs):
        """BuilderConfig for the APPS Code dataset.

        Args:
            difficulties (:obj:`List[str]`): List of problem difficulty levels to load.
            skills (:obj:`List[str]`): List of algorithm skills of problems to load.
            **kwargs: keyword arguments forwarded to super.
        """
        if "ALL" in difficulties:
            assert len(difficulties) == 1
            self.filter_difficulties = False
        else:
            self.filter_difficulties = True
        if "ALL" in skills:
            assert len(skills) == 1
            self.filter_skills = False
        else:
            self.filter_skills = True
        
        if self.filter_difficulties:
            subset_name = '+'.join(sorted(difficulties))
            assert not self.filter_skills, "Not supported to filter difficulties and skills together."
        elif self.filter_skills:
            subset_name = '+'.join(sorted(skills))
        else:
            subset_name = 'ALL'
            
        super().__init__(
            *args,
            name=subset_name,
            **kwargs,
        )
        
        self.subsets = {"difficulties": difficulties, "skills": skills}


class TACO(datasets.GeneratorBasedBuilder):
    """TACO dataset."""

    VERSION = datasets.Version("1.0.0")
    
    BUILDER_CONFIG_CLASS = TACOConfig
    BUILDER_CONFIGS = [
        TACOConfig(difficulties=[level]) for level in _DIFFICULTY_CONFIGS
    ] + [
        TACOConfig(skills=[skill]) for skill in _SKILL_CONFIGS if skill!='ALL'
    ]
    DEFAULT_CONFIG_NAME = "ALL"
    
    def _info(self):
        return datasets.DatasetInfo(
            description=_DESCRIPTION,
            features=datasets.Features({
                'question': datasets.Value(dtype='string', id=None), 
                'solutions': datasets.Value(dtype='string', id=None), 
                'starter_code': datasets.Value(dtype='string', id=None), 
                'input_output': datasets.Value(dtype='string', id=None), 
                'difficulty': datasets.Value(dtype='string', id=None), 
                'raw_tags': datasets.Value(dtype='string', id=None), 
                'name': datasets.Value(dtype='string', id=None), 
                'source': datasets.Value(dtype='string', id=None), 
                'tags': datasets.Value(dtype='string', id=None), 
                'skill_types': datasets.Value(dtype='string', id=None),
                'url': datasets.Value(dtype='string', id=None), 
                'Expected Auxiliary Space': datasets.Value(dtype='string', id=None), 
                'time_limit': datasets.Value(dtype='string', id=None), 
                'date': datasets.Value(dtype='string', id=None), 
                'picture_num': datasets.Value(dtype='string', id=None), 
                'memory_limit': datasets.Value(dtype='string', id=None), 
                'Expected Time Complexity': datasets.Value(dtype='string', id=None),
            }),
            supervised_keys=None,
            citation=_CITATION,
            homepage=_HOMEPAGE,
            license="MIT License",
            
        )   

    def _split_generators(self, dl_manager):
        
        downloaded_files = dl_manager.download_and_extract(_URLS)
        
        return [
            datasets.SplitGenerator(name=datasets.Split.TRAIN, gen_kwargs={"filepath": downloaded_files["train"]}),
            datasets.SplitGenerator(name=datasets.Split.TEST, gen_kwargs={"filepath": downloaded_files["test"]}),
        ]  
        
    def _generate_examples(self, filepath):
        key = 0
        dataset = datasets.concatenate_datasets([datasets.Dataset.from_file(file) for file in filepath])
        for idx, data in enumerate(dataset):
            difficulty = data['difficulty']
            skills = eval(data['skill_types'])
            if self.config.filter_difficulties and not difficulty in self.config.subsets['difficulties']:
                continue
            if self.config.filter_skills:
                valid_skills = self.config.subsets['skills']
                if not bool(set(valid_skills) & set(skills)):
                    continue

            yield key, data
            key += 1