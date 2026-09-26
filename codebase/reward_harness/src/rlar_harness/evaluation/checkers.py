"""Trusted, version-pinned checkers exposed to reward code through ``context``.

These are *not* a universal oracle. A task pack declares which checker bundle
its rewards may use in ``permitted_apis``; calling anything else raises
:class:`ForbiddenAPI`. Nothing here exposes the filesystem, the dataset, or the
evaluator's oracle labels.

Stdlib only: this module is imported inside the execution worker.
"""

from __future__ import annotations

import ast
import re
from fractions import Fraction
from typing import Any

CHECKER_BUNDLE_VERSION = "checkers.v1"


class ForbiddenAPI(Exception):
    """Raised when reward code calls a capability its profile does not permit."""


class CheckerError(Exception):
    """Raised when a checker cannot produce a verdict (a scoring error)."""


# --------------------------------------------------------------------------
# answer extraction
# --------------------------------------------------------------------------

_BOXED = re.compile(r"\\boxed\{([^{}]*)\}")
_CODE_BLOCK = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)


def extract_boxed(text: str) -> str | None:
    matches = _BOXED.findall(text or "")
    return matches[-1].strip() if matches else None


def extract_code_block(text: str) -> str | None:
    matches = _CODE_BLOCK.findall(text or "")
    if matches:
        return matches[-1]
    return None


def parse_rational(text: str | None) -> Fraction | None:
    """Parse an exact rational. Returns ``None`` when the text is not one."""
    if text is None:
        return None
    cleaned = text.strip().replace(" ", "").replace(",", "")
    cleaned = cleaned.replace("\\dfrac", "\\frac").replace("\\tfrac", "\\frac")
    frac = re.fullmatch(r"\\frac\{(-?\d+)\}\{(-?\d+)\}", cleaned)
    if frac:
        num, den = int(frac.group(1)), int(frac.group(2))
        if den == 0:
            return None
        return Fraction(num, den)
    try:
        return Fraction(cleaned)
    except (ValueError, ZeroDivisionError):
        return None


# --------------------------------------------------------------------------
# bundles
# --------------------------------------------------------------------------


class RationalArithmeticCheckerV1:
    """Exact rational arithmetic answer checking. No floating-point slack."""

    api_id = "rational_arithmetic_v1"
    version = "1.0.0"

    def check_answer(self, example: dict[str, Any]) -> float:
        reference = example.get("reference")
        if reference is None:
            raise CheckerError("no reference answer is available for this example")
        expected = parse_rational(str(reference))
        if expected is None:
            raise CheckerError(f"reference {reference!r} is not an exact rational")
        got = parse_rational(extract_boxed(example.get("response", "")))
        if got is None:
            return 0.0
        return 1.0 if got == expected else 0.0

    def check_declared_format(self, example: dict[str, Any]) -> float:
        """Only the formatting the task contract actually requires."""
        return 1.0 if extract_boxed(example.get("response", "")) is not None else 0.0

    def extract_answer(self, example: dict[str, Any]) -> str | None:
        return extract_boxed(example.get("response", ""))


class PythonUnitTestsCheckerV1:
    """Runs a candidate's Python function against the pack's fixed test cases.

    The test cases arrive as a whitelisted scoring input (``task_tests``) that
    is part of the public task contract, not as an oracle label.
    """

    api_id = "python_unit_tests_v1"
    version = "1.0.0"

    def run_task_tests(self, example: dict[str, Any]) -> float:
        tests = example.get("task_tests") or []
        if not tests:
            raise CheckerError("no task_tests were supplied for this example")
        entrypoint = example.get("task_entrypoint")
        if not entrypoint:
            raise CheckerError("no task_entrypoint was supplied for this example")

        code = extract_code_block(example.get("response", "")) or example.get("response", "")
        namespace: dict[str, Any] = {}
        try:
            compiled = compile(code, "<candidate>", "exec")
            exec(compiled, namespace)  # noqa: S102 - controlled prototype worker
        except BaseException:
            # A candidate that does not even import is a valid low score, not a
            # scoring error: it simply passes none of the tests.
            return 0.0
        fn = namespace.get(entrypoint)
        if not callable(fn):
            return 0.0

        passed = 0
        for case in tests:
            try:
                args = case.get("args", [])
                kwargs = case.get("kwargs", {})
                expected = case["expected"]
                if fn(*args, **kwargs) == expected:
                    passed += 1
            except BaseException:
                continue  # a failing candidate is a low score, not an error
        return passed / len(tests)

    def has_docstring(self, example: dict[str, Any]) -> float:
        code = extract_code_block(example.get("response", "")) or example.get("response", "")
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return 0.0
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and ast.get_docstring(node):
                return 1.0
        return 0.0


BUNDLES: dict[str, Any] = {
    RationalArithmeticCheckerV1.api_id: RationalArithmeticCheckerV1,
    PythonUnitTestsCheckerV1.api_id: PythonUnitTestsCheckerV1,
}

#: Methods each bundle exposes on ``context``.
BUNDLE_METHODS: dict[str, tuple[str, ...]] = {
    "rational_arithmetic_v1": ("check_answer", "check_declared_format", "extract_answer"),
    "python_unit_tests_v1": ("run_task_tests", "has_docstring"),
}


def bundle_versions(api_ids: list[str]) -> dict[str, str]:
    out = {}
    for api_id in sorted(set(api_ids)):
        cls = BUNDLES.get(api_id)
        if cls is not None:
            out[api_id] = cls.version
    return out
