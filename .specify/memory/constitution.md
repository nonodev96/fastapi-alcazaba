# Alcazaba Constitution

<!--
Sync impact report
- Version: (template) → 1.0.0. First ratification, for the Spec Kit pilot.
- Source: no rule here is new. Each principle restates what the project already
  requires in CONTRIBUTING.md, SECURITY.md, CHANGELOG.md, pyproject.toml,
  .pre-commit-config.yaml and .github/workflows/ci.yml.
- Templates: unchanged; they read this file at run time.
-->

## Core Principles

### I. Secure by Default (NON-NEGOTIABLE)

- Anything that could serve one user's data to another, trust data read from the
  backend, or let a client bypass the cache, a limit or a check MUST be opt-in
  and documented. The default is the safe behavior.
- A feature is designed against the threats SECURITY.md puts in scope for its
  tool (another client's data or response, poisoned entries, bypassed limits,
  replayed or guessed tokens, injected headers, leaked errors, work run twice).
  The spec names the ones that apply and how the design prevents them.
- Secrets never reach logs, errors, metrics or audit records; failures are
  reported without the sensitive payload.
- Code that reads data from outside (requests, uploads, cursors, webhooks,
  stored entries) gets a fuzz target in `fuzz/` with the properties it must
  keep, run by `tests/unit/test_fuzz_targets.py`.

### II. Every Change Comes With Tests

- New behavior comes with tests; a bug fix comes with a test that fails without
  it.
- Unit and typing tests (`tests/unit`, `tests/typing`) run without servers.
  Anything that needs a real backend or server is marked `integration` (or
  `e2e`) and runs against the throwaway services of `make services-up`.
- Warnings are errors (`filterwarnings = ["error"]`): a new warning is fixed,
  not filtered, unless the filter explains why it comes from a dependency.
- CI is the gate and MUST be green before merging: unit, integration and typing
  tests on Python 3.11 to 3.14 with branch coverage of at least 90 %, the
  end-to-end suite, the package checks, the dependency audit and the strict docs
  build. Performance-sensitive paths keep their benchmarks passing
  `scripts/bench_gate.py`.

### III. Strict, Uniform Code Quality

- `ruff check`, `ruff format`, `mypy --strict` and `pylint` pass on
  `fastapi_alcazaba`, `examples`, `tests`, `scripts` and `fuzz`.
- A signature that doesn't fit on one line has one parameter per line, the last
  one with a trailing comma (`scripts/check_signatures.py`).
- Public APIs are checked from the user's side too, in
  `tests/typing/test_public_api.py`.

### IV. One Package, Shared Foundations

- The tools share one backend layer (memory, Redis, Valkey, memcached,
  DynamoDB), one error format and one set of metrics. A new tool or option uses
  them rather than adding its own; a feature that can't work on every backend
  says which ones it supports and fails clearly on the others.
- What the documentation promises "in every process" is tested against a shared
  backend (integration or end-to-end), not only in memory.
- Supported platform: Python 3.11+, FastAPI 0.115+, Pydantic v2.

### V. Documented and Versioned Public API

- The project follows Semantic Versioning and Keep a Changelog. Every
  user-visible change is noted in `CHANGELOG.md` under "Unreleased"; breaking
  changes are called out there.
- Documentation is part of the feature: the guide has a page per tool in
  `docs/guide/`; a new tool also needs its tested example in `examples/`, shown
  in `docs/examples/`, and a line in the README's table. `mkdocs build --strict`
  must pass.
- Published versions never move: a fix is a new release.

### VI. Dependencies Only When Worth It

- A new dependency is added only when it's worth it, as an optional extra where
  possible, and never a release younger than three days
  (`PIP_UPLOADED_PRIOR_TO=P3D`).
- The supply chain stays pinned: actions pinned to commits and checked with
  zizmor and actionlint; the fuzzing requirements pinned by hash
  (`make fuzz-requirements`).
- Simplicity first: no option, abstraction or backend that the spec doesn't
  need.

## Development Workflow

- Work on a branch from `main`; commit messages in English, a short summary
  line, then what changed and why. Spec Kit artifacts (`spec.md`, `plan.md`,
  `tasks.md`, checklists) are written in English as well.
- Each feature lives in `specs/NNN-name/`, committed on the same branch as its
  code so that the pull request reviews intent and implementation together.
- Order: `/speckit-specify` → `/speckit-clarify` → `/speckit-plan` →
  `/speckit-tasks` → `/speckit-analyze` → `/speckit-implement` →
  `/speckit-converge`. Clarify and analyze may be skipped only for changes
  without ambiguity.
- Persistence model: **flow-forward**. A delivered `specs/` directory is a
  historical record and is not rewritten; a later change opens a new directory
  that links to the earlier one.
- Spec Kit is not required for a small bug fix, a dependency bump or a
  documentation fix.
- Security vulnerabilities are never specified in the open: they follow
  SECURITY.md and its private advisories.

## Governance

- This constitution takes precedence over the Spec Kit templates.
  `/speckit-plan` and `/speckit-analyze` check plans against it; a justified
  deviation is recorded in the plan's complexity tracking table.
- It is amended with `/speckit-constitution` on its own branch, reviewed like
  any other change. Semantic versioning: MAJOR when a principle is removed or
  redefined, MINOR when one is added, PATCH for wording.
- When CONTRIBUTING.md, SECURITY.md or the CI gates change, this file is updated
  in the same change.

**Version**: 1.0.0 | **Ratified**: 2026-11-02 | **Last Amended**: 2026-11-02
