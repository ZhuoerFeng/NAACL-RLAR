"""Deterministic admission. Model provenance claims are never credentials."""
from itertools import combinations

from ..schemas import FrozenSuite, SuiteDraft
from ..storage.canonical import digest


def suite_body(suite):
    return suite.model_dump(mode='json', exclude={'suite_digest'})


def assert_frozen(suite):
    if digest(suite_body(suite)) != suite.suite_digest:
        raise ValueError('frozen suite digest mismatch')


def example_input(example, query, pack):
    # Overrides may only repeat the allowed task context, never inject labels.
    base = {'query': query.query, 'reference': query.reference, 'metadata': query.metadata,
            **query.metadata, 'response': example.response}
    return {k: base[k] for k in pack.permitted_inputs if k in base}


def admission_binding(case, examples):
    return digest({'case': case, 'examples': examples})


def admit_suite(draft, query, pack, *, generation_config_ref, blobs):
    if pack.reward_logic_policy:
        from ..runtime.policy import validate_pack
        validate_pack(pack)
    policy = pack.suite_policy
    if policy is None or not pack.capabilities:
        raise ValueError('task pack needs capabilities and suite_policy')
    if draft.capabilities != pack.capabilities:
        raise ValueError('capabilities differ from the fixed task requirements')
    if len(draft.cases) > policy.max_cases or len(draft.examples) > policy.max_examples:
        raise ValueError('suite size exceeds policy')
    caps = {c.id for c in pack.capabilities}
    examples = {e.id: e for e in draft.examples}
    if len(examples) != len(draft.examples) or len({c.id for c in draft.cases}) != len(draft.cases):
        raise ValueError('duplicate IDs')
    if len({digest(e.response) for e in draft.examples}) != len(examples):
        raise ValueError('duplicate candidate answers')
    for e in draft.examples:
        if '/' in e.id:
            raise ValueError('example IDs cannot contain evidence-reference separators')
        if e.query_ref != query.query_id:
            raise ValueError('unknown query reference')
        actual = example_input(e, query, pack)
        if any(k not in actual or actual[k] != v for k, v in e.allowed_reference_or_metadata.items()):
            raise ValueError('candidate cannot change the authorized task context')
    counts = {(cap, category): 0 for cap in caps for category in policy.categories}
    labels, relations = {}, {}
    used, signatures = set(), set()
    for case in draft.cases:
        if not set(case.capability_ids) <= caps or len(set(case.capability_ids)) != len(case.capability_ids):
            raise ValueError('unknown or repeated capability')
        if not set(case.example_ids) <= examples.keys() or len(set(case.example_ids)) != len(case.example_ids):
            raise ValueError('unknown or repeated example reference')
        used.update(case.example_ids)
        category = case.expected_label if case.kind == 'pointwise' else 'ranking'
        if category not in policy.categories:
            raise ValueError('case belongs to a removed category')
        signature = (case.kind, case.scope, tuple(sorted(case.capability_ids)), tuple(sorted(case.example_ids)))
        if signature in signatures:
            raise ValueError('duplicate case intent')
        signatures.add(signature)
        if case.required:
            if case.evidence_source not in policy.required_sources:
                raise ValueError('required case has disallowed evidence source')
            for cap in case.capability_ids:
                counts[cap, category] += 1
        if case.kind == 'pointwise':
            if len(case.example_ids) != 1 or case.expected_label is None or case.relations:
                raise ValueError('pointwise needs one candidate, one label and no relations')
            for cap in case.capability_ids:
                key = (case.scope, cap, case.example_ids[0])
                if key in labels and labels[key] != case.expected_label:
                    raise ValueError('contradictory pointwise labels')
                labels[key] = case.expected_label
        else:
            n = 2 if policy.ranking_layout == 'pairwise' else 3
            pairs = {frozenset((r.left, r.right)) for r in case.relations}
            if (case.expected_label is not None or len(case.example_ids) != n
                or len(case.relations) != n * (n - 1) // 2
                or pairs != {frozenset(p) for p in combinations(case.example_ids, 2)}):
                raise ValueError('ranking requires all distinct pairwise relations for its layout')
            edges = [(r.left, r.operator, r.right, f'case {case.id}: {r.justification}') for r in case.relations]
            for cap in case.capability_ids:
                relations.setdefault((case.scope, cap), []).extend(edges)
        selected = [examples[i] for i in case.example_ids]
        refs = case.evidence_refs + [ref for r in case.relations for ref in r.evidence_refs]
        if case.evidence_source == 'human_annotated':
            binding = admission_binding(case, selected)
            if any(policy.trusted_evidence.get(ref) != binding for ref in refs):
                raise ValueError('human evidence lacks an externally pinned annotation binding')
        else:
            allowed = [query.query_id, pack.profile_id, *caps]
            bad = sorted({ref for ref in refs if ref not in allowed})
            if bad:
                raise ValueError(f'model_inferred basis references unavailable task evidence in case {case.id!r}: '
                                 f'{bad}; evidence_refs must be exact ids from {allowed}')
    if used != examples.keys():
        raise ValueError('unused candidate answers')
    if any(n < policy.min_cases_per_category for n in counts.values()):
        raise ValueError('required capability/category coverage missing')
    implied = _implied_edges(relations, labels, caps)
    for (scope, cap), edges in relations.items():
        if scope == 'overall':
            continue
        _check_order(edges + implied[scope, cap], f'capability {cap!r}')
    overall = [e for (scope, _), edges in relations.items() if scope == 'overall' for e in edges]
    if overall:
        _check_order(list({id(e): e for e in overall}.values()) + implied['overall', None], 'overall ranking')
    report = {'admitted': True, 'draft_digest': digest(draft), 'policy_digest': digest(policy),
              'task_digest': digest(pack), 'query_digest': digest(query),
              'source_levels': sorted({c.evidence_source for c in draft.cases}),
              'semantic_truth_proven': False}
    if pack.reward_logic_policy:
        report['admission_scope'] = 'structure_and_provenance_only'
        report['semantic_truth_proven'] = False
    body = {**draft.model_dump(mode='json'), 'schema_version': 'rlar.suite.v2',
            'generation_config_ref': generation_config_ref, 'admission_report_ref': blobs.put_json(report)}
    return FrozenSuite(**body, suite_digest=digest(body))


def _consistent_relations(relations):
    """Explicit relations alone must not form a strict cycle."""
    _check_order([(r.left, r.operator, r.right, 'explicit relation') for r in relations], 'ranking relations')


def _implied_edges(case_edges, labels, caps):
    """Orders implied by pointwise labels, keyed like ``case_edges``.

    Capability scope: a candidate passing capability c ranks above one failing c.
    Overall scope: overall pass ranks above overall fail, and a candidate whose
    capability labels Pareto-dominate another's (all labelled, >= everywhere,
    > somewhere) ranks above it under any monotone aggregation.
    """
    implied = {}
    for (scope, cap) in case_edges:
        if scope == 'overall':
            continue
        passed = [e for (sc, c, e), v in labels.items() if sc == 'capability' and c == cap and v == 'pass']
        failed = [e for (sc, c, e), v in labels.items() if sc == 'capability' and c == cap and v == 'fail']
        implied[scope, cap] = [(p, '>', f, f'implied by labels: {p} passes {cap}, {f} fails it') for p in passed for f in failed]
    overall = []
    passed = {e for (sc, _, e), v in labels.items() if sc == 'overall' and v == 'pass'}
    failed = {e for (sc, _, e), v in labels.items() if sc == 'overall' and v == 'fail'}
    overall += [(p, '>', f, f'implied by labels: {p} is an overall pass, {f} an overall fail') for p in sorted(passed) for f in sorted(failed)]
    profiles = {}
    for (sc, cap, e), v in labels.items():
        if sc == 'capability':
            profiles.setdefault(e, {})[cap] = 1 if v == 'pass' else 0
    complete = {e: p for e, p in profiles.items() if set(p) == caps}
    for a, pa in sorted(complete.items()):
        for b, pb in sorted(complete.items()):
            if a != b and all(pa[c] >= pb[c] for c in caps) and any(pa[c] > pb[c] for c in caps):
                better = sorted(c for c in caps if pa[c] > pb[c])
                overall.append((a, '>', b, f'implied by labels: {a} passes {better} where {b} fails and is no worse elsewhere'))
    implied['overall', None] = overall
    return implied


def _check_order(edges, where):
    """Reject strict cycles over explicit and implied edges, naming the chain."""
    parent = {v: v for left, _, right, _ in edges for v in (left, right)}
    def root(x):
        while parent[x] != x:
            x = parent[x]
        return x
    equal = [e for e in edges if e[1] == '=']
    for left, _, right, _ in equal:
        parent[root(left)] = root(right)
    graph = {}
    for edge in edges:
        left, op, right, _ = edge
        if op != '>':
            continue
        if root(left) == root(right):
            chain = '; '.join(f'{a} = {b} ({why})' for a, _, b, why in equal if root(a) == root(left))
            raise ValueError(f'contradictory partial order in {where}: strict preference cycle {left} > {right} '
                             f'({edge[3]}) but they are declared equivalent via {chain}. '
                             'Make every pointwise label and ranking relation agree.')
        graph.setdefault(root(left), []).append((root(right), edge))
    state, path = {}, []
    def visit(x):
        state[x] = 'open'
        for y, edge in graph.get(x, []):
            path.append(edge)
            if state.get(y) == 'open':
                cycle = path[next(i for i, e in enumerate(path) if root(e[0]) == y):]
                raise ValueError(f'contradictory partial order in {where}: strict preference cycle '
                                 + ' -> '.join(f'{a} > {b} ({why})' for a, _, b, why in cycle)
                                 + '. Make every pointwise label and ranking relation agree.')
            if y not in state:
                visit(y)
            path.pop()
        state[x] = 'done'
    for x in list(graph):
        if x not in state:
            visit(x)
