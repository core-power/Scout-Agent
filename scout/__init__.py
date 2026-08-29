"""Scout Agent — The self-evolving AI agent that grows with you."""

from importlib.metadata import version as _get_version, PackageNotFoundError

try:
    __version__ = _get_version("scout-agent")
except PackageNotFoundError:
    __version__ = "0.1.0"
