"""Domain types for resolved Linear tickets and the findings they produce."""

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict

from .constants import BlockedReason, LinearStateType, Severity
from .text_utils import strip_control_chars

CleanStr = Annotated[str, AfterValidator(strip_control_chars)]
CleanOptStr = Annotated[
    str | None, AfterValidator(lambda v: strip_control_chars(v) if v is not None else None)
]


class TicketState(BaseModel):
    """A Linear workflow state. ``type`` drives grading; ``name`` is the display label."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: CleanStr
    type: LinearStateType


class TicketRef(BaseModel):
    """A lightweight reference to a parent or child ticket, for context only."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    identifier: CleanStr
    title: CleanOptStr = None
    state: TicketState


class Ticket(BaseModel):
    """One resolved Linear issue. All string fields are control-stripped on construction."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    identifier: CleanStr
    title: CleanOptStr = None
    url: CleanStr
    state: TicketState
    parent: TicketRef | None = None
    children: tuple[TicketRef, ...] = ()


@dataclass(frozen=True, slots=True)
class Finding:
    """One reportable result.

    For a graded finding (DANGER, WARNING, INFO), ``ticket`` is the resolved issue and
    ``reason`` is None. For a BLOCKED finding, ``ticket`` is None and ``reason`` says why the
    ref could not be resolved. The two fields are mutually exclusive.
    """

    severity: Severity
    node_id: str
    node_title: str | None
    node_path: Path
    drifted_refs: tuple[str, ...]
    ticket_ref: str
    reason: BlockedReason | None
    ticket: Ticket | None
