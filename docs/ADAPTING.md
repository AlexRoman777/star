# Adapting Star

Star tells machines apart by what they can do, not by what they are. A worker reports its
capabilities when it enrols (for example `small-vlm`, `medium-vlm`, `apple-silicon`,
`semantic-embedding`), and the scheduler hands it only the work those capabilities cover.

This page lists what you can configure, and what is still fixed in the code.

## What you configure without touching code

| What | Where |
|---|---|
| The coordinator's address, name and data directory | `Deployment/coordinator/.env` (`STAR_LAN_ADDRESS`, `STAR_COORDINATOR_NAME`, `STAR_DATA_DIR`) |
| Which networks may enrol workers | `STAR_WORKER_AUTO_ENROLL_CIDRS` |
| Where a worker finds the coordinator | `STAR_CONTROL_PLANE_URL` in the worker's `.env` |
| A worker's name on the Machines page | the hostname settings in the worker's `.env` |
| Memory limits, concurrency and quiet hours | the worker's `.env` |
| The rating scale and every prompt | the web UI |
| The family roster used for attributed names | the web UI. It starts empty and is stored encrypted. |
| Worker schedules | the Machines page |
| The share of photos that may hold five stars | the library program policy |
| Whether ratings are written back to Immich | off until you enable it and approve a batch |

## What is still fixed

These are limits of the current code, not configuration you have missed.

**Two worker families.** The scheduler and the web UI know two families, `nvidia` and `apple`,
and group workers as `nvidia-main` and `apple-main`. There is no third family: AMD GPUs, Intel
GPUs and Intel Macs are not supported.

**Fixed models.** The pipeline expects these exact model keys:

| Role | Model |
|---|---|
| Primary review | `ministral-3:8b-instruct-2512-q4_K_M` |
| Comparison of top candidates | `qwen3-vl:8b-instruct-q4_K_M` |
| Advanced review | `qwen3.8:27b` |
| Image similarity | DINOv2 |

Swapping a model means changing the allow-list in `Deployment/coordinator/compose.yaml`
(`STAR_AUTOMATIC_ASSET_VLM_MODELS`), the model profiles in the backend and the labels in the UI.
Budgets, context sizes and prompts were tuned for these models on 8 GB of VRAM.

**The default time zone is `Europe/Stockholm`.** It is the default for schedules and quiet
hours. Change it per worker in `.env` and per schedule in the web UI.

**The release tool assumes one layout.** `tools/star` builds image archives for a coordinator
it cannot reach over SSH, deploys an NVIDIA worker over SSH and an Apple worker locally.
Override `STAR_NVIDIA_SSH`, `STAR_NVIDIA_DIR`, `STAR_NVIDIA_SSH_KEY` and `STAR_NAS_URL`, or use
the Compose files directly as `docs/SETUP.md` shows.

**macOS service names.** The Apple worker installs launchd services named `io.star.*`.

## A sensible order

1. Bring up the coordinator alone and connect Immich. The web UI works without any worker.
2. Add one worker with the primary review model.
3. Let it review a few hundred photos and correct the ratings yourself.
4. Only then decide whether the advanced review and a second machine are worth it.

## If you change the worker families or the models

The single source of truth for the pipeline is `backend/src/star/services/pipeline_contract.py`.
Coordinator and workers both import it, and they refuse to work together when their contract
hashes differ. The rule that decides a worker's family is `machine_kind_of` in
`backend/src/star/services/model_slots.py`. Start there, run `scripts/verify.sh`, and read
`AGENTS.md` for the invariants the tests will hold you to.
