"""Load cinema_gamma.py for tests on any OS: every Win32 DLL becomes a do-nothing stub.

A stubbed function returns 0 until a test replaces it, for example:
    cg.user32._fns["IsWindow"] = lambda *args: 1
"""
import atexit
import ctypes
import importlib.util
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "cinema_gamma.py"
_temp_dirs: list = []


class FakeFn:
    """Stands in for one Win32 function; accepts argtypes/restype like the real one."""

    def __init__(self):
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return 0


class FakeDLL:
    """Stands in for user32, gdi32, ...: any attribute is a FakeFn, created on first use."""

    def __init__(self, *args, **kwargs):
        object.__setattr__(self, "_fns", {})

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._fns.setdefault(name, FakeFn())


def temp_dir() -> str:
    """A fresh temporary folder, removed when the test process exits."""
    path = tempfile.mkdtemp(prefix="cinema-gamma-test-")
    _temp_dirs.append(path)
    return path


@atexit.register
def _remove_temp_dirs() -> None:
    for path in _temp_dirs:
        shutil.rmtree(path, ignore_errors=True)


def load_cinema_gamma():
    """Import cinema_gamma.py as module "cg" with stubbed Win32 and throwaway app-data folders.

    %LOCALAPPDATA% and %APPDATA% point to temporary folders, so the tests never touch the
    real settings, the real Eye-CareU logs or the real Startup folder.
    """
    # Some tests trigger errors on purpose; keep their log records out of the test output.
    logging.getLogger().addHandler(logging.NullHandler())
    ctypes.WinDLL = FakeDLL
    ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE
    sys.platform = "win32"
    os.environ["LOCALAPPDATA"] = temp_dir()
    os.environ["APPDATA"] = temp_dir()
    spec = importlib.util.spec_from_file_location("cg", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cg"] = module  # dataclasses resolve string annotations through sys.modules
    spec.loader.exec_module(module)
    module.APP_DIR.mkdir(parents=True, exist_ok=True)
    return module
