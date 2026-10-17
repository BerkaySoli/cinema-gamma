"""Logic tests for cinema_gamma.py: engine, config, Eye-CareU log watcher, docking geometry.

Runs on any OS (Win32 is stubbed, the monitor and the gamma curve are faked):
    python tests/test_logic.py
"""
import ctypes
import json
import os
import time

from winstub import load_cinema_gamma, temp_dir

cg = load_cinema_gamma()


def copy_ramp(ramp):
    out = cg.GammaRamp()
    ctypes.memmove(out, ramp, ctypes.sizeof(ramp))
    return out


class FakeGamma:
    next_initial = None

    def __init__(self, source):
        self.source = source
        self.ramp = copy_ramp(FakeGamma.next_initial or cg.IDENTITY_RAMP)
        self.last_ok = True
        self.sets = 0

    def get(self):
        return copy_ramp(self.ramp)

    def set(self, ramp):
        self.ramp = copy_ramp(ramp)
        self.last_ok = True
        self.sets += 1
        return True


class FakeDdc:
    def __init__(self, vcp_code):
        self.vcp_code = vcp_code
        self.modes = []
        self.default = 0
        self.reads = 0

    def read(self):
        self.reads += 1
        return self.modes.pop(0) if self.modes else self.default

    def close(self):
        pass


class FakeLogWatch:
    def __init__(self):
        self.switches = []

    def poll(self):
        return self.switches.pop(0) if self.switches else None


_RealEngine = cg.ModeEngine


def make_engine(config):
    engine = _RealEngine(config)
    engine.log_watch = FakeLogWatch()
    return engine


source = {"name": r"\\.\DISPLAY2"}
cg.find_display_source = lambda tag: source["name"]
cg.DisplayGamma = FakeGamma
cg.DdcMonitor = FakeDdc
match = cg.ramps_match
P = cg.Profile

# --- ramps -------------------------------------------------------------------
ident = cg.IDENTITY_RAMP
assert all(ident[c][i] == i * 257 for c in range(3) for i in range(256)), "identity ramp wrong"
r = cg.build_ramp(P(gamma=1.2))
vals = list(r[0])
assert vals[0] == 0 and vals[-1] == 65535 and all(b >= a for a, b in zip(vals, vals[1:]))
assert r[0][128] > ident[0][128], "gamma > 1 must lift mid-tones"
extreme = list(cg.build_ramp(P(gamma=1.6, brightness=0.3, contrast=1.5))[0])
assert all(0 <= v <= 65535 for v in extreme) and all(b >= a for a, b in zip(extreme, extreme[1:]))
assert P().is_default() and not P(gamma=1.2).is_default() and P(gamma=1.004).is_default()
print("ramps ok")

# --- config ------------------------------------------------------------------------
# The user's real config from the previous version: three taught modes, gamma back at 1.0
cg.CONFIG_PATH.write_text(json.dumps({
    "vcp_code": 220, "cinema_values": [50, 295, 0], "monitor_tag": "BNQ",
    "eyecare_match": "eye-careu", "poll_seconds": 2.0,
    "cinema": {"gamma": 1.0, "brightness": 0.0, "contrast": 1.0}}))
c = cg.Config.load()
assert c.profiles == {}, "default settings migrate to 'no custom setting'"
assert c.mode_names["50"] == "Cinema" and c.mode_names["0"] == "Standard"
cg.CONFIG_PATH.write_text(json.dumps({"cinema_values": [50], "cinema": {"gamma": 1.2}}))
assert cg.Config.load().profiles == {"50": P(gamma=1.2)}, "old Cinema setting carries over"
c = cg.Config.load()
c.profiles["1063"] = P(gamma=0.9, contrast=1.1)
c.mode_names["7"] = "Game"
c.save()
raw = json.loads(cg.CONFIG_PATH.read_text())
assert raw["profiles"]["1063"] == {"gamma": 0.9, "brightness": 0.0, "contrast": 1.1}
raw["junk"] = 1
raw["ddc_check_open_s"] = "abc"  # bad value must not crash
cg.CONFIG_PATH.write_text(json.dumps(raw))
c2 = cg.Config.load()
assert c2.profiles["1063"] == P(gamma=0.9, contrast=1.1) and c2.profiles["50"] == P(gamma=1.2)
assert c2.mode_names["7"] == "Game" and c2.mode_names["295"] == "Care Mode" and c2.ddc_check_open_s == 10.0
cg.CONFIG_PATH.write_text("{ broken")
assert cg.Config.load().profiles == {}, "corrupt config must fall back to defaults"
assert cg.Config().profiles == {} and cg.Config().profiles is not cg.Config().profiles
print("config + migration ok")

# --- mode names from Eye-CareU's log -----------------------------------------------
log_dir = cg.EYECARE_LOG_DIR
log_dir.mkdir(parents=True, exist_ok=True)
(log_dir / "Eye-CareU-2026-10-16.log").write_text(
    'I 21:00:00.000 [EcMonitorModel] setColorMode: "Game"\n'
    'I 21:00:00.050 [MonitorModel][SetVCPFeature] true :  "dc" 9 retry:  0 delay: 0 ms\n')
(log_dir / "Eye-CareU-2026-10-17.log").write_text(
    'I 22:35:18.343 [EcMonitorModel] setColorMode: "Cinema"\n'
    'I 22:35:18.398 [MonitorModel][SetVCPFeature] true :  "dc" 50 retry:  0 delay: 0 ms\n'
    'I 22:35:19.000 [MonitorModel][SetVCPFeature] true :  "10" 80 retry:  0 delay: 0 ms\n'
    'I 23:08:04.686 [EcMonitorModel] setColorMode: "ePaper"\n'
    'I 23:08:04.700 something unrelated\n'
    'I 23:08:04.741 [MonitorModel][SetVCPFeature] true :  "dc" 1063 retry:  0 delay: 0 ms\n'
    'I 23:09:00.000 [MonitorModel][SetVCPFeature] true :  "dc" 5 retry:  0 delay: 0 ms\n')
assert cg.learn_mode_names() == {"9": "Game", "50": "Cinema", "1063": "ePaper"}, cg.learn_mode_names()
assert cg.learn_mode_names(cg.Path("/nonexistent")) == {}
print("mode-name learning ok:", cg.learn_mode_names())

# --- docking geometry ----------------------------------------------------------------
work = (0, 0, 1920, 1040)
assert cg.dock_position((100, 100, 400, 520), work, 270, 380, 14) == (386, 100, 284, 420, "right")
assert cg.dock_position((1700, 100, 1900, 520), work, 270, 380, 14) == (1430, 100, 270, 420, "left")
assert cg.dock_position((100, 900, 400, 1320), work, 270, 380, 14)[1] == 620   # kept on screen
assert cg.dock_position((100, 100, 400, 300), work, 270, 380, 14)[3] == 380    # min height
print("docking geometry ok")

# --- finding the Eye-CareU main window -------------------------------------------------
EYE = r"C:\Program Files\BenQ\Eye-CareU\Eye-CareU.exe"
windows = {  # hwnd: (pid, visible, rect, exstyle, title)
    1: (100, True, (500, 300, 1320, 730), 0x80000, "Eye-CareU"),                   # main window
    2: (100, True, (600, 280, 720, 310), 0x8, ""),                                 # tooltip
    3: (100, True, (0, 0, 1920, 1080), 0x80000 | 0x20 | 0x8, ""),                  # dimming overlay
    4: (100, True, (0, 0, 1920, 1080), 0x8, "Mola"),                               # break screen
    5: (200, True, (0, 0, 1900, 1000), 0, "Eye-CareU - Google'da ara - Chrome"),   # browser tab
    6: (100, False, (0, 0, 640, 480), 0, "hidden helper"),                         # hidden Qt window
}
paths = {100: EYE, 200: r"C:\Program Files\Google\Chrome\Application\chrome.exe", 300: ""}


def fake_enum(callback, _lparam):
    for hwnd in list(windows):
        callback(hwnd, 0)
    return 1


def fake_pid(hwnd, ref):
    ref._obj.value = windows[hwnd][0]
    return 1


screen = cg.MONITORINFO()
screen.rcMonitor.right, screen.rcMonitor.bottom = 1920, 1080
u = cg.user32._fns
u["EnumWindows"] = fake_enum
u["IsWindowVisible"] = lambda hwnd: int(windows[hwnd][1])
u["GetWindowThreadProcessId"] = fake_pid
u["GetWindowLongW"] = lambda hwnd, index: windows[hwnd][3]
saved = (cg.process_path, cg.visible_rect, cg._monitor_info, cg.window_title)
cg.process_path = lambda pid: paths[pid]
cg.visible_rect = lambda hwnd: windows[hwnd][2]
cg._monitor_info = lambda hwnd: screen
cg.window_title = lambda hwnd: windows[hwnd][4]
assert cg.find_eyecare_window("eye-careu") == 1, "visible main window, not overlays/tooltips/browser"
windows[1] = (100, False, (500, 300, 1320, 730), 0x80000, "Eye-CareU")  # closed to the tray
assert cg.find_eyecare_window("eye-careu") == 1, "the hidden main window still counts (larger than helpers)"
windows[8] = (100, True, (10, 10, 830, 440), 0, "Eye-CareU")
assert cg.find_eyecare_window("eye-careu") == 8, "a visible window wins over a hidden one"
del windows[8], windows[1], windows[6]
assert cg.find_eyecare_window("eye-careu") is None, "overlays alone are not a main window"
windows[7] = (300, True, (10, 10, 830, 440), 0, "BenQ Eye-CareU")  # exe path unreadable -> title
assert cg.find_eyecare_window("eye-careu") == 7
cg.process_path, cg.visible_rect, cg._monitor_info, cg.window_title = saved
print("Eye-CareU window detection ok")

# --- engine: one setting per mode ----------------------------------------------------
STD, CINEMA, CARE = 0, 50, 295
cfg = cg.Config(ddc_check_open_s=0, ddc_check_closed_s=0, profiles={str(CINEMA): P(gamma=1.2), str(CARE): P(contrast=1.2)})
eng = make_engine(cfg)
ddc = eng.ddc
cinema_ramp, care_ramp = cg.build_ramp(P(gamma=1.2)), cg.build_ramp(P(contrast=1.2))

ddc.modes = [STD]; eng._step()
g = eng.gamma
s = eng.snapshot()
assert (s.state, s.mode, s.name) == ("default", STD, "Standard") and match(g.ramp, ident) and not eng.active

ddc.modes = [CINEMA]; eng._step()
assert eng.snapshot().state == "custom" and eng.snapshot().name == "Cinema" and match(g.ramp, cinema_ramp)

ddc.modes = [CARE]; eng._step()                      # custom -> another custom: no baseline capture
assert match(g.ramp, care_ramp) and match(eng.baseline, ident)

g.ramp = copy_ramp(ident)                              # driver / Night light reset the curve
ddc.modes = [CARE]; eng._step()
assert match(g.ramp, care_ramp), "curve must be re-applied while in a custom mode"

ddc.modes = [STD]; eng._step()                       # back to a default mode: original curve
assert match(g.ramp, ident) and not eng.active and eng.snapshot().state == "default"

reads = ddc.reads                                      # slider change: fast path, no DDC read
eng.set_profile(STD, P(gamma=0.8)); eng._step()
assert ddc.reads == reads and match(g.ramp, cg.build_ramp(P(gamma=0.8))) and eng.active
assert eng.profile_for(STD) == P(gamma=0.8) and eng.profile_for(1063) == P()

eng.set_profile(STD, P()); eng._step()               # slider back at default = no custom setting
assert str(STD) not in cfg.profiles and match(g.ramp, ident) and not eng.active

ddc.modes = [None]; eng._step()                      # DDC hiccup: keep the current state
assert eng.snapshot().state == "ddc_error" and eng.snapshot().mode == STD

ddc.modes = [CINEMA]; eng._step()                    # replug: new output, curve applied there again
source["name"] = r"\\.\DISPLAY3"
ddc.modes = [CINEMA]; eng._step()
assert eng.gamma is not g and match(eng.gamma.ramp, cinema_ramp)
source["name"] = None
eng._step()
assert eng.snapshot().state == "no_monitor"
source["name"] = r"\\.\DISPLAY2"

eng.reset_all(); eng._step()
assert cfg.profiles == {} and json.loads(cg.CONFIG_PATH.read_text())["profiles"] == {}
assert match(eng.gamma.ramp, ident) and not eng.active
print("engine per-mode transitions ok")

# unknown mode: named "Mod N" until Eye-CareU's log names it
eng2 = make_engine(cg.Config(ddc_check_open_s=0))
eng2.ddc.modes = [77]
eng2._step()
assert eng2.snapshot().name == "Mod 77"
with open(log_dir / "Eye-CareU-2026-10-17.log", "a") as f:
    f.write('I 23:30:00.000 [EcMonitorModel] setColorMode: "Game"\n'
            'I 23:30:00.050 [MonitorModel][SetVCPFeature] true :  "dc" 77 retry:  0 delay: 0 ms\n')
eng2._next_name_lookup = 0
eng2.ddc.modes = [77]
eng2._step()
assert eng2.snapshot().name == "Game" and json.loads(cg.CONFIG_PATH.read_text())["mode_names"]["77"] == "Game"
print("unknown mode naming ok")

# previous run killed mid-way: its curve must not become the "original"
FakeGamma.next_initial = cinema_ramp
eng3 = make_engine(cg.Config(ddc_check_open_s=0, profiles={str(CINEMA): P(gamma=1.2)}))
eng3.ddc.modes = [CINEMA, STD]
eng3._step(); eng3._step()
assert match(eng3.gamma.ramp, ident), "must fall back to neutral, not the stale curve"
FakeGamma.next_initial = None
print("stale-curve recovery ok")

# polling: fast while Eye-CareU is open, slow while closed; opening wakes the engine
eng4 = make_engine(cg.Config(ddc_check_open_s=0.05, ddc_check_closed_s=30, profiles={str(CINEMA): P(gamma=1.2)}))
eng4.ddc.default = CINEMA
eng4.start(); time.sleep(0.3)
assert eng4.active and match(eng4.gamma.ramp, cinema_ramp)
eng4.set_eyecare_open(False)
time.sleep(0.4)
reads = eng4.ddc.reads
time.sleep(0.4)
assert eng4.ddc.reads == reads, "closed: no reads within the slow interval"
eng4.set_eyecare_open(True)
time.sleep(0.2)
assert eng4.ddc.reads > reads, "opening Eye-CareU reads the mode right away"
eng4.stop()
assert not eng4.is_alive() and match(eng4.gamma.ramp, ident) and not eng4.active
eng4.stop()  # second call must be harmless
print("adaptive polling + thread start/stop ok")

# follower: a move starts the 10 s tight-follow timer; other events don't
timers = []
u["SetTimer"] = lambda *a: timers.append(a) or 42
u["KillTimer"] = lambda *a: timers.append(("kill",) + a) or 1
fol = cg.EyeCareFollower(lambda event, hwnd: hwnd == 5, lambda: None)
fol._callback(0, cg.EVENT_SYSTEM_FOREGROUND, 5, 0, 0, 0, 0)
assert fol._timer == 0, "only moves start tight-follow"
fol._callback(0, cg.EVENT_OBJECT_LOCATIONCHANGE, 6, 0, 0, 0, 0)
assert fol._timer == 0, "moves of other windows are ignored"
fol._callback(0, cg.EVENT_OBJECT_LOCATIONCHANGE, 5, -4, 0, 0, 0)
assert fol._timer == 0, "child objects are ignored"
fol._callback(0, cg.EVENT_OBJECT_LOCATIONCHANGE, 5, 0, 0, 0, 0)
assert fol._timer == 42 and fol._burst_until > time.monotonic() + 9
fol._callback(0, cg.EVENT_OBJECT_LOCATIONCHANGE, 5, 0, 0, 0, 0)
assert len([t for t in timers if t[0] != "kill"]) == 1, "an active burst is extended, not restarted"
fol._stop_burst()
assert fol._timer == 0 and timers[-1][0] == "kill"
assert cg.EyeCareFollower._safely(lambda: 1 / 0) is False, "errors never escape the callback"
print("follower tight-follow timer ok")

# --- Eye-CareU log watcher: instant mode switches -------------------------------------
wdir = cg.Path(temp_dir())
old = wdir / "Eye-CareU-2026-10-17.log"
old.write_text('I 10:00:00.000 [EcMonitorModel] setColorMode: "Cinema"\n'
               'I 10:00:00.050 [MonitorModel][SetVCPFeature] true :  "dc" 50 retry:  0 delay: 0 ms\n')
w = cg.EyeCareLogWatcher(wdir)
assert w.poll() is None, "history is skipped on first attach"


def append(path, text):
    with open(path, "a", newline="") as f:
        f.write(text)


append(old, 'I 23:40:00.000 [EcMonitorModel] setColorMode: "Game"\n'
            'I 23:40:00.050 [MonitorModel][SetVCPFeature] true :  "dc" 40 retry:  0 delay: 0 ms\n')
assert w.poll() == (40, "Game")
assert w.poll() is None, "nothing new"
append(old, 'I 23:41:00.000 [EcMonitorModel] setColorMode: "ePaper"\nI 23:41:00.050 [MonitorModel][SetVCPF')
assert w.poll() is None, "half-written line waits for the rest"
append(old, 'eature] true :  "dc" 1063 retry:  0 delay: 0 ms\n')
assert w.poll() == (1063, "ePaper")
append(old, 'I 23:42:00.000 [MonitorModel][SetVCPFeature] false :  "dc" 5 retry:  3\n'
            'I 23:42:01.000 [EcMonitorModel] setColorMode: "Standard"\n'
            'I 23:42:01.050 [MonitorModel][SetVCPFeature] true :  "dc" 0 retry:  0 delay: 0 ms\n'
            'I 23:42:02.000 [EcMonitorModel] setColorMode: "Cinema"\n'
            'I 23:42:02.050 [MonitorModel][SetVCPFeature] true :  "dc" 50 retry:  0 delay: 0 ms\n')
assert w.poll() == (50, "Cinema"), "failed writes are ignored; the latest switch wins"
time.sleep(0.05)
new = wdir / "Eye-CareU-2026-10-18.log"  # Eye-CareU started a new day's file
new.write_text('I 00:00:01.000 [EcMonitorModel] setColorMode: "Care Mode"\n'
               'I 00:00:01.050 [MonitorModel][SetVCPFeature] true :  "dc" 295 retry:  0 delay: 0 ms\n')
os.utime(new, (time.time() + 5, time.time() + 5))
w._next_rescan = 0
assert w.poll() == (295, "Care Mode"), "a new file is read from its start"
assert cg.EyeCareLogWatcher(cg.Path("/nonexistent")).poll() is None
print("Eye-CareU log watcher ok")

# engine: a switch seen in Eye-CareU's log is applied at once, without a DDC/CI read
eng5 = make_engine(cg.Config(ddc_check_open_s=60, profiles={str(CINEMA): P(gamma=1.2)}))
eng5.ddc.default = STD
eng5._step()  # first step: DDC read (Standard)
reads = eng5.ddc.reads
eng5._step()
assert eng5.ddc.reads == reads, "no hardware access while nothing happens"
eng5.log_watch.switches.append((CINEMA, "Cinema"))
eng5._step()
assert eng5.ddc.reads == reads and eng5.snapshot().mode == CINEMA and match(eng5.gamma.ramp, cinema_ramp)
eng5.log_watch.switches.append((40, "Game"))
eng5._step()
assert eng5.snapshot().name == "Game" and eng5.config.mode_names["40"] == "Game" and match(eng5.gamma.ramp, ident)
print("instant mode switch via Eye-CareU's log ok")

# stop file: consumed exactly once
assert not cg.stop_requested()
cg.STOP_PATH.write_text("")
assert cg.stop_requested() and not cg.STOP_PATH.exists() and not cg.stop_requested()
print("stop file ok")

# launch command: Python + script while developing, the exe itself in the installed build
script_cmd = cg.launch_command()
assert script_cmd[-1].endswith("cinema_gamma.py") and len(script_cmd) == 2
assert cg.app_folder() == cg.Path(cg.__file__).resolve().parent
cg.FROZEN = True
try:
    assert cg.launch_command() == [cg.sys.executable] == cg.launch_command(windowless=True)
    assert cg.app_folder() == cg.Path(cg.sys.executable).resolve().parent
finally:
    cg.FROZEN = False
print("launch command (script / exe) ok")
print("ALL TESTS PASSED")
