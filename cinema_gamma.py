r"""
cinema_gamma.py
===============
Companion panel for BenQ Eye-CareU (GW2790C). It sits flush against the
Eye-CareU window, looks like part of it, and gives every Eye-CareU colour mode
(Standard, Cinema, Game, ePaper, Care Mode) its own gamma / brightness /
contrast setting. Switching the mode in Eye-CareU (or on the monitor) loads
that mode's setting; modes that were never adjusted stay at the default.

How it works (Eye-CareU is closed source and has no plugin API):
  * the monitor's current mode is read over DDC/CI (VCP 0xDC), the register
    Eye-CareU itself writes; the mode names come from Eye-CareU's own log;
  * a setting is applied as a GPU gamma ramp on the BenQ's output only (in
    Duplicate mode the laptop screen shares that output and gets it too);
  * the panel follows Eye-CareU through WinEvents: while Eye-CareU stands still
    nothing runs; when it moves, the panel follows every step and stays in a
    tight-follow mode for 10 s after the last move, then goes back to sleep.

Usage
  Baslat.cmd / Durdur.cmd              (next to this file) start / stop it with a double-click
  python  cinema_gamma.py              run (the console shows the log)
  pythonw cinema_gamma.py              run without a console window
  python  cinema_gamma.py --install    start automatically at Windows login (and start now)
  python  cinema_gamma.py --uninstall  remove the login shortcut
  python  cinema_gamma.py --no-panel   apply the saved settings only, without the panel

Requirements: Windows 10/11, Python 3.9+ with Tk (default installer), no pip packages.
Files:        %LOCALAPPDATA%\CinemaGamma\config.json  and  cinema_gamma.log
"""
from __future__ import annotations

import argparse
import atexit
import ctypes
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field, fields, replace
from logging.handlers import RotatingFileHandler
from pathlib import Path

if sys.platform != "win32":
    raise SystemExit("cinema_gamma.py only runs on Windows.")

from ctypes import wintypes  # noqa: E402  (Windows-only types)

# ---------------------------------------------------------------------------
# Paths & logging
# ---------------------------------------------------------------------------
LOCAL_APPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
APP_DIR = LOCAL_APPDATA / "CinemaGamma"
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "cinema_gamma.log"
STOP_PATH = APP_DIR / "stop"        # Durdur.cmd creates it: a running copy exits cleanly
RESTART_PATH = APP_DIR / "restart"  # after an update: a running copy restarts on the new code
EYECARE_LOG_DIR = LOCAL_APPDATA / "BenQ" / "Eye-CareU" / "log"
STARTUP_LINK = (
    Path(os.environ.get("APPDATA", str(Path.home())))
    / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Cinema Gamma.lnk"
)

log = logging.getLogger("cinema_gamma")


def setup_logging() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=512_000, backupCount=1, encoding="utf-8")
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)
    if sys.stderr is not None:  # pythonw has no console
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        log.addHandler(console)


def take_flag(path: Path) -> bool:
    """True once if the flag file exists (removing it), so each request is handled once."""
    try:
        path.unlink()
        return True
    except OSError:  # FileNotFoundError in the normal case
        return False


def stop_requested() -> bool:
    return take_flag(STOP_PATH)


restart_requested = False  # set by the panel; main() starts the new copy after cleaning up


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass
class Profile:
    gamma: float = 1.0       # > 1 lifts mid-tones (same direction as NVIDIA's slider)
    brightness: float = 0.0  # -0.30 ... +0.30, added to every level
    contrast: float = 1.0    # 0.50 ... 1.50, stretches around mid-grey

    def is_default(self) -> bool:
        return abs(self.gamma - 1) < 0.005 and abs(self.brightness) < 0.005 and abs(self.contrast - 1) < 0.005


DEFAULT_PROFILE = Profile()

# VCP 0xDC values of the GW2790C's colour modes, as Eye-CareU writes them.
# Others (e.g. Game) are learned automatically from Eye-CareU's log.
DEFAULT_MODE_NAMES = {"0": "Standard", "50": "Cinema", "295": "Care Mode", "1063": "ePaper"}


def _profile_from(data) -> Profile:
    keys = {f.name for f in fields(Profile)}
    if not isinstance(data, dict):
        return Profile()
    return Profile(**{k: float(v) for k, v in data.items() if k in keys})


@dataclass
class Config:
    vcp_code: int = 0xDC             # BenQ picture-mode register
    monitor_tag: str = "BNQ"         # EDID vendor id that appears in the Windows device id
    eyecare_match: str = "eye-careu"  # matched against the Eye-CareU exe path
    # DDC/CI is only a fallback for mode changes made with the monitor's own buttons:
    # Eye-CareU's switches are seen instantly in its log, so these can be slow.
    ddc_check_open_s: float = 10.0   # while Eye-CareU is open
    ddc_check_closed_s: float = 20.0  # while it is closed
    profiles: dict[str, Profile] = field(default_factory=dict)  # mode value -> its setting
    mode_names: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MODE_NAMES))
    panel_collapsed: bool = False    # the panel is folded into the small arrow tab

    @classmethod
    def load(cls) -> "Config":
        config = cls()
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return config
        except (OSError, ValueError) as exc:
            log.warning("config unreadable (%s), using defaults", exc)
            return config
        if not isinstance(raw, dict):
            return config
        for name in ("vcp_code", "monitor_tag", "eyecare_match", "ddc_check_open_s", "ddc_check_closed_s",
                     "panel_collapsed"):
            if name in raw:
                try:
                    setattr(config, name, type(getattr(config, name))(raw[name]))
                except (TypeError, ValueError):
                    log.warning("ignoring bad config value %s=%r", name, raw[name])
        profiles = {str(k): _profile_from(v) for k, v in (raw.get("profiles") or {}).items()}
        # Older versions had one "cinema" setting shared by the "cinema_values" taught on the panel.
        if "cinema_values" in raw:
            legacy = _profile_from(raw.get("cinema"))
            for value in raw["cinema_values"]:
                profiles.setdefault(str(value), legacy)
        config.profiles = {k: p for k, p in profiles.items() if not p.is_default()}
        config.mode_names.update({str(k): str(v) for k, v in (raw.get("mode_names") or {}).items()})
        return config

    def save(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(CONFIG_PATH)  # atomic swap: never leaves half a file behind


_SET_COLOR_MODE = re.compile(r'setColorMode: "([^"]+)"')
_SET_VCP_DC = re.compile(r'\[SetVCPFeature\] true :\s+"dc" (\d+)')


class EyeCareLogWatcher:
    """Sees Eye-CareU switch the monitor's mode the moment it happens: Eye-CareU
    logs every switch (`setColorMode: "Cinema"`, then `"dc" 50`). Checking the
    log's size is a single cheap file-system call, unlike a DDC/CI round trip."""

    RESCAN_S = 5.0  # how often to look for a newer log file (Eye-CareU starts one per day)

    def __init__(self, log_dir: Path = EYECARE_LOG_DIR) -> None:
        self.log_dir = log_dir
        self.path: Path | None = None
        self.offset = 0
        self._attached = False
        self._next_rescan = 0.0
        self._pending_name: str | None = None

    def _newest(self) -> Path | None:
        try:
            return max(self.log_dir.glob("Eye-CareU-*.log"), key=lambda p: p.stat().st_mtime, default=None)
        except OSError:
            return None

    def poll(self) -> tuple[int, str | None] | None:
        """(mode value, name) of the latest switch since the previous call, else None."""
        now = time.monotonic()
        if self.path is None or now >= self._next_rescan:
            self._next_rescan = now + self.RESCAN_S
            newest = self._newest()
            if newest is not None and newest != self.path:
                # The first file we see may hold weeks of history: start at its end.
                # A file that appears later is new: read it from the start.
                self.offset = newest.stat().st_size if not self._attached else 0
                self.path, self._attached = newest, True
        if self.path is None:
            return None
        try:
            size = self.path.stat().st_size
            if size < self.offset:  # truncated or replaced
                self.offset = 0
            if size == self.offset:
                return None
            with open(self.path, "rb") as file:
                file.seek(self.offset)
                data = file.read(size - self.offset)
        except OSError:
            return None
        end = data.rfind(b"\n")
        if end < 0:
            return None  # only read whole lines; the rest arrives with the next write
        self.offset += end + 1
        switched = None
        for line in data[:end].decode("utf-8", "replace").splitlines():
            match = _SET_COLOR_MODE.search(line)
            if match:
                self._pending_name = match.group(1)
                continue
            match = _SET_VCP_DC.search(line)
            if match:
                switched = (int(match.group(1)), self._pending_name)
                self._pending_name = None
        return switched


def learn_mode_names(log_dir: Path = EYECARE_LOG_DIR) -> dict[str, str]:
    """Eye-CareU logs `setColorMode: "Cinema"` right before it writes VCP 0xDC = 50,
    so its own log names every mode value it has ever set."""
    names: dict[str, str] = {}
    try:
        paths = sorted(log_dir.glob("Eye-CareU-*.log"))[-14:]  # names sort by date
    except OSError:
        return names
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        pending = None
        for line in lines:
            match = _SET_COLOR_MODE.search(line)
            if match:
                pending = match.group(1)
                continue
            match = _SET_VCP_DC.search(line)
            if match and pending:
                names[match.group(1)] = pending
                pending = None
    return names


# ---------------------------------------------------------------------------
# Win32 bindings
# ---------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

EDD_GET_DEVICE_INTERFACE_NAME = 0x1
DISPLAY_DEVICE_ACTIVE = 0x1               # flag on a monitor entry
DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x1  # flag on an adapter (output) entry
ERROR_ALREADY_EXISTS = 183
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DWMWA_EXTENDED_FRAME_BOUNDS = 9
MONITOR_DEFAULTTONEAREST = 2
GA_ROOT = 2
GW_HWNDPREV = 3
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x8
WS_EX_TRANSPARENT = 0x20
HWND_TOP = 0
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
SWP_ASYNCWINDOWPOS = 0x4000  # from another thread: post the move, never block on Tk's thread
RGN_OR = 2
WM_QUIT, WM_TIMER, WM_APP = 0x0012, 0x0113, 0x8000
PM_NOREMOVE = 0x0
# Where the panel waits while Eye-CareU is closed. Not -32000: Windows uses that
# spot for minimised windows, and Tk then stops moving the window.
OFFSCREEN = -20000

# WinEvents (SetWinEventHook): Windows tells us the moment Eye-CareU moves.
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MINIMIZEEND = 0x0017       # range FOREGROUND..MINIMIZEEND also has MINIMIZESTART
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_LOCATIONCHANGE = 0x800B    # range SHOW..LOCATIONCHANGE also has HIDE
WINEVENT_OUTOFCONTEXT = 0x0
OBJID_WINDOW = 0
CHILDID_SELF = 0


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", wintypes.WCHAR * 128),
        ("DeviceKey", wintypes.WCHAR * 128),
    ]


class PHYSICAL_MONITOR(ctypes.Structure):
    _pack_ = 1  # matches #pragma pack(1) in PhysicalMonitorEnumerationAPI.h
    _fields_ = [
        ("hPhysicalMonitor", wintypes.HANDLE),
        ("szPhysicalMonitorDescription", wintypes.WCHAR * 128),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


GammaRamp = (wintypes.WORD * 256) * 3  # R, G, B curves, 16-bit values
MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                  wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)


def _sig(func, restype, *argtypes) -> None:
    func.restype, func.argtypes = restype, list(argtypes)


# Display enumeration + gamma
_sig(user32.EnumDisplayDevicesW, wintypes.BOOL, wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(DISPLAY_DEVICEW), wintypes.DWORD)
_sig(user32.EnumDisplayMonitors, wintypes.BOOL, wintypes.HDC, ctypes.POINTER(wintypes.RECT), MONITORENUMPROC, wintypes.LPARAM)
_sig(gdi32.CreateDCW, wintypes.HDC, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p)
_sig(gdi32.DeleteDC, wintypes.BOOL, wintypes.HDC)
_sig(gdi32.GetDeviceGammaRamp, wintypes.BOOL, wintypes.HDC, ctypes.c_void_p)
_sig(gdi32.SetDeviceGammaRamp, wintypes.BOOL, wintypes.HDC, ctypes.c_void_p)
# DDC/CI. c_ubyte, not wintypes.BYTE: BYTE is signed in ctypes and 0xDC (220) would overflow.
_sig(dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR, wintypes.BOOL, wintypes.HMONITOR, ctypes.POINTER(wintypes.DWORD))
_sig(dxva2.GetPhysicalMonitorsFromHMONITOR, wintypes.BOOL, wintypes.HMONITOR, wintypes.DWORD, ctypes.POINTER(PHYSICAL_MONITOR))
_sig(dxva2.GetVCPFeatureAndVCPFeatureReply, wintypes.BOOL, wintypes.HANDLE, ctypes.c_ubyte, ctypes.c_void_p,
     ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD))
_sig(dxva2.DestroyPhysicalMonitor, wintypes.BOOL, wintypes.HANDLE)
# Windows, processes, docking
_sig(user32.EnumWindows, wintypes.BOOL, WNDENUMPROC, wintypes.LPARAM)
_sig(user32.IsWindow, wintypes.BOOL, wintypes.HWND)
_sig(user32.IsWindowVisible, wintypes.BOOL, wintypes.HWND)
_sig(user32.IsIconic, wintypes.BOOL, wintypes.HWND)
_sig(user32.GetWindowThreadProcessId, wintypes.DWORD, wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_sig(user32.GetWindowTextLengthW, ctypes.c_int, wintypes.HWND)
_sig(user32.GetWindowTextW, ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_sig(user32.GetWindowRect, wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.RECT))
_sig(user32.GetWindow, wintypes.HWND, wintypes.HWND, wintypes.UINT)
_sig(user32.GetWindowLongW, wintypes.LONG, wintypes.HWND, ctypes.c_int)
_sig(user32.GetForegroundWindow, wintypes.HWND)
_sig(user32.SetForegroundWindow, wintypes.BOOL, wintypes.HWND)
_sig(user32.GetAncestor, wintypes.HWND, wintypes.HWND, wintypes.UINT)
_sig(user32.SetWindowPos, wintypes.BOOL, wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
     ctypes.c_int, ctypes.c_int, wintypes.UINT)
_sig(user32.SetWindowRgn, ctypes.c_int, wintypes.HWND, wintypes.HRGN, wintypes.BOOL)
_sig(gdi32.CreateRoundRectRgn, wintypes.HRGN, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
     ctypes.c_int, ctypes.c_int)
_sig(gdi32.CreateRectRgn, wintypes.HRGN, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)
_sig(gdi32.CombineRgn, ctypes.c_int, wintypes.HRGN, wintypes.HRGN, wintypes.HRGN, ctypes.c_int)
_sig(gdi32.DeleteObject, wintypes.BOOL, wintypes.HGDIOBJ)
_sig(user32.MonitorFromWindow, wintypes.HMONITOR, wintypes.HWND, wintypes.DWORD)
_sig(user32.GetMonitorInfoW, wintypes.BOOL, wintypes.HMONITOR, ctypes.POINTER(MONITORINFO))
_sig(user32.SetWinEventHook, wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
     WINEVENTPROC, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD)
_sig(user32.UnhookWinEvent, wintypes.BOOL, wintypes.HANDLE)
_sig(user32.GetMessageW, wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
_sig(user32.PeekMessageW, wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
     wintypes.UINT, wintypes.UINT)
_sig(user32.PostThreadMessageW, wintypes.BOOL, wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_sig(user32.SetTimer, ctypes.c_size_t, wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p)
_sig(user32.KillTimer, wintypes.BOOL, wintypes.HWND, ctypes.c_size_t)
_sig(dwmapi.DwmGetWindowAttribute, ctypes.c_long, wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
_sig(kernel32.GetCurrentThreadId, wintypes.DWORD)
_sig(kernel32.OpenProcess, wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_sig(kernel32.QueryFullProcessImageNameW, wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
     ctypes.POINTER(wintypes.DWORD))
_sig(kernel32.CloseHandle, wintypes.BOOL, wintypes.HANDLE)
_sig(kernel32.CreateMutexW, wintypes.HANDLE, ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)


def set_dpi_awareness() -> str:
    """Work in physical pixels so our coordinates match Eye-CareU's (a Qt, DPI-aware app)."""
    try:
        _sig(user32.SetProcessDpiAwarenessContext, wintypes.BOOL, ctypes.c_void_p)
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            return "per-monitor v2"
    except (AttributeError, OSError):
        pass
    try:
        if ctypes.WinDLL("shcore").SetProcessDpiAwareness(2) == 0:  # Windows 8.1+ fallback
            return "per-monitor"
    except (AttributeError, OSError):
        pass
    return "system" if user32.SetProcessDPIAware() else "unaware"


_mutex_handle = None


def acquire_single_instance(name: str) -> bool:
    """Two copies would fight over the gamma ramp, so allow only one."""
    global _mutex_handle
    _mutex_handle = kernel32.CreateMutexW(None, False, name)
    return bool(_mutex_handle) and ctypes.get_last_error() != ERROR_ALREADY_EXISTS


# ---------------------------------------------------------------------------
# Finding the BenQ's display output (for the gamma ramp)
# ---------------------------------------------------------------------------
def _display_device() -> DISPLAY_DEVICEW:
    device = DISPLAY_DEVICEW()
    device.cb = ctypes.sizeof(DISPLAY_DEVICEW)
    return device


def find_display_source(monitor_tag: str) -> str | None:
    r"""Return the GDI output name (e.g. '\\.\DISPLAY2') the tagged monitor is on."""
    adapter_index = 0
    while True:
        adapter = _display_device()
        if not user32.EnumDisplayDevicesW(None, adapter_index, ctypes.byref(adapter), 0):
            return None
        adapter_index += 1
        if not adapter.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
            continue
        monitor_index = 0
        while True:
            monitor = _display_device()
            if not user32.EnumDisplayDevicesW(
                adapter.DeviceName, monitor_index, ctypes.byref(monitor), EDD_GET_DEVICE_INTERFACE_NAME
            ):
                break
            monitor_index += 1
            # DeviceID looks like \\?\DISPLAY#BNQ7F78#... -> "BNQ" is BenQ's EDID vendor id.
            if monitor.StateFlags & DISPLAY_DEVICE_ACTIVE and monitor_tag.upper() in monitor.DeviceID.upper():
                return adapter.DeviceName


# ---------------------------------------------------------------------------
# DDC/CI: reading the monitor's picture mode
# ---------------------------------------------------------------------------
def read_vcp(handle, code: int, retries: int = 2) -> int | None:
    current, maximum = wintypes.DWORD(), wintypes.DWORD()
    for _ in range(retries + 1):
        if dxva2.GetVCPFeatureAndVCPFeatureReply(handle, code, None, ctypes.byref(current), ctypes.byref(maximum)):
            return current.value
        time.sleep(0.1)  # monitors need a short pause between DDC/CI commands
    return None


class DdcMonitor:
    """Read-only DDC/CI access to the first physical monitor that answers `vcp_code`."""

    def __init__(self, vcp_code: int) -> None:
        self.vcp_code = vcp_code
        self.handle = None

    def _open(self) -> bool:
        candidates: list[tuple[int, str]] = []

        def on_monitor(hmonitor, _hdc, _rect, _lparam):
            count = wintypes.DWORD()
            if dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmonitor, ctypes.byref(count)) and count.value:
                monitors = (PHYSICAL_MONITOR * count.value)()
                if dxva2.GetPhysicalMonitorsFromHMONITOR(hmonitor, count.value, monitors):
                    candidates.extend((m.hPhysicalMonitor, m.szPhysicalMonitorDescription) for m in monitors)
            return True

        callback = MONITORENUMPROC(on_monitor)  # keep a reference for the duration of the call
        user32.EnumDisplayMonitors(None, None, callback, 0)

        # In Duplicate mode the laptop panel and the BenQ share one HMONITOR.
        # The laptop panel has no DDC/CI, so "the one that answers" is the BenQ.
        candidates.sort(key=lambda c: "benq" not in c[1].lower())
        for handle, description in candidates:
            if self.handle is None and read_vcp(handle, self.vcp_code) is not None:
                self.handle = handle
                log.info("DDC/CI monitor found: %s", description)
            else:
                dxva2.DestroyPhysicalMonitor(handle)
        return self.handle is not None

    def read(self) -> int | None:
        if self.handle is None and not self._open():
            return None
        value = read_vcp(self.handle, self.vcp_code)
        if value is None:
            self.close()  # stale handle (replug, sleep): re-enumerate on the next read
        return value

    def close(self) -> None:
        if self.handle is not None:
            dxva2.DestroyPhysicalMonitor(self.handle)
            self.handle = None


# ---------------------------------------------------------------------------
# Gamma ramp
# ---------------------------------------------------------------------------
def build_ramp(profile: Profile) -> GammaRamp:
    """256-entry curve per channel: contrast and brightness first, then gamma."""
    ramp = GammaRamp()
    gamma = max(profile.gamma, 0.1)
    for i in range(256):
        level = (i / 255 - 0.5) * profile.contrast + 0.5 + profile.brightness
        level = min(max(level, 0.0), 1.0) ** (1.0 / gamma)
        value = round(level * 65535)
        for channel in ramp:
            channel[i] = value
    return ramp


IDENTITY_RAMP = build_ramp(DEFAULT_PROFILE)


def ramps_match(a: GammaRamp, b: GammaRamp, tolerance: int = 512) -> bool:
    # Drivers quantise the ramp (often to 10 bits), so compare with some slack.
    return all(abs(a[c][i] - b[c][i]) <= tolerance for c in range(3) for i in range(256))


class DisplayGamma:
    """Get/set the GPU gamma ramp of one display output."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.last_ok = True

    def _call(self, func, ramp: GammaRamp) -> bool:
        hdc = gdi32.CreateDCW(self.source, None, None, None)
        if not hdc:
            return False
        try:
            return bool(func(hdc, ramp))
        finally:
            gdi32.DeleteDC(hdc)

    def get(self) -> GammaRamp | None:
        ramp = GammaRamp()
        return ramp if self._call(gdi32.GetDeviceGammaRamp, ramp) else None

    def set(self, ramp: GammaRamp) -> bool:
        ok = self._call(gdi32.SetDeviceGammaRamp, ramp)
        if not ok and self.last_ok:  # log once per failure streak
            log.warning(
                "Windows rejected the curve on %s. Extreme curves (big brightness offsets) are "
                "refused; reduce them, or set DWORD GdiIcmGammaRange=256 under "
                r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ICM and sign out/in.",
                self.source,
            )
        self.last_ok = ok
        return ok


# ---------------------------------------------------------------------------
# Engine: reads the monitor mode and applies that mode's setting (own thread)
# ---------------------------------------------------------------------------
@dataclass
class EngineStatus:
    state: str = "starting"   # starting | no_monitor | ddc_error | custom | default
    mode: int | None = None   # last mode value read from the monitor
    name: str = ""            # its display name ("Cinema", or "Mod 123" if unknown)
    curve_ok: bool = True     # False when Windows rejected the curve


class ModeEngine:
    """The only code that talks DDC/CI and writes the gamma ramp.

    It runs on its own thread so slow DDC/CI reads never freeze the panel. The
    panel changes settings only through the thread-safe methods below and reads
    `snapshot()`; it never touches the hardware itself.
    """

    NAME_LOOKUP_INTERVAL_S = 30.0  # how often an unknown mode re-checks Eye-CareU's log
    LOG_CHECK_OPEN_S = 0.15        # Eye-CareU open: notice its mode switches within ~0.2 s
    LOG_CHECK_CLOSED_S = 2.0       # closed: it rarely switches modes from the tray

    def __init__(self, config: Config) -> None:
        self.config = config
        self.lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._settings_changed = False
        self._eyecare_open = True  # poll quickly until the panel reports otherwise
        self._status = EngineStatus()
        self._thread = threading.Thread(target=self._run, name="mode-engine", daemon=True)
        # Worker-thread state
        self.ddc = DdcMonitor(config.vcp_code)
        self.gamma: DisplayGamma | None = None
        self.baseline: GammaRamp | None = None  # curve that was on screen before ours
        self.active = False                     # one of our curves is on screen
        self._logged_mode: int | None = None
        self._next_name_lookup = 0.0
        self.log_watch = EyeCareLogWatcher()
        self._next_ddc = 0.0  # DDC/CI is only the fallback (mode changed on the monitor itself)

    # ---- thread-safe API (used by the panel) -----------------------------
    def start(self) -> None:
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def stop(self) -> None:
        """Stop the worker and put the original curve back. Safe to call twice."""
        if self._stopping.is_set():
            return
        self._stopping.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
            if self._thread.is_alive():  # stuck in a DDC call: restore from here instead
                self._restore()

    def snapshot(self) -> EngineStatus:
        with self.lock:
            return replace(self._status)

    def profile_for(self, mode: int) -> Profile:
        with self.lock:
            return replace(self.config.profiles.get(str(mode), DEFAULT_PROFILE))

    def set_profile(self, mode: int, profile: Profile) -> None:
        """Store a mode's setting (the default removes it) and apply it right away."""
        with self.lock:
            if profile.is_default():
                self.config.profiles.pop(str(mode), None)
            else:
                self.config.profiles[str(mode)] = replace(profile)
            self._settings_changed = True
        self._wake.set()

    def reset_all(self) -> None:
        with self.lock:
            self.config.profiles.clear()
            self._settings_changed = True
            self.config.save()
        self._wake.set()

    def save_config(self) -> None:
        with self.lock:
            self.config.save()

    def set_eyecare_open(self, is_open: bool) -> None:
        """Check often only while Eye-CareU is open; sleep long while it is closed."""
        if is_open != self._eyecare_open:
            self._eyecare_open = is_open
            if is_open:
                self._next_ddc = 0.0  # the mode may have changed on the monitor meanwhile
                self._wake.set()

    # ---- worker thread -----------------------------------------------------
    def _run(self) -> None:
        log.info("engine started")
        try:
            self._learn_names()
            while not self._stopping.is_set():
                self._wake.clear()  # clear first, so a wake during _step isn't lost
                try:
                    self._step()
                except Exception:  # the worker must never die silently
                    log.exception("engine step failed")
                self._wake.wait(self.LOG_CHECK_OPEN_S if self._eyecare_open else self.LOG_CHECK_CLOSED_S)
        finally:
            self._restore()
            self.ddc.close()
            log.info("engine stopped")

    def _set_status(self, **changes) -> None:
        with self.lock:
            for key, value in changes.items():
                setattr(self._status, key, value)

    def _restore(self) -> None:
        if self.active and self.gamma is not None:
            self.gamma.set(self.baseline or IDENTITY_RAMP)
        self.active = False

    def _learn_names(self) -> None:
        learned = learn_mode_names()
        with self.lock:
            new = {k: v for k, v in learned.items() if self.config.mode_names.get(k) != v}
            if new:
                self.config.mode_names.update(new)
                self.config.save()
        if new:
            log.info("mode names learned from Eye-CareU's log: %s", new)

    def _name(self, mode: int) -> str:
        with self.lock:
            name = self.config.mode_names.get(str(mode))
        if name is None and time.monotonic() >= self._next_name_lookup:
            self._next_name_lookup = time.monotonic() + self.NAME_LOOKUP_INTERVAL_S
            self._learn_names()  # Eye-CareU logs the name when it switches to a mode
            with self.lock:
                name = self.config.mode_names.get(str(mode))
        return name or f"Mod {mode}"

    def _remember_name(self, mode: int, name: str) -> None:
        with self.lock:
            if self.config.mode_names.get(str(mode)) == name:
                return
            self.config.mode_names[str(mode)] = name
            self.config.save()
        log.info("mode %s is called %r (from Eye-CareU's log)", mode, name)

    def _step(self) -> None:
        with self.lock:
            monitor_tag = self.config.monitor_tag
            self.ddc.vcp_code = self.config.vcp_code
            changed, self._settings_changed = self._settings_changed, False
            known_mode = self._status.mode
            ddc_interval = self.config.ddc_check_open_s if self._eyecare_open else self.config.ddc_check_closed_s

        now = time.monotonic()
        switched = self.log_watch.poll()  # Eye-CareU switched the mode: we know it instantly
        ddc_due = now >= self._next_ddc
        if switched is None and not changed and not ddc_due:
            return  # nothing to do: no hardware access at all

        source = find_display_source(monitor_tag)
        if source is None:
            self._set_status(state="no_monitor", mode=None, name="")
            return
        if self.gamma is None or self.gamma.source != source:
            # First run, or the cable was replugged: Windows has reset that output's ramp.
            self.gamma, self.active = DisplayGamma(source), False

        if switched is not None:
            mode, name = switched
            if name:
                self._remember_name(mode, name)
            self._next_ddc = now + ddc_interval
        elif changed and known_mode is not None:
            mode = known_mode  # a setting changed on the panel: apply it now, no DDC round trip
        else:
            self._next_ddc = now + ddc_interval
            mode = self.ddc.read()  # fallback: the mode may have been changed on the monitor
            if mode is None:
                self._set_status(state="ddc_error")  # keep the current curve, retry next time
                return
        self._apply(mode)

    def _apply(self, mode: int) -> None:
        name = self._name(mode)
        with self.lock:
            profile = self.config.profiles.get(str(mode))
            saved_ramps = [build_ramp(p) for p in self.config.profiles.values()]

        if profile is None:
            self._restore()  # this mode uses the default: original curve
            state, ok = "default", True
        else:
            target = build_ramp(profile)
            current = self.gamma.get()
            if not self.active:
                # A curve equal to one of our settings is left over from a previous
                # run that was killed mid-way, so it is not the real original.
                stale = current is None or any(ramps_match(current, ramp) for ramp in saved_ramps)
                self.baseline = IDENTITY_RAMP if stale else current
                self.active = self.gamma.set(target)
            elif current is None or not ramps_match(current, target):
                self.gamma.set(target)  # setting changed, or a driver / Night light reset it
            state, ok = "custom", self.gamma.last_ok

        if mode != self._logged_mode:
            self._logged_mode = mode
            log.info("mode %s (%s): %s", mode, name, f"custom {profile}" if profile else "default")
        self._set_status(state=state, mode=mode, name=name, curve_ok=ok)


# ---------------------------------------------------------------------------
# Finding and following the Eye-CareU window
# ---------------------------------------------------------------------------
_process_paths: dict[int, str] = {}


def process_path(pid: int) -> str:
    """Full exe path of a process ('' if it can't be opened). Cached per pid."""
    path = _process_paths.get(pid)
    if path is not None:
        return path
    path = ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                path = buffer.value
        finally:
            kernel32.CloseHandle(handle)
    if len(_process_paths) > 512:  # pids get reused: keep the cache small
        _process_paths.clear()
    _process_paths[pid] = path
    return path


def window_title(hwnd) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def visible_rect(hwnd) -> tuple[int, int, int, int]:
    """Window bounds without the invisible resize border Windows adds around some windows."""
    rect = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def _monitor_info(hwnd) -> MONITORINFO:
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST), ctypes.byref(info))
    return info


def work_area(hwnd) -> tuple[int, int, int, int]:
    """Usable area (without the taskbar) of the screen the window is on."""
    r = _monitor_info(hwnd).rcWork
    return r.left, r.top, r.right, r.bottom


def is_overlay(hwnd, rect: tuple[int, int, int, int]) -> bool:
    """Click-through or full-screen windows (software dimming, break-reminder
    screens) belong to Eye-CareU too, but are never its main window."""
    if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TRANSPARENT:
        return True
    screen = _monitor_info(hwnd).rcMonitor
    left, top, right, bottom = rect
    return right - left >= screen.right - screen.left and bottom - top >= screen.bottom - screen.top


MIN_MAIN_WINDOW_HEIGHT = 250  # Eye-CareU's main window is ~430 px; tooltips/toasts are far smaller


def is_main_window(hwnd, rect: tuple[int, int, int, int]) -> bool:
    left, top, right, bottom = rect
    return bottom - top >= MIN_MAIN_WINDOW_HEIGHT and not is_overlay(hwnd, rect)


def find_eyecare_window(match: str):
    """Eye-CareU's main window, preferring a visible one. Hidden windows count too,
    so the panel is already hooked up when Eye-CareU is opened from the tray."""
    needle = match.lower()
    own_pid = os.getpid()
    best, best_key = None, (False, 0)

    def on_window(hwnd, _lparam):
        nonlocal best, best_key
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == own_pid:
            return True
        path = process_path(pid.value).lower()
        # Match the exe path; use the title only when the path is unreadable, so a
        # browser tab titled "Eye-CareU" never gets mistaken for the app.
        if not ((needle in path) if path else (needle in window_title(hwnd).lower())):
            return True
        rect = visible_rect(hwnd)
        if not is_main_window(hwnd, rect):
            return True  # tooltip / notification / full-screen overlay
        left, top, right, bottom = rect
        key = (bool(user32.IsWindowVisible(hwnd)), (right - left) * (bottom - top))
        if key > best_key:
            best, best_key = hwnd, key
        return True

    callback = WNDENUMPROC(on_window)
    user32.EnumWindows(callback, 0)
    return best


def dock_position(window: tuple[int, int, int, int], work: tuple[int, int, int, int],
                  width: int, min_height: int, overlap: int) -> tuple[int, int, int, int, str]:
    """(x, y, w, h, side) for the panel. On the right it slides `overlap` pixels
    under Eye-CareU's rounded corners so the two read as one window; if the
    screen has no room there it goes on the left. Same height as Eye-CareU."""
    left, top, right, bottom = window
    work_left, work_top, work_right, work_bottom = work
    height = max(bottom - top, min_height)
    y = max(work_top, min(top, work_bottom - height))
    if right + width <= work_right:
        return right - overlap, y, width + overlap, height, "right"
    return left - width, y, width, height, "left"


# ---------------------------------------------------------------------------
# Instant following: WinEvent hook on its own message-loop thread
# ---------------------------------------------------------------------------
class EyeCareFollower(threading.Thread):
    """Sleeps until Eye-CareU moves, shows, hides, minimises or comes to the front.

    The hook lives on a dedicated thread with a plain GetMessage loop, which is
    where Windows delivers such events immediately; nothing runs while
    Eye-CareU stands still. After every move it stays in a tight-follow mode
    (~60 checks a second) for BURST_SECONDS, then goes back to sleep.
    """

    WM_REHOOK = WM_APP + 1
    RANGES = ((EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_MINIMIZEEND),
              (EVENT_OBJECT_SHOW, EVENT_OBJECT_LOCATIONCHANGE))
    BURST_SECONDS = 10.0
    BURST_INTERVAL_MS = 15

    def __init__(self, on_event, on_burst) -> None:
        super().__init__(name="eyecare-follower", daemon=True)
        self._on_event = on_event  # (event, hwnd) -> handled?  Runs on this thread: Win32 only
        self._on_burst = on_burst  # () -> None                  Runs on this thread: Win32 only
        self._proc = WINEVENTPROC(self._callback)  # must stay referenced while hooked
        self._thread_id = 0
        self._ready = threading.Event()
        self._pid: int | None = None
        self._timer = 0
        self._burst_until = 0.0

    def run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)  # creates the message queue
        self._ready.set()
        hooks: list = []
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:  # 0 = WM_QUIT, -1 = error
                if msg.message == self.WM_REHOOK:
                    for hook in hooks:
                        user32.UnhookWinEvent(hook)
                    pid = msg.wParam
                    hooks = [hook for hook in (
                        user32.SetWinEventHook(first, last, None, self._proc, pid, 0, WINEVENT_OUTOFCONTEXT)
                        for first, last in self.RANGES) if hook] if pid else []
                    if pid:
                        log.info("instant following: %s", "on" if hooks else "unavailable, using the slow check")
                elif msg.message == WM_TIMER:
                    if time.monotonic() < self._burst_until:
                        self._safely(self._on_burst)
                    else:
                        self._stop_burst()  # 10 s without a move: back to sleep
        finally:
            self._stop_burst()
            for hook in hooks:
                user32.UnhookWinEvent(hook)

    def follow(self, pid: int | None) -> None:
        """Watch this Eye-CareU process (None: stop watching). Any thread."""
        if pid != self._pid and self._ready.wait(2):
            self._pid = pid
            user32.PostThreadMessageW(self._thread_id, self.WM_REHOOK, pid or 0, 0)

    def stop(self) -> None:
        if self._ready.is_set():
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    def _callback(self, _hook, event, hwnd, id_object, id_child, _thread, _time) -> None:
        if id_object != OBJID_WINDOW or id_child != CHILDID_SELF:
            return  # a control inside a window, not a window
        handled = self._safely(self._on_event, event, hwnd)
        if handled and event == EVENT_OBJECT_LOCATIONCHANGE:
            self._burst_until = time.monotonic() + self.BURST_SECONDS
            if not self._timer:
                self._timer = user32.SetTimer(None, 0, self.BURST_INTERVAL_MS, None)

    def _stop_burst(self) -> None:
        if self._timer:
            user32.KillTimer(None, self._timer)
            self._timer = 0

    @staticmethod
    def _safely(func, *args):
        try:
            return func(*args)
        except Exception:  # never let an error escape into Windows' callback machinery
            log.exception("window following failed")
            return False


# ---------------------------------------------------------------------------
# Docked panel (Tk, main thread)
# ---------------------------------------------------------------------------
SLIDERS = (
    ("gamma", "Gamma", 0.70, 1.60),
    ("brightness", "Parlaklık", -0.30, 0.30),
    ("contrast", "Kontrast", 0.50, 1.50),
)


class DockedPanel:
    WIDTH = 270              # visible width, physical pixels
    OVERLAP = 14             # tucked under Eye-CareU's rounded corners (= its corner radius)
    RADIUS = 14              # our own outer corners, same as Eye-CareU's
    HEADER_H, TABS_H = 77, 50  # Eye-CareU's right-pane colour bands (100 % display scaling)
    TOP_INSET = 1            # Eye-CareU's first painted row is 1 px below its frame bounds
    TAB_W, TAB_H = 30, 44    # the folded panel: a small tab with an arrow, visible part
    TAB_TOP = 12             # its distance from Eye-CareU's top edge
    TAB_RADIUS = 10
    TICK_OPEN_MS = 150       # status refresh while the panel is showing (mode switches show at once)
    TICK_CLOSED_MS = 2000    # while it is parked (Eye-CareU closed)
    FOLLOW_CHECK_S = 0.5     # safety-net re-check of Eye-CareU's window (moves come as WinEvents)
    SEARCH_INTERVAL_S = 2.0  # look for Eye-CareU's window while we don't have it
    COLORS = {               # sampled from Eye-CareU's own window
        "header": "#112639", "tabs": "#08131B", "content": "#02090F", "line": "#142B44",
        "text": "#E6EDF5", "label": "#B8C4CF", "tab": "#6DABBF", "muted": "#5F7A90",
        "accent": "#24BAB1", "accent_hover": "#3BD3C9", "button": "#205281",
        "button_hover": "#2A68A3", "warn": "#F0A340",
    }

    def __init__(self, engine: ModeEngine, config: Config) -> None:
        import tkinter as tk  # imported here so --no-panel works even without Tk
        from tkinter import ttk

        self.tk, self.ttk = tk, ttk
        self.engine = engine
        self.config = config
        self.eyecare = None       # HWND of the Eye-CareU main window (may be hidden)
        self.mode: int | None = None  # the mode whose setting the sliders show
        self.visible = False
        self.collapsed = config.panel_collapsed  # folded into the arrow tab
        self._geometry: tuple[int, int, int, int] | None = None  # last (x, y, w, h) we set
        self._shape: tuple[int, int, str] | None = None         # last (w, h, side) region
        self._side = "right"
        self._padding_side: str | None = None
        self._sync_lock = threading.Lock()  # the tick and the follower thread both move the panel
        self._next_search = 0.0
        self._next_follow = 0.0
        self._flash_until = 0.0
        self._save_job = None
        self._sliders_enabled = True

        self.root = root = tk.Tk()
        # Under pythonw there is no console, so Tk would swallow callback errors silently.
        root.report_callback_exception = lambda *exc: log.error("panel callback failed", exc_info=exc)
        root.title("Cinema Gamma")
        root.overrideredirect(True)  # no title bar: it reads as part of Eye-CareU
        self._build()
        self._update_padding()

        # The window is created off-screen and stays mapped for its whole life;
        # "hiding" means parking it off-screen again. After creation it is moved
        # only with SetWindowPos (see _place), never through Tk's geometry code.
        root.update_idletasks()
        self.min_height = self.full_view.winfo_reqheight()  # also right when starting folded
        previous_foreground = user32.GetForegroundWindow()
        width = self.WIDTH + self.OVERLAP
        root.geometry(f"{width}x{self.min_height}+{OFFSCREEN}+{OFFSCREEN}")
        root.update()
        self._geometry = (OFFSCREEN, OFFSCREEN, width, self.min_height)
        # Tk's top-level wrapper window. Cached as a plain int because the follower
        # thread must not call into tkinter; refreshed on every tick.
        self._hwnd = user32.GetAncestor(root.winfo_id(), GA_ROOT)
        if previous_foreground:
            user32.SetForegroundWindow(previous_foreground)  # mapping a Tk window activates it
        self._set_sliders_enabled(False)  # until the monitor's mode is known

        self.follower = EyeCareFollower(self._on_win_event, self._on_burst)
        self.follower.start()

    # ---- layout ------------------------------------------------------------
    def _build(self) -> None:
        tk, ttk, c, root = self.tk, self.ttk, self.COLORS, self.root
        root.configure(bg=c["header"])

        # Folded state: only a small tab with an arrow, stuck to Eye-CareU's edge.
        self.tab_view = tk.Canvas(root, bg=c["header"], highlightthickness=0, bd=0, cursor="hand2")
        self._bind_click(self.tab_view, self.expand)

        # Open state: three bands lined up with Eye-CareU's: header, tab row, content.
        self.full_view = full = tk.Frame(root, bg=c["content"])
        header = tk.Frame(full, bg=c["header"], height=self.HEADER_H)
        header.pack(fill="x")
        header.pack_propagate(False)
        header_in = tk.Frame(header, bg=c["header"])
        header_in.pack(fill="both", expand=True)
        self.fold_button = tk.Canvas(header_in, width=22, height=22, bg=c["header"], highlightthickness=0,
                                     bd=0, cursor="hand2")
        self.fold_button.place(relx=1.0, x=6, y=8, anchor="ne")
        self._bind_click(self.fold_button, self.collapse)
        tk.Label(header_in, text="Mod Ayarları", bg=c["header"], fg=c["text"],  # on Eye-CareU's "Settings" line
                 font=("Segoe UI Semibold", 11)).place(x=0, rely=1.0, y=-6, anchor="sw")

        tabs = tk.Frame(full, bg=c["tabs"], height=self.TABS_H)
        tabs.pack(fill="x")
        tabs.pack_propagate(False)
        tabs_in = tk.Frame(tabs, bg=c["tabs"])
        tabs_in.pack(fill="both", expand=True)
        self.status = tk.StringVar(value="Monitör okunuyor…")
        self.status_label = tk.Label(tabs_in, textvariable=self.status, bg=c["tabs"], fg=c["tab"],
                                     font=("Segoe UI", 10), anchor="w", justify="left",
                                     wraplength=self.WIDTH - 32)
        self.status_label.place(x=0, rely=0.5, anchor="w")

        body = tk.Frame(full, bg=c["content"])
        body.pack(fill="both", expand=True)

        style = ttk.Style(root)
        style.theme_use("clam")  # lets the slider knob have its own colour
        style.configure("Panel.Horizontal.TScale", background=c["accent"], troughcolor=c["line"],
                        bordercolor=c["content"], lightcolor=c["accent"], darkcolor=c["accent"],
                        gripcount=0)
        style.map("Panel.Horizontal.TScale",
                  background=[("disabled", c["line"]), ("active", c["accent_hover"])])

        self.slider_vars: dict[str, object] = {}
        self.value_labels: dict[str, object] = {}
        self.scales: list = []
        for index, (key, label, low, high) in enumerate(SLIDERS):
            row = tk.Frame(body, bg=c["content"])
            row.pack(fill="x", pady=(12 if index == 0 else 9, 0))
            tk.Label(row, text=label, bg=c["content"], fg=c["label"], font=("Segoe UI", 10)).pack(side="left")
            self.value_labels[key] = tk.Label(row, bg=c["content"], fg=c["text"], font=("Segoe UI", 10))
            self.value_labels[key].pack(side="right")
            var = tk.DoubleVar(value=getattr(DEFAULT_PROFILE, key))
            self.slider_vars[key] = var
            scale = ttk.Scale(body, from_=low, to=high, orient="horizontal", variable=var, takefocus=False,
                              style="Panel.Horizontal.TScale", command=self._on_slider)
            scale.pack(fill="x", pady=(6, 9))
            self.scales.append(scale)
            tk.Frame(body, bg=c["line"], height=1).pack(fill="x")  # row separator, like Eye-CareU's
        self._sync_value_labels()

        self._button(body, "Bu modu sıfırla", self.reset, bg=c["button"], fg=c["text"],
                     hover=c["button_hover"], pady=7).pack(fill="x", pady=(14, 6))
        self._button(body, "Tüm modları sıfırla", self.reset_all, bg=c["content"], fg=c["muted"],
                     hover=c["content"], hover_fg=c["text"], font=("Segoe UI", 9), padx=0, pady=0).pack(anchor="w")
        tk.Label(body, text="Her mod kendi ayarını hatırlar.", bg=c["content"], fg=c["muted"],
                 font=("Segoe UI", 9), anchor="w").pack(fill="x", pady=(6, 0))

        self._padded = (header_in, tabs_in, body)
        self._show_view()

    def _button(self, parent, text: str, command, *, bg: str, fg: str, hover: str, hover_fg: str | None = None,
                font=("Segoe UI Semibold", 9), padx: int = 10, pady: int = 6):
        """A flat label that acts as a button: it fires when the mouse button is
        released over it. tk.Button also needs a prior <Enter> event, which
        injected clicks (accessibility / automation tools) don't always produce."""
        hover_fg = hover_fg or fg
        button = self.tk.Label(parent, text=text, bg=bg, fg=fg, font=font, padx=padx, pady=pady, cursor="hand2")

        def on_release(event) -> None:
            inside = 0 <= event.x < button.winfo_width() and 0 <= event.y < button.winfo_height()
            button.configure(bg=hover if inside else bg, fg=hover_fg if inside else fg)
            if inside:
                command()

        button.bind("<Enter>", lambda _event: button.configure(bg=hover, fg=hover_fg))
        button.bind("<Leave>", lambda _event: button.configure(bg=bg, fg=fg))
        button.bind("<ButtonRelease-1>", on_release)
        return button

    def _bind_click(self, canvas, command) -> None:
        """Fire on mouse-button release over the canvas (works for injected clicks too); the
        arrow lights up on hover."""
        c = self.COLORS

        def recolor(color: str) -> None:
            for item in canvas.find_all():
                canvas.itemconfigure(item, fill=color)

        def on_release(event) -> None:
            if 0 <= event.x < canvas.winfo_width() and 0 <= event.y < canvas.winfo_height():
                command()

        canvas.bind("<Enter>", lambda _event: recolor(c["accent_hover"]))
        canvas.bind("<Leave>", lambda _event: recolor(c["accent"]))
        canvas.bind("<ButtonRelease-1>", on_release)

    def _draw_arrow(self, canvas, cx: float, cy: float, pointing: str) -> None:
        """A chevron like Eye-CareU's own "<" button, pointing left or right."""
        canvas.delete("all")
        dx = 4 if pointing == "right" else -4
        canvas.create_line(cx - dx, cy - 7, cx + dx, cy, cx - dx, cy + 7, width=2,
                           fill=self.COLORS["accent"], capstyle="round", joinstyle="round")

    def _redraw_arrows(self) -> None:
        """Open: the arrow points back towards Eye-CareU (fold). Folded: it points outwards (open)."""
        towards_eyecare = "left" if self._side == "right" else "right"
        away = "right" if self._side == "right" else "left"
        self._draw_arrow(self.fold_button, 11, 11, towards_eyecare)
        tab_x = self.OVERLAP + self.TAB_W / 2 if self._side == "right" else self.TAB_W / 2
        self._draw_arrow(self.tab_view, tab_x, self.TAB_H / 2, away)

    def _show_view(self) -> None:
        if self.collapsed:
            self.full_view.pack_forget()
            self.tab_view.pack(fill="both", expand=True)
        else:
            self.tab_view.pack_forget()
            self.full_view.pack(fill="both", expand=True)

    def _update_padding(self) -> None:
        """Keep content clear of the strip that sits under Eye-CareU (Tk thread only)."""
        if self._side != self._padding_side:
            self._padding_side = self._side
            pad = (self.OVERLAP + 16, 16) if self._side == "right" else (16, 16)
            for frame in self._padded:
                frame.pack_configure(padx=pad)
            self._redraw_arrows()

    # ---- user actions -------------------------------------------------------
    def _profile_from_sliders(self) -> Profile:
        return Profile(**{key: round(var.get(), 2) for key, var in self.slider_vars.items()})

    def _sync_value_labels(self) -> None:
        for key, var in self.slider_vars.items():
            self.value_labels[key].configure(text=f"{var.get():.2f}")

    def _load_sliders(self, profile: Profile) -> None:
        for key, var in self.slider_vars.items():
            var.set(getattr(profile, key))  # setting the variable doesn't fire the command
        self._sync_value_labels()

    def _set_sliders_enabled(self, enabled: bool) -> None:
        if enabled != self._sliders_enabled:
            self._sliders_enabled = enabled
            for scale in self.scales:
                scale.state(["!disabled"] if enabled else ["disabled"])

    def _on_slider(self, _value=None) -> None:
        self._sync_value_labels()
        if self.mode is None:
            return
        self.engine.set_profile(self.mode, self._profile_from_sliders())  # applied right away
        self._schedule_save()

    def _schedule_save(self) -> None:
        if self._save_job is not None:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(700, self._save)  # debounce disk writes while dragging

    def _save(self) -> None:
        self._save_job = None
        self.engine.save_config()

    def reset(self) -> None:
        if self.mode is None:
            self._flash("Monitörün modu henüz okunamadı.", "warn")
            return
        self._load_sliders(DEFAULT_PROFILE)
        self.engine.set_profile(self.mode, DEFAULT_PROFILE)
        self._schedule_save()
        self._flash(f"{self.engine.snapshot().name or 'Bu mod'} varsayılana döndü.", "accent")
        log.info("mode %s reset to default", self.mode)

    def reset_all(self) -> None:
        self.engine.reset_all()
        self._load_sliders(DEFAULT_PROFILE)
        self._flash("Tüm modlar varsayılana döndü.", "accent")
        log.info("all modes reset to default")

    def collapse(self) -> None:
        self._set_collapsed(True)

    def expand(self) -> None:
        self._set_collapsed(False)

    def _set_collapsed(self, collapsed: bool) -> None:
        if collapsed == self.collapsed:
            return
        self.collapsed = collapsed
        self._show_view()
        self._sync(self.eyecare)  # resize now, not at the next tick
        self.config.panel_collapsed = collapsed
        self._schedule_save()     # remembered across restarts

    def quit(self) -> None:
        if self._save_job is not None:
            self.root.after_cancel(self._save_job)
            self._save()
        self.follower.stop()
        self.engine.stop()  # puts the original curve back
        self.root.destroy()

    def _flash(self, text: str, color: str) -> None:
        self.status.set(text)
        self.status_label.configure(fg=self.COLORS[color])
        self._flash_until = time.monotonic() + 2.5

    # ---- periodic work (Tk thread) -----------------------------------------------
    def run(self) -> None:
        self.root.after(0, self._tick)
        self.root.mainloop()

    def _tick(self) -> None:
        global restart_requested
        delay = self.TICK_OPEN_MS
        try:
            if stop_requested():
                log.info("stop requested (Durdur.cmd)")
                self.quit()
                return
            if take_flag(RESTART_PATH):
                log.info("restart requested (updated program)")
                restart_requested = True
                self.quit()
                return
            now = time.monotonic()
            if now >= self._next_follow:  # moves arrive as WinEvents; this is only a safety net
                self._next_follow = now + self.FOLLOW_CHECK_S
                self._follow_eyecare()
                self._update_padding()
            self._refresh_status()
            if not self.visible:
                delay = self.TICK_CLOSED_MS
        except Exception:
            log.exception("panel update failed")
        self.root.after(delay, self._tick)

    def _follow_eyecare(self) -> None:
        """Find Eye-CareU and hook it; also a slow safety net for the follower."""
        hwnd_now = user32.GetAncestor(self.root.winfo_id(), GA_ROOT)
        if hwnd_now != self._hwnd:  # Tk recreated its wrapper window: re-apply the shape
            self._hwnd, self._shape = hwnd_now, None
        hwnd = self.eyecare
        if hwnd and not user32.IsWindow(hwnd):
            hwnd = self.eyecare = None  # Eye-CareU exited
            self.follower.follow(None)
        now = time.monotonic()
        if (hwnd is None or not user32.IsWindowVisible(hwnd)) and now >= self._next_search:
            self._next_search = now + self.SEARCH_INTERVAL_S
            found = find_eyecare_window(self.config.eyecare_match)
            if found and found != hwnd:
                hwnd = self.eyecare = found
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(found, ctypes.byref(pid))
                self.follower.follow(pid.value)
                log.info("Eye-CareU window found: %r (%s)", window_title(found),
                         "visible" if user32.IsWindowVisible(found) else "in the tray")
        self._sync(hwnd)
        if self.visible and user32.GetForegroundWindow() == hwnd:
            self._stack_above(hwnd)  # come forward together with Eye-CareU

    def _refresh_status(self) -> None:
        s = self.engine.snapshot()
        if s.mode is not None and s.mode != self.mode:
            self.mode = s.mode
            self._load_sliders(self.engine.profile_for(s.mode))  # each mode shows its own setting
        self._set_sliders_enabled(self.mode is not None and s.state in ("custom", "default"))
        if time.monotonic() < self._flash_until:
            return
        messages = {
            "starting": ("Monitör okunuyor…", "tab"),
            "no_monitor": ("BenQ monitör bulunamadı.", "warn"),
            "ddc_error": ("Monitör modu okunamıyor (DDC/CI).", "warn"),
            "custom": (f"●  {s.name}  ·  özel ayar", "accent"),
            "default": (f"○  {s.name}  ·  varsayılan", "tab"),
        }
        text, color = messages.get(s.state, messages["starting"])
        if s.state == "custom" and not s.curve_ok:
            text, color = "Windows bu ayarı reddetti; parlaklığı azalt.", "warn"
        self.status.set(text)
        self.status_label.configure(fg=self.COLORS[color])

    # ---- following: everything below runs on the Tk thread AND on the follower
    # thread (inside WinEvent callbacks), so it is plain Win32, never tkinter.
    def _on_win_event(self, event: int, hwnd) -> bool:
        if hwnd != self.eyecare and not (event == EVENT_OBJECT_SHOW and self._adopt(hwnd)):
            return False  # another Eye-CareU window (popup, tooltip, overlay)
        self._sync(hwnd)
        if event == EVENT_SYSTEM_FOREGROUND and self.visible:
            self._stack_above(hwnd)
        return True

    def _on_burst(self) -> None:
        self._sync(self.eyecare)

    def _adopt(self, hwnd) -> bool:
        """A newly shown Eye-CareU window becomes 'the' window if ours isn't showing."""
        current = self.eyecare
        if current and user32.IsWindow(current) and user32.IsWindowVisible(current):
            return False
        if not is_main_window(hwnd, visible_rect(hwnd)):
            return False
        self.eyecare = hwnd
        log.info("Eye-CareU window shown: %r", window_title(hwnd))
        return True

    def _sync(self, hwnd) -> None:
        with self._sync_lock:
            shape = self._sync_locked(hwnd)
        if shape is not None:
            self._apply_shape(*shape)  # outside the lock: it may wait for Tk's thread

    def _sync_locked(self, hwnd) -> tuple[int, int, str] | None:
        showing = bool(hwnd) and bool(user32.IsWindowVisible(hwnd)) and not user32.IsIconic(hwnd)
        self.engine.set_eyecare_open(showing)
        if not showing:
            self._park()
            return None
        left, top, right, bottom = visible_rect(hwnd)
        window = (left, top + self.TOP_INSET, right, bottom)
        x, y, w, h, side = dock_position(window, work_area(hwnd), self.WIDTH, self.min_height, self.OVERLAP)
        self._side = side
        if self.collapsed:  # just the arrow tab, on the same side the panel would open
            y, h = window[1] + self.TAB_TOP, self.TAB_H
            x, w = (right - self.OVERLAP, self.OVERLAP + self.TAB_W) if side == "right" else (left - self.TAB_W, self.TAB_W)
        self._place(x, y, w, h)
        if not self.visible:
            self.visible = True
            log.info("panel docked at %d,%d %dx%d on the %s of Eye-CareU %s", x, y, w, h, side, window)
            self._stack_above(hwnd)
        if (w, h, side) == self._shape:
            return None
        self._shape = (w, h, side)
        return self._shape

    def _place(self, x: int, y: int, w: int, h: int) -> None:
        """Move/resize with Win32 directly. Tk adopts external moves (like a user
        drag) whatever state it believes the window is in, and the async flag
        means the follower thread never waits for Tk's thread."""
        if (x, y, w, h) != self._geometry:  # only touch the window when something changed
            user32.SetWindowPos(self._hwnd, HWND_TOP, x, y, w, h,
                                SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS)
            self._geometry = (x, y, w, h)

    def _park(self) -> None:
        self._place(OFFSCREEN, OFFSCREEN, self.WIDTH + self.OVERLAP, self.min_height)
        self.visible = False

    def _stack_above(self, target) -> None:
        """Put the panel directly above Eye-CareU in the z-order without activating it."""
        above = user32.GetWindow(target, GW_HWNDPREV)
        if above == self._hwnd:
            return
        if not above or user32.GetWindowLongW(above, GWL_EXSTYLE) & WS_EX_TOPMOST:
            above = HWND_TOP  # never slot in under a topmost window (that would make us topmost)
        user32.SetWindowPos(self._hwnd, above, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS)

    def _apply_shape(self, w: int, h: int, side: str) -> None:
        """Round the outer corners like Eye-CareU's; keep the side that meets it square."""
        r = self.TAB_RADIUS if h == self.TAB_H else self.RADIUS
        region = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, 2 * r, 2 * r)
        if side == "right":
            square = gdi32.CreateRectRgn(0, 0, r + 1, h)
        else:
            square = gdi32.CreateRectRgn(w - r - 1, 0, w, h)
        gdi32.CombineRgn(region, region, square, RGN_OR)
        gdi32.DeleteObject(square)
        if not user32.SetWindowRgn(self._hwnd, region, True):
            gdi32.DeleteObject(region)  # on failure the region is still ours to free


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def run_panel(engine: ModeEngine, config: Config) -> None:
    try:
        panel = DockedPanel(engine, config)
    except Exception:  # e.g. Python installed without Tk
        log.exception("panel unavailable - continuing without it")
        run_headless(engine)
        return
    panel.run()


def run_headless(engine: ModeEngine) -> None:
    global restart_requested
    log.info("running without the panel (Ctrl+C to stop)")
    try:
        while engine.is_alive() and not stop_requested():
            if take_flag(RESTART_PATH):
                restart_requested = True
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass


FROZEN = bool(getattr(sys, "frozen", False))  # True inside CinemaGamma.exe (PyInstaller build)


def launch_command(windowless: bool = False) -> list[str]:
    """The command that starts this program: the exe itself, or Python running this script."""
    if FROZEN:
        return [sys.executable]
    interpreter = Path(sys.executable)
    pythonw = interpreter.with_name("pythonw.exe")
    if windowless and pythonw.exists():
        interpreter = pythonw
    return [str(interpreter), str(Path(__file__).resolve())]


def app_folder() -> Path:
    """Folder of the exe (installed build) or of this script."""
    return Path(sys.executable if FROZEN else __file__).resolve().parent


def restart_self() -> None:
    """Start a fresh copy of this program (same interpreter and arguments) once we are cleaned up."""
    global _mutex_handle
    if _mutex_handle:
        kernel32.CloseHandle(_mutex_handle)  # let the new copy take the single-instance mutex
        _mutex_handle = None
    args = launch_command()
    args += [a for a in sys.argv[1:] if a != "--after-restart"] + ["--after-restart"]
    subprocess.Popen(args, cwd=str(app_folder()), close_fds=True,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    log.info("restarted")


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def say(text: str) -> None:
    """print() that is safe under pythonw, where there is no console (sys.stdout is None)."""
    if sys.stdout is not None:
        print(text)
    log.info(text)


def install_startup() -> None:
    """Startup-folder shortcut (exe or pythonw: no console window), then start the app now."""
    target, *arguments = launch_command(windowless=True)
    quoted_arguments = " ".join(f'"{a}"' for a in arguments)  # empty for the exe
    command = (
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(str(STARTUP_LINK))}); "
        f"$s.TargetPath = {_ps_quote(target)}; "
        f"$s.Arguments = {_ps_quote(quoted_arguments)}; "
        f"$s.WorkingDirectory = {_ps_quote(str(app_folder()))}; "
        "$s.Save()"
    )
    STARTUP_LINK.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command], check=True)
    subprocess.Popen(
        [target, *arguments], cwd=str(app_folder()), close_fds=True,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    say(f"Tamam: Windows açılışında otomatik başlayacak ({STARTUP_LINK}).")
    say("Şu an da arka planda başlatıldı; Eye-CareU'yu açınca panel yanında görünecek.")


def uninstall_startup() -> None:
    try:
        STARTUP_LINK.unlink()
        say("Başlangıç kısayolu kaldırıldı.")
    except FileNotFoundError:
        say("Başlangıç kısayolu zaten yok.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Per-mode gamma panel for BenQ Eye-CareU")
    parser.add_argument("--install", action="store_true", help="start automatically at Windows login (and start now)")
    parser.add_argument("--uninstall", action="store_true", help="remove the login shortcut")
    parser.add_argument("--no-panel", action="store_true", help="apply the saved settings only, without the panel")
    parser.add_argument("--after-restart", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    setup_logging()
    if args.install:
        install_startup()
        return
    if args.uninstall:
        uninstall_startup()
        return
    # After a restart the old copy may need a moment to exit and release the mutex.
    for attempt in range(25 if args.after_restart else 1):
        if acquire_single_instance("Local\\CinemaGamma"):
            break
        kernel32.CloseHandle(_mutex_handle)  # our handle would keep the old mutex alive
        time.sleep(0.2)
    else:
        log.info("already running - exiting")
        return
    stop_requested()          # drop leftover requests meant for an older copy
    take_flag(RESTART_PATH)

    log.info("starting (DPI awareness: %s)", set_dpi_awareness())  # before any window exists
    config = Config.load()
    engine = ModeEngine(config)
    atexit.register(engine.stop)  # Ctrl+C / normal exit puts the original curve back
    engine.start()
    if args.no_panel:
        run_headless(engine)
    else:
        run_panel(engine, config)
    engine.stop()
    if restart_requested:
        restart_self()


if __name__ == "__main__":
    main()
