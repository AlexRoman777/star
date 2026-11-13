# Star operations

This is the runbook for the reference setup: a NAS coordinator (example address `192.168.0.10`,
managed through the NAS's Docker UI, no SSH from tools), an NVIDIA GPU worker reached over SSH
(`ssh -i ~/.ssh/star user@192.168.0.20`, deployment directory `/opt/star/Deployment/worker-nvidia`) and
a Mac that holds this checkout (`Deployment/worker-apple`). Every address, user and path here is an
example. Start with `docs/SETUP.md` for a first install, and `docs/ADAPTING.md` if your machines
differ. Secrets are named here by file only. Never print them.

## Release

```sh
tools/star build v260        # next number only; bumps versions, runs the 25 s gate, builds wheel + NAS images
# install on the NAS by hand (below), then:
tools/star deploy --apply    # NVIDIA over SSH + rsync, Mac locally, in parallel; then checks heartbeats
tools/star status            # artifacts, NAS release, both machines, native Apple services
```

Commit before `build`; it refuses a dirty tree. `build` writes `Deployment/coordinator/star-{api,web}-nas-vNN.tar.gz`
and `Deployment/{NVIDIA,Apple}/Package/{star-NN.0.0-py3-none-any.whl,provenance.env}`.
`deploy` without `--apply` prints what it would run. `deploy nvidia|apple` deploys one machine;
`deploy --action logs|restart|stop|start` are the lifecycle actions.

## NAS install

1. Copy `Deployment/coordinator/compose.yaml` and the two `.tar.gz` files to `/opt/star`
   (the `.env` there is kept; first install: create it from `Deployment/coordinator/.env.example`).
2. In the NAS's Docker UI, import both image archives as they are. Expect `star/api:vNN`, `star/web:vNN`.
3. In the Docker UI, open the `star` project and redeploy from `/opt/star/compose.yaml`
   without removing volumes. Expect: `postgres` healthy, `api` healthy after Alembic, `web` on
   `:3822`, `scheduler`, `writeback-worker`, `database-backup`.
4. `curl --fail http://192.168.0.10:3822/api/v1/health/ready`.

First install only, on the NAS host: create `./star-data/{artifacts,models,starml}`
owned by `10001:10001` (mode 0750) and `./star-data/postgres` owned by `999:999`
(mode 0700). First login at `http://192.168.0.10:3822` → First setup → create the owner account. Add the Immich
connection under Accounts with a least-privilege key (`asset.read asset.view asset.statistics
album.read person.read face.read duplicate.read tag.read sync.stream syncCheckpoint.update`, plus
`asset.update tag.create tag.asset` for write-back; never delete, upload, replace, admin or `all`).

Rollback: import the previous `star-*-nas-vNN.tar.gz` (keep the last one) and redeploy the
matching `compose.yaml` from git. Never attach the PostgreSQL 18 data directory to an older major.

## NVIDIA

Runs `technical-worker` (staging + DINOv2, CPU), `gpu-worker` (Ministral, Qwen3-VL, Bonsai on one
GPU lane), `ollama`, `bonsai-runtime`, `embedding-runtime`. Settings live in
`Deployment/worker-nvidia/.env` (`STAR_` prefix). `deploy` rsyncs the directory excluding `.env`, then
`docker compose --env-file .env --env-file Package/provenance.env up -d --wait`.

Models (once, then after every `ollama pull`):

```sh
docker compose exec ollama ollama pull ministral-3:8b-instruct-2512-q4_K_M
docker compose exec ollama ollama pull qwen3-vl:8b-instruct-q4_K_M
printf 'FROM ministral-3:8b-instruct-2512-q4_K_M\nPARAMETER temperature 0.2\n' > /tmp/Modelfile
docker compose exec -T ollama ollama create ministral-3:8b-instruct-2512-q4_K_M -f /tmp/Modelfile
docker compose exec ollama ollama ps        # PROCESSOR must read 100% GPU; nvidia-smi idles ~400 MiB
```

Bonsai (Advanced review): build PrismML's llama.cpp fork (branch `prism`) for compute capability
7.5 under `STAR_BONSAI_DIR`, copy `libgomp.so.1` beside `llama-server`, place the PTQ1_0 model and
projector `.gguf` files, set `STAR_BONSAI_ARTIFACT_CHECKSUM` (sha256 of the weights) in `.env`.
GPU lost after suspend (`Failed to initialize NVML`): the resume hook restarts the runtime
container; check `ollama ps` again.

## Mac

Native launchd services (installed by `deploy apple`): `io.star.ollama` (six parallel
lanes, one loaded model, `127.0.0.1:11434`), `io.star.qwen38-mlx` (`mlx_vlm.server`,
`127.0.0.1:8772`, token in `~/Library/Application Support/Star/qwen38-mlx-runtime/token`),
`io.star.apple-power` (`127.0.0.1:8769`, token file `apple-power-token`). The worker
container `accelerator-worker` reads `Deployment/worker-apple/.env` (`STAR_APPLE_POWER_TOKEN`,
`STAR_ADVISER_MODEL_API_KEY`, `STAR_MAC_MODEL_CONCURRENCY=6`, `STAR_MAC_PARALLEL_MODEL_KEY`,
`STAR_MAC_MINIMUM_BATTERY=50`). Docker Desktop must start at login with Local Network permission.

```sh
brew install --cask ollama && ollama pull ministral-3:8b && ollama cp ministral-3:8b ministral-3:8b-instruct-2512-q4_K_M
ollama pull qwen3-vl:8b-instruct-q4_K_M
Deployment/worker-apple/download-qwen38-mlx-8bit.sh && Deployment/worker-apple/install-qwen38-mlx.sh
tools/star deploy apple --apply
launchctl list | grep io.star
```

## When something is wrong

- Machines page → the worker card shows `lease_refusal` and `model_turn_decision`; the worker log
  says why every refused poll was refused:
  `docker compose --env-file Deployment/worker-apple/.env -f Deployment/worker-apple/compose.yaml logs --since 15m accelerator-worker | grep "refused a lane poll"`
  (nvidia: `gpu-worker` over SSH). Paused → press Resume on Machines. On battery → plug in.
- Failures page (`/work?view=failures`) or `GET /api/v1/agent/failures?hours=72` with the
  diagnostics bearer token from `.env.agent`: every class has a kind, severity, retry policy and
  action, and says whether a code fix is likely. Rules live in `services/failure_overview.py`.
- Snapshot without the UI: `python3 scripts/star_agent_diagnostics.py` (`--progress`, `--trace
  <asset_id>`, `--reviews`). `GET /api/v1/agent/diagnostics` has queue counts, scheduler gates,
  database activity and slow statements.
- Nothing runs and the queue is empty: compare `current_analysis_signature` with what the
  generations carry; a re-review wave starts only when the contract changed.
- Repeated `Running upgrade …` in the API log without the Uvicorn line: the migration waits on a
  lock; do not restart the database.
- Reset only through Settings → Accounts (photo library only, or fresh installation). Never
  `docker compose down -v`; never edit the database by hand.

## Local copy of the NAS database

The NAS dumps nightly at 03:00 UTC to `./star-data/starml/database/star-<stamp>.dump`
(+ `.sha256`). Copy the newest dump into `private/database/` (git-ignored) and run
`scripts/star-db-local.sh restore private/database/star-<stamp>.dump`; it prints
`STAR_DATABASE_URL` for a loopback PostgreSQL 18 on `127.0.0.1:55432`. Measure every schema or
query change there before building. Never point a worker or a write-back batch at the copy.
