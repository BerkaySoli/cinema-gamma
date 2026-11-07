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
cg.work_area_at = lambda x, y: (0, 0, 1920, 1040) if 0 <= x < 1920 and 0 <= y < 1040 else None
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
assert abs(float(root.tk.call("tk", "scaling")) - 96 / 72) < 0.01, "fonts pinned to 100 % (96 dpi)"
assert panel._geometry == parked and not panel.visible, "panel must start parked off-screen"
assert ("foreground", PREVIOUS_FOREGROUND) in calls, "focus must go back to the previous window"
assert all(s.instate(["disabled"]) for s in panel.scales), "sliders disabled until the mode is known"
root.update()
assert root.winfo_ismapped(), "the Tk window must stay mapped"

# Eye-CareU visible -> tucked 19 px under its right edge, level with its top, stacked above it, rounded
calls.clear()
state["foreground"] = EYECARE
panel._follow_eyecare()
root.update()
height = max(430, panel.min_height)
assert panel._geometry == (420 - 19, 100, W, height), panel._geometry
assert panel.visible and engine.open_states[-1] is True
assert ("pos", OUR_HWND, cg.HWND_TOP, 401, 100, W, height,
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

# no "–" / "×" any more: one arrow folds the panel into a small tab stuck to Eye-CareU's edge
assert [w for w in root.winfo_children() if w.winfo_class() == "Canvas"] == [panel.tab_view]
calls.clear()
panel.fold_button.event_generate("<ButtonRelease-1>", x=10, y=10)  # injected click
root.update()
tab = (401, 112, panel.OVERLAP + panel.TAB_W, panel.TAB_H)
assert panel.collapsed and panel.visible and panel._geometry == tab, panel._geometry
assert ("roundrect", 0, 0, tab[2] + 1, tab[3] + 1, 20, 20) in calls, "tab corners rounded (r=10)"
assert panel.tab_view.winfo_ismapped() and not panel.full_view.winfo_ismapped()
assert panel.config.panel_collapsed is True
rect["value"] = (300, 200, 620, 630)  # the tab follows Eye-CareU like the panel does
panel._on_win_event(cg.EVENT_OBJECT_LOCATIONCHANGE, EYECARE)
assert panel._geometry[:2] == (601, 212)
rect["value"] = (100, 100, 420, 530)
panel._follow_eyecare()
calls.clear()
panel.tab_view.event_generate("<ButtonRelease-1>", x=-3, y=5)  # release outside: nothing happens
assert panel.collapsed
panel.tab_view.event_generate("<ButtonRelease-1>", x=20, y=20)  # the arrow opens the panel again
root.update()
assert not panel.collapsed and panel._geometry == (401, 100, W, height)
assert panel.full_view.winfo_ismapped() and not panel.tab_view.winfo_ismapped()
assert ("roundrect", 0, 0, W + 1, height + 1, 28, 28) in calls
root.after(900, root.quit)
root.mainloop()
assert panel.config.panel_collapsed is False and "save" in engine.calls, "folded state is saved"
print("fold arrow / tab ok")

# minimise -> park (panel or tab), back when Eye-CareU is restored
state["iconic"] = 1
panel._follow_eyecare()
assert not panel.visible and panel._geometry == parked and engine.open_states[-1] is False
state["iconic"] = 0
panel._follow_eyecare()
assert panel.visible and panel._geometry[:2] == (401, 100)
print("show/hide ok")

# Eye-CareU at the right screen edge -> the panel stays on the right, slid in to stay on screen
rect["value"] = (1600, 100, 1900, 530)
calls.clear()
panel._follow_eyecare()
assert panel._geometry[:3] == (1920 - W, 100, W) and panel._side == "right", panel._geometry
panel._update_padding()
assert panel._padding_side == "right"
rect["value"] = (100, 100, 420, 530)
panel._follow_eyecare()

print("right-edge docking ok")

# WinEvents (follower thread): moves are followed immediately, other windows ignored
rect["value"] = (300, 200, 620, 630)
assert panel._on_win_event(cg.EVENT_OBJECT_LOCATIONCHANGE, EYECARE) is True
assert panel._geometry[:2] == (601, 200), panel._geometry
before = panel._geometry
rect["value"] = (0, 0, 320, 430)
assert panel._on_win_event(cg.EVENT_OBJECT_LOCATIONCHANGE, 4242) is False
assert panel._geometry == before, "events for other windows must be ignored"
panel._on_burst()  # tight-follow tick catches up with the current position
assert panel._geometry[:2] == (301, 0)
# Eye-CareU closed to the tray, then shown again with a new main window -> adopted at once
state["visible"] = 0
panel._on_win_event(cg.EVENT_OBJECT_HIDE if hasattr(cg, "EVENT_OBJECT_HIDE") else 0x8003, EYECARE)
assert not panel.visible
assert panel._on_win_event(cg.EVENT_OBJECT_SHOW, 5555) is True and panel.eyecare == 5555
print("instant following (WinEvents) ok")

# Basic / Advanced tabs: Advanced adds temperature + RGB; both tabs edit the same values
assert panel.tab == "basic" and panel.tab_frames["basic"].winfo_ismapped()
panel.tab_labels["advanced"].event_generate("<ButtonRelease-1>", x=3, y=3)  # injected click
root.update()
assert panel.tab == "advanced" and panel.tab_frames["advanced"].winfo_ismapped()
assert not panel.tab_frames["basic"].winfo_ismapped() and panel.config.panel_tab == "advanced"
engine.status = cg.EngineStatus(state="custom", mode=50, name="Cinema")
panel.mode = 50
panel.slider_vars["temperature"].set(5230)
panel.slider_vars["gamma"].set(1.1)
panel._on_slider()
assert engine.calls[-1] == ("profile", 50, P(gamma=1.1, temperature=5250.0)), engine.calls[-1]
assert [lb.cget("text") for lb in panel.value_labels["gamma"]] == ["1.10", "1.10"], "shared between tabs"
assert panel.value_labels["temperature"][0].cget("text") == "5250 K"
panel.tab_labels["basic"].event_generate("<ButtonRelease-1>", x=3, y=3)
root.update()
assert panel.tab == "basic" and panel.config.panel_tab == "basic"
print("Basic / Advanced tabs ok")


# Scale tab: one slider per display, 25-point steps, applied after dragging, restored on demand


class FakeDisplay:
    def __init__(self, key, scale):
        self.key, self.label, self.scale, self.sets = key, f"{key}. Ekran", scale, []

    def get_scale(self):
        return self.scale

    def max_scale(self):
        return 200

    def set_scale(self, percent):
        self.sets.append(percent)
        self.scale = percent


import tempfile  # noqa: E402
displays = [FakeDisplay("1", 150), FakeDisplay("2", 100)]
panel.scale_manager = cg.ScaleManager(lambda: displays, cg.Path(tempfile.mkdtemp()) / "s.json")
panel.tab_labels["scale"].event_generate("<ButtonRelease-1>", x=3, y=3)
root.update()
assert panel.tab == "scale" and not panel.buttons.winfo_ismapped(), "mode buttons hidden on the Scale tab"
assert set(panel.scale_rows) == {"1", "2"}
var, value = panel.scale_rows["1"]
assert value.cget("text") == "%150"
var.set(117)  # dragged between steps -> snaps to 125
panel._on_scale_slide("1")
assert var.get() == 125 and value.cget("text") == "%150 → %125" and displays[0].sets == []
root.after(700, root.quit)
root.mainloop()
assert displays[0].sets == [125], "applied once, after the drag"
panel.restore_scales()
root.update()
assert displays[0].scale == 150 and panel.scale_rows["1"][1].cget("text") == "%150"
panel.tab_labels["basic"].event_generate("<ButtonRelease-1>", x=3, y=3)
root.update()
assert panel.buttons.winfo_ismapped()
print("Scale tab ok")

# starting on the Scale tab (remembered from last time) must work too
started_on_scale = cg.DockedPanel(FakeEngine(), cg.Config(panel_tab="scale"),
                                  cg.ScaleManager(lambda: displays, cg.Path(tempfile.mkdtemp()) / "t.json"))
assert started_on_scale.tab == "scale" and started_on_scale.min_height <= 430
started_on_scale.follower.stop()
started_on_scale.root.destroy()
print("start on the Scale tab ok")

# stop file -> clean exit
state["visible"] = 1
cg.STOP_PATH.write_text("")
panel._tick()
assert "stop" in engine.calls and not cg.STOP_PATH.exists()
print("stop file ok")

print("ALL PANEL TESTS PASSED")
