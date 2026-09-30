"""The episode's resource index — everything ``read_resource`` can reach.

The index is built once per episode from the task pack, the query and the
library snapshot, and is then frozen. Two rules hold it together:

* nothing carrying an oracle label, an audit path, a credential or a service
  endpoint is ever placed in it;
* the ids are fixed and listed in the run prefix, so the controller reads by
  id and never by path — there is no way to name a file that was not offered.

Large entries are materialized lazily and bounded when they are read, not when
the index is built, so constructing an episode stays cheap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..schemas import QueryRecord, TaskPack
from ..storage.library import LibrarySnapshot

#: Fixed resource ids. Declared in the run prefix so the set is knowable
#: without a round trip.
QUERY = "query"
TASK_CONTRACT = "task_contract"
SCORING_ABI = "scoring_abi"
EXAMPLES = "examples_unlabeled"
LIBRARY_INDEX = "library_index"
MODEL_CATALOGUE = "model_catalogue"
COMPONENT_TEMPLATE = "component_template"


@dataclass
class Resource:
    resource_id: str
    kind: str
    description: str
    loader: Callable[[], Any]
    #: Bytes of rendered JSON beyond which the read is truncated with a marker.
    max_chars: int = 4000


@dataclass
class ResourceIndex:
    resources: dict[str, Resource] = field(default_factory=dict)

    def add(self, resource: Resource) -> None:
        self.resources[resource.resource_id] = resource

    def ids(self) -> list[str]:
        return list(self.resources)

    def catalogue(self) -> list[dict[str, str]]:
        """What the episode prefix shows: ids and descriptions, no contents."""
        return [
            {
                "resource_id": r.resource_id,
                "kind": r.kind,
                "description": r.description,
            }
            for r in self.resources.values()
        ]

    def get(self, resource_id: str) -> Resource | None:
        return self.resources.get(resource_id)

    def as_plain(self) -> dict[str, Any]:
        """Materialized view, used by the audit-leak assertion at run start."""
        return {rid: r.description for rid, r in self.resources.items()}


def build_resource_index(
    query: QueryRecord,
    pack: TaskPack,
    snapshot: LibrarySnapshot,
    *,
    model_cards: dict[str, dict[str, Any]] | None = None,
    max_chars: int = 4000,
) -> ResourceIndex:
    index = ResourceIndex()

    index.add(
        Resource(
            QUERY,
            "json",
            "The query to build a reward for, with its declared metadata.",
            lambda: {
                "query_id": query.query_id,
                "query": query.query,
                "task_profile_id": query.task_profile_id,
                "reward_mode": query.reward_mode,
                # `reference` is the task's own reference answer, which the
                # reward is allowed to use. Dev-suite oracle labels are a
                # different thing entirely and are not here.
                "reference": query.reference if "reference" in pack.permitted_inputs else None,
                "metadata": query.metadata if "metadata" in pack.permitted_inputs else {k: v for k, v in query.metadata.items() if k in pack.permitted_inputs},
            },
            max_chars,
        )
    )


    index.add(
        Resource(
            EXAMPLES,
            "json",
            "A few unlabeled example inputs of the shape the reward will see.",
            lambda: _public_examples(pack),
            max_chars,
        )
    )

    index.add(
        Resource(
            LIBRARY_INDEX,
            "json",
            "Committed rewards that are applicable to this task profile.",
            lambda: _library_view(snapshot, pack),
            max_chars,
        )
    )

    index.add(
        Resource(
            MODEL_CATALOGUE,
            "json",
            "Frozen model catalogue for synthesis and raw rubric calls. Identities "
            "only; no endpoints.",
            lambda: model_cards or {},
            max_chars,
        )
    )


    for entry in snapshot.entries:
        if pack.reward_logic_policy and entry.definition.runtime_contract.reward_logic_policy != pack.reward_logic_policy:
            continue
        if entry.applicability.task_contract_digest == pack.applicability_rule.task_contract_digest:
            index.add(Resource("reward:" + entry.reward_key, "json", "Committed reward definition",
                               lambda e=entry: e.definition.model_dump(mode="json"), max_chars))

    for extra_id, text in pack.resources.items():
        if extra_id in index.resources:
            continue
        index.add(
            Resource(
                extra_id,
                "text",
                f"Task-pack resource {extra_id!r}.",
                (lambda t=text: t),
                max_chars,
            )
        )

    from ..runtime.policy import MODULE_EXPORTS
    index.add(Resource(SCORING_ABI, 'json', 'Self-contained reward contract', lambda: {
        'scoring_abi': 'v2', 'reward_logic_policy': pack.reward_logic_policy,
        'signature': 'score(example, context) -> {raw_score, feedback, evidence}',
        'general_purpose_modules': MODULE_EXPORTS,
        'dependency_policy': 'Only listed module members are available. No dynamic execution, imports or introspection.',
        'verifiable_context_apis': [], 'rubric_context_apis': ['judge_spec', 'call_llm_api'],
        'zero_is_valid': True, 'normalization': 'apply exactly once in the trusted aggregator'}))
    index.add(Resource(COMPONENT_TEMPLATE, 'text', 'ABI skeleton without a task scoring implementation', lambda:
        'def score(example, context):\n    # Implement parsing, validation, comparisons and raw scoring here.\n'
        '    # You may define local helper functions in this source artifact.\n'
        '    raise NotImplementedError("Implement the task criterion in generated source")\n'))
    index.add(Resource(TASK_CONTRACT, 'json', 'Task requirements and reward dependency boundary', lambda: {
        'profile_id': pack.profile_id, 'task_contract': pack.task_contract,
        'capabilities': [c.model_dump(mode='json') for c in pack.capabilities],
        'permitted_inputs': pack.permitted_inputs, 'permitted_apis': pack.permitted_apis,
        'reward_logic_policy': pack.reward_logic_policy,
        'normalization_mappings': pack.normalization_mappings, 'max_components': pack.max_components}))
    return index


def _public_examples(pack: TaskPack) -> list[dict[str, Any]]:
    """Input shapes only. Derived from the permitted-input list, never from
    the dev suite, so no held-out example can leak through this door."""
    return [{key: f"<{key}>" for key in pack.permitted_inputs}]


def _library_view(snapshot: LibrarySnapshot, pack: TaskPack) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in snapshot.entries:
        if pack.reward_logic_policy and entry.definition.runtime_contract.reward_logic_policy != pack.reward_logic_policy:
            continue
        if entry.applicability.task_contract_digest != pack.applicability_rule.task_contract_digest:
            continue
        out.append(
            {
                "reward_key": entry.reward_key,
                "mode": entry.definition.mode,
                "criteria": [c.criterion for c in entry.definition.components],
                "component_ids": [c.id for c in entry.definition.components],
                "runtime_fingerprint": entry.applicability.runtime_fingerprint,
                "validation": entry.validation_summary,
            }
        )
    return out
