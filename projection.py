#!/usr/bin/env python3
"""Projection Settings - a small Win+P style display switcher for X11 (RandR).

GUI:  projection.py
CLI:  projection.py --mode {pc-only,duplicate,extend,second-only} [--position right|left|above|below]
      projection.py --list
"""

import argparse
import sys
from dataclasses import dataclass, field, replace

from Xlib import X, display
from Xlib.ext import randr

ROTATIONS = {
    "Normal": randr.Rotate_0,
    "Left (90°)": randr.Rotate_90,
    "Inverted (180°)": randr.Rotate_180,
    "Right (270°)": randr.Rotate_270,
}
POSITIONS = ["right", "left", "above", "below"]
RR_INTERLACE = 0x10
RR_DOUBLESCAN = 0x20
DPI = 96


# --------------------------------------------------------------------------- model

@dataclass(frozen=True)
class Mode:
    id: int
    width: int
    height: int
    rate: float

    @property
    def label(self):
        return f"{self.width}×{self.height}"


@dataclass
class Output:
    id: int
    name: str
    connected: bool
    modes: list            # list[Mode], preferred first
    crtcs: list            # CRTCs this output can use
    crtc: int = 0          # current CRTC (0 = off)
    mm: tuple = (0, 0)

    @property
    def preferred(self):
        return self.modes[0] if self.modes else None


@dataclass
class OutputConfig:
    enabled: bool = False
    mode: Mode = None
    x: int = 0
    y: int = 0
    rotation: int = randr.Rotate_0

    def size(self):
        w, h = self.mode.width, self.mode.height
        if self.rotation & (randr.Rotate_90 | randr.Rotate_270):
            w, h = h, w
        return w, h


@dataclass
class Layout:
    outputs: dict = field(default_factory=dict)   # name -> OutputConfig
    primary: str = None

    def copy(self):
        return Layout({k: replace(v) for k, v in self.outputs.items()}, self.primary)


def mode_rate(m):
    if not m.h_total or not m.v_total:
        return 0.0
    rate = m.dot_clock / (m.h_total * m.v_total)
    if m.flags & RR_INTERLACE:
        rate *= 2
    if m.flags & RR_DOUBLESCAN:
        rate /= 2
    return round(rate, 2)


class Screen:
    """Reads and writes the RandR state of the default X screen."""

    def __init__(self):
        self.d = display.Display()
        self.root = self.d.screen().root
        if not self.d.has_extension("RANDR"):
            raise RuntimeError("X server has no RandR extension")
        self.reload()

    def reload(self):
        res = self.root.xrandr_get_screen_resources()
        self.res = res
        self.ts = res.config_timestamp
        modes = {m.id: m for m in res.modes}
        self.outputs = []
        for oid in res.outputs:
            info = self.d.xrandr_get_output_info(oid, self.ts)
            out_modes = []
            for i, mid in enumerate(info.modes):
                m = modes[mid]
                out_modes.append(Mode(mid, m.width, m.height, mode_rate(m)))
            # preferred modes first, then by size and rate
            pref = out_modes[:info.num_preferred]
            rest = sorted(out_modes[info.num_preferred:],
                          key=lambda m: (m.width * m.height, m.rate), reverse=True)
            self.outputs.append(Output(
                id=oid, name=info.name, connected=info.connection == randr.Connected,
                modes=pref + rest, crtcs=list(info.crtcs), crtc=info.crtc,
                mm=(info.mm_width, info.mm_height)))
        self.crtc_info = {c: self.d.xrandr_get_crtc_info(c, self.ts) for c in res.crtcs}
        self.size_range = self.root.xrandr_get_screen_size_range()

    @property
    def connected(self):
        return [o for o in self.outputs if o.connected]

    def output(self, name):
        return next(o for o in self.outputs if o.name == name)

    def current_layout(self):
        lay = Layout()
        for o in self.outputs:
            cfg = OutputConfig()
            ci = self.crtc_info.get(o.crtc)
            if o.crtc and ci and ci.mode:
                mode = next((m for m in o.modes if m.id == ci.mode), None)
                if mode:
                    cfg = OutputConfig(True, mode, ci.x, ci.y, ci.rotation & 0x0F or randr.Rotate_0)
            lay.outputs[o.name] = cfg
        prim = self.root.xrandr_get_output_primary().output
        lay.primary = next((o.name for o in self.outputs if o.id == prim), None)
        return lay

    # -- applying -----------------------------------------------------------

    def apply(self, layout):
        enabled = {n: c for n, c in layout.outputs.items() if c.enabled and c.mode}
        if not enabled:
            raise ValueError("At least one display must stay on")

        width = max(c.x + c.size()[0] for c in enabled.values())
        height = max(c.y + c.size()[1] for c in enabled.values())
        rng = self.size_range
        if width > rng.max_width or height > rng.max_height:
            raise ValueError(f"Layout {width}×{height} exceeds max screen size "
                             f"{rng.max_width}×{rng.max_height}")
        width, height = max(width, rng.min_width), max(height, rng.min_height)

        # Pick a CRTC for every enabled output (keep current one when possible).
        assign, used = {}, set()
        for name in enabled:
            o = self.output(name)
            if o.crtc and o.crtc not in used:
                assign[name] = o.crtc
                used.add(o.crtc)
        for name in enabled:
            if name in assign:
                continue
            o = self.output(name)
            free = [c for c in o.crtcs if c not in used and not self.crtc_info[c].outputs]
            free = free or [c for c in o.crtcs if c not in used]
            if not free:
                raise ValueError(f"No free CRTC for {name}")
            assign[name] = free[0]
            used.add(free[0])

        self.d.grab_server()
        try:
            # 1. Turn off CRTCs that are unused, change owner, or don't fit the new screen.
            for c, ci in self.crtc_info.items():
                if not ci.mode:
                    continue
                target = next((n for n, cc in assign.items() if cc == c), None)
                fits = ci.x + ci.width <= width and ci.y + ci.height <= height
                if target is None or not fits or \
                        [self.output(target).id] != list(ci.outputs):
                    self.d.xrandr_set_crtc_config(c, self.ts, 0, 0, 0, randr.Rotate_0, [])
            # 2. Resize the screen.
            self.root.xrandr_set_screen_size(
                width, height, round(width * 25.4 / DPI), round(height * 25.4 / DPI))
            # 3. Configure each CRTC.
            for name, c in assign.items():
                cfg = enabled[name]
                self.d.xrandr_set_crtc_config(c, self.ts, cfg.x, cfg.y, cfg.mode.id,
                                              cfg.rotation, [self.output(name).id])
            # 4. Primary.
            prim = layout.primary if layout.primary in enabled else next(iter(enabled))
            self.root.xrandr_set_output_primary(self.output(prim).id)
        finally:
            self.d.ungrab_server()
            self.d.sync()
        self.reload()

    # -- presets --------------------------------------------------------------

    def internal_and_external(self):
        conn = self.connected
        internal = next((o for o in conn if o.name.startswith(("eDP", "LVDS", "DSI"))), None)
        internal = internal or (conn[0] if conn else None)
        externals = [o for o in conn if o is not internal]
        return internal, externals

    def preset(self, mode, position="right"):
        internal, externals = self.internal_and_external()
        if internal is None:
            raise ValueError("No connected display found")
        ext = externals[0] if externals else None
        lay = Layout({o.name: OutputConfig() for o in self.outputs}, internal.name)
        current = self.current_layout()

        def keep_or_pref(o):
            c = current.outputs.get(o.name)
            return c.mode if c and c.enabled else o.preferred

        if mode == "pc-only" or ext is None:
            lay.outputs[internal.name] = OutputConfig(True, keep_or_pref(internal))
        elif mode == "second-only":
            lay.outputs[ext.name] = OutputConfig(True, keep_or_pref(ext))
            lay.primary = ext.name
        elif mode == "duplicate":
            m_int, m_ext = best_common_modes(internal, ext)
            lay.outputs[internal.name] = OutputConfig(True, m_int)
            lay.outputs[ext.name] = OutputConfig(True, m_ext)
        elif mode == "extend":
            a, b = internal.preferred, ext.preferred
            ca, cb = OutputConfig(True, a), OutputConfig(True, b)
            place(ca, cb, position)
            lay.outputs[internal.name], lay.outputs[ext.name] = ca, cb
        else:
            raise ValueError(f"Unknown mode {mode}")
        return lay


def best_common_modes(a, b):
    """Largest resolution both outputs support (best refresh for each)."""
    sizes_b = {(m.width, m.height) for m in b.modes}
    common = [m for m in a.modes if (m.width, m.height) in sizes_b]
    if not common:
        return a.preferred, b.preferred
    w, h = max(((m.width, m.height) for m in common), key=lambda s: s[0] * s[1])
    pick = lambda o: max((m for m in o.modes if (m.width, m.height) == (w, h)),
                         key=lambda m: (m in o.modes[:1], m.rate))
    return pick(a), pick(b)


def place(primary_cfg, other_cfg, position):
    """Place other_cfg relative to primary_cfg, normalising to (0, 0)."""
    pw, ph = primary_cfg.size()
    ow, oh = other_cfg.size()
    px = py = ox = oy = 0
    if position == "right":
        ox = pw
    elif position == "left":
        px = ow
    elif position == "below":
        oy = ph
    elif position == "above":
        py = oh
    primary_cfg.x, primary_cfg.y = px, py
    other_cfg.x, other_cfg.y = ox, oy


def detect_mode(screen, layout):
    internal, externals = screen.internal_and_external()
    on = [n for n, c in layout.outputs.items() if c.enabled]
    if not externals or on == [internal.name]:
        return "pc-only"
    ext = externals[0].name
    if internal.name not in on and ext in on:
        return "second-only"
    a, b = layout.outputs[internal.name], layout.outputs.get(ext)
    if b and b.enabled and (a.x, a.y) == (b.x, b.y):
        return "duplicate"
    return "extend"


def detect_position(layout, internal, ext):
    a, b = layout.outputs[internal], layout.outputs[ext]
    if b.x >= a.x + a.size()[0]:
        return "right"
    if a.x >= b.x + b.size()[0]:
        return "left"
    if b.y >= a.y + a.size()[1]:
        return "below"
    return "above"


# --------------------------------------------------------------------------- CLI

def cli(args):
    screen = Screen()
    if args.list:
        lay = screen.current_layout()
        for o in screen.outputs:
            c = lay.outputs[o.name]
            state = (f"{c.mode.label}@{c.mode.rate:g}Hz +{c.x}+{c.y}" if c.enabled
                     else "off") if o.connected else "disconnected"
            star = " (primary)" if o.name == lay.primary else ""
            print(f"{o.name:10} {state}{star}")
        print("mode:", detect_mode(screen, lay))
        return 0
    screen.apply(screen.preset(args.mode, args.position))
    return 0


# --------------------------------------------------------------------------- GUI

MODES = [
    ("pc-only", "PC screen only", "computer-symbolic"),
    ("duplicate", "Duplicate", "edit-copy-symbolic"),
    ("extend", "Extend", "view-dual-symbolic"),
    ("second-only", "Second screen only", "video-display-symbolic"),
]
REVERT_SECONDS = 15


def gui():
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib, Gtk

    class Window(Adw.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title="Projection Settings",
                             default_width=520, default_height=640)
            self.screen = Screen()
            self.pending = None
            self._building = False

            tv = Adw.ToolbarView()
            header = Adw.HeaderBar()
            refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Re-detect displays")
            refresh.connect("clicked", lambda *_: self.rebuild(reload=True))
            header.pack_start(refresh)
            tv.add_top_bar(header)
            self.toasts = Adw.ToastOverlay()
            scroller = Gtk.ScrolledWindow(vexpand=True)
            self.page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                                margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
            clamp = Adw.Clamp(maximum_size=560, child=self.page)
            scroller.set_child(clamp)
            self.toasts.set_child(scroller)
            tv.set_content(self.toasts)
            self.set_content(tv)
            self.rebuild()

        # -- building ---------------------------------------------------------

        def rebuild(self, reload=False):
            if reload:
                self.screen.reload()
            self._building = True
            while (child := self.page.get_first_child()):
                self.page.remove(child)
            self.layout = self.screen.current_layout()
            internal, externals = self.screen.internal_and_external()
            self.internal = internal.name if internal else None
            self.external = externals[0].name if externals else None

            # Projection mode buttons (Win+P style)
            group = Adw.PreferencesGroup(title="Projection mode")
            grid = Gtk.Grid(column_spacing=8, row_spacing=8, column_homogeneous=True)
            current = detect_mode(self.screen, self.layout)
            first = None
            for i, (key, label, icon) in enumerate(MODES):
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                              margin_top=12, margin_bottom=12)
                box.append(Gtk.Image(icon_name=icon, pixel_size=32))
                box.append(Gtk.Label(label=label, wrap=True, justify=Gtk.Justification.CENTER))
                btn = Gtk.ToggleButton(child=box, active=key == current)
                if first:
                    btn.set_group(first)
                first = first or btn
                btn.set_sensitive(key == "pc-only" or self.external is not None)
                btn.connect("toggled", self.on_mode, key)
                grid.attach(btn, i % 2, i // 2, 1, 1)
            group.add(grid)
            self.page.append(group)

            if self.external:
                pos_group = Adw.PreferencesGroup()
                row = Adw.ComboRow(title="Extend position",
                                   subtitle=f"Where {self.external} sits relative to {self.internal}",
                                   model=Gtk.StringList.new([p.title() for p in POSITIONS]))
                if current == "extend":
                    row.set_selected(POSITIONS.index(
                        detect_position(self.layout, self.internal, self.external)))
                row.connect("notify::selected", self.on_position)
                self.position_row = row
                pos_group.add(row)
                self.page.append(pos_group)
            else:
                self.position_row = None
                self.page.append(Adw.StatusPage(
                    icon_name="video-display-symbolic", title="No second display",
                    description="Connect a projector or monitor, then press refresh."))

            # Per-display advanced settings
            for o in self.screen.connected:
                self.page.append(self.output_group(o))

            apply_btn = Gtk.Button(label="Apply", css_classes=["suggested-action", "pill"],
                                   halign=Gtk.Align.CENTER)
            apply_btn.connect("clicked", lambda *_: self.apply(self.layout))
            self.page.append(apply_btn)
            self._building = False

        def output_group(self, o):
            cfg = self.layout.outputs[o.name]
            mm = f" · {(o.mm[0] ** 2 + o.mm[1] ** 2) ** 0.5 / 25.4:.0f}″" if o.mm[0] else ""
            group = Adw.PreferencesGroup(title=o.name, description=f"Connected{mm}")

            enabled = Adw.SwitchRow(title="Enabled", active=cfg.enabled)
            group.add(enabled)

            sizes = []
            for m in o.modes:
                if (m.width, m.height) not in sizes:
                    sizes.append((m.width, m.height))
            res_row = Adw.ComboRow(title="Resolution", model=Gtk.StringList.new(
                [f"{w}×{h}" + ("  (preferred)" if (w, h) == (o.preferred.width, o.preferred.height) else "")
                 for w, h in sizes]))
            rate_row = Adw.ComboRow(title="Refresh rate")
            rot_row = Adw.ComboRow(title="Rotation", model=Gtk.StringList.new(list(ROTATIONS)))
            primary = Adw.SwitchRow(title="Primary display", active=self.layout.primary == o.name)
            for r in (res_row, rate_row, rot_row, primary):
                group.add(r)

            mode = cfg.mode or o.preferred
            res_row.set_selected(sizes.index((mode.width, mode.height)))
            rot_row.set_selected(list(ROTATIONS.values()).index(cfg.rotation)
                                 if cfg.rotation in ROTATIONS.values() else 0)

            def rates_for(size):
                return [m for m in o.modes if (m.width, m.height) == size]

            def fill_rates(select=None):
                rates = rates_for(sizes[res_row.get_selected()])
                rate_row.set_model(Gtk.StringList.new([f"{m.rate:g} Hz" for m in rates]))
                idx = rates.index(select) if select in rates else 0
                rate_row.set_selected(idx)

            fill_rates(mode)

            def sync(*_):
                if self._building:
                    return
                c = self.layout.outputs[o.name]
                c.enabled = enabled.get_active()
                rates = rates_for(sizes[res_row.get_selected()])
                c.mode = rates[min(rate_row.get_selected(), len(rates) - 1)]
                c.rotation = list(ROTATIONS.values())[rot_row.get_selected()]
                if primary.get_active():
                    self.layout.primary = o.name
                for w in (res_row, rate_row, rot_row, primary):
                    w.set_sensitive(c.enabled)
                self.relayout()

            def on_res(*_):
                if self._building:
                    return
                self._building = True
                fill_rates()
                self._building = False
                sync()

            res_row.connect("notify::selected", on_res)
            for w, sig in ((enabled, "notify::active"), (rate_row, "notify::selected"),
                           (rot_row, "notify::selected"), (primary, "notify::active")):
                w.connect(sig, sync)
            for w in (res_row, rate_row, rot_row, primary):
                w.set_sensitive(cfg.enabled)
            return group

        def relayout(self):
            """Keep positions consistent after manual mode/rotation edits."""
            if not (self.internal and self.external):
                return
            a, b = self.layout.outputs[self.internal], self.layout.outputs[self.external]
            if a.enabled and b.enabled and (a.x, a.y) != (b.x, b.y):
                place(a, b, POSITIONS[self.position_row.get_selected()])
            elif a.enabled != b.enabled:
                for c in (a, b):
                    c.x = c.y = 0

        # -- handlers -----------------------------------------------------------

        def selected_position(self):
            return POSITIONS[self.position_row.get_selected()] if self.position_row else "right"

        def on_mode(self, btn, key):
            if self._building or not btn.get_active():
                return
            self.apply(self.screen.preset(key, self.selected_position()))

        def on_position(self, *_):
            if self._building:
                return
            if detect_mode(self.screen, self.layout) == "extend":
                lay = self.layout.copy()
                place(lay.outputs[self.internal], lay.outputs[self.external], self.selected_position())
                self.apply(lay)

        def apply(self, layout):
            before = self.screen.current_layout()
            try:
                self.screen.apply(layout)
            except Exception as e:
                self.toasts.add_toast(Adw.Toast(title=f"Could not apply: {e}", timeout=6))
                self.rebuild(reload=True)
                return
            self.rebuild()
            self.confirm(before)

        def confirm(self, before):
            remaining = [REVERT_SECONDS]
            dlg = Adw.AlertDialog(heading="Keep these display settings?",
                                  body=f"Reverting in {REVERT_SECONDS} seconds…")
            dlg.add_response("revert", "Revert")
            dlg.add_response("keep", "Keep changes")
            dlg.set_response_appearance("keep", Adw.ResponseAppearance.SUGGESTED)
            dlg.set_default_response("keep")
            dlg.set_close_response("revert")

            def tick():
                remaining[0] -= 1
                if remaining[0] <= 0:
                    dlg.close()
                    return False
                dlg.set_body(f"Reverting in {remaining[0]} seconds…")
                return True

            timer = GLib.timeout_add_seconds(1, tick)

            def done(_d, response):
                if remaining[0] > 0:
                    GLib.source_remove(timer)
                if response != "keep":
                    try:
                        self.screen.apply(before)
                    except Exception as e:
                        self.toasts.add_toast(Adw.Toast(title=f"Revert failed: {e}"))
                    self.rebuild(reload=True)

            dlg.connect("response", done)
            dlg.present(self)

    app = Adw.Application(application_id="local.ProjectionSettings")
    app.connect("activate", lambda a: (a.get_active_window() or Window(a)).present())
    return app.run([sys.argv[0]])


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=[m[0] for m in MODES])
    p.add_argument("--position", choices=POSITIONS, default="right")
    p.add_argument("--list", action="store_true", help="show current display state")
    args = p.parse_args()
    try:
        if args.mode or args.list:
            return cli(args)
        return gui()
    except (RuntimeError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
