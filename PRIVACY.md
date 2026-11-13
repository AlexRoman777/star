# Privacy

Star handles a family's private photos, so this page says exactly where data goes.

**The short version: nowhere.** Star has no telemetry, no account, no cloud component and no
hosted model. Photos are processed only on machines you run.

## What Star stores

| Data | Where | Notes |
|---|---|---|
| Ratings, proposals, review history, queues, audit log | PostgreSQL on the coordinator | Immich stays the system of record |
| Bounded image derivatives (previews for the models) | the coordinator's artifact cache | size-capped and pruned |
| Image embeddings | PostgreSQL with pgvector | used to group similar photos |
| The Immich API key | the database, encrypted | never shown again after entry |
| The family roster (names and aliases) | the database, encrypted | empty until you fill it in |
| Login passwords | the database, as Argon2 hashes | |
| Tokens and other secrets | ignored `.env` files on each machine | |

Star does not copy your original photos. It reads them from Immich when a model needs them.

## What leaves your network

Nothing, in normal operation.

- **Inference is local.** The provider allow-lists reject hosted model APIs, cloud-suffixed
  models, public hosts, redirects and proxies.
- **No external observability.** There is no OpenTelemetry, Prometheus or Grafana integration,
  and a test pins that.
- **Downloading models** is the one time a machine contacts the internet: when you pull a model
  with Ollama or from Hugging Face. That request carries the model name, not your data.

## What the models see

A model receives the image and a prompt. If Immich has attributed a person to the photo and
that person is in your roster, the model also receives that name as trusted context. Models
never receive file names as instructions, and they are told not to infer identity, relationship
or emotion from pixels.

## Logs and diagnostics

Logs and diagnostics refer to photos by opaque identifiers only. Raw model output, file names,
EXIF values and prompts are not written to logs.

## Deleting your data

Remove the coordinator's data directory (`STAR_DATA_DIR`) and the worker caches. Ratings that
were written back to Immich stay in Immich, because they are now part of your library.
