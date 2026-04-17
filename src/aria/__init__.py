"""Aria — lean local-LLM agent."""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("aria-agent")
except PackageNotFoundError:
    __version__ = "dev"
