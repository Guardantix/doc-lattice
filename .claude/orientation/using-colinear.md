---
name: using-colinear
description: Auto-loaded orientation for Linear workflows with colinear. Injected at session start by the consuming project. Do not invoke directly.
---

# Using colinear

Linear work for this repo flows through one named pipeline.
Two named commands cover it.
`/colinear` is a direct-invocation router whose verbs are the pipeline's stage transitions — each invocation names one verb, loads exactly one mode file, and never fires on its own.
`/linear-finalize` is the one skill that auto-invokes, on an open `ABC-N` PR, and it runs the same handback as `/colinear handback`.
For ad-hoc reads/writes outside a workflow, run the `colinear` CLI with `--help`.

This orientation matches colinear 0.82.x — verify against the `version:` line in `doctor` output; on a major/minor mismatch STOP and tell the user to re-run `colinear orientation enable`.
If `colinear` is not found at all, run `./install.sh` from the colinear repo checkout — that is the version-skew recovery path and does not depend on the new binary.

## The pipeline

```
Triage ─→ Backlog ─→ Ready ─→ In Progress ─→ In Review ─→ Done
   │         │          │          │              │          │
/colinear /colinear  /colinear human work    /colinear  /colinear
triage    ready      start                   handback   ship
          then       then                               (human-
          /colinear  /colinear                          gated)
          ready      start
          ABC-N      ABC-N
```

Every arrow on that line is driven by a colinear command, including the one into the review state.
`/colinear handback` performs that handback itself rather than depending on the team's Linear GitHub automation, whose rows are configured per team and may move the issue on PR open, on review request, or not at all.
It attaches the attention marker either way, which no automation does, and verifies the result.
Where an automation has already moved the issue to the review state, the handback says so and does the label half alone.
The last arrow is the one colinear does not write directly.
`/colinear ship` merges the PR and writes no Linear state of its own; where the team's Linear GitHub integration closes issues on merge, which is its stock default, the issue reaches Done from that merge.
So the gate is on invoking `/colinear ship`, and an automated close is the expected outcome rather than a fault.
Where that automation is off, a successful merge leaves the issue in the review state and a human moves it.

The authoritative "agent finished, a human is needed" signal is the configured `labels.needs_human_review` marker, attached by the same handback write.
Read that, not the workflow state, when you want to know whether work is waiting on a person: the state says where the issue sits in the pipeline, and the marker says who owes the next move.

## Per-stage commands

- **Triage → Backlog (or other dispositions)**: `/colinear triage` — batch-review the queue and apply on user confirmation.
- **Deferred review**: `/colinear defer` — batch-review Deferred issues and apply confirmed decisions.
- **Backlog → Ready**: `/colinear ready` to discover newly unblocked items, then `/colinear ready ABC-N` to gate one through.
- **Ready → In Progress**: `/colinear start` to see the queue, then `/colinear start ABC-N` to start work in an isolated worktree.
- **A UI issue that wants a design reference first**: `/colinear design ABC-N` drafts the Claude Design brief (human-gated, and invoked directly — nothing routes an issue to it).
- **An issue whose direction is not settled**: `/colinear refine ABC-N` reviews it adversarially and returns the review in chat; add `--post` to file it as one advisory comment (human-gated).
- **In Progress**: implementation work — done by a human, typically inside the worktree `/colinear start ABC-N` set up. Run the pipeline commands as you go; there is no autonomous driver. When you run the test suite during delegated work, record it: `colinear review record-test --issue ABC-N --command '<cmd>' --passed N --failed N`. Re-run after fixes — the report keeps the latest run per command.
- **Open PR → reviewer handoff**: `/colinear handback` — hand an issue with an open `ABC-N` PR back to the reviewer; run from the issue's own branch it needs no operand. It ensures the issue is in the configured review state, moving it there unless an automation already did, attaches the attention marker either way, and verifies both; it covers the delegated and the non-delegated return alike, selecting the path from whichever marker the issue carries under the configured `queue_ready` and `delegated` roles.
- **In Review → Done**: `/colinear ship ABC-N` — human-gated; never auto-invoke.

## Bulk filing

To file a whole reviewed package of issues (bodies, projects, estimates, labels, blocked-by relations) from a YAML manifest, use `colinear issue bulk-create FILE`; run `colinear issue bulk-create --template` for the manifest format.
