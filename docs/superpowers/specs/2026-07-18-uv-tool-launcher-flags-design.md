# uv Tool Launcher Flags

## Context

The GitHub CI shell scanner recognizes policy-sensitive `doc-lattice` commands without executing shell input. Its `uvx` and `uv tool run` launchers currently reject several documented valueless flags before reaching the command payload. That makes otherwise valid pull-request workflows fail closed with a configuration error instead of classifying the effective `doc-lattice` invocation.

## Design

Add a dedicated immutable flag set for the equivalent `uvx` and `uv tool run` launchers. It will extend their existing launcher flags with the documented valueless tool-run options supported by the locally verified uv CLI surface. Both launcher descriptors will use this set. The separate `uv run` descriptor will remain unchanged so tool-only options do not broaden unrelated grammar.

Unknown options, dynamic option positions, options requiring arguments, help/version stop behavior, launcher nesting limits, and command classification will retain their current behavior. The scanner will continue to fail closed when it cannot statically determine the command boundary.

## Testing

Use test-first coverage at two layers:

- A parameterized scanner test will exercise every newly accepted flag with both `uvx` and `uv tool run`, proving the effective dry-run reconcile command is recognized.
- End-to-end audit cases will cover a safe dry-run command using `--no-index` plus its required `--find-links` argument and a mutating reconcile command using another newly accepted flag.
- Existing unknown-option tests will remain unchanged and will be rerun to prove unsupported options still fail closed.

The focused scanner and audit suites will run before the full repository verification commands.
