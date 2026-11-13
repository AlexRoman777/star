# Contributing

Thank you for looking. Star is maintained by one person for one household, and it is published
mainly as a reference. Please read this before spending time on a change.

## Before you write code

Open an issue first for anything larger than a typo. The project is opinionated, and a short
conversation saves a rejected pull request.

## Running the checks

You need Python 3.14, [uv](https://docs.astral.sh/uv/), Node 24 and pnpm.

```sh
scripts/verify.sh
```

That runs the fast gate: backend lint (ruff), strict types (mypy), the backend test suite,
frontend lint, frontend tests and a production build. It takes about half a minute and must
pass. `scripts/verify.sh --full` adds the PostgreSQL suite and the visual tests.

Security checks that CI also runs:

```sh
gitleaks detect --source . --config .gitleaks.toml --redact
trivy fs . --scanners vuln,misconfig --severity HIGH,CRITICAL --skip-dirs node_modules --skip-dirs .venv
```

## Rules that will not bend

`AGENTS.md` is the full rulebook. The ones that decide whether a change can be merged:

- **No delete capability**, anywhere, for any reason.
- **Every automated rating is a proposal.** The owner's rating wins.
- **Inference stays local.** No hosted model API.
- **No personal data in the repository.** No real photos, file names, EXIF values, names,
  addresses, keys or raw model output, in code, tests, fixtures or documentation. Use synthetic
  data.
- **Only the web container publishes a port.**

## Known issues

- **Migration drift.** `alembic check` reports 43 differences between the migrations and the
  ORM models on a clean database, mostly check-constraint and index names. Every migration
  applies and the database tests pass, so CI reports the drift without failing. A migration
  that reconciles the names would be a welcome contribution.
- **No screenshots.** The interface shows family photos, so there are none in the repository
  yet. They will come from synthetic data.
- **One tested layout.** The maintainer runs a coordinator, one NVIDIA worker and one Apple
  worker on separate machines. Other layouts in `docs/SETUP.md` are expected to work and have
  not been tested.

## Commits and pull requests

- One logical change per pull request.
- `main` is protected: changes arrive through pull requests and the checks must pass.
- Say what you ran and what you saw, not what you assume works.

## Licensing

By contributing you agree that your contribution is licensed under AGPL-3.0-or-later, the same
licence as the project. The maintainer keeps the right to offer the project under other terms.

## Things that will probably be declined

- Hosted or cloud inference.
- Anything that deletes or hides photos in Immich.
- Automatic write-back without the owner's approval.
- Support for hardware the maintainer cannot test, unless it is small and self-contained.
