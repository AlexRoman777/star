from __future__ import annotations

import argparse
import asyncio
import socket

from star.agent.core import run_agent
from star.agent.inference_tasks import validate_agent_inference_contracts
from star.agent.runtime_smoke import smoke_qwen_runtime
from star.core.logging import configure_logging


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Star narrow worker agent")
    subcommands = command.add_subparsers(dest="command", required=True)
    subcommands.add_parser(
        "validate-contracts",
        help="Validate the released worker, inference, and lease task registries.",
    )
    smoke = subcommands.add_parser(
        "smoke-runtime",
        help="Run the released synthetic Qwen image envelope against local Ollama.",
    )
    smoke.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    smoke.add_argument("--ollama-allowed-host", action="append", default=[])
    smoke.add_argument("--model-key", default="qwen3-vl:8b-instruct-q4_K_M")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--server", required=True)
    common.add_argument("--hostname", default=socket.gethostname())
    common.add_argument("--machine-group", default="")
    common.add_argument("--capabilities", default="image-quality")
    common.add_argument("--ca-file")
    run_command = subcommands.add_parser("run", parents=[common])
    run_command.add_argument("--worker-token-file", required=True)
    run_command.add_argument("--identity-file")
    run_command.add_argument(
        "--automatic-enrollment",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    run_command.add_argument("--poll-seconds", type=float, default=5.0)
    run_command.add_argument("--heartbeat-seconds", type=float, default=30.0)
    run_command.add_argument("--technical-concurrency", type=int, choices=range(1, 129))
    run_command.add_argument("--model-concurrency", type=int, default=1, choices=range(1, 9))
    run_command.add_argument(
        "--parallel-model-key",
        help=(
            "Restrict model lanes after the first to this exact model identity. "
            "Required when model concurrency is greater than one and no measured "
            "per-model lane allowance is given."
        ),
    )
    run_command.add_argument(
        "--parallel-model-lanes",
        default="",
        help=(
            "Measured per-model lane allowance as model=lanes pairs, for example "
            "ministral-3:8b=6,qwen3-vl:8b=4. A lane may only offer a model whose "
            "allowance reaches it."
        ),
    )
    run_command.add_argument("--model-artifact-checksums", default="")
    # BooleanOptionalAction so a deployment can say --no-require-ac-power and
    # run on battery deliberately, instead of the flag having to be deleted
    # from the command line and put back afterwards.
    run_command.add_argument(
        "--require-ac-power", action=argparse.BooleanOptionalAction, default=False
    )
    run_command.add_argument("--minimum-battery", type=int, default=0, choices=range(0, 101))
    run_command.add_argument(
        "--battery-resume-percent",
        type=int,
        default=0,
        choices=range(0, 101),
        help="After reaching the battery floor, wait for this percentage before resuming.",
    )
    run_command.add_argument(
        "--heavy-task-cooldown-seconds",
        type=float,
        default=0,
    )
    run_command.add_argument("--quiet-hours-start", default="22:00")
    run_command.add_argument("--quiet-hours-end", default="07:00")
    run_command.add_argument("--quiet-hours-timezone", default="Europe/Stockholm")
    run_command.add_argument(
        "--quiet-hours-cooldown-seconds",
        type=float,
        default=0,
    )
    run_command.add_argument(
        "--quiet-hours-performance-percent",
        type=int,
        default=100,
        choices=range(1, 101),
        help="Approximate quiet-hours duty cycle using post-job rest.",
    )
    run_command.add_argument("--power-status-url")
    run_command.add_argument("--power-status-allowed-host", action="append", default=[])
    run_command.add_argument("--ollama-model")
    run_command.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    run_command.add_argument("--ollama-allowed-host", action="append", default=[])
    run_command.add_argument(
        "--model-provider",
        choices=("ollama", "openai-compatible"),
        default="ollama",
    )
    run_command.add_argument("--model-key", action="append")
    run_command.add_argument("--model-key-csv", default="")
    run_command.add_argument("--model-runtime-alias", action="append", default=[])
    run_command.add_argument("--model-runtime-alias-csv", default="")
    run_command.add_argument(
        "--adaptive-final-queue-threshold",
        type=int,
        default=0,
        choices=range(0, 10_001),
    )
    run_command.add_argument("--model-url")
    run_command.add_argument("--model-keep-alive", default="30m")
    run_command.add_argument(
        "--require-full-accelerator-residency",
        action="store_true",
        help="Reject Ollama models that silently offload compute into host memory.",
    )
    run_command.add_argument(
        "--model-idle-ttl-seconds",
        type=int,
        default=3600,
        choices=range(0, 86_401),
    )
    run_command.add_argument("--model-load-context-length", type=int)
    run_command.add_argument("--model-load-parallel", type=int, choices=range(1, 9))
    run_command.add_argument("--model-allowed-host", action="append", default=[])
    run_command.add_argument("--adviser-model-key")
    run_command.add_argument("--adviser-model-url")
    run_command.add_argument("--adviser-model-runtime-alias")
    run_command.add_argument("--adviser-model-artifact-checksum", default="")
    run_command.add_argument("--adviser-model-allowed-host", action="append", default=[])
    # The runtime label the adviser's results carry ("mlx" on the Apple worker,
    # "llama.cpp-prism" for the ternary Bonsai server on the NVIDIA worker).
    run_command.add_argument("--adviser-model-runtime-name", default="mlx")
    # A llama.cpp adviser started with --sleep-idle-seconds N: the agent waits
    # for it to sleep before another runtime takes the GPU.
    run_command.add_argument("--adviser-model-sleep-idle-seconds", type=int, default=None)
    return command


def main() -> None:
    configure_logging("INFO")
    args = parser().parse_args()
    if args.command == "validate-contracts":
        validate_agent_inference_contracts()
        return
    if args.command == "smoke-runtime":
        asyncio.run(
            smoke_qwen_runtime(
                base_url=args.ollama_url,
                allowed_hosts=tuple(args.ollama_allowed_host or ["127.0.0.1"]),
                model_key=args.model_key,
            )
        )
        return
    else:
        asyncio.run(run_agent(args))
