# Changelog

## Version numbers

Star has two numbers. The **release version** (`1.0.0`) is the public one: it follows
[semantic versioning](https://semver.org), and a change that breaks an existing installation
raises the first number. The **build number** (`v272` in the `RELEASE` file) counts every build
since the project began. It is what the images, the workers and the Machines page report, and
`tools/star build` raises it by one each time.

## 1.0.0, build 272 (14 November 2026)

The first public release.

- Local-first photo curation for Immich: a coordinator, and workers on NVIDIA GPUs or Apple
  Silicon that review photos with vision models running on your own machines.
- Every rating is a proposal. The owner decides, and approved ratings are written back to
  Immich in a separate, explicitly approved step. Nothing can be deleted.
- A device that enrols as a worker for the first time waits, paused, until the owner accepts it.
- The API answers only for the host names it was told about.

### What was verified for this release

- About 600 backend tests and 200 frontend tests, lint, strict types and the production build.
- The PostgreSQL suite, including the upgrade path from the earlier schema.
- The coordinator images build, and the coordinator starts on a fresh database.
- A source security audit; its fixes and its two open limitations are in `SECURITY.md`.

### What was not verified

- **No worker has been run against this public code yet.** The maintainer's own installation
  runs the private predecessor, which uses different names for the worker families. Expect to
  report problems on a first install.
- The single-machine layout in `docs/SETUP.md`.
