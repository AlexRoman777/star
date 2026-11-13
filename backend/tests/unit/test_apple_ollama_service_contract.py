from __future__ import annotations

from pathlib import Path
from plistlib import loads

ROOT = Path(__file__).resolve().parents[3]
APPLE = ROOT / "Deployment" / "worker-apple"


def test_apple_runtime_is_native_ollama_only() -> None:
    assert not (APPLE / "lmstudio-watchdog.sh").exists()
    assert not (APPLE / "io.star.lmstudio.plist").exists()

    service = loads((APPLE / "io.star.ollama.plist").read_bytes())
    assert service["ProgramArguments"] == ["/opt/homebrew/bin/ollama", "serve"]


def test_ollama_service_serializes_models_and_disables_cloud() -> None:
    service = loads((APPLE / "io.star.ollama.plist").read_bytes())
    environment = service["EnvironmentVariables"]

    assert environment["OLLAMA_NO_CLOUD"] == "1"
    assert environment["OLLAMA_HOST"] == "127.0.0.1:11434"
    assert environment["OLLAMA_MAX_LOADED_MODELS"] == "1"
    # One loaded model with six prediction slots. Star restricts the
    # extra five lanes to the exact canonical Ministral helper identity.
    assert environment["OLLAMA_NUM_PARALLEL"] == "6"
    assert environment["OLLAMA_KEEP_ALIVE"] == "30s"
    assert service["RunAtLoad"] is True
    assert service["KeepAlive"] is True


def test_mlx_installer_includes_its_runtime_template_dependency() -> None:
    installer = (APPLE / "install-qwen38-mlx.sh").read_text()
    downloader = (APPLE / "download-qwen38-mlx-8bit.sh").read_text()
    benchmark = (APPLE / "benchmark-qwen38-mlx-fit.py").read_text()

    assert "'mlx-vlm>=0.6.8,<0.7'" in installer
    assert "'jinja2>=3.1.6,<4'" in installer
    assert "import mlx_vlm, jinja2" in installer
    assert "Qwen3.8-27B-active" in installer
    assert ".star-fit.json" in installer
    assert "config_digest" in installer
    assert "quantization_bits" in installer
    assert "cmp -s" in installer
    assert "launchctl print" in installer
    assert "for delay in 0 1 2 3" in installer
    assert "mlx-community/Qwen3.8-27B-8bit" in downloader
    assert "815b83c0df8ffd1d1b5244cf75fd6ef14fca9ef9" in downloader
    assert "HF_HUB_DISABLE_TELEMETRY=1" in downloader
    assert "MAX_PEAK_MEMORY_GB = 42.0" in benchmark
    assert 'reasoning_effort="medium"' in benchmark
    assert '"kv_bits": 8' in benchmark
