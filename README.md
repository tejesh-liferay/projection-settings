# Projection Settings

A small Win+P style display switcher for X11 (works on Openbox/LXDE, no GNOME needed).
Talks to the X server directly via RandR (python-xlib), so it doesn't need the `xrandr` binary.

- Projection modes: PC screen only, Duplicate, Extend (right/left/above/below), Second screen only
- Per-display resolution, refresh rate, rotation, primary display and on/off
- 15-second "Keep these settings?" countdown that reverts automatically

## Requirements

Python 3, `python3-xlib`, GTK 4 + libadwaita (`python3-gobject`, `libadwaita`).

## Usage

```
./projection.py                                   # GUI
./projection.py --list                            # current state
./projection.py --mode extend --position left     # pc-only | duplicate | extend | second-only
```

To install the launcher, copy `projection-settings.desktop` to `~/.local/share/applications/`
and adjust the `Exec=` path.
