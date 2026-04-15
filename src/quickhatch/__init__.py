"""QuickHatch - Windows-to-Linux migration wizard powered by AI."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("quickhatch")
except PackageNotFoundError:
    # Source checkout without an installed package (e.g. running from a clone)
    __version__ = "0.0.0+dev"

__all__ = ["__version__"]
