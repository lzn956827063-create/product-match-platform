"""Prepare child-process environment for LightGBM on macOS."""
import os
import sys
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def openmp_directory():
    if sys.platform != "darwin":
        return None
    candidates = []
    for entry in sys.path:
        root = Path(entry)
        candidates.extend((root / "sklearn" / ".dylibs").glob("libomp*.dylib"))
        candidates.extend((root / "scipy" / ".dylibs").glob("libomp*.dylib"))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.parent)
    return None


def runtime_environment(environment=None):
    env = dict(environment or os.environ)
    directory = openmp_directory()
    if directory and not env.get("DYLD_LIBRARY_PATH"):
        env["DYLD_LIBRARY_PATH"] = directory
    return env


def reexec_module(module):
    """Restart a CLI module before LightGBM is imported by the dynamic loader."""
    if sys.platform != "darwin" or os.environ.get("DYLD_LIBRARY_PATH") or not openmp_directory():
        return
    os.execve(sys.executable, [sys.executable, "-m", module, *sys.argv[1:]], runtime_environment())
