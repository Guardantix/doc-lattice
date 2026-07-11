# doc-lattice Init Slice: Design Spec

**Date:** 2026-06-28
**Status:** Design (post brainstorm). Ready for implementation planning.
**Scope:** Onboarding ergonomics. One `init` command that scaffolds the project config and emits
pre-commit and CI codegen for an adopting repo. No network, no secrets, no LLM. The only new
disk-touching code is a single config write; the rest is printed.
**Builds on:** `docs/superpowers/specs/2026-06-27-doc-lattice-local-core-design.md` (the local
core) and `docs/superpowers/specs/2026-06-27-doc-lattice-linear-design.md` (the linear slice).

This spec turns the deferred `init` item from the local-core deferral map (section 12, "init
scaffolding, pre-commit and CI codegen") into a buildable design. It does not re-open any locked
decision from the local core. The local-core spec already states (section 7) that `init` will
scaffold `.doc-lattice.yml` later and that the tool never depends on `init` having run; this slice
realizes that promise without changing how config is read.

## 1. Scope

In scope:

- A single `init` command that bootstraps a repo adopting doc-lattice.
- It writes one file, `.doc-lattice.yml`, when absent, and prints two codegen artifacts: a
  pre-commit hook and a GitHub Actions workflow, each of which runs `doc-lattice check` as the
  drift gate.
- The config write is deterministic and templated, with two optional flags (`--docs-root`,
  repeatable, and `--linear-team`) that bake values into the generated file.
- The command is idempotent and re-runnable: with a config already present it leaves it untouched
  and still prints the codegen, so `init` doubles as a way to re-fetch the snippets.
- Shipping `init` as version 0.2.0: bumping `__version__` across the version locations and adding a
  `RELEASING.md` checklist that makes the matching `v0.2.0` release tag a non-optional, atomic part
  of the release (section 8), since the generated gates pin that tag.
- Updating `roadmap.md` as the closing step of the slice, so the PR is atomic and self-consistent:
  move `init` out of the roadmap's "Later spec" into a shipped entry pointing at this spec, and
  correct the now-stale `linear` entry (already merged in PR #3) to shipped in the same edit, so the
  roadmap reflects reality the moment this lands.

Explicitly out of scope, deferred or declined (see section 11):

- Merging into or editing an existing `.pre-commit-config.yaml` or CI workflow. The command prints
  those two artifacts; it never mutates a file the adopting repo owns.
- Interactive prompts, `--json` output, a `--force` overwrite, a `--config` write-target override,
  and non-GitHub CI providers.
- Automating the release tag. Cutting and pushing `v0.2.0` stays a human step; section 8 makes it an
  enforced, atomic part of shipping `init`, documented by the `RELEASING.md` checklist. The slice
  adds no tooling that creates or protects the tag automatically.

## 2. The mutation boundary

doc-lattice keeps mutation narrow and deliberate. Today `reconcile` is the only command that
writes, and it writes surgically. This slice adds the second writing command, and draws its
boundary just as sharply so the careful-mutation posture is preserved:

- `init` writes exactly one file, `.doc-lattice.yml`, and only when it does not already exist. It
  never overwrites it, never edits it, and never touches any other file on disk. The never-overwrite
  guarantee is enforced atomically by a no-overwrite create (section 5), not by a separate existence
  check, so there is no window in which a concurrently created config could be clobbered, and the
  same write is crash-safe so a partial config can never be left behind.
- The pre-commit and CI artifacts are codegen: printed to stdout for the user to place, never
  written. This is why the slice can scaffold a repo it knows nothing about without risking a file
  the adopter has hand-tuned.
- The spec's own wording splits the job this way: the config is "scaffolding" (written), the
  pre-commit and CI are "codegen" (printed). The command honors that split exactly.

## 3. Operating model and command surface

```
doc-lattice init                                # write .doc-lattice.yml (if absent), print codegen
doc-lattice init --docs-root design --docs-root lore   # repeatable; sets docs_roots
doc-lattice init --linear-team PC               # bakes linear_team into the written config
```

There is no `--json`, no `--force`, no `--config`, and no interactive prompt. The command's
behavior is fully determined by the two flags and whether a config already exists.

Streams are split so a captured run yields only the artifacts:

- stdout carries the two codegen snippets, each under a `#`-comment banner naming its destination
  file. Emitted with plain `typer.echo` (no rich markup) so the YAML pastes clean and stays valid.
- stderr carries narration: whether the config was written or skipped, where to paste each snippet,
  and the tag prerequisite from section 8.

So `doc-lattice init > setup.yml` captures exactly the two snippets and nothing else.

## 4. Architecture

The slice extends the existing pure/impure split rather than bending it.

**Pure layer, new module `scaffold.py`** (filesystem-free, beside `render.py` and `reconcile.py`).
It generates three strings from typed inputs and returns them as a frozen value:

```python
@dataclass(frozen=True, slots=True)
class Scaffold:
    config_text: str       # .doc-lattice.yml
    precommit_text: str    # the repo: local hook snippet
    ci_text: str           # the GitHub Actions workflow

def build_scaffold(
    docs_roots: tuple[str, ...], linear_team: str | None, rev: str
) -> Scaffold: ...
```

`rev` is injected by the caller, never read inside the module, so `scaffold` stays pure and is
tested against arbitrary revs. The repository URL is a single module constant
`DOC_LATTICE_REPO_URL = "https://github.com/Guardantix/doc-lattice"` (the SSH remote rewritten to
the https git form `uvx` needs), referenced by both snippet renderers so the literal is never
duplicated.

**Impure layer, `init` command in `cli.py`** (the only new disk-touching code). It computes
`rev = f"v{__version__}"`, builds the scaffold, writes the config through a crash-safe atomic
create helper (`_atomic_create`, see section 5), and prints the codegen. It follows the same
`ProjectError -> exit 2` handling every other command uses. It deliberately does not reuse
`reconcile`'s `_atomic_write`: that helper publishes with a path replace, which is correct for
overwriting a tracked doc but would clobber an existing config here. `_atomic_create` is its
create-only counterpart, publishing with a no-overwrite primitive instead of a replace.

This keeps `scaffold` testable with no I/O, exactly as `render` and `reconcile.reconcile` are.

## 5. Command behavior

1. Resolve inputs: `rev = f"v{__version__}"`; `docs_roots = tuple(--docs-root) or ("docs",)`;
   `linear_team` from the flag or `None`.
2. Validate the flag values, a pure, filesystem-free guard so `init` never writes a config
   guaranteed to fail loading:
   - Every `--docs-root` entry is rejected if it is absolute or contains a `..` segment, with the
     same message style `config._resolve_roots` uses. Full containment resolution still happens later
     when the config is loaded; this is only the early, obvious-case check.
   - Every flag value (`--docs-root` and `--linear-team`) is rejected if it is empty or contains a
     control character (newline, tab, and the rest of the C0 set). Control characters in a path
     segment or a team slug are always a mistake, and rejecting them up front keeps a stray newline
     from reshaping the generated YAML. Non-control special characters are not rejected; step 3
     serializes them safely.
3. Build the scaffold (pure). The dynamic values (`docs_roots` entries and `linear_team`) are
   emitted through the YAML serializer, never string-interpolated, so a value like `1.0`, `*ref`, a
   leading `#`, or an embedded `:` is quoted and typed correctly rather than silently misparsed,
   commented out, or rejected by the strict `Config` model. See section 4 and section 6.1.
4. Config write to `cwd/.doc-lattice.yml` through `_atomic_create`, which is both no-overwrite and
   crash-safe. It writes `scaffold.config_text` to a uniquely named temp file in the same directory,
   flushes and `os.fsync`s it so the bytes are durable, then publishes by `os.link`ing the temp onto
   the final path and unlinking the temp. `os.link` is atomic and fails with `FileExistsError` if
   the target already exists, so the final path only ever appears complete, never empty or partial:
   - If the link succeeds, the file was absent and is now fully written: note the write on stderr.
   - If the link raises `FileExistsError`, a config already exists: leave it untouched, remove the
     temp, note the skip on stderr.
   - On any other error the temp is removed before propagating, so a failed run leaves no final file
     and no litter, and a rerun starts clean. A crash between the temp write and the link leaves at
     most an orphaned temp (cleaned best-effort on the next run), never a corrupt config, because the
     final path is published atomically by the link.
5. Print the codegen to stdout: the pre-commit snippet then the CI snippet, each under a `#`-comment
   banner naming its destination file.
6. Print placement guidance and the tag caveat (section 8) to stderr.
7. Exit 0 on success. A `ProjectError` (for example an unwritable directory surfaced as a project
   error) exits 2, consistent with the other commands. `init` never reports drift, so it never
   exits 1.

## 6. The three artifacts

### 6.1 `.doc-lattice.yml` (written)

The active keys reflect the flags; the remaining optional keys appear as commented examples the
user can uncomment. With no flags:

```yaml
# doc-lattice configuration. See https://github.com/Guardantix/doc-lattice
docs_roots:
  - docs
# ignore_globs:
#   - "**/superpowers/plans/**"
# linear_team: my-team-slug
# binding_layers: null
```

`--docs-root design --docs-root lore` replaces the `docs_roots` list with `design` and `lore`.
`--linear-team PC` turns the commented `linear_team` line into an active `linear_team: PC`.

The active block (the `docs_roots` list, and `linear_team` when supplied) is rendered by dumping a
plain dict through the YAML serializer (`ruamel.yaml`, already a dependency), so every value is
quoted and typed by the library rather than by hand. The fixed scaffolding around it, the header
comment and the commented-out example keys, is static text; comments cannot affect parsing, so they
stay literal. This is what makes a hostile value (`--linear-team "1.0"`, a root containing `:` or a
leading `#`) come out as a correctly quoted scalar instead of malformed YAML.

The generated text must round-trip through the real `Config` pydantic model. A test loads the
generated file through `load_config` and asserts the resulting `Config`, so the template can never
drift from the schema (`extra="forbid"` would catch a stray key).

### 6.2 pre-commit hook (printed)

Added by the user under `repos:` in their `.pre-commit-config.yaml`:

```yaml
  - repo: local
    hooks:
      - id: doc-lattice-check
        name: doc-lattice check
        entry: uvx --python 3.14 --from git+https://github.com/Guardantix/doc-lattice@v0.2.0 doc-lattice check
        language: system
        files: \.md$
        pass_filenames: false
```

`language: system` runs the entry as given (it needs `uvx` on PATH, which the uv toolchain
provides). `--python 3.14` makes `uv` provision the interpreter doc-lattice requires
(`requires-python >= 3.14`), downloading it if absent, so the hook does not depend on the developer
already having 3.14. `files: \.md$` triggers the hook when markdown changes; `pass_filenames: false`
because `check` evaluates the whole lattice, not the individual changed files.

### 6.3 CI workflow (printed)

Saved by the user as `.github/workflows/doc-lattice.yml`:

```yaml
name: doc-lattice
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
jobs:
  check:
    name: Traceability check
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uvx --python 3.14 --from git+https://github.com/Guardantix/doc-lattice@v0.2.0 doc-lattice check
```

`--python 3.14` makes `uv` provision the interpreter doc-lattice requires
(`requires-python >= 3.14`), downloading it on the runner if absent, so the gate fails on real drift
rather than on a missing interpreter. `check` exits 0 on a clean lattice, 1 on drift, and 2 on a
tool error; the job fails on 1 or 2, which is exactly the gate this is meant to provide. Only
`check` runs. `linear` is deliberately excluded from the generated CI: it needs a token and network,
which are out of scope for an onboarding default.

## 7. Invocation model

Both snippets invoke doc-lattice through `uvx --python 3.14 --from git+<url>@<rev> doc-lattice
check`. This is zero-install: `uv` fetches and runs the pinned revision on demand, provisions the
required Python 3.14 (downloading it if absent), adds no dependency to the adopter's project, and
works identically in pre-commit and GitHub Actions. It fits because `uv` is already the toolchain
and there is no PyPI release to install from. Putting `--python 3.14` on the invocation rather than
in a separate setup step is what lets the same one-line command serve both the pre-commit hook
(which has no place for a setup step) and CI uniformly.

## 8. Release model

The snippets pin `@v{__version__}`. `init` ships as version 0.2.0, so they pin `@v0.2.0`. That tag
must contain the init commit, because it serves two roles at once: it is the revision the generated
gates resolve to run `check`, and it is the revision the onboarding docs tell adopters to run `init`
itself from (`uvx --from git+...@v0.2.0 doc-lattice init`). A tag pointing at an init-less commit
would satisfy the first role but break the second.

There is no chicken-and-egg here, because a git tag is created after the commit it names, and `init`
emits the pin as a literal string, not a resolved ref. The release is one linear sequence:

1. On the feature branch, set `__version__` to 0.2.0 across the version locations (`__init__.py`,
   `pyproject.toml`) and finish `init`.
2. Merge to main. The merge commit now carries `init`, `check`, and the logic that emits `@v0.2.0`.
3. Tag that merge commit `v0.2.0` and push the tag.

Step 3 points backward at a commit that already exists, so nothing in steps 1 and 2 ever waits on
the tag. Verification is decoupled the same way: unit tests assert the emitted string (`@v0.2.0`)
with no tag required, and the live `uvx` invocation is smoke-tested during development against an
already-resolvable ref (`@main` or the branch sha). The literal `@v0.2.0` gets its one real smoke
test right after the tag is pushed; if it is wrong, the fix is to cut `v0.2.1`.

The one real constraint is that the release is atomic: bumping the version, merging, and tagging the
merge commit are a single "cut the release" step. A half-done release (init merged but no tag, or a
tag without the version bump) is exactly what produces a broken gate. The slice therefore adds a
short `RELEASING.md` checklist that makes the tag a non-optional part of shipping `init` and of every
later version. Its closing step is the smoke test named above: after pushing `vX.Y.Z`, run the exact
generated command (`uvx --python 3.14 --from git+...@vX.Y.Z doc-lattice check`) once and confirm it
resolves before the release is considered done. The code stays version-agnostic: it reads
`__version__` and pins `v{__version__}`, so future versions need only the matching tag, cut the same
way.

The residual risk is operational, not in the code: a maintainer could still forget the checklist, or
tag the wrong commit, and adopters who copied a snippet pinned to a missing tag would see it fail
before `check` runs. Closing that hole with release automation that creates and verifies the tag in
CI (an enforced, machine-checked smoke test rather than a human checklist) is deliberately out of
scope for this slice. It is a release-tooling concern that applies to the whole project, not just
`init`, and it is recorded as a deferred follow-up (section 11). The `RELEASING.md` checklist with
its post-tag smoke test is the accepted mitigation until then.

## 9. Conventions and invariants

- `scaffold.py` is a pure typed module: no `typing.Any`, no `typing.cast` (it is not a `_parser`
  boundary module). The pre-commit and CI snippets are built from fixed templates; the config's
  dynamic values are emitted through `ruamel.yaml` (dumping to a string, no I/O), which needs no
  `Any` or `cast`, so the module stays inside the typing boundary.
- No user-controlled value reaches the generated config by string interpolation. Flag values are
  control-character-rejected at validation (section 5 step 2) and YAML-serialized at render
  (section 6.1), so a hostile scalar cannot deform the output.
- The repo URL is centralized as one constant and referenced from every renderer that needs it
  (both snippets and the config header); no duplicated literal.
- No `datetime` use: the artifacts carry no timestamps.
- Module docstring on `scaffold.py`; Google-style docstrings on `build_scaffold` and the `init`
  command. No em-dashes in docstrings, messages, or comments.
- Paths: `init` writes the fixed filename `.doc-lattice.yml` into the current working directory; it
  is not a user-provided path, so it does not need `safe_resolve`. The `--docs-root` values are not
  resolved against the filesystem at init time; section 5 step 2 guards them syntactically and
  section 6.1 serializes them safely into the config.

## 10. Testing

- `tests/test_scaffold.py` (pure, no I/O):
  - default `docs_roots` yields `docs` active with the optional keys commented;
  - `--docs-root` overrides produce the listed roots; `--linear-team` produces an active
    `linear_team` line;
  - `rev` is interpolated into both snippets, alongside the repo URL and the `--python 3.14` pin;
  - the generated `config_text` round-trips through the real `Config` model;
  - hostile scalar round-trips: values that are all-numeric (`1.0`), start with `#`, contain `:` or
    a YAML indicator character, are emitted so that `load_config` reads back exactly the input
    string, with no misparse, comment-out, or `extra="forbid"` failure.
- `tests/test_cli.py` (extend, using `tmp_path`):
  - `init` in an empty directory writes `.doc-lattice.yml` with the expected content and prints
    both snippets to stdout, exit 0;
  - `init` with a config already present does not change the file (exercising the `FileExistsError`
    skip branch of `_atomic_create`, asserted by writing distinct sentinel content first and
    confirming it survives byte-for-byte) but still prints both snippets, notes the skip on stderr,
    exit 0;
  - crash safety: a forced failure (for example monkeypatching `os.link` or `os.fsync` to raise)
    leaves no `.doc-lattice.yml` and no leftover temp behind, the command surfaces the error, and a
    subsequent clean `init` writes the config correctly;
  - `--docs-root` and `--linear-team` bake their values into the written file;
  - stdout carries both destination banners and the pinned rev, asserted as `f"v{__version__}"`
    (currently `@v0.2.0`) rather than a hardcoded literal, so the test tracks the version source;
  - an absolute or `..`-bearing `--docs-root`, and a flag value containing a control character, are
    each rejected before any write.
- `tests/test_conventions.py` stays green (new module obeys the typing-boundary and docstring
  rules). Coverage stays at or above the existing 80 percent gate.

## 11. Non-goals and deferral map

| Deferred or declined item | Disposition |
|---|---|
| Merging into an existing `.pre-commit-config.yaml` or CI workflow | declined; print, never edit a file the adopter owns |
| Interactive prompts | declined; non-deterministic, fights the offline/CI posture |
| `--json`, `--force`, `--config` write target | declined; YAGNI for an onboarding command |
| Non-GitHub CI providers | out of scope; GitHub Actions only, matching this repo |
| Automating and CI-verifying the release tag | deferred follow-up; a project-wide release-tooling concern. For this slice the tag is an enforced manual step with a `RELEASING.md` checklist whose closing step is a post-tag smoke test of the pinned ref (section 8) |
| Authority-ladder validation, display-prefix lint | still deferred by the local-core map; unrelated to init |

## 12. Acceptance

| Goal | Solved by | Verifiable when |
|---|---|---|
| Onboarding a repo | `init` writes a valid config and prints both gate snippets | a fresh directory gains a loadable `.doc-lattice.yml` and copy-paste pre-commit and CI in one command |
| Safe re-run | skip-and-continue on an existing config | re-running `init` never alters a hand-tuned config yet still prints the codegen |
| Reproducible gate | `uvx ... @v{__version__}` pinning | the generated pre-commit and CI both pin the same version that generated them |
