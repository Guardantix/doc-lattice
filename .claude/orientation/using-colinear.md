---
name: using-colinear
description: Auto-loaded orientation for Linear workflows with colinear. Injected at session start by the consuming project. Do not invoke directly.
---

# Using colinear

Linear work for this repo flows through one named pipeline.
Three named commands cover it.
`/linear-promote` and `/linear-show` are direct-invocation routers — each loads exactly one `modes/` file per invocation and never fires on its own.
`/linear-finalize` has no modes and is the one skill that auto-invokes, on an open `ABC-N` PR.
For ad-hoc reads/writes outside a workflow, run the `colinear` CLI with `--help`.

This orientation matches colinear 0.61.x — verify against the `version:` line in `doctor` output; on a major/minor mismatch STOP and tell the user to re-run `colinear orientation enable`.
If `colinear` is not found at all, run `./install.sh` from the colinear repo checkout — that is the version-skew recovery path and does not depend on the new binary.

## The pipeline

```
Triage ─→ Backlog ─→ Ready ─→ In Progress ─→ In Review ─→ Done
   │         │          │          │              │          │
/linear-  /linear-   /linear-  human work    /linear-   /linear-
promote   show       show                    finalize   promote
--triage  --next     --ready                             --ship
          then       then                                (human-
          /linear-   /linear-                            gated)
          promote    promote
          --ready    --delegate
```

## Per-stage commands

- **Triage → Backlog (or other dispositions)**: `/linear-promote --triage` — batch-review the queue and apply on user confirmation.
- **Deferred review**: `/linear-promote --deferred` — batch-review Deferred issues and apply confirmed decisions.
- **Backlog → Ready**: `/linear-show --next` to discover newly unblocked items, then `/linear-promote --ready ABC-N` to gate one through.
- **Ready → In Progress**: `/linear-show --ready` to see the queue, then `/linear-promote --delegate ABC-N` to start work in an isolated worktree.
- **UI issues routed design-first**: `/linear-promote --design ABC-N` drafts the Claude Design brief (human-gated).
- **An issue whose direction is not settled**: `/linear-promote --refine ABC-N` reviews it adversarially and returns the review in chat; add `--post` to file it as one advisory comment (human-gated).
- **In Progress**: implementation work — done by a human, typically inside the worktree `--delegate` set up. Run the pipeline commands as you go; there is no autonomous driver. When you run the test suite during delegated work, record it: `colinear review record-test --issue ABC-N --command '<cmd>' --passed N --failed N`. Re-run after fixes — the report keeps the latest run per command.
- **Open PR → reviewer handoff**: `/linear-finalize` — hand an issue with an open `ABC-N` PR back to the reviewer, including the non-delegated `ai:ready` path.
- **In Review → Done**: `/linear-promote --ship` — human-gated; never auto-invoke.

## Bulk filing

To file a whole reviewed package of issues (bodies, projects, estimates, labels, blocked-by relations) from a YAML manifest, use `colinear issue bulk-create FILE`; run `colinear issue bulk-create --template` for the manifest format.
