# Benchmark protocol

Source: [2026-07-19-allowlist-recognizer-design.md](../../../docs/superpowers/specs/2026-07-19-allowlist-recognizer-design.md),
"Predeclaration checkpoint", item 7. Transcribed verbatim.

The benchmark protocol: fleetyard-VM (Linux Mint 22.3) with no concurrent workload, CPython
3.13 and 3.14 via uv, timed scope of one full replay-inventory scan through the harness entry
point, 3 discarded warm-up runs, 30 measured repetitions per Python version, median statistic
per version, ceiling 250 ms for each version's median, candidate runs interleaved with
current-scanner baseline runs and the ratio reported. Exceeding the ceiling rejects the
candidate. This is a trusted fleetyard-only decision gate recorded in the decision record; it
is not CI-enforced, and the workstation is never attached as a self-hosted runner.
Deterministic work and corpus gates remain CI-enforced.
