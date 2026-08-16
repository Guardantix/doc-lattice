# Security policy

## Supported versions

Only the latest released version of doc-lattice receives security fixes. Fixes ship as a new
release rather than as a patch to an older line, so an adopter on an older version upgrades to
pick one up. [CHANGELOG.md](CHANGELOG.md) records what each release changed, and
[RELEASING.md](RELEASING.md) owns how a fix reaches PyPI and what happens when a released
version turns out to be bad.

| Version | Supported |
|---------|-----------|
| Latest release | Yes |
| Every earlier version | No |

## Reporting a vulnerability

Report privately through GitHub's private vulnerability reporting:

**<https://github.com/Guardantix/doc-lattice/security/advisories/new>**

The same form is reachable from the repository's **Security** tab under **Report a
vulnerability**. The report is visible only to the maintainers until an advisory is published.

Do not open a public issue, pull request, or discussion for a suspected vulnerability, and do
not include a working exploit in any public channel. If private reporting is unavailable to you
for any reason, open a public issue that says only that you have a security report and asks for
a private channel, with no technical detail.

### What to include

A report is actionable when it carries enough to reproduce the behavior:

* The version, from `doc-lattice --version`, and whether it came from PyPI or a git ref.
* Python version and operating system.
* A minimal reproduction: the smallest `.doc-lattice.yml` and set of Markdown documents that
  show the behavior, plus the exact command line.
* What you expected to happen and what happened instead.
* Your assessment of the impact, including what an attacker would need to control to reach it.

### What to expect

* An acknowledgement within 7 days that the report was received and read.
* An initial assessment, including whether it is accepted as a vulnerability, within 14 days.
* Progress updates on the advisory thread as the fix develops.

doc-lattice is maintained by one person today, so these are targets rather than a contractual
SLA. If 7 days pass with no acknowledgement, assume the report reached nobody rather than
following up in the same unread thread. Use the fallback above: open a public issue saying only
that you have an unacknowledged security report and asking for a private channel, with no
technical detail.

### Coordinated disclosure

Please give us a chance to ship a fix before disclosing publicly. We ask that you hold public
details until the advisory is published or 90 days have passed, whichever comes first. We will
publish a GitHub Security Advisory with affected and fixed version ranges once the fix has
released, and credit you by the name or handle you prefer unless you ask us not to.

## Scope

doc-lattice is a local command-line tool. It reads a YAML configuration file and Markdown
documents from a project directory, and `reconcile` writes back into that directory. It executes
no code from the project directory; the only program it runs is `git`, with fixed arguments. Its
only network use is the `linear` command's ticket lookup, which talks to
`https://api.linear.app/graphql` and only when `LINEAR_API_KEY` is set. It also renders GitHub
Actions workflows through `ci refresh`, which is the one output of the tool that handles a
secret. So the security-relevant surface is what the engine does with the paths and file
contents it was pointed at, plus that one credentialed call and what it renders.

In scope, as examples rather than an exhaustive list:

* A path in configuration or in a document that escapes the project root, defeating the
  containment checks that `path_utils.safe_resolve()` and the reconcile boundary apply.
* A `reconcile` run that writes, moves, or deletes a file outside the destinations it planned,
  or that loses data across a crash in a way its transaction and recovery contract says it
  cannot. [RECONCILE.md](RECONCILE.md) owns that contract.
* Parsing a configuration file or document that leads to code execution, or to reads or writes
  the invoking user did not authorize.
* A cache file that changes the result of a later run into something the documents on disk do
  not support.
* Anything that sends `LINEAR_API_KEY` somewhere other than the Linear GraphQL endpoint, or that
  leaks it into output, an error message, or a log. The client pins that URL over HTTPS and
  refuses every redirect specifically to prevent this, so a way around either is a vulnerability.
* A workflow rendered by `ci refresh` that mishandles the Linear API key, for example by
  widening where the secret is readable or exposing it in a log.
  [MANAGED_CI.md](MANAGED_CI.md) owns that security model and is the place to read what the
  generated artifacts are meant to guarantee.

Out of scope:

* Findings that `ci audit` does not report. It performs a structural workflow check, not an
  adversarial one, and it has performed no shell analysis since 3.0.0
  ([ARCHITECTURE.md](ARCHITECTURE.md), AD-25). A gap in what it detects is a feature request, so
  file it as an ordinary issue.
* The extracted shell linter. Report anything about `doc-lattice-shell-lint` to
  [its own repository](https://github.com/Guardantix/doc-lattice-shell-lint); the two projects
  are fully severed and share no code.
* Consequences of pointing doc-lattice at a project you do not trust while intending it to be
  safe to do so. Running it on a directory grants it that directory.
* Vulnerabilities in a dependency, with no doc-lattice-specific exploit path. Report those
  upstream. If doc-lattice's use of the dependency is what makes it exploitable, that is in
  scope and worth reporting here.
