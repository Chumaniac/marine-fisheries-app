"""Marine fisheries application. Real, demo, and unavailable states stay explicit."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import tomllib


def _source_version():
    # Local distributions reuse dependency runtimes; the selected source owns
    # application identity, while installed wheels use their package metadata.
    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if project.is_file():
        with project.open("rb") as stream:
            metadata = tomllib.load(stream).get("project", {})
        if metadata.get("name") == "marine-fisheries-intelligence":
            return metadata["version"]
    try:
        return version("marine-fisheries-intelligence")
    except PackageNotFoundError:
        return "unknown"


__version__ = _source_version()
