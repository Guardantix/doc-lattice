# Linear not-found redesign (Finding 1)

Status: IMPLEMENTED. This redesign shipped in PR #3 (commit `fd5ba3f`, the linear slice), the
same change that added this document, so the not-found handling in
`docs/superpowers/specs/2026-06-27-doc-lattice-linear-design.md` sections 5 and 6 already
reflects it. The sections below are kept as the design rationale, not pending work. The shipped
code went slightly past this draft: one filtered request per `(team, number-chunk)` rather than a
single multi-alias document, plus `includeArchived: true` and leading-zero rejection in the
identifier shape. The `(team, list[int])` grouping is `group_by_team`; `chunk_identifiers` shipped
as `chunk_numbers`; `QueryPlan.team` replaces the planned `alias_to_id`. The Finding 1 regression
is `test_missing_node_absent_not_error` in `tests/test_linear_fetch.py`.

## Problem

The `linear` command resolves each referenced ticket with an aliased batch of root
`issue(id:)` lookups (`linear_query.build_query`). The parser (`linear_parser.parse_tickets`)
treats any top-level GraphQL `errors` array as fatal and treats a `null` alias as the
not-found case. That not-found path is unreachable, and one missing ticket aborts the whole
audit.

## Evidence (verified 2026-06-28 against the live Linear API)

A live probe of `query GetIssue($id: String!) { issue(id: $id) {...} }` with a well-formed
but non-existent id (`GUA-999999`) returned, over HTTP 200:

```json
{ "errors": [ { "message": "Entity not found: Issue", "path": ["issue"],
    "extensions": { "code": "INPUT_ERROR", "userPresentableMessage": "Could not find referenced Issue." } } ],
  "data": null }
```

Confirmed against the published schema (`packages/sdk/src/schema.graphql`):

- The root `issue(id: String!)` field returns a non-null `Issue!`. A missing id resolves to
  an error, and because the field is non-null the error propagates to `data`, which becomes
  `null` for the entire response.
- In the aliased batch (`i0: issue(...) i1: issue(...) ...`) every alias is a non-null root
  field, so one not-found id nulls the whole `data` object, discarding every other alias.

Consequences:

1. The documented "missing ticket comes back as a null alias" case never happens. Linear
   reports not-found through `errors`, not a null alias.
2. A single deleted, archived, or valid-format-but-typo'd id makes `parse_tickets` raise
   `LinearError`, so `doc-lattice linear` exits 2 and reports no findings. A drifted doc
   then dodges its own gate by crashing the audit rather than producing the intended
   fail-closed BLOCKED.

This also subsumes two related findings:

- Finding 5 (query complexity): a complexity rejection arrives as the same fatal `errors`
  array, so a large lattice cannot run at all.
- Finding 13 (the parser's `unresolved` set): it is the dead remnant of the unreachable
  null-alias path.

## Root cause

The design assumed `issue(id:)` is nullable (missing returns `null`). It is non-null, so
absence is an error, and GraphQL non-null error propagation makes that error batch-wide. Any
fix that keeps the non-null `issue(id:)` lookup inherits this, because a partial result is
impossible: the response is all-or-nothing per request.

## Redesign: filter by team and number instead of looking up by id

Look issues up through the `issues(filter:)` connection, which returns whatever matches and
is never an error for a missing id. An absent ticket is simply not in the returned nodes.

Linear has no filter on the composite identifier string, but it does filter on the parts.
Confirmed in the schema:

- `Query.issues(filter: IssueFilter, first: Int): IssueConnection`
- `IssueFilter.number: NumberComparator` and `NumberComparator.in: [Float!]`
- `IssueFilter.team: TeamFilter` and `TeamFilter.key: StringComparator` (`eq`, `in`)

Every identifier `TEAM-NUMBER` splits into a team key and an integer number. Group the valid
identifiers by team key and ask one filtered connection per team group:

```graphql
query Audit($t0: String!, $n0: [Float!]!) {
  g0: issues(filter: { team: { key: { eq: $t0 } }, number: { in: $n0 } }, first: 50) {
    nodes {
      identifier number title url
      state { name type }
      parent { identifier title state { name type } }
      children(first: 50) { nodes { identifier title state { name type } } }
    }
  }
}
```

Multiple team groups are aliased (`g0`, `g1`, ...) in one document. Each `issues` connection
returns an empty `nodes` list when nothing matches, never an error, so a not-found id can no
longer null the response. Group numbers are chunked at `BATCH_SIZE` per group (one issue per
number per team, so `first` equals the chunk length and no pagination is needed). When
`linear_team` is set there is exactly one group; when it is null, identifiers are grouped by
their own prefix.

### Not-found becomes absence

`parse_tickets` keys each returned node by the id it queried, reconstructed as
`f"{team_key}-{int(node['number'])}"` using the validated team key for that alias and the
node's own `number`. Because the query filtered on `team.key.eq` and `number.in`, every node
belongs to that team and carries a number we asked for, so the reconstructed key always
matches a queried id and the "key by queried id, never by the echoed identifier" invariant
(spec section 6) is preserved. A queried id with no returned node is the not-found case.

`stale_shipped` already turns a `tickets.get(ref)` miss into a BLOCKED `not-found` finding, so
not-found is handled with no extra bookkeeping. The parser returns `dict[str, Ticket]` only
and the `unresolved` set is removed (resolves Finding 13). A top-level `errors` array stays
fatal, which is now correct because routine not-found no longer travels that path.

## Module-by-module changes

- `linear_query.py`
  - `partition_identifiers`: unchanged (still validates shape and team prefix, still returns
    `rejected`).
  - New `group_by_team(valid) -> list[tuple[str, list[int]]]`: split each id on the last `-`,
    group numbers by team key, deterministic order.
  - `build_query(groups)`: emit one aliased `issues(filter:)` per `(team, number-chunk)`,
    team and numbers as variables (never interpolated, same rule as today). Return a plan
    that records, per alias, the team key (replacing `alias_to_id`).
  - `QueryPlan.variables` widens from `dict[str, str]` to `dict[str, str | list[int]]` (team
    string plus number list). `chunk_identifiers` is reused to chunk each group's numbers.
- `linear_client.py`
  - `execute(document, variables)` widens its `variables` type to the same union; it only
    `json.dumps`es it, so no logic changes.
- `linear_parser.py`
  - Parse `data[alias]["nodes"]` per alias; key by reconstructed `TEAM-number`; return
    `dict[str, Ticket]`. Drop the `unresolved` set. Keep `errors`/missing-`data` fatal and
    the per-node control-char stripping.
- `linear_fetch.py`
  - Build groups, chunk per group, build one document per request batch, merge node maps.
    The team boundary still means `LINEAR_API_KEY` is read only when a valid id remains.
- `stale_shipped.py`, `tickets.py`, `cli.py`: no change. Not-found still flows from the
  `tickets.get` miss.

## Spec changes

- Section 5 (query construction): replace the aliased `issue(id:)` batch with the per-team
  `issues(filter:)` connection; describe team grouping and per-group number chunking.
- Section 6 (parser): replace "a missing ticket comes back as a null alias" with "a queried
  id absent from the filtered nodes is the not-found case"; note the `errors` array is now
  fatal only for genuine errors; remove the `unresolved` set from the contract.
- Section 5.1 / the BLOCKED table: `not-found` now means "queried but absent from the filter
  result" rather than "Linear returned null".

## Test plan

- Query builder: emits `issues(filter:)`, numbers and team as variables (never interpolated),
  groups by team, chunks at `BATCH_SIZE`.
- Parser: parses connection nodes; keys by reconstructed `TEAM-number`, not the echoed
  identifier (case/format echo cannot cause a miss); a queried number absent from nodes does
  not appear in the result.
- Parser: a genuine `errors` array still raises `LinearError`; missing `data` still raises.
- Regression for Finding 1: a batch where one queried id is absent yields a BLOCKED
  `not-found` finding and the audit still completes (exit reflects findings, not a crash).
  This is the test that would have caught the original defect.
- Fetch: cross-team ids are still rejected before any request; with `linear_team` null,
  multiple team groups produce multiple aliased connections.

## Trade-offs and open questions

- With `linear_team` null and ids spanning many teams, the document carries one aliased
  connection per team. This is more fields per request but bounded by the number of distinct
  teams, and each connection is independent and empty-safe.
- `number` is a GraphQL `Float`; integer numbers serialize cleanly and `int(node["number"])`
  reconstructs the key. Worth a guard if Linear ever returns a non-integer number.
- Complexity (Finding 5) is reduced, not eliminated: `children(first: 50)` is still the
  dominant per-node cost, but the not-found landmine is gone and `BATCH_SIZE` / `first` can
  be tuned down safely now that a smaller batch no longer changes correctness.
- Effort: moderate, no new dependencies. Touches `linear_query`, `linear_parser`,
  `linear_fetch`, `linear_client` (type only), the spec, and the linear tests.
