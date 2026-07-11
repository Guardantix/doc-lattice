# doc-lattice Linear Slice: Design Spec

**Date:** 2026-06-27
**Status:** Design (post brainstorm). Ready for implementation planning.
**Scope:** The first network-touching slice. Resolve referenced Linear tickets to live status
and surface shipped-against-stale-spec drift. No mutations, no committed status, no LLM.
**Source decision record:** `~/.claude/LCARS/decisions/2026-06-27-doc-lattice-doc-traceability.md`
**Builds on:** `docs/superpowers/specs/2026-06-27-doc-lattice-local-core-design.md` (the local core).

This spec turns the deferred `linear` item from the local-core deferral map (section 12) into a
buildable design. It does not re-open any locked decision from the decision record. Decision 6
(direct GraphQL, a standalone client copied from `gx-linear-skills`, no hard dependency on it, no
MCP) and decision 11 (the tool ships zero secrets; credentials live in the consuming repo's env)
are taken as given and realized precisely below.

## 1. Scope

In scope:

- A single `linear` command that resolves the `PC-*` tickets referenced in lattice frontmatter to
  live Linear status, then reports the dangerous intersection of doc drift and ticket progress.
- The headline finding is shipped-against-stale-spec: a downstream doc carries a STALE edge while a
  ticket that implements it is already Done. That is code built against a spec that has since moved.
- Two trigger modes over one shared analysis: a standing audit of current drift, and a
  forward-looking `--from <id>` blast-radius for a change not yet made.
- Findings graded DANGER / WARNING / INFO by the implementing ticket's workflow state.

Explicitly out of scope, deferred to a later spec or enhancement (see section 13):

- Any Linear mutation. This slice only reads.
- Persisting ticket status into frontmatter or any committed file. Status is live and volatile;
  it is never written back.
- The full Linear relation graph (blocks, blocked-by, related). This slice pulls parent and
  child links only, for context.
- `init` scaffolding, pre-commit, and CI codegen (already deferred by the local-core spec).
- Any gitignored status cache. One batched query per run is cheap at this corpus size.

## 2. The determinism boundary

doc-lattice's identity is deterministic and offline. This slice introduces network and
credentials for the first time, so the boundary is drawn sharply and stated as an invariant:

- `linear` is the only command that touches the network. `check`, `impact`, `graph`, and
  `reconcile` stay byte-for-byte offline and deterministic, and are not modified by this slice.
- `linear` reuses the pure layers it needs rather than changing them. It calls `check`'s edge
  classifier to find STALE edges and `impact`'s pure walk for `--from`, and adds no behavior to
  either module.
- The analysis that produces findings is pure and network-free. Only fetching the ticket status
  map is impure. This is the same pure-core, impure-edge discipline the local core already uses.

## 3. Operating model and command surface

```
doc-lattice linear [TARGET]          # whole-lattice audit; optional id narrows to one subtree
doc-lattice linear --from <id>       # forward-looking: impact-walk from a doc about to change
doc-lattice linear ... --json        # machine-readable findings, consistent with check and impact
doc-lattice linear ... --exit-code   # opt-in gate: any DANGER or BLOCKED finding exits 1
doc-lattice linear ... --warn-exit   # with --exit-code, WARNING findings also exit 1
doc-lattice linear ... --config PATH # same config override as the other commands
```

The two modes differ only in which downstream nodes are eligible to produce findings. The grading
and rendering are identical.

- **Audit (default).** The trigger set is every node that currently carries a STALE edge, as
  classified by `check`. This answers "what is dangerous right now." An optional positional
  `TARGET` narrows the trigger set to the currently-STALE nodes that are `TARGET` itself or fall in
  its impact set, for a focused look at drift at or downstream of one id. Scoping to a node still
  audits that node's own shipped tickets, so a gate narrowed to a drifted doc cannot pass while that
  doc itself ships a stale ticket.
- **Forward-looking (`--from <id>`).** The trigger set is the impact set of `<id>` computed by
  `impact`'s pure walk, regardless of current stale state. This answers "if I change `<id>`, which
  shipped or in-flight tickets does that endanger." It runs before the edit, so the edges are not
  STALE yet; membership in the impact set is the trigger.

`--from` and a positional `TARGET` are mutually exclusive; supplying both is a usage error.

## 4. Data model

Four immutable types, all network-free once built.

- `TicketState(name: str, type: LinearStateType)`. The Linear workflow state. `type` is one of the
  seven Linear state categories and drives grading; `name` is the display label (for example "In
  Review"). Linear exposes `duplicate` as its own state category (distinct from `canceled`), so it is
  enumerated here and graded as an omitted terminal state.
- `TicketRef(identifier: str, title: str | None, state: TicketState)`. A lightweight reference used
  for a ticket's parent and children, so context can be shown without a second fetch.
- `Ticket(identifier, title, url, state: TicketState, parent: TicketRef | None,
  children: tuple[TicketRef, ...])`. One resolved Linear issue.
- `Finding(severity: Severity, node_id, node_title, node_path, drifted_refs: tuple[str, ...],
  ticket_ref: str, reason: BlockedReason | None, ticket: Ticket | None)`. One reportable result.
  `ticket_ref` is the raw identifier as written in frontmatter and is always present. For a graded
  finding (DANGER, WARNING, INFO), `ticket` is the resolved issue and `reason` is `None`. For a
  `BLOCKED` finding, `ticket` is `None` and `reason` says why the ref could not be resolved. The two
  fields are mutually exclusive, so the model can represent exactly the `--json` shape in section 4.1
  and nothing illegal.

`LinearStateType = Literal["triage", "backlog", "unstarted", "started", "completed", "canceled", "duplicate"]`,
`Severity = Literal["DANGER", "WARNING", "INFO", "BLOCKED"]`, and
`BlockedReason = Literal["malformed", "not-found", "cross-team"]` are added to `constants.py` with the
existing `Literal` plus `get_args()` plus `frozenset` pattern, and imported wherever those values are
used.

`Ticket` types live in a new `tickets.py`, kept separate from `model.py` so the lattice graph types
stay free of any network-derived domain.

### 4.1 The `--json` payload

`--json` emits one object, shaped inline as `check` and `impact` already shape theirs:

```json
{
  "findings": [
    {
      "severity": "DANGER",
      "node_id": "pc-design",
      "node_title": "PC Design Tokens",
      "node_path": "docs/pc-design.md",
      "drifted_refs": ["art-direction#accent-color"],
      "ticket_ref": "PC-228",
      "ticket": {
        "identifier": "PC-228",
        "title": "Implement accent tokens",
        "url": "https://linear.app/acme/issue/PC-228",
        "state": {"name": "Done", "type": "completed"},
        "parent": null,
        "children": [
          {"identifier": "PC-261", "title": "Tune motion", "state": {"name": "In Progress", "type": "started"}}
        ]
      }
    },
    {
      "severity": "BLOCKED",
      "node_id": "pc-design",
      "node_title": "PC Design Tokens",
      "node_path": "docs/pc-design.md",
      "drifted_refs": ["art-direction#motion"],
      "ticket_ref": "PC-999",
      "reason": "not-found",
      "ticket": null
    }
  ]
}
```

`findings` is the only key; it is ordered as in section 5.1. A BLOCKED finding carries the offending
`ticket_ref`, a `null` ticket, and a `reason` of `"malformed"` (rejected by the shape check),
`"cross-team"` (a `linear_team` is set and the ref belongs to another team, so it is never queried,
section 7), or `"not-found"` (queried, but absent from the filtered result). There are no separate top-level
lists: the command examines only the tickets on trigger nodes, so an unresolvable ref is always
either irrelevant (its node is not in the trigger map, so it is never collected) or a BLOCKED finding
(its node is). Under `--exit-code`, DANGER and BLOCKED findings drive exit 1.

## 5. The stale-shipped join

`stale_shipped.py` is pure. Its entry point takes the built `Lattice`, a trigger map
`Mapping[node_id, tuple[str, ...]]` (each downstream node id mapped to the upstream refs that justify
looking at it), a `Mapping[identifier, Ticket]`, and a `rejected` map `Mapping[ref, BlockedReason]`
of the refs the pure partitioner refused to query (each tagged `"malformed"` or `"cross-team"`). It
returns the graded findings (DANGER, BLOCKED, WARNING, INFO) and nothing else; there are no separate
report-only lists, because only trigger-node tickets are ever in scope. It performs no I/O and no
network, so it is unit-tested against synthetic trigger maps and ticket maps. For each trigger node,
each of its `tickets:` refs is resolved: a hit in the ticket map grades by state; otherwise it is
BLOCKED, with `reason` taken from the `rejected` map when present (`"malformed"` or `"cross-team"`)
and `"not-found"` when the ref was queried but absent from the filtered result. A node that lists the
same ref twice yields one finding, not two.

### 5.1 Finding identity and grading

A finding is keyed on the pair `(downstream_node_id, ticket_identifier)`, not on the edge. The
node's justifying refs are carried whole in `drifted_refs`; a node with several of them does not fan
out into one finding per edge. The severity comes from the implementing ticket's own state type:

| Ticket state / ref outcome | Severity | Meaning |
|---|---|---|
| `completed` | DANGER | Shipped work built against a spec that has since drifted. |
| malformed, cross-team, or not-found (absent from the filtered result) | BLOCKED | A ticket ref on a drifted node cannot be resolved, so whether shipped work is endangered is undeterminable. The gate fails closed on it. |
| `started` | WARNING | In-flight work (In Progress or In Review) against a spec that just drifted. |
| `unstarted`, `backlog` | INFO | Not started; the worker will pick up the current spec. |
| `canceled`, `triage`, `duplicate` | omitted | Not a real risk; produces no finding. |

Consequences worth stating: a node in the trigger map with no tickets produces nothing, because no
shipped work is endangered. A node whose tickets are all canceled produces nothing. In audit mode a
node with only OK edges never enters the trigger map at all, so the healthy case produces nothing. A
completed ticket's still-open children are attached to its DANGER finding as context, since a "done"
parent with open children is itself a signal.

The gate fails closed. A ticket ref on a trigger node that the partitioner refused to query
(malformed shape, or cross-team when a `linear_team` is set) or that was queried but absent from the
filtered result becomes a **BLOCKED** finding carrying the node id, path, the offending `ticket_ref`,
and a `reason`,
because `tickets:` is repo-controlled and a drifted doc must not be able to silence its own DANGER by
swapping a completed ticket for a typo, a deleted id, or another team's id. An unresolvable ref on a
node that is *not* a trigger node endangers no shipped work and is never collected in the first
place, so it simply does not appear. The gate fails closed exactly where drift and an unverifiable
ticket coincide, and stays quiet everywhere else.

Findings are returned in a deterministic order keyed on an explicit severity rank (DANGER 0, BLOCKED
1, WARNING 2, INFO 3), then node id, then `ticket_ref`. The rank is explicit, not the declaration
order of the `Severity` literal, which lists `BLOCKED` last; a sort must not key on the literal's
order. This keeps the human table and the `--json` payload stable and directly assertable in tests.

### 5.2 How each mode builds the trigger map

The join is one function; only the trigger map handed to it differs, and each mode also fixes what
`drifted_refs` means:

- **Audit.** Call `check.check_lattice(lattice)`, keep the `EdgeStatus`es whose state is `STALE`,
  and group them by `source_id`. Each downstream node maps to the `target_ref`s of its STALE edges:
  the drift that has already happened. A positional `TARGET` further intersects this with `TARGET`
  itself together with its impact set (`expand_targets` file ids unioned with `impact`'s dependents),
  so the named node is audited rather than excluded.
- **`--from <id>`.** Take the downstream nodes from `impact.impact(lattice, <id>)`. Each such node
  maps to the `target_ref`s of its own edges whose resolved `target_id` lies in the *transitive*
  impacted-id closure, the drift that would propagate if `<id>` changed. The closure is not just
  `expand_targets(<id>)`: a two-hop dependent's edges point at an intermediate node, not at `<id>`'s
  own ids, so keying on `expand_targets` alone would leave transitive dependents with empty
  `drifted_refs`. `stale_shipped` reconstructs the closure purely as `expand_targets(lattice, <id>)`
  unioned with every affected node's id and the anchors of its file (the same set `impact`'s walk
  visits), so `impact` itself is not modified. An unknown `<id>` raises `ValidationError` from
  `impact`, surfaced as exit 2, so a typo is reported rather than returning a silently empty result.

## 6. Fetching ticket status

`linear_fetch.py` is the thin impure wiring that turns the identifiers named across the trigger
nodes into the ticket map. It deduplicates identifiers first, so a ticket referenced by several docs
is fetched once. When no valid identifier remains to resolve (the lattice references no tickets, or
`--from` reaches no ticketed downstream node), it returns an empty map without reading
`LINEAR_API_KEY` or touching the network. A no-ticket run therefore succeeds with no findings and
needs no credential.

`linear_query.py` is pure. It first partitions identifiers into a valid set and a `rejected` map
`Mapping[ref, BlockedReason]`: a ref failing the shape check is tagged `"malformed"`, and, when a
`linear_team` is configured, a well-formed ref whose team prefix is not the configured one is tagged
`"cross-team"` (section 7). Only the valid set reaches the wire, so what is queried is decided in
pure, unit-tested code; both rejected kinds are returned for the join to turn into BLOCKED findings
and are never queried, which also keeps an off-team identifier from leaking metadata into output. For
the valid set it builds GraphQL `query` documents over the `issues(filter:)` connection rather than
the root `issue(id:)` lookup, because the root `issue(id:)` field is non-null (`Issue!`): a missing,
deleted, or typo'd id resolves to a top-level `errors` array with `data: null` that, by GraphQL
non-null propagation, nulls the whole batch and would crash the audit on a single bad ref. The
`issues(filter:)` connection instead returns whatever matches and an empty node list for no match,
never an error, so absence is a normal result. Identifiers are grouped by team key (the segment
before the last `-`); each group queries `issues(filter: { team: { key: { eq: $team } }, number: {
in: $numbers } }, first: <batch>)` for that team's numbers, sharing one fragment for the ticket
fields including `parent` and `children(first: 50)`. The child cap is deliberate and not paginated in
this slice; children are context, not a gate. A group's numbers larger than a fixed batch size (for
example 50) are chunked into several documents whose results are merged. A total distinct-identifier
cap bounds the whole run, so a malicious doc set listing thousands of `tickets:` cannot drive
unbounded outbound requests against Linear (section 9); exceeding it is a `LinearError`, not a silent
flood. The team key and numbers are passed as GraphQL variables, never interpolated into the document
text. The builder records the team key for each document so results can be keyed back to the exact
identifier that was queried.

`linear_client.py` is the transport, copied in shape from the proven `gx-linear-skills` client but
hardened (see section 9): a synchronous `urllib.request` POST to the hardcoded
`https://api.linear.app/graphql`, the API key read lazily from `LINEAR_API_KEY` on each request, a
bounded timeout, and a capped read so an oversized response cannot exhaust memory. Two deliberate
deviations from the precedent close network attack surface. First, the scheme guard accepts
`https://` only and rejects `http://`, because the key rides in the `Authorization` header and must
never cross the wire in cleartext. Second, the request runs through an opener that does not follow
redirects: `urllib.request.urlopen` follows 3xx by default, which would let a hostile or
intercepted response steer the credentialed client at an internal address; a redirect is instead
turned into a `LinearError`. TLS certificate and hostname verification are left at the secure stdlib
default (no unverified `ssl` context is ever constructed). HTTP and URL errors map to `LinearError`,
a 429 included: this slice issues a single batched request per chunk and deliberately does not retry
or back off (a local single-user tool), so a rate limit or transport failure surfaces as a clear
exit-2 `LinearError` telling the user to retry rather than as hidden retry machinery. If identifiers
are chunked and any chunk fails, the whole command fails; it never reports a silently partial audit.
The transport returns the raw response text and interprets none of it.

`linear_parser.py` is the boundary. It is the only new module permitted `Any` and `cast`, because
its name ends in `_parser` and `scripts/check_typing_boundaries.py` allows the untyped-to-typed
conversion there. It parses the JSON, rejects a GraphQL `errors` array or a missing `data` object as
a `LinearError`, and validates each issue node into a typed `Ticket`. Because Linear's schema is not
ours to pin, ticket models validate the fields we query and ignore unknown fields (pydantic
`extra="ignore"`), unlike our own frontmatter models, which forbid extras. The ticket map is keyed
by the identifier that was *queried*, reconstructed as `{team}-{number}` from the validated team key
for that document and each node's own `number`, never by the `identifier` the response echoes, so a
case or formatting difference in the echo cannot cause a lookup miss that a downstream node would
then read as a false BLOCKED. Because the query filtered on that team key and those numbers, every
returned node belongs to the team and carries a queried number, so the reconstructed key always
matches a queried id. Every string field from the response, not only titles and state names but also
identifiers and urls, is stripped of control characters (C0, C1, and DEL) here, at the boundary, so a
hostile response cannot smuggle terminal escape sequences downstream of validation (section 9). A
missing ticket is simply absent from the filtered nodes: a queried identifier with no returned node
is the not-found case, derived downstream as a `tickets.get` miss rather than recorded here, so the
parser returns only the resolved ticket map and does not raise. A top-level `errors` array remains a
`LinearError`, now correctly reserved for genuine errors (auth, rate limit, malformed query) rather
than routine not-found. This mirrors the local core's decision that a BROKEN edge is a normal
reported state rather than a crash.

## 7. Configuration

No new config keys. `linear_team`, already parsed and forward-compat in the local core, becomes
active here:

```yaml
linear_team: PC          # optional; when set, confines queries to this team and fails closed off it
```

When `linear_team` is set it is a fail-closed boundary, not a soft note. An identifier whose team
prefix is not the configured one is **never queried**: the partitioner tags it `"cross-team"`, so it
cannot leak another team's ticket metadata into output, and on a trigger node it becomes a BLOCKED
finding with `reason="cross-team"` that fails `--exit-code`. This matters because `tickets:` is
repo-controlled and the command runs with a credential; silently dropping an off-team ref would let a
drifted doc dodge its own gate. When `linear_team` is null there is no team boundary to enforce, so
identifiers are validated only against the generic shape and queried as written. Because
`linear_team` comes from a repo-controlled `.doc-lattice.yml`, it is never interpolated into a
regex; it is itself validated against a fixed team-key shape `\A[A-Z][A-Z0-9]*\Z` when first used, and
the prefix match is done by splitting the identifier on `-` and comparing the segment by string
equality (section 9). This keeps a crafted `linear_team` from causing catastrophic backtracking or
from widening the allowlist. Credentials never live in config; `LINEAR_API_KEY` is read from the
environment, per decision 11.

## 8. Error handling

Extends the `ProjectError` hierarchy with one coded error, consistent with the local core and the
`gx-linear-skills` precedent:

- `LinearError` (code `LINEAR_ERROR`): a missing or empty `LINEAR_API_KEY`, an HTTP or network
  failure, a refused redirect or a non-`https` scheme, an exceeded identifier cap, a GraphQL
  `errors` array, or an unparseable or malformed response. Every message names the cause and the
  fix, and never includes the API key or the `Authorization` header.

Exit codes:

- 0: success, including when findings exist. The command is informational by default, so DANGER,
  WARNING, INFO, and BLOCKED findings all exit 0 unless `--exit-code` is passed.
- 1: only under `--exit-code`, when a DANGER or BLOCKED finding exists (or a WARNING finding too,
  under the additional `--warn-exit`). BLOCKED is the fail-closed case: a drifted node whose ticket
  ref cannot be resolved gates rather than passing silently (section 5.1).
- 2: a `LinearError` (missing credential, auth failure, network failure, rate limit, bad response),
  a `ValidationError` from an unknown `--from` or `TARGET` id, or any local load error already
  defined by the core (missing or invalid config, duplicate id, unreadable doc). A missing
  `LINEAR_API_KEY` is an actionable exit 2 only when there was actually a ticket to resolve; its
  message points at setting the key or at running `impact` for the offline raw-ticket view.

No bare `except Exception`. No `datetime.now()` outside `datetime_utils.py`.

## 9. Security

This is the first slice with a real security surface, so it carries the dedicated pass the roadmap
promised.

**Threat model.** Three inputs are untrusted. The frontmatter `tickets:` values and the
`.doc-lattice.yml` `linear_team` are repo-controlled: the same threat the local core already names,
where `check` runs in CI against potentially hostile repository contents, now extended because
`linear` can run with a credential present. The Linear API response is network-controlled: a hostile
or intercepted endpoint, or a redirect, is in scope. The asset is the `LINEAR_API_KEY` and the
internal network the credentialed process can reach.

- **No redirect following (CWE-918, SSRF).** `urllib.request.urlopen` follows 3xx redirects by
  default. A hostile or man-in-the-middle response could redirect the credentialed client to an
  internal address such as a cloud metadata service. The transport runs through an opener that
  refuses redirects (a `HTTPRedirectHandler` whose `redirect_request` returns `None`, or an
  `OpenerDirector` built without the redirect handler), turning any 3xx into a `LinearError`. This
  is the OWASP-prescribed SSRF defense for an HTTP client. The endpoint is also a hardcoded
  constant, never built from input.
- **HTTPS only, no cleartext credential (CWE-319).** The scheme guard accepts `https://` and rejects
  `http://`, deviating from the copied precedent, because the key travels in the `Authorization`
  header. With redirects disabled there is no downgrade path either. TLS certificate and hostname
  verification stay at the secure stdlib default (PEP 476); no unverified `ssl` context is ever
  constructed.
- **Credentials (CWE-200, CWE-532).** `LINEAR_API_KEY` is read from the environment only, lazily, per
  request. It is never sourced from config, CLI arguments, or files, and never logged, echoed, or
  placed in any error message, `--json` output, or surfaced Linear error body. The key stays a local
  in the transport and is never put into an exception's arguments. Surfaced Linear error bodies are
  length-capped and escaped before printing.
- **GraphQL injection (CWE-943).** Ticket identifiers travel as GraphQL variables, never interpolated
  into the document; aliases are index-derived (`i0`, `i1`), never from input. A repo-controlled
  `tickets:` value cannot change query structure.
- **Identifier validation (CWE-20, CWE-176).** Each identifier is validated in the pure
  `linear_query` partitioner against a fixed literal regex using explicit ASCII classes
  (`^[A-Z][A-Z0-9]*-[0-9]+$`, compiled with `re.ASCII` so Unicode digits and letters cannot slip
  through), matched as written with no case normalization, before any document is built. A malformed
  value is returned for reporting and never put on the wire, so the decision of what reaches Linear
  is pure and unit-tested.
- **`linear_team` not interpolated into a regex (CWE-1333, ReDoS).** The repo-controlled team key is
  never spliced into a pattern; it is validated against `^[A-Z][A-Z0-9]*$` and the prefix is matched
  by splitting the identifier on `-` and comparing for equality, so a crafted value cannot cause
  catastrophic backtracking or widen the allowlist.
- **Team isolation is fail-closed (CWE-285, improper authorization scope).** When `linear_team` is
  set, an off-team identifier is never queried, so repo-controlled `tickets:` cannot use the
  credential to pull another team's ticket metadata, and on a trigger node it surfaces as a
  `cross-team` BLOCKED finding that fails `--exit-code` rather than silently dropping out of the gate
  (section 7).
- **Bounded outbound fan-out (CWE-400, CWE-770).** A total distinct-identifier cap bounds each run,
  so a doc set listing thousands of `tickets:` cannot drive unbounded requests or burn the user's
  rate limit; exceeding it is a `LinearError`. The per-response read is byte-capped and
  `children(first: 50)` is unpaginated, both deliberate resource bounds. No retry or backoff is added
  this slice, so there is no amplification on a 429.
- **Unsafe deserialization avoided (CWE-502).** The response is parsed with `json.loads` (no code
  execution) and validated by pydantic (`extra="ignore"`, since the schema is Linear's, not ours).
  No new YAML parsing is added; `linear_team` arrives through the local core's `YAML(typ="safe")`
  config load.
- **Terminal injection (CWE-150).** rich's `escape()` neutralizes `[...]` markup but not raw control
  bytes, so escaping alone is insufficient for either network or repo-controlled strings. Defense is
  two-layered and covers every emitted field. At the boundary, the parser strips control
  characters (C0, C1, and DEL) from all Linear response strings (titles, state names, identifiers,
  urls). At
  rendering, `linear_render` routes every external string it emits, the Linear fields and the
  repo-derived node ids, refs, and paths alike, through one shared render-safe helper that both
  strips control characters and applies `escape()`. So no field, network or repo, reaches the
  terminal with raw control bytes. Note that the existing `check`, `impact`, and `graph` commands
  escape these repo-derived strings but do not yet strip control characters from them; that is a
  pre-existing local-core exposure, not introduced here, and retrofitting those commands is a
  separate hardening left outside this slice's determinism boundary (section 2).
- **Zero secrets in the repo.** Every test mocks the transport: no real network, no real key, only
  synthetic ticket JSON. No fixture, config, or CI file carries a token. The public repo's own CI
  does not run `linear`; `check` remains its CI gate. The pure layers (`stale_shipped`, `tickets`,
  `linear_query`, `linear_render`) touch neither network nor secrets.

**Deployment note (CI trust boundary).** A consuming repo that wires `linear --exit-code` into CI
must run it only on trusted refs, never on fork-pull-request workflows, because the gate needs
`LINEAR_API_KEY` in the environment while processing repo-controlled `tickets:` and `linear_team`.
Exposing the secret to untrusted PR code is a CI misconfiguration the tool cannot prevent; the
`init` scaffolding (a later spec) will generate a workflow that respects this boundary.

No new path input enters this slice, so the local core's `safe_resolve()` containment is unchanged
and there is no added traversal surface. There is no SQL, no browser or web surface (so no XSS, CORS,
or CSP concern), and no secret-dependent comparison (the `seen` hash compare is drift detection, not
authentication, so timing is not a concern).

## 10. Testing

Test-driven, per project conventions, tests mirroring sources one to one, all offline.

- **Pure unit tests.**
  - `test_stale_shipped.py`: a parametrized case per Linear state type asserts its bucket
    (`completed` DANGER, `started` WARNING, `unstarted` and `backlog` INFO, `canceled` and `triage`
    omitted), so no state can be silently mis-graded; one finding per `(node, ticket_ref)`, with a
    ref listed twice on a node collapsing to one finding; `drifted_refs` aggregation across multiple
    stale edges on a node; a completed ticket's open children attached as context; a trigger-node
    ref becomes a BLOCKED finding with `reason="malformed"` when in the rejected map as malformed,
    `reason="cross-team"` when rejected as cross-team, and `reason="not-found"` when queried but
    absent from the ticket map, while a ref whose node is outside the trigger map produces nothing; a
    two-hop `--from` dependent receives non-empty `drifted_refs` from the
    transitive closure, not an empty tuple (the regression test for the closure fix); the explicit
    severity rank orders DANGER, then BLOCKED, then WARNING, then INFO, with node id and `ticket_ref`
    tie-breaks; a node with no tickets yields nothing.
  - `test_linear_query.py`: the document is a `query` and never a `mutation`; the partition returns
    the valid set plus a `rejected` map tagging malformed refs (including an empty or whitespace
    `tickets:` entry, and a non-ASCII-digit value under the `re.ASCII` guard) `"malformed"` and, when
    `linear_team` is set, off-team refs `"cross-team"`, with neither reaching the document; with no
    `linear_team`, an off-team-looking ref is queried, not rejected; a crafted `linear_team` is
    validated by shape and matched by split-and-equality, never spliced into a regex; the
    total-identifier cap raises `LinearError` at one over the cap but not at exactly the cap; chunking
    emits one document at exactly the batch size and two at batch size plus one (the off-by-one
    boundary); identifiers passed as variables, not interpolated; aliases unique; the
    alias-to-identifier mapping is returned and round-trips; dedupe.
  - `test_tickets.py`: model construction; a graded `Finding` has a resolved ticket and `reason
    None` while a BLOCKED one has `ticket None` and a non-null `reason`; the `LinearStateType`,
    `Severity`, and `BlockedReason` literal enforcement.
  - `test_linear_render.py`: severity grouping in the human table including the BLOCKED bucket; the
    `--json` shape with a `null` ticket and a `reason` on a BLOCKED finding; a rich-markup title is
    escaped; a hypothesis property that the shared render-safe helper is idempotent and leaves no
    ASCII control byte in its output for any generated string (mirroring the existing hashing
    canonicalization properties); a worked case that control bytes in a Linear url or identifier and
    in a repo-derived ref, path, or node id are all stripped; the API key never appears in any output.
- **Boundary test.** `test_linear_parser.py`: a valid response yields typed `Ticket`s including
  parent and children; the ticket map is keyed by the queried identifier even when the response
  echoes a different-cased `identifier`, so no false BLOCKED; an unknown extra field is ignored, not
  rejected; control characters in a title, a url, and an identifier are stripped at the boundary; a
  GraphQL `errors` array raises `LinearError`; a missing `data` raises `LinearError`; a queried number
  absent from the filtered nodes is omitted from the ticket map without raising; a malformed response
  raises `LinearError`.
- **Fetch test (mocked `urlopen`, no real network).** `test_linear_fetch.py`: identifiers are
  deduplicated before the request; an empty identifier set returns an empty map, makes no request,
  and never reads `LINEAR_API_KEY`; a chunked fetch merges results across chunks and keys them by the
  queried identifier; a queried id absent from the response is omitted from the ticket map (surfaced
  downstream as not-found); a failing chunk aborts the whole fetch.
- **Transport test (mocked HTTP, no real network).** `test_linear_client.py`: the Authorization
  header carries the key; a POST with JSON content type; the HTTPS scheme guard rejects an `http://`
  URL; a 3xx redirect raises `LinearError` rather than being followed; the timeout is passed; the
  read is byte-capped; `HTTPError` (including 429) and `URLError` map to `LinearError`; the key never
  appears in any raised message; a missing `LINEAR_API_KEY` raises an actionable `LinearError`.
- **CLI tests (mocked fetch).** Assert content, not mere presence: the audit table for a known
  fixture contains the expected node id, severity label, and ticket identifier, and omits a node
  whose only ticket is canceled; the `--json` payload matches the exact finding objects; `--from`
  mode; the exit codes (0 by default even with findings, 1 on a DANGER finding under `--exit-code`,
  1 on a BLOCKED finding under `--exit-code` with no DANGER present, WARNING also under `--warn-exit`,
  2 on auth or network error); a malformed, unresolved, or (with `linear_team` set) cross-team ticket
  on a drifted node fails the gate, the cross-team case asserting the ref was never queried (the
  false-pass regression tests); a lattice with no tickets succeeds with no findings and without
  requiring `LINEAR_API_KEY`; an unknown `--from` id exits 2; the no-key actionable error when a
  ticket was present; `--from` and a positional target together as a usage error.
- `test_constants.py` and `test_error_types.py` gain the new members: the `VALID_LINEAR_STATE_TYPES`,
  `VALID_SEVERITIES`, and `VALID_BLOCKED_REASONS` frozensets match their literals, and `LinearError`
  carries code `LINEAR_ERROR`.
- Coverage at or above the existing 80 percent gate. `test_conventions.py` stays green: the new
  modules carry module docstrings and Google-style docstrings, use the constants pattern, and keep
  `Any` and `cast` confined to `linear_parser.py`.

Fixtures extend `tests/conftest.py` with a node whose STALE edge also carries `tickets:` (including
one drifted node that lists a malformed, an unresolved, and an off-team ticket ref, to exercise all
three BLOCKED reasons), plus a synthetic ticket-map fixture covering completed, started, unstarted,
and canceled states, a parent with open children, and an unresolved identifier. Zero secrets, zero
network.

## 11. Dependencies

None new. The transport is stdlib `urllib.request`, copied in shape from `gx-linear-skills`.
`pydantic`, `rich`, `typer`, and `ruamel.yaml` are already present. The network slice adds zero
third-party dependencies.

## 12. Module decomposition

| Module | Purpose | Pure? |
|---|---|---|
| `tickets.py` | `TicketState`, `TicketRef`, `Ticket`, `Finding` types | pure |
| `linear_query.py` | Partition identifiers, group by team, build the per-team `issues(filter:)` document and variables | pure |
| `linear_client.py` | Transport: stdlib POST, lazy key, scheme guard, timeout, capped read, error mapping | impure I/O |
| `linear_parser.py` | Boundary: JSON to typed `Ticket`, envelope and shape validation | boundary |
| `linear_fetch.py` | Thin wiring: dedupe, skip-when-empty, query, client, parser, into a ticket map | impure |
| `stale_shipped.py` | Pure join: lattice plus trigger map plus ticket map to graded findings and unresolved | pure |
| `linear_render.py` | Severity-grouped human table and the `--json` shape | pure |

Edits: `error_types.py` adds `LinearError`; `constants.py` adds `LinearStateType` and `Severity`;
`cli.py` adds the `linear` command. `model`, `check`, `impact`, `graph`, `reconcile`, and
`orchestrate` are unchanged.

## 13. Non-goals and deferral map

| Deferred item | Where it lands |
|---|---|
| Any Linear mutation | out of scope by design; this slice only reads |
| Persisting ticket status into frontmatter | out of scope by design; status is live, never committed |
| Full Linear relation graph (blocks, blocked-by, related) | later enhancement; this slice pulls parent and children only |
| `init` scaffolding, pre-commit and CI codegen | later spec (already deferred by the local core) |
| Gitignored status cache | not needed at this corpus size |
| Control-char strip in `check`, `impact`, `graph` output | pre-existing local-core exposure; separate hardening, outside this slice's boundary |
| Display-prefix lint | optional future enhancement |

## 14. Acceptance

| Goal | Solved by | Verifiable when |
|---|---|---|
| Surface shipped-against-stale-spec drift | the audit mode's DANGER findings | a STALE edge whose ticket is Done appears as a DANGER finding |
| Pre-edit blast-radius on shipped work | the `--from <id>` mode | `--from` on an upstream id lists downstream Done and in-flight tickets |
| Gate cannot be silenced by a bad ref | the fail-closed BLOCKED finding | a drifted node whose ticket ref is a typo or deleted id fails `--exit-code`, not passes |
| Keep the core deterministic | the determinism boundary | `check`, `impact`, `graph`, `reconcile` run offline and unchanged |
| Ship zero secrets safely | the security pass | no token in repo; missing key is an actionable exit 2, not a leak |
