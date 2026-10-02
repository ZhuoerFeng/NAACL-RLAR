"""Versioned reward dependency policy; not a production Python sandbox.

The AST admission check and restricted worker namespace enforce the documented
capability surface together. Host/process isolation remains the runner's job.
"""
import ast
import builtins
import importlib
from types import SimpleNamespace

from .errors import ForbiddenAPI

SELF_CONTAINED = 'self_contained_v1'
MODULE_EXPORTS = {
    're': ('compile', 'search', 'match', 'fullmatch', 'findall', 'finditer', 'split', 'sub', 'subn',
           'escape', 'IGNORECASE', 'MULTILINE', 'DOTALL', 'VERBOSE', 'ASCII', 'error'),
    'json': ('loads', 'dumps', 'JSONDecodeError'),
    'math': ('isfinite', 'isclose', 'isnan', 'isinf', 'sqrt', 'floor', 'ceil', 'trunc', 'fabs',
             'gcd', 'lcm', 'factorial', 'prod', 'fsum', 'log', 'log10', 'exp', 'pow', 'pi', 'e', 'inf', 'nan'),
    'decimal': ('Decimal', 'InvalidOperation', 'DivisionByZero', 'Overflow', 'localcontext',
                'ROUND_HALF_EVEN', 'ROUND_HALF_UP', 'ROUND_DOWN'),
    'fractions': ('Fraction',),
}
BUILTINS = ('abs', 'all', 'any', 'bool', 'bytes', 'bytearray', 'callable', 'chr', 'dict', 'divmod',
            'enumerate', 'filter', 'float', 'frozenset', 'int', 'isinstance', 'issubclass', 'iter',
            'len', 'list', 'map', 'max', 'min', 'next', 'ord', 'pow', 'print', 'range', 'repr',
            'reversed', 'round', 'set', 'slice', 'sorted', 'str', 'sum', 'tuple', 'zip',
            'Exception', 'ValueError', 'TypeError', 'KeyError', 'IndexError', 'ZeroDivisionError',
            'ArithmeticError', 'OverflowError', 'RuntimeError', 'AssertionError', 'StopIteration',
            'NotImplementedError', 'ImportError', 'AttributeError')
FORBIDDEN_NAMES = {'eval', 'exec', 'compile', 'open', 'input', 'globals', 'locals', 'vars',
                   'getattr', 'setattr', 'delattr', 'hasattr', 'dir', 'type', 'object', 'super',
                   'breakpoint', 'help', 'exit', 'quit'}
FRAME_ATTRIBUTES = {'gi_frame', 'gi_code', 'gi_yieldfrom', 'cr_frame', 'cr_code', 'cr_await',
                    'ag_frame', 'ag_code', 'ag_await', 'f_globals', 'f_locals', 'f_builtins',
                    'f_back', 'f_code', 'tb_frame', 'tb_next', 'func_globals', 'func_code'}


def validate_pack(pack):
    if pack.reward_logic_policy != SELF_CONTAINED:
        raise ValueError('self_contained_v1 requires a task pack with the same reward_logic_policy')
    allowed = {'call_llm_api'} | ({'run_code'} if pack.code_execution is not None else set())
    if set(pack.permitted_apis) - allowed:
        raise ValueError('self-contained rewards may only declare the raw rubric call_llm_api utility'
                         ' and, with a declared code_execution environment, run_code')
    if 'run_code' in pack.permitted_apis and pack.code_execution is None:
        raise ValueError('run_code requires a declared code_execution environment')
    policy = pack.suite_policy
    if policy and (policy.objective_checker or 'objective_verified' in policy.required_sources):
        raise ValueError('self_contained_v1 has no objective checker; use model_inferred or externally bound human_annotated evidence')


def validate_component(component, *, dependencies=()):
    if dependencies:
        raise ForbiddenAPI('self_contained_v1 supports only its versioned standard-library allowlist: '
                           f'runtime_contract.dependencies must be [] (got {list(dependencies)!r}); allowlisted '
                           'modules are imported directly in source and are not declared as dependencies')
    # run_code is a brokered capability, not a dependency: the reward process
    # still has only the allowlist below; the task pack decides availability.
    allowed = ({'call_llm_api'} if component.kind == 'rubric' else set()) | {'run_code'}
    if set(component.required_apis) - allowed:
        raise ForbiddenAPI('task-level checker/scoring APIs are unavailable to self-contained reward code')
    tree = ast.parse(component.source)
    # Names bound by `import re` / `import re as rx`; their attributes must be exported members.
    module_aliases = {n.asname or n.name: n.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                      for n in node.names if n.name in MODULE_EXPORTS}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(n.name not in MODULE_EXPORTS for n in node.names):
                raise ForbiddenAPI('import must use the declared general-purpose module allowlist')
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module not in MODULE_EXPORTS or any(n.name not in MODULE_EXPORTS[node.module] for n in node.names):
                raise ForbiddenAPI('imported module/member is outside the general-purpose allowlist')
        elif isinstance(node, ast.Name) and (node.id.startswith('__') or node.id in FORBIDDEN_NAMES):
            raise ForbiddenAPI('dynamic execution/introspection is unavailable: ' + node.id)
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith('_') or node.attr in FRAME_ATTRIBUTES:
                raise ForbiddenAPI('private/introspection attributes are unavailable: ' + node.attr)
            exposed = ({'judge_spec', 'call_llm_api'} if component.kind == 'rubric' else set()) | (
                {'run_code'} if 'run_code' in component.required_apis else set())
            if isinstance(node.value, ast.Name) and node.value.id == 'context' and node.attr not in exposed:
                raise ForbiddenAPI('context does not expose ' + node.attr)
            module = module_aliases.get(node.value.id) if isinstance(node.value, ast.Name) else None
            if module and node.attr not in MODULE_EXPORTS[module]:
                raise ForbiddenAPI(f'{module}.{node.attr} is outside the general-purpose allowlist; '
                                   f'{module} exports only {list(MODULE_EXPORTS[module])}')
    return tree


class DependencyGuard:
    def __init__(self):
        self.violation = None

    def deny(self, message):
        self.violation = message
        raise ForbiddenAPI(message)

    def check(self):
        if self.violation:
            raise ForbiddenAPI(self.violation)

    def namespace(self):
        modules = {name: SimpleNamespace(**{attr: getattr(importlib.import_module(name), attr)
                                           for attr in attrs}) for name, attrs in MODULE_EXPORTS.items()}

        def restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
            if level or name not in modules or any(attr not in MODULE_EXPORTS[name] for attr in (fromlist or ())):
                self.deny('module/member is outside the reward dependency policy: ' + name)
            return modules[name]

        return {'__builtins__': {**{name: getattr(builtins, name) for name in BUILTINS},
                                 '__import__': restricted_import}}


def reward_context(judge_channel, component, guard):
    """No checker instances, proxy, or worker internals on the public object."""
    public = {}
    if component.kind == 'rubric':
        import copy
        public['judge_spec'] = copy.deepcopy(judge_channel.judge_spec)

        def call(message, model_name):
            try:
                return judge_channel.call_llm_api(message, model_name)
            except ForbiddenAPI as exc:
                guard.deny(str(exc))

        public['call_llm_api'] = call

    if 'run_code' in component.required_apis:
        def run_code(source, stdin='', timeout_s=None):
            try:
                return judge_channel.run_code(source, stdin=stdin, timeout_s=timeout_s)
            except ForbiddenAPI as exc:
                guard.deny(str(exc))

        public['run_code'] = run_code

    class Context:
        def __getattribute__(self, name):
            if name not in public:
                guard.deny('context does not expose ' + name)
            return public[name]

        def __setattr__(self, name, value):
            guard.deny('reward context is read-only')

    return Context()
