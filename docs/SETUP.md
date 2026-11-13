# Setting up Star

Star has two kinds of machine. A **coordinator** holds the database, the scheduler and the web
UI. One or more **workers** run the vision models and connect to the coordinator. The same
software runs on any compatible hardware; this guide says what "compatible" means and walks
through an install.

Every address, user and path below is an example. Replace them with your own.

## Compatible hardware

| Role | Runs on | Needs |
|---|---|---|
| Coordinator | any 64-bit Docker host: a NAS, a mini PC, a home server, a Linux PC | about 6 GB of free RAM, and disk for the database and previews |
| NVIDIA worker | Linux with an NVIDIA GPU and the NVIDIA Container Toolkit | 8 GB of VRAM or more |
| Apple worker | a Mac with Apple Silicon, Docker Desktop and Ollama | 16 GB of memory for the primary review; about 48 GB for the advanced review |

You need **one coordinator and at least one worker**. A second worker makes the review faster
and is never required.

Not supported: AMD and Intel GPUs, Intel Macs, and Windows as a worker. The model runtimes Star
drives (Ollama with CUDA, and MLX) do not cover them.

**The smallest setup is one machine**: a Linux PC with an NVIDIA GPU can run the coordinator
and an NVIDIA worker side by side. The maintainer has not tested this layout.

**Check what address the coordinator sees.** Workers may enrol only from the networks in
`STAR_WORKER_AUTO_ENROLL_CIDRS`, and that check uses the address the web container sees. On
Docker Desktop, rootless Docker and some NAS systems, every client appears as the Docker gateway
address. Do not add a Docker network range such as `172.16.0.0/12` to the list to work around
this: it would let every device that can reach the port enrol. Use host networking for the web
container, or enrol the worker once from the coordinator itself.

## What each worker does

| Step | Model | NVIDIA worker | Apple worker |
|---|---|---|---|
| Primary review, every photo | Ministral 3 8B | yes | yes |
| Similarity embeddings | DINOv2 | yes | no |
| Comparison of top candidates | Qwen3-VL 8B | yes | yes |
| Advanced review of 4 and 5 star proposals | Qwen 3.8 27B | optional, see below | yes, with about 48 GB of memory |

Without any machine that can run the advanced review, Star still rates every photo. Four- and
five-star proposals simply stay proposals until you confirm them yourself.

You need to be comfortable with Docker Compose, with editing `.env` files and with reading
container logs. Plan for an afternoon, most of it spent downloading models.

## 1. Create an Immich key

In Immich, create an API key for Star with **only** these permissions:

```
asset.read asset.view asset.statistics album.read person.read face.read
duplicate.read tag.read sync.stream syncCheckpoint.update
```

To let Star write approved ratings back, add `asset.update tag.create tag.asset`.

Never give it delete, upload, replace, admin or `all`. Star has no code path that uses them,
and a key without them cannot damage the library even if something goes wrong.

## 2. Build the images

On a machine with this checkout, Docker, [uv](https://docs.astral.sh/uv/) and pnpm:

```sh
tools/star build "$(cat RELEASE)"
```

This runs the verification gate, then writes the coordinator images to
`Deployment/coordinator/star-{api,web}-nas-vX.Y.Z.tar.gz` and the worker package to
`Deployment/worker-nvidia/Package/` and `Deployment/worker-apple/Package/`. The coordinator
images are built for `linux/amd64`; change the `--platform` value in `tools/star` if your
coordinator is ARM.

## 3. Start the coordinator

Copy `Deployment/coordinator/compose.yaml`, its `.env.example` and the two image archives to
the coordinator. Then:

```sh
docker load --input star-api-nas-vX.Y.Z.tar.gz
docker load --input star-web-nas-vX.Y.Z.tar.gz
cp .env.example .env        # then edit it
```

In `.env`, set `STAR_LAN_ADDRESS` to this machine's address, set
`STAR_WORKER_AUTO_ENROLL_CIDRS` to your own network, and generate every secret. The example
file says how. Without `STAR_LAN_ADDRESS` the web UI listens on `127.0.0.1` only.

Star answers only to the address in `STAR_LAN_ADDRESS`. If you reach it by a name such as
`star.lan`, list that name in `STAR_EXTRA_ALLOWED_HOSTS`, or the API refuses the request.

Create the data directories once, with the ownership the containers expect:

```sh
mkdir -p star-data/{artifacts,models,starml,postgres}
chown -R 10001:10001 star-data/{artifacts,models,starml} && chmod 0750 star-data/{artifacts,models,starml}
chown 999:999 star-data/postgres && chmod 0700 star-data/postgres
```

Start it and check that it is ready:

```sh
docker compose up -d
curl --fail http://192.168.0.10:3822/api/v1/health/ready
```

Open `http://192.168.0.10:3822`, choose **First setup** and create the owner account. Add the
Immich connection under Accounts with the key from step 1.

## 4. Start an NVIDIA worker

On the Linux machine with the NVIDIA GPU, copy `Deployment/worker-nvidia/` including its
`Package/` directory, then:

```sh
cp .env.example .env        # set STAR_CONTROL_PLANE_URL to the coordinator
docker compose --env-file .env --env-file Package/provenance.env up -d --wait
docker compose exec ollama ollama pull ministral-3:8b-instruct-2512-q4_K_M
docker compose exec ollama ollama pull qwen3-vl:8b-instruct-q4_K_M
docker compose exec ollama ollama ps      # PROCESSOR should read 100% GPU
```

Check the memory limits in `.env` (`STAR_OLLAMA_MEMORY_LIMIT` and its reservation). The example
values assume a host with plenty of RAM; lower them to fit yours.

The worker enrols itself if it connects from a network listed in
`STAR_WORKER_AUTO_ENROLL_CIDRS`. It appears on the Machines page within a minute, **paused**.

**Press Resume to accept it.** A device that enrols for the first time receives no work, and so
no photos, until you resume it. If a machine you do not recognise appears there, remove it.

**Advanced review on an NVIDIA worker is optional and experimental.** It uses a compressed 27B
model served by a third-party llama.cpp fork that you build yourself. It sits behind a Compose
profile and is off by default. `docs/OPERATIONS.md` describes the build; start it with
`COMPOSE_PROFILES=advanced-review`.

## 5. Start an Apple worker

On a Mac with Apple Silicon, Docker Desktop and Ollama:

```sh
ollama pull ministral-3:8b && ollama cp ministral-3:8b ministral-3:8b-instruct-2512-q4_K_M
ollama pull qwen3-vl:8b-instruct-q4_K_M
cp Deployment/worker-apple/.env.example Deployment/worker-apple/.env     # then edit it
tools/star deploy apple --apply
```

The advanced review model runs natively with MLX and is installed by
`Deployment/worker-apple/download-qwen38-mlx-8bit.sh` and `install-qwen38-mlx.sh`. Skip both on
a Mac with less than about 48 GB of memory.

A Mac that runs on battery stops taking work when it is unplugged or low, and resumes by
itself. A Mac without a battery is simply always available.

## 6. Start reviewing

In the web UI, open the library program, choose how many photos to bring in, and start it.
Write-back stays off until you enable it and approve a batch.

## When something is wrong

[OPERATIONS.md](OPERATIONS.md) has the troubleshooting section. The short version: the Machines
page shows why a worker is not taking work, and the Failures page classifies every failure and
says whether it will retry by itself.

## Before you expose it

Do not. Star speaks plain HTTP and is designed for a home network. Reach it from outside through
a VPN. See [SECURITY.md](../SECURITY.md).
