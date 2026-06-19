from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("walkcode")
except PackageNotFoundError:  # running from source without an install
    __version__ = "unknown"
