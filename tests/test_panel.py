"""Smoke test for the docked Tk panel: Win32 is stubbed and the Eye-CareU window is faked.

Needs a display:
    python tests/test_panel.py              (Windows)
    xvfb-run python3 tests/test_panel.py    (Linux, with python3-tk)
"""
from winstub import load_cinema_gamma

cg = load_cinema_gamma()
P = cg.Profile

EYECARE = 1234
OUR_HWND = 999
PREVIOUS_FOREGROUND = 555
calls = []
state = {"visible": 1, "iconic": 0, "foreground": PREVIOUS_FOREGROUND}
u = cg.user32._fns
g = cg.gdi32._fns
u["IsWindow"] = lambda *a: 1
u["IsWindowVisible"] = lambda hwnd: state["visible"] if hwnd == EYECARE else 1
u["IsIconic"] = lambda *a: state["iconic"]
u["GetForegroundWindow"] = lambda *a: state["foreground"]
u["SetForegroundWindow"] = lambda hwnd: calls.append(("foreground", hwnd)) or 1
u["GetWindow"] = lambda *a: 0
u["GetWindowLongW"] = lambda *a: 0
u["GetAncestor"] = lambda *a: OUR_HWND
u["SetWindowPos"] = lambda *a: calls.append(("pos",) + a) or 1
u["SetWindowRgn"] = lambda *a: calls.append(("rgn",) + a) or 1
g["CreateRoundRectRgn"] = lambda *a: calls.append(("roundrect",) + a) or 1
g["CreateRectRgn"] = lambda *a: calls.append(("rect",) + a) or 2
rect = {"value": (100, 100, 420, 530)}
cg.find_eyecare_window = lambda match: EYECARE
cg.visible_rect = lambda hwnd: rect["value"]
cg.work_area = lambda hwnd: (0, 0, 1920, 1040)
cg.window_title = lambda hwnd: "Eye-CareU"
cg.is_main_window = lambda hwnd, r: True


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.profiles = {}
        self.status = cg.EngineStatus(state="starting")
        self.open_states = []

    def snapshot(self):
        return self.status

    def profile_for(self, mode):
        return self.profiles.get(mode, P())

    def set_profile(self, mode, profile):
        self.calls.append(("profile", mode, profile))
        if profile.is_default():
            self.profiles.pop(mode, None)
        else:
            self.profiles[mode] = profile

    def reset_all(self):
        self.calls.append("reset_all")
        self.profiles.clear()

    def save_config(self):
        self.calls.append("save")

    def set_eyecare_open(self, is_open):
        self.open_states.append(is_open)

    def stop(self):
        self.calls.append("stop")


engine = FakeEngine()
panel = cg.DockedPanel(engine, cg.Config())
root = panel.root
W = panel.WIDTH + panel.OVERLAP
parked = (cg.OFFSCREEN, cg.OFFSCREEN, W, panel.min_height)
print("panel built, requested size:", W, "x", panel.min_height)
assert panel.min_height <= 430, "content must fit in Eye-CareU's height"
assert panel._geometry == parked and not panel.visible, "panel must start parked off-screen"
assert ("foreground", PREVIOUS_FOREGROUND) in calls, "focus must go back to the previous window"
assert all(s.instate(["disabled"]) for s in panel.scales), "sliders disabled until the mode is known"
root.update()
assert root.winfo_ismapped(), "the Tk window must stay mapped"

# Eye-CareU visible -> tucked 14 px under its right edge, 1 px top inset, stacked above it, rounded
calls.clear()
state["foreground"] = EYECARE
panel._follow_eyecare()
root.update()
height = max(429, panel.min_height)
assert panel._geometry == (420 - 14, 101, W, height), panel._geometry
assert panel.visible and engine.open_states[-1] is True
assert ("pos", OUR_HWND, cg.HWND_TOP, 406, 101, W, height,
        cg.SWP_NOZORDER | cg.SWP_NOACTIVATE | cg.SWP_ASYNCWINDOWPOS) in calls
assert any(c[0] == "pos" and c[1] == OUR_HWND and c[3:7] == (0, 0, 0, 0) for c in calls), "stacked above"
assert ("roundrect", 0, 0, W + 1, height + 1, 28, 28) in calls, "outer corners rounded (r=14)"
assert ("rect", 0, 0, 15, height) in calls, "the side under Eye-CareU stays square"
assert sum(1 for c in calls if c[0] == "rgn") == 1
calls.clear()
panel._follow_eyecare()
assert not any(c[0] in ("rgn", "pos") for c in calls if c[0] != "pos" or c[3:7] != (0, 0, 0, 0)), \
    "nothing changes when Eye-CareU stands still"
print("docking ok:", panel._geometry)

# one setting per mode: the sliders follow the monitor's mode
engine.profiles[50] = P(gamma=1.2)
engine.status = cg.EngineStatus(state="custom", mode=50, name="Cinema")
panel._refresh_status()
assert panel.mode == 50 and abs(panel.slider_vars["gamma"].get() - 1.2) < 1e-9
assert "Cinema" in panel.status.get() and "özel" in panel.status.get()
assert all(not s.instate(["disabled"]) for s in panel.scales)
engine.status = cg.EngineStatus(state="default", mode=295, name="Care Mode")
panel._refresh_status()
assert panel.mode == 295 and abs(panel.slider_vars["gamma"].get() - 1.0) < 1e-9
assert "Care Mode" in panel.status.get() and "varsayılan" in panel.status.get()
engine.status = cg.EngineStatus(state="custom", mode=50, name="Cinema", curve_ok=False)
panel._refresh_status()
assert panel.mode == 50 and abs(panel.slider_vars["gamma"].get() - 1.2) < 1e-9 and "reddetti" in panel.status.get()
print("per-mode sliders + status ok")

# sliders -> the current mode's setting, debounced save
engine.status = cg.EngineStatus(state="custom", mode=50, name="Cinema")
panel.slider_vars["gamma"].set(1.35)
panel._on_slider()
assert engine.calls[-1] == ("profile", 50, P(gamma=1.35))
root.after(900, root.quit)
root.mainloop()
assert "save" in engine.calls, "debounced save must run"
print("sliders ok")


def find_label(widget, text):
    for child in widget.winfo_children():
        if child.winfo_class() == "Label" and child.cget("text") == text:
            return child
        found = find_label(child, text)
        if found is not None:
            return found
    return None


# "Bu modu sıfırla" via an injected click (bare ButtonRelease, no <Enter>)
reset_button = find_label(root, "Bu modu sıfırla")
root.update()
reset_button.event_generate("<ButtonRelease-1>", x=-5, y=5)
root.update()
assert engine.calls[-1] != ("profile", 50, P()), "release outside must not fire"
reset_button.event_generate("<ButtonRelease-1>", x=5, y=5)
root.update()
assert ("profile", 50, P()) in engine.calls and 50 not in engine.profiles
assert abs(panel.slider_vars["gamma"].get() - 1.0) < 1e-9 and "varsayılana döndü" in panel.status.get()
engine.profiles[295] = P(contrast=1.3)
find_label(root, "Tüm modları sıfırla").event_generate("<ButtonRelease-1>", x=3, y=3)
root.update()
assert "reset_all" in engine.calls and engine.profiles == {}
print("reset buttons ok (incl. injected clicks)")

# the drawn "–" icon hides the panel (injected click)
icons = [w for w in root.winfo_children()[0].winfo_children()[0].winfo_children()[0].winfo_children()
         if w.winfo_class() == "Canvas"]
assert len(icons) == 2, icons
icons[1].event_generate("<ButtonRelease-1>", x=10, y=10)
root.update()
assert panel.user_hidden and not panel.visible
panel.user_hidden = False
panel._follow_eyecare()
assert panel.visible
print("drawn window-control icons ok")

# minimise -> park; "–" stays parked until Eye-CareU is closed/minimised; slow polling while closed
state["iconic"] = 1
panel._follow_eyecare()
assert not panel.visible and panel._geometry == parked and engine.open_states[-1] is False
state["iconic"] = 0
panel._follow_eyecare()
assert panel.visible and panel._geometry[:2] == (406, 101)
panel.hide_by_user()
panel._follow_eyecare()
assert not panel.visible, "'–' must keep the panel hidden"
state["iconic"] = 1
panel._follow_eyecare()
state["iconic"] = 0
panel._follow_eyecare()
assert panel.visible, "panel must come back after Eye-CareU is reopened"
print("show/hide ok")

# Eye-CareU near the right screen edge -> left side, no overlap, other corners rounded, padding swaps
rect["value"] = (1600, 100, 1900, 530)
calls.clear()
panel._follow_eyecare()
assert panel._geometry[:3] == (1330, 101, panel.WIDTH) and panel._side == "left"
assert ("rect", panel.WIDTH - 15, 0, panel.WIDTH, height) in calls
panel._update_padding()
assert panel._padding_side == "left"
rect["value"] = (100, 100, 420, 530)
panel._follow_eyecare()
panel._update_padding()
assert panel._padding_side == "right"
print("left-side docking ok")

# WinEvents (follower thread): moves are followed immediately, other windows ignored
rect["value"] = (300, 200, 620, 630)
assert panel._on_win_event(cg.EVENT_OBJECT_LOCATIONCHANGE, EYECARE) is True
assert panel._geometry[:2] == (606, 201), panel._geometry
before = panel._geometry
rect["value"] = (0, 0, 320, 430)
assert panel._on_win_event(cg.EVENT_OBJECT_LOCATIONCHANGE, 4242) is False
assert panel._geometry == before, "events for other windows must be ignored"
panel._on_burst()  # tight-follow tick catches up with the current position
assert panel._geometry[:2] == (306, 1)
# Eye-CareU closed to the tray, then shown again with a new main window -> adopted at once
state["visible"] = 0
panel._on_win_event(cg.EVENT_OBJECT_HIDE if hasattr(cg, "EVENT_OBJECT_HIDE") else 0x8003, EYECARE)
assert not panel.visible
assert panel._on_win_event(cg.EVENT_OBJECT_SHOW, 5555) is True and panel.eyecare == 5555
print("instant following (WinEvents) ok")

# stop file -> clean exit
state["visible"] = 1
cg.STOP_PATH.write_text("")
panel._tick()
assert "stop" in engine.calls and not cg.STOP_PATH.exists()
print("stop file ok")
print("ALL PANEL TESTS PASSED")
