---
name: using-colinear
description: Auto-loaded orientation for Linear workflows with colinear. Injected at session start by the consuming project. Do not invoke directly.
---

# Using colinear

This project has declared the **Delegate** stage of the colinear method.
What follows is that stage and the ones it builds on — not the whole command tree.
When the project declares a further stage, this document teaches more.

This orientation matches colinear 0.88.x — verify against the `version:` line in `doctor` output; on a major/minor mismatch STOP and tell the user to re-run `colinear orientation enable`.
If `colinear` is not found at all, run `./install.sh` from the colinear repo checkout — that is the version-skew recovery path and does not depend on the new binary.

Issues here are identified as `GTX-N`; read every `GTX-N` below as a placeholder for a real one.

## Your workspace nouns

Every name below is this project's own declaration, read from its configuration.
Read it here rather than assuming the names another project uses.

Workflow states:

- `ready` — `Ready`
- `backlog` — `Backlog`
- `triage` — `Triage`
- `deferred` — `Deferred`
- `canceled` — `Canceled`
- `duplicate` — `Duplicate`
- `in_progress` — `In Progress`
- `in_review` — `In Review`
- `done` — `Done`

Labels:

- `queue_ready` — `ai:ready`
- `parent` — `type:parent`
- `human_only` — `ai:human-only`
- `delegated` — `ai:delegated`
- `needs_human_review` — `ai:needs-human-review`
- `spike` — not declared
- `design` — not declared

## Observe

Reads and the queue. Nothing here writes to Linear; trust is earned by visibility first.

- **Check this project's credential, team, and declared roles** — `colinear doctor`
- **Read one issue, or list them** — `colinear issue get GTX-N`
- **See what blocks an issue and what it blocks** — `colinear relation list GTX-N`
- **Read an issue's discussion** — `colinear comment list GTX-N`
- **See the queue an agent would pick from** — `/colinear start`
  *Narrowed here.* This project declares no `label_prefixes.milestone`, so `queue ready` still runs and simply covers less ground.

## Batch

The first writes, and they arrive already disciplined: plan, review what the plan says, then apply it. Never mutate a queue issue by issue.

- **Batch-review the triage queue, then apply what you confirmed** — `/colinear triage`
  *Unguarded here.* This project declares no `labels.design`, `labels.spike`, so `triage plan` cannot tell that marker apart from a work-type label, and can refuse any issue carrying one. Declare it in `colinear.toml` if your workspace uses such a label.
- **Batch-review deferred work and apply the confirmed decisions** — `/colinear defer`
- **Discover work a just-closed blocker has newly unblocked** — `/colinear ready`
  *Narrowed here.* This project declares no `label_prefixes.milestone`, so `queue next` still runs and simply covers less ground.
- **File one issue** — `colinear issue create "<title>" <body-file>`
- **Review one issue adversarially before committing to its direction** — `/colinear refine GTX-N`
- **Get an advisory estimate for one issue** — `colinear advise estimate GTX-N --mode triage --files <N> --loc <N> --subsystems <N>`
  *Unguarded here.* This project declares no `label_prefixes.subsystem`, so `advise estimate` has no configured family to read that input from, and can refuse work the family would have backed. Declare it in `colinear.toml` if your workspace has one.

## Delegate

The ready contract and the handback contract turn on, and agents execute queue work end to end. An issue enters the queue only by passing the contract, and comes back only through the handback.

- **Gate one issue through the ready contract into the queue** — `/colinear ready GTX-N`
  *Unguarded here.* This project declares no `labels.design`, `labels.spike`, so `ready plan` cannot tell that marker apart from a work-type label, and can refuse any issue carrying one. Declare it in `colinear.toml` if your workspace uses such a label. This project declares no `label_prefixes.subsystem`, so `advise estimate` has no configured family to read that input from, and can refuse work the family would have backed. Declare it in `colinear.toml` if your workspace has one.
- **Check one issue against the ready contract without writing** — `colinear issue check --issue GTX-N`
  *Unguarded here.* This project declares no `labels.design`, `labels.spike`, so `issue check` cannot tell that marker apart from a work-type label, and can refuse any issue carrying one. Declare it in `colinear.toml` if your workspace uses such a label.
- **Hand one queued issue to an agent in an isolated worktree** — `/colinear start GTX-N`
- **Draft the design brief for one issue whose UI work needs it** — `/colinear design GTX-N`
- **Record a test run against the issue being worked** — `colinear review record-test --issue GTX-N --command '<cmd>' --passed <N> --failed <N>`
- **Hand delegated work back to its reviewer once a PR is open** — `/colinear handback`
- **Merge reviewed work and close the issue** — `/colinear ship GTX-N`
