"""Boundary module: validate GitHub Actions YAML into a typed audit model."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Never

from ruamel.yaml import YAML
from ruamel.yaml.constructor import DuplicateKeyError
from ruamel.yaml.error import YAMLError
from ruamel.yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from ..error_types import ConfigError
from .model import (
    PermissionValue,
    WorkflowDocument,
    WorkflowJob,
    WorkflowScalar,
    WorkflowStep,
    WorkflowTrigger,
)

_YAML_MERGE_TAG = "tag:yaml.org,2002:merge"
_YAML_1_2 = (1, 2)

# Inclusive security budgets for repository-controlled workflow audit input. The root YAML node
# has depth 1; visits count each expanded syntax or loaded-value occurrence, including aliases.
_MAX_UTF8_INPUT_BYTES = 1_048_576
_MAX_YAML_NESTING_DEPTH = 100
_MAX_EXPANDED_VISITS = 50_000
_MAX_COLLECTED_STRING_SCALARS = 10_000
_UTF8_COUNT_CHUNK_CHARS = 8_192


@dataclass(slots=True)
class _TraversalBudget:
    workflow_path: Path
    visits: int = 0
    string_scalars: int = 0

    def collect_string(self) -> None:
        self.string_scalars += 1
        if self.string_scalars > _MAX_COLLECTED_STRING_SCALARS:
            raise _resource_limit(self.workflow_path)

    def reserve_visits(self, additional: int, *, depth: int) -> None:
        if additional == 0:
            return
        if depth > _MAX_YAML_NESTING_DEPTH:
            raise _resource_limit(self.workflow_path)
        if self.visits + additional > _MAX_EXPANDED_VISITS:
            raise _resource_limit(self.workflow_path)
        self.visits += additional


def parse_workflow(path: Path, text: str) -> WorkflowDocument:
    """Validate workflow YAML into the typed subset required by audit.

    Args:
        path: Display path used in the returned model and any validation error.
        text: Complete workflow YAML text.

    Returns:
        A typed workflow document containing the fields required by policy audit.

    Raises:
        ConfigError: If the YAML is malformed or has a shape the audit cannot inspect.
    """
    try:
        if _exceeds_utf8_input_budget(text):
            raise _resource_limit(path)

        yaml = YAML(typ="safe")
        yaml.allow_duplicate_keys = False
        syntax_tree = yaml.compose(text)
        version: Any = yaml.version
        if version not in (None, _YAML_1_2):
            raise _parse_error(path, "unsupported YAML version directive")

        budget = _TraversalBudget(path)
        _validate_syntax_tree(syntax_tree, path, budget)
        raw: Any = yaml.load(text)
        root = _require_mapping(raw, path, ())
        scalars = tuple(_collect_scalars(root, path, budget))
        triggers = _parse_triggers(root["on"], path) if "on" in root else ()
        permissions = _parse_permissions(root.get("permissions"), path, ("permissions",))
        if "jobs" not in root:
            _invalid(path, ("jobs",), "must be a mapping")
        jobs = _parse_jobs(root["jobs"], path)
        return WorkflowDocument(
            path=path,
            triggers=triggers,
            permissions=permissions,
            jobs=jobs,
            scalars=scalars,
        )
    except DuplicateKeyError as exc:
        raise _parse_error(path, "duplicate YAML mapping key") from exc
    except YAMLError as exc:
        raise _parse_error(path, "malformed YAML") from exc
    except RecursionError as exc:
        raise _resource_limit(path) from exc
    except (UnicodeEncodeError, UnicodeDecodeError, ValueError) as exc:
        raise _parse_error(path, "malformed YAML") from exc


def _validate_syntax_tree(node: Node | None, workflow_path: Path, budget: _TraversalBudget) -> None:
    if node is None:
        return

    active_nodes: set[int] = set()
    budget.reserve_visits(1, depth=1)
    stack: list[tuple[Node, tuple[str, ...], int, bool]] = [(node, (), 1, False)]
    while stack:
        current, yaml_path, depth, exiting = stack.pop()
        current_id = id(current)
        if exiting:
            active_nodes.remove(current_id)
            continue

        if isinstance(current, ScalarNode):
            continue
        if not isinstance(current, (MappingNode, SequenceNode)):
            continue
        if current_id in active_nodes:
            _invalid(workflow_path, yaml_path, "recursive YAML aliases are not supported")

        active_nodes.add(current_id)
        stack.append((current, yaml_path, depth, True))
        if isinstance(current, MappingNode):
            budget.reserve_visits(len(current.value) * 2, depth=depth + 1)
            _validate_mapping_syntax(current, workflow_path, yaml_path)
            for key_node, value_node in reversed(current.value):
                key_component = _syntax_key_component(key_node)
                stack.append(
                    (
                        value_node,
                        (*yaml_path, key_component),
                        depth + 1,
                        False,
                    )
                )
                stack.append((key_node, yaml_path, depth + 1, False))
        else:
            budget.reserve_visits(len(current.value), depth=depth + 1)
            for index in range(len(current.value) - 1, -1, -1):
                stack.append(
                    (
                        current.value[index],
                        (*yaml_path, str(index)),
                        depth + 1,
                        False,
                    )
                )


def _validate_mapping_syntax(
    node: MappingNode, workflow_path: Path, yaml_path: tuple[str, ...]
) -> None:
    seen: set[tuple[str, str]] = set()
    for key_node, _value_node in node.value:
        key_component = _syntax_key_component(key_node)
        value_path = (*yaml_path, key_component)
        if isinstance(key_node, ScalarNode):
            if key_node.tag == _YAML_MERGE_TAG:
                _invalid(workflow_path, value_path, "YAML merge keys are not supported")
            marker = (key_node.tag, key_node.value)
            if marker in seen:
                _invalid(workflow_path, value_path, "duplicate YAML mapping key")
            seen.add(marker)


def _syntax_key_component(node: Node) -> str:
    return node.value if isinstance(node, ScalarNode) else "<non-scalar-key>"


def _parse_triggers(raw: Any, workflow_path: Path) -> tuple[WorkflowTrigger, ...]:
    if isinstance(raw, str):
        return (WorkflowTrigger(name=raw, shape="null", branches=None),)
    if isinstance(raw, list):
        triggers: list[WorkflowTrigger] = []
        for index, event in enumerate(raw):
            if not isinstance(event, str):
                _invalid(workflow_path, ("on", str(index)), "event name must be a string")
            triggers.append(WorkflowTrigger(name=event, shape="null", branches=None))
        return tuple(triggers)
    if not isinstance(raw, dict):
        _invalid(workflow_path, ("on",), "must be a string, sequence, or mapping")

    triggers = []
    for event, config in raw.items():
        if config is None:
            triggers.append(WorkflowTrigger(name=event, shape="null", branches=None))
        elif isinstance(config, dict):
            branches = (
                _parse_branches(config["branches"], workflow_path, ("on", event, "branches"))
                if "branches" in config
                else None
            )
            triggers.append(WorkflowTrigger(name=event, shape="mapping", branches=branches))
        elif isinstance(config, list):
            triggers.append(WorkflowTrigger(name=event, shape="sequence", branches=None))
        else:
            _invalid(
                workflow_path,
                ("on", event),
                "event configuration must be null, a mapping, or a sequence",
            )
    return tuple(triggers)


def _parse_branches(raw: Any, workflow_path: Path, yaml_path: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, list) and all(isinstance(branch, str) for branch in raw):
        return tuple(raw)
    _invalid(workflow_path, yaml_path, "branches must be a string or a sequence of strings")


def _parse_permissions(
    raw: Any, workflow_path: Path, yaml_path: tuple[str, ...]
) -> PermissionValue:
    if raw is None or isinstance(raw, str):
        return raw
    if not isinstance(raw, dict):
        _invalid(workflow_path, yaml_path, "permissions must be a string, mapping, or null")
    pairs: list[tuple[str, str]] = []
    for key, value in raw.items():
        if not isinstance(value, str):
            _invalid(
                workflow_path,
                (*yaml_path, key),
                "permission value must be a string",
            )
        pairs.append((key, value))
    return tuple(sorted(pairs))


def _parse_jobs(raw: Any, workflow_path: Path) -> tuple[WorkflowJob, ...]:
    jobs = _require_mapping(raw, workflow_path, ("jobs",))
    parsed: list[WorkflowJob] = []
    for job_id, raw_job in jobs.items():
        job_path = ("jobs", job_id)
        job = _require_mapping(raw_job, workflow_path, job_path)
        permissions = _parse_permissions(
            job.get("permissions"), workflow_path, (*job_path, "permissions")
        )
        env = (
            _parse_scalar_mapping(job["env"], workflow_path, (*job_path, "env"))
            if "env" in job
            else ()
        )
        steps = (
            _parse_steps(job["steps"], workflow_path, (*job_path, "steps"))
            if "steps" in job
            else ()
        )
        parsed.append(
            WorkflowJob(
                job_id=job_id,
                if_condition=_optional_audited_string(
                    job.get("if"), workflow_path, (*job_path, "if")
                ),
                environment=job.get("environment")
                if isinstance(job.get("environment"), str)
                else None,
                runs_on=job.get("runs-on") if isinstance(job.get("runs-on"), str) else None,
                permissions=permissions,
                env=env,
                steps=steps,
            )
        )
    return tuple(parsed)


def _parse_steps(
    raw: Any, workflow_path: Path, yaml_path: tuple[str, ...]
) -> tuple[WorkflowStep, ...]:
    if not isinstance(raw, list):
        _invalid(workflow_path, yaml_path, "steps must be a sequence of mappings")
    parsed: list[WorkflowStep] = []
    for index, raw_step in enumerate(raw):
        step_path = (*yaml_path, str(index))
        step = _require_mapping(raw_step, workflow_path, step_path)
        env = (
            _parse_scalar_mapping(step["env"], workflow_path, (*step_path, "env"))
            if "env" in step
            else ()
        )
        with_values = (
            _parse_scalar_mapping(step["with"], workflow_path, (*step_path, "with"))
            if "with" in step
            else ()
        )
        parsed.append(
            WorkflowStep(
                index=index,
                step_id=_optional_audited_string(step.get("id"), workflow_path, (*step_path, "id")),
                name=_optional_audited_string(
                    step.get("name"), workflow_path, (*step_path, "name")
                ),
                uses=_optional_audited_string(
                    step.get("uses"), workflow_path, (*step_path, "uses")
                ),
                run=_optional_audited_string(step.get("run"), workflow_path, (*step_path, "run")),
                env=env,
                with_values=with_values,
            )
        )
    return tuple(parsed)


def _parse_scalar_mapping(
    raw: Any, workflow_path: Path, yaml_path: tuple[str, ...]
) -> tuple[tuple[str, str], ...]:
    mapping = _require_mapping(raw, workflow_path, yaml_path)
    pairs: list[tuple[str, str]] = []
    for key, value in mapping.items():
        pairs.append((key, _normalize_scalar(value, workflow_path, (*yaml_path, key))))
    return tuple(sorted(pairs))


def _normalize_scalar(raw: Any, workflow_path: Path, yaml_path: tuple[str, ...]) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, (int, float)):
        return str(raw)
    _invalid(workflow_path, yaml_path, "value must be a string, boolean, or number")


def _optional_audited_string(
    raw: Any, workflow_path: Path, yaml_path: tuple[str, ...]
) -> str | None:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (dict, list, set, tuple)):
        _invalid(workflow_path, yaml_path, "value must be a scalar")
    return None


def _require_mapping(raw: Any, workflow_path: Path, yaml_path: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        detail = "workflow root must be a mapping" if not yaml_path else "must be a mapping"
        _invalid(workflow_path, yaml_path, detail)
    for key in raw:
        if not isinstance(key, str):
            _invalid(workflow_path, yaml_path, "mapping keys must be strings")
    return raw


def _collect_scalars(
    raw: Any, workflow_path: Path, budget: _TraversalBudget
) -> list[WorkflowScalar]:
    scalars: list[WorkflowScalar] = []
    active_containers: set[int] = set()
    budget.reserve_visits(1, depth=1)
    stack: list[tuple[Any, tuple[str, ...], int, bool]] = [(raw, (), 1, False)]
    while stack:
        current, yaml_path, depth, exiting = stack.pop()
        current_id = id(current)
        if exiting:
            active_containers.remove(current_id)
            continue

        if isinstance(current, str):
            budget.collect_string()
            scalars.append(WorkflowScalar(path=yaml_path, value=current))
            continue
        if isinstance(current, dict):
            if current_id in active_containers:
                _invalid(workflow_path, yaml_path, "recursive YAML aliases are not supported")
            active_containers.add(current_id)
            stack.append((current, yaml_path, depth, True))
            _schedule_mapping_values(current, yaml_path, depth, budget, stack)
        elif isinstance(current, list):
            if current_id in active_containers:
                _invalid(workflow_path, yaml_path, "recursive YAML aliases are not supported")
            active_containers.add(current_id)
            stack.append((current, yaml_path, depth, True))
            _schedule_sequence_values(current, yaml_path, depth, budget, stack)
        elif isinstance(current, (set, tuple)):
            _invalid(workflow_path, yaml_path, "unsupported YAML container")
    return scalars


def _schedule_mapping_values(
    mapping: dict[Any, Any],
    yaml_path: tuple[str, ...],
    depth: int,
    budget: _TraversalBudget,
    stack: list[tuple[Any, tuple[str, ...], int, bool]],
) -> None:
    budget.reserve_visits(len(mapping), depth=depth + 1)
    for key in mapping:
        if not isinstance(key, str):
            _invalid(budget.workflow_path, yaml_path, "mapping keys must be strings")
    for key in reversed(mapping):
        if not isinstance(key, str):
            _invalid(budget.workflow_path, yaml_path, "mapping keys must be strings")
        stack.append((mapping[key], (*yaml_path, key), depth + 1, False))


def _schedule_sequence_values(
    sequence: list[Any],
    yaml_path: tuple[str, ...],
    depth: int,
    budget: _TraversalBudget,
    stack: list[tuple[Any, tuple[str, ...], int, bool]],
) -> None:
    budget.reserve_visits(len(sequence), depth=depth + 1)
    for index in range(len(sequence) - 1, -1, -1):
        stack.append((sequence[index], (*yaml_path, str(index)), depth + 1, False))


def _invalid(workflow_path: Path, yaml_path: tuple[str, ...], detail: str) -> Never:
    raise ConfigError(
        f"invalid GitHub workflow {_display_path(workflow_path)} "
        f"at {_display_yaml_path(yaml_path)}: {detail}"
    )


def _parse_error(workflow_path: Path, detail: str) -> ConfigError:
    return ConfigError(f"cannot parse GitHub workflow {_display_path(workflow_path)}: {detail}")


def _resource_limit(workflow_path: Path) -> ConfigError:
    return _parse_error(workflow_path, "workflow resource limit exceeded")


def _exceeds_utf8_input_budget(text: str) -> bool:
    if len(text) > _MAX_UTF8_INPUT_BYTES:
        return True
    total = 0
    for start in range(0, len(text), _UTF8_COUNT_CHUNK_CHARS):
        chunk = text[start : start + _UTF8_COUNT_CHUNK_CHARS]
        total += len(chunk.encode("utf-8"))
        if total > _MAX_UTF8_INPUT_BYTES:
            return True
    return False


def _display_path(workflow_path: Path) -> str:
    return json.dumps(str(workflow_path), ensure_ascii=True)


def _display_yaml_path(yaml_path: tuple[str, ...]) -> str:
    return "$" + "".join(f"[{json.dumps(component, ensure_ascii=True)}]" for component in yaml_path)
