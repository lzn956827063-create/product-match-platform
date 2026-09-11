"""Run the full test suite with the platform runtime environment prepared."""
import os
import sys

from packages.matching.runtime import runtime_environment


def main():
    env = runtime_environment()
    if env.get("DYLD_LIBRARY_PATH") != os.environ.get("DYLD_LIBRARY_PATH"):
        os.execve(sys.executable, [sys.executable, "-m", "pytest", *sys.argv[1:]], env)
    import pytest
    raise SystemExit(pytest.main(sys.argv[1:]))


if __name__ == "__main__":
    main()
