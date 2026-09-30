"""Explicit authored offline simulations; never model-quality evidence."""
import json
from pathlib import Path
from ..errors import ConfigError
from ..storage.canonical import digest
from .scripted import ScriptedLLM, Turn

def fixture_adapter(config, record, role):
    model = config.role_model(role)
    if not model.scripted_responses:
        raise ConfigError('scripted adapter requires model.scripted_responses or an injected adapter')
    scripts = json.loads(Path(model.scripted_responses).read_text())
    if scripts.get('fixture') == 'suite_template':
        suite = json.loads(scripts['default'][0]['content'])
        for example in suite['examples']:
            example['query_ref'] = record.query_id
        scripts['default'][0]['content'] = json.dumps(suite)
    turns = scripts.get(record.query_id, scripts.get(record.task_profile_id, scripts.get('default', [])))
    def choose(request):
        if scripts.get('fixture') == 'self_contained_verifier':
            # Authored offline tool simulator; never a real verifier-quality result.
            from ..evaluation.verifier import base_decision
            evidence = json.loads(request.messages[-1].content)['evidence']
            expected = scripts['expected_scores']
            passed = all(c['score'] == expected[s['example_id']] for s in evidence['scores'] for c in s['component_results'])
            return Turn(content=base_decision(evidence, passed=passed, rationale='Explicit offline expected-score fixture').model_dump_json())
        if scripts.get('fixture') == 'self_contained_rubric':
            data = json.loads(json.loads(request.messages[-1].content))
            return Turn(content=json.dumps(scripts['responses'][data['response']]))
        # A logical index survives physical retries and process restart.
        index = int(request.logical_call_id.rsplit(':', 1)[1]) - 1
        if index >= len(turns):
            raise ConfigError(f'script has no logical turn {index + 1}')
        data = dict(turns[index])
        if 'actions' in data:
            content = json.dumps({'actions': data.pop('actions')})
            content = content.replace('$JUDGE_CONFIG_DIGEST', digest(config.role_model('rubric_judge')))
            return Turn(content=content, **data)
        return Turn(**data)
    return ScriptedLLM(on_exhausted=choose)
