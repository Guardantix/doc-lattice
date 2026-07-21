"""Three-valued PR-reachability evaluation of job-level if: conditions (spec D1).

A job is pruned from the PR scan only when its condition is provably false for every
triggered PR event. The only recognized atom is ``github.event_name == '<literal>'`` in
either operand order inside an optional ``${{ ... }}`` wrapper around a top-level ``&&``
conjunction. Everything else, including any parenthesized grouping, evaluates to unknown, and
unknown never proves falsity.
"""

import re

_WRAPPER_RE = re.compile(r"^\$\{\{(?P<body>.*)\}\}$", re.DOTALL)
_ATOM_LEFT_RE = re.compile(
    r"^github\.event_name\s*==\s*'(?P<literal>[^']*)'$", re.ASCII | re.IGNORECASE
)
_ATOM_RIGHT_RE = re.compile(
    r"^'(?P<literal>[^']*)'\s*==\s*github\.event_name$", re.ASCII | re.IGNORECASE
)

_TRUE = "true"
_FALSE = "false"
_UNKNOWN = "unknown"


def _atom_literal(atom: str) -> str | None:
    """Return the compared event literal when ``atom`` is the recognized form, else None."""
    match = _ATOM_LEFT_RE.match(atom) or _ATOM_RIGHT_RE.match(atom)
    return match.group("literal") if match else None


def _split_conjunction(body: str) -> list[str] | None:
    """Split on top-level ``&&`` outside single quotes; None on structural failure.

    An unquoted parenthesis is a structural failure because a grouping can nest a conjunction the
    splitter would otherwise misread as top-level, so any such body returns None (unknown).
    """
    if "||" in body:
        return None
    atoms: list[str] = []
    current: list[str] = []
    in_quote = False
    index = 0
    while index < len(body):
        char = body[index]
        if char == "'":
            in_quote = not in_quote
            current.append(char)
            index += 1
            continue
        if not in_quote and char in "()":
            return None
        if not in_quote and body.startswith("&&", index):
            atoms.append("".join(current).strip())
            current = []
            index += 2
            continue
        current.append(char)
        index += 1
    if in_quote:
        return None
    atoms.append("".join(current).strip())
    if any(not atom for atom in atoms):
        return None
    return atoms


def _condition_value(condition: str, event: str) -> str:
    """Evaluate the condition for one event: ``true``, ``false``, or ``unknown``."""
    text = condition.strip()
    wrapped = _WRAPPER_RE.match(text)
    if wrapped:
        text = wrapped.group("body").strip()
    elif "${{" in text:
        return _UNKNOWN
    atoms = _split_conjunction(text)
    if atoms is None:
        return _UNKNOWN
    saw_unknown = False
    for atom in atoms:
        literal = _atom_literal(atom)
        if literal is None:
            saw_unknown = True
            continue
        if literal.lower() != event.lower():
            return _FALSE
    return _UNKNOWN if saw_unknown else _TRUE


def job_is_pr_reachable(if_condition: str | None, event_names: frozenset[str]) -> bool:
    """Return whether a job can run for any triggered PR event (spec D1).

    Args:
        if_condition: The job-level ``if:`` text, or None when absent.
        event_names: The document's trigger names intersected with ``PR_EVENTS``.

    Returns:
        False only when the condition is provably false for every event in ``event_names``,
        or when ``event_names`` is empty. Structural failures and unrecognized atoms keep
        the job reachable.
    """
    if not event_names:
        return False
    if if_condition is None or not if_condition.strip():
        return True
    return any(_condition_value(if_condition, event) != _FALSE for event in sorted(event_names))
