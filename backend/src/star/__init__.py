"""Star private media curation control plane."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("star")
except PackageNotFoundError:  # pragma: no cover - source tree without an installed package
    __version__ = "0.0.0"
