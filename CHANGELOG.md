# Changelog

All notable changes to this project are documented here. Release notes are
generated from the matching section below, followed by the support footer in
`SUPPORT.md`.

## [0.1.0] — 2026-10-25

### Added

- `haus` command: resolve a spoken name, call the service, confirm the state.
- Local index of controllable entities with 30-day logbook usage counters,
  refreshed incrementally when older than 12 hours (`sync`, `status`).
- Usage-ranked search (`find`): every query token must match, candidates are
  ranked by real activations, unavailable entities sink.
- Ambiguity guard: two candidates within 1.5 points → refuse and list them.
- Duplicate collapsing: `switch.*` + `light.*`/`cover.*` twins are merged by
  name tokens, usage folded with `max`.
- Group commands and aliases in English and Croatian (`haus lights on`,
  `haus open "all covers"`, `sva svjetla`), plus `--dry-run`.
- Cover positions (`haus position --position 30 "living room"`).
- `--json`, `--no-wait`, `--lang`, `--config`, `--cache` flags.
- `tools/bench.py`: naive discovery path vs `haus` (turns, bytes, latency).
- Unit tests and CI matrix for Python 3.10–3.13.
