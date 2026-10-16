#!/usr/bin/env python3
"""
pen-calibrate — a 5-point calibrator for a built-in pen digitizer.

Shows the same five crosshairs XP-Pen's own calibrator uses (top-left,
bottom-left, bottom-right, top-right, centre), measures where the pointer
actually lands when you tap each one, solves for the affine correction that
removes the error, and writes it into the tablet's calibration matrix — the same
value System Settings' Tablet / Touchscreen pages show, so their Reset button
still works on it.

This is the sibling of otd-calibrate.py.  That one drives OpenTabletDriver
through its settings.json; this one drives the compositor (KWin) over D-Bus for
a digitizer that is part of the machine — a pen-enabled convertible, where there
is no vendor driver and nothing to configure but the matrix.

Why this works
--------------
KWin keeps a 4x4 matrix per input device and pushes the top-left 3x3 into
libinput, which applies it to the device's *normalised* coordinates.  For a pen
tool that means:

    screen = M . pen_normalised          # column vector, [0,1] device space

Five measured points over-determine the six free parameters of that affine map,
so the fit lands on OTD-style least squares rather than a guess.  We measure
where the pointer *went* (`have`) and where the mark *is* (`want`), solve the
correction C that sends have -> want, and compose it onto whatever matrix is
already there:

    M_new = C . M_old      (a correction in the space we can observe)

A correct calibration is a fixed point: when the pointer already lands on the
marks, have == want, C is the identity, and the matrix is left alone — which is
what makes the tool safe to re-run.

If some unmodelled stage (output scaling, a compositor gain) sits between the
matrix and the pointer, the first pass is approximate and the residual is
measured again on the next run.  `--iterate N` does that loop internally.

The matrix layout
-----------------
Established by measurement, not guesswork -- `--probe` re-derives it on demand.
The 16 comma-separated values are a row-major 4x4 whose top-left 3x3 is the
affine map above:

    index  0  1  2  3     ->   m00 m01 m02 m03      x' = m00*x + m01*y + m02
    index  4  5  6  7     ->   m10 m11 m12 m13      y' = m10*x + m11*y + m12
    index  8  9 10 11     ->   m20 m21 m22 m23
    index 12 13 14 15     ->   m30 m31 m32 m33

so scale lives on 0 and 5, shear on 1 and 4, and translation on 2 and 6.
KWin requires *exactly* 16 values: anything else is silently replaced with the
identity, with no error, so a malformed write looks like a working one.

Requirements: Python 3.8+, standard library only (Tk for the overlay, `busctl`
for D-Bus).  KWin on a Wayland session.  Verified on KWin 6.7.5 / Plasma 6.7.5.

Everything is reversible: nothing is written without --apply, the current value
is backed up first, and `--reset` puts the identity back.
"""

import argparse
import os
import shutil
import subprocess
import sys
import time

BUS = "org.kde.KWin"
IFACE = "org.kde.KWin.InputDevice"
IFACE_MGR = "org.kde.KWin.InputDeviceManager"
MANAGER_PATH = "/org/kde/KWin/InputDevice"
KC_MINPUTRC = os.path.expanduser("~/.config/kcminputrc")

IDENTITY = [1, 0, 0, 0,
            0, 1, 0, 0,
            0, 0, 1, 0,
            0, 0, 0, 1]

# Where the four parameters we solve for live in the 16-value string.
I_XX, I_XY, I_XT = 0, 1, 2
I_YX, I_YY, I_YT = 4, 5, 6

# XP-Pen's order: top-left, bottom-left, bottom-right, top-right, centre.
POINT_NAMES = [
    "top-left",
    "bottom-left",
    "bottom-right",
    "top-right",
    "centre",
]


# --------------------------------------------------------------------------- #
# D-Bus (via busctl; no python dbus module needed)
# --------------------------------------------------------------------------- #

def _busctl(*args):
    try:
        out = subprocess.run(["busctl", "--user", *args],
                             capture_output=True, text=True)
    except OSError as exc:
        sys.exit(f"error: could not run busctl ({exc}).\n"
                 "       This tool talks to KWin over the session bus.")
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "busctl failed")
    return out.stdout.strip()


def _prop(path, name):
    """Return (type, payload) for one D-Bus property."""
    out = _busctl("get-property", BUS, path, IFACE, name)
    kind, _, rest = out.partition(" ")
    return kind, rest.strip()


def _as_str(payload):
    p = payload.strip()
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        return p[1:-1]
    return p


def _as_bool(payload):
    return payload.strip() == "true"


def device_path(sys_name):
    return f"{MANAGER_PATH}/{sys_name}"


def list_devices():
    """Every input device KWin exposes, as dicts of the properties we need."""
    out = _busctl("get-property", BUS, MANAGER_PATH, IFACE_MGR,
                  "devicesSysNames")
    # e.g.  as 17 "event0" "event1" ...
    names = [t.strip('"') for t in out.split()[1:] if t.startswith('"')]

    devices = []
    for name in names:
        path = device_path(name)
        try:
            d = {
                "sysName": name,
                "path": path,
                "name": _as_str(_prop(path, "name")[1]),
                "tabletTool": _as_bool(_prop(path, "tabletTool")[1]),
                "touch": _as_bool(_prop(path, "touch")[1]),
                "enabled": _as_bool(_prop(path, "enabled")[1]),
                "supportsCalibration":
                    _as_bool(_prop(path, "supportsCalibrationMatrix")[1]),
                "outputName": _as_str(_prop(path, "outputName")[1]),
            }
        except RuntimeError:
            continue          # a device that vanished mid-enumeration
        devices.append(d)
    return devices


def pick_device(devices, wanted=None):
    usable = [d for d in devices
              if d["tabletTool"] and d["supportsCalibration"] and d["enabled"]]
    if wanted:
        usable = [d for d in usable if wanted.lower() in d["name"].lower()]
    if not usable:
        lines = ["error: no enabled pen tool with a calibratable matrix."]
        for d in devices:
            if d["tabletTool"]:
                lines.append(f"       saw pen tool {d['name']!r} "
                             f"(calibratable={d['supportsCalibration']}, "
                             f"enabled={d['enabled']})")
        sys.exit("\n".join(lines))
    if len(usable) > 1:
        print("note: several pen tools qualify; using the first. "
              "Pick one with --device NAME:")
        for d in usable:
            print(f"        {d['name']}")
    return usable[0]


def read_matrix(dev):
    _kind, payload = _prop(dev["path"], "calibrationMatrix")
    return parse_matrix(_as_str(payload))


def write_matrix(dev, m):
    """Write the matrix and read it back.  Raises if it did not take."""
    _busctl("set-property", BUS, dev["path"], IFACE,
            "calibrationMatrix", "s", format_matrix(m))
    back = read_matrix(dev)
    if not close_enough(back, m):
        raise RuntimeError(
            "KWin did not accept the matrix (it is probably malformed; KWin "
            "falls back to the identity in silence).\n"
            f"  wrote: {format_matrix(m)}\n"
            f"  read : {format_matrix(back)}")
    return back


# --------------------------------------------------------------------------- #
# the matrix
# --------------------------------------------------------------------------- #

def parse_matrix(text):
    """16 comma-separated floats -> list of 16; anything else -> identity."""
    parts = [p for p in text.split(",")]
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        return list(IDENTITY)
    if len(vals) != 16:
        return list(IDENTITY)
    return vals


def format_matrix(m):
    return ",".join(repr(float(v)) for v in m)


def close_enough(a, b, tol=1e-5):
    return all(abs(x - y) <= tol * max(1.0, abs(x), abs(y))
               for x, y in zip(a, b))


def terms(m):
    return {
        "xx": m[I_XX], "xy": m[I_XY], "xt": m[I_XT],
        "yx": m[I_YX], "yy": m[I_YY], "yt": m[I_YT],
    }


def compose(c_scale_x, c_off_x, c_scale_y, c_off_y, m):
    """C . M, where C is the axis-independent correction (scale+offset)."""
    out = list(m)
    out[I_XX] = c_scale_x * m[I_XX]
    out[I_XY] = c_scale_x * m[I_XY]
    out[I_XT] = c_scale_x * m[I_XT] + c_off_x
    out[I_YX] = c_scale_y * m[I_YX]
    out[I_YY] = c_scale_y * m[I_YY]
    out[I_YT] = c_scale_y * m[I_YT] + c_off_y
    return out


# --------------------------------------------------------------------------- #
# targets
# --------------------------------------------------------------------------- #

def make_targets(disp, inset_px):
    """Five points inset from the display-area corners, plus the centre."""
    left, top, w, h = disp
    inset = min(inset_px, w / 4.0, h / 4.0)   # never let the insets cross over
    l, r = left + inset, left + w - inset
    t, b = top + inset, top + h - inset
    return [
        (l, t),                       # top-left
        (l, b),                       # bottom-left
        (r, b),                       # bottom-right
        (r, t),                       # top-right
        ((l + r) / 2.0, (t + b) / 2.0),  # centre
    ]


def parse_gray(value):
    """Accept a 0-255 grey level or '#rrggbb'; return a Tk colour string."""
    v = str(value).strip()
    if v.startswith("#"):
        return v
    try:
        n = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected a 0-255 grey level or #rrggbb, got {value!r}")
    if not 0 <= n <= 255:
        raise argparse.ArgumentTypeError("grey level must be 0-255")
    return f"#{n:02x}{n:02x}{n:02x}"


# --------------------------------------------------------------------------- #
# overlay
# --------------------------------------------------------------------------- #

class Overlay:
    """Translucent full-screen overlay: one red crosshair at a time."""

    ARM = 22          # half-length of the crosshair arms, px
    HIT = 26          # half-size of the accepted hit box, px

    def __init__(self, targets, opacity, bg="#808080"):
        import tkinter as tk  # imported here so --selftest needs no display

        self.tk = tk
        self.targets = targets
        self.index = 0
        self.observed = []
        self.cancelled = False

        root = tk.Tk()
        self.root = root
        root.overrideredirect(True)
        try:
            root.attributes("-topmost", True)
        except tk.TclError:
            pass
        try:
            root.attributes("-alpha", opacity)
        except tk.TclError:
            print("note: window transparency unavailable; overlay will be opaque")

        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{sw}x{sh}+0+0")
        root.configure(bg=bg)

        self.canvas = tk.Canvas(root, bg=bg, highlightthickness=0,
                                width=sw, height=sh, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)

        root.update_idletasks()
        # Where the window really landed.  Normally (0,0), but XWayland / a WM may
        # place it elsewhere, so everything is drawn relative to this.
        self.origin = (root.winfo_rootx(), root.winfo_rooty())

        # Every mark has to be visible and touchable, or the sequence would stall on
        # a crosshair the user cannot reach.
        unreachable = [POINT_NAMES[i] for i, (tx, ty) in enumerate(self.targets)
                       if not (self.origin[0] <= tx <= self.origin[0] + sw
                               and self.origin[1] <= ty <= self.origin[1] + sh)]
        if unreachable:
            root.destroy()
            sys.exit("error: these marks fall outside the overlay window: "
                     f"{', '.join(unreachable)}\n"
                     f"       window at {self.origin[0]},{self.origin[1]} "
                     f"size {sw}x{sh}; the pen's desktop area does not fit.\n"
                     "       Pass --display with the rectangle the pen maps to.")

        # Put the annotation inside the digitizer's own screen area, not at the top
        # of the whole workspace: with several monitors those are different places.
        cx = (targets[0][0] + targets[3][0]) / 2.0
        cy = min(targets[0][1] + 90.0, targets[1][1] - 60.0)
        cx -= self.origin[0]
        cy -= self.origin[1]

        # White text over a black outline, so it stays legible whether the fill lands
        # on a dark or a light part of the screen.
        self.status_halo = [
            self.canvas.create_text(
                cx + dx, cy + dy, fill="#000000",
                font=("Sans", 16, "bold"), text="", justify="center")
            for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2),
                           (-1, -1), (1, -1), (-1, 1), (1, 1))
        ]
        self.status = self.canvas.create_text(
            cx, cy, fill="#ffffff", font=("Sans", 16, "bold"),
            text="", justify="center")
        self.marks = []

        root.bind("<Escape>", lambda _e: self._cancel())
        # Right-click (the pen's barrel button) cancels too, in case the keyboard
        # never reaches the window.
        root.bind("<ButtonPress-3>", lambda _e: self._cancel())
        root.bind("<ButtonPress-1>", self._press)
        # An override-redirect window gets no keyboard focus from the window
        # manager, so Esc would go to whatever had focus before.  Take it.
        root.after(100, self._grab_keyboard)
        self._draw()

    # -- drawing ----------------------------------------------------------- #

    def _draw(self):
        for item in self.marks:
            self.canvas.delete(item)
        self.marks = []
        ox, oy = self.origin
        tx, ty = self.targets[self.index]
        x, y = tx - ox, ty - oy
        for dx, dy in ((self.ARM, 0), (0, self.ARM)):
            self.marks.append(self.canvas.create_line(
                x - dx, y - dy, x + dx, y + dy, fill="#ff0002", width=2))
        label = (f"Pen calibration — point {self.index + 1} of 5\n"
                 f"Tap the crosshair: {POINT_NAMES[self.index]}\n"
                 f"(Esc or right-click cancels)")
        for item in self.status_halo + [self.status]:
            self.canvas.itemconfigure(item, text=label)

    # -- input ------------------------------------------------------------- #

    def _press(self, event):
        px, py = self.root.winfo_pointerxy()
        tx, ty = self.targets[self.index]
        if abs(px - tx) > self.HIT or abs(py - ty) > self.HIT:
            # Outside the hit box: ignore, exactly like XP-Pen's calibrator.
            self.canvas.itemconfigure(self.status, fill="#ffcc44")
            self.root.after(150, lambda: self.canvas.itemconfigure(
                self.status, fill="#ffffff"))
            return
        self.observed.append((float(px), float(py)))
        self.index += 1
        if self.index >= len(self.targets):
            self.root.destroy()
            return
        self._draw()

    def _grab_keyboard(self):
        self.root.focus_force()
        try:
            self.root.grab_set_global()
        except self.tk.TclError:
            pass          # another client holds the grab; focus_force still helps

    def _cancel(self):
        self.cancelled = True
        self.root.destroy()

    # -- entry ------------------------------------------------------------- #

    def run(self):
        self.root.mainloop()
        if self.cancelled or len(self.observed) != len(self.targets):
            return None
        return self.observed


# --------------------------------------------------------------------------- #
# solver
# --------------------------------------------------------------------------- #

def _fit_axis(old_off, old_scale, ws, ts):
    """
    Least squares for  new_off + w * new_scale = t  over the sample set.
    Returns (new_off, new_scale, max_residual).
    """
    n = len(ws)
    sw = sum(ws)
    sww = sum(w * w for w in ws)
    sb = sum(t for t in ts)
    swb = sum(w * t for w, t in zip(ws, ts))
    det = n * sww - sw * sw
    if abs(det) < 1e-12:
        return old_off, old_scale, 0.0
    new_scale = (n * swb - sw * sb) / det
    new_off = (sb - sw * new_scale) / n
    resid = max(abs(new_off + w * new_scale - t) for w, t in zip(ws, ts))
    return new_off, new_scale, resid


def solve_matrix(old_m, disp, targets, observed):
    """Return (new_m, diagnostics).

    The correction C maps observed -> wanted in normalised display space, and is
    composed onto the existing matrix; a shear or rotation already present in it
    is carried through unchanged.
    """
    dl, dt, dw, dh = disp

    want_x = [(tx - dl) / dw for tx, _ in targets]
    want_y = [(ty - dt) / dh for _, ty in targets]
    have_x = [(cx - dl) / dw for cx, _ in observed]
    have_y = [(cy - dt) / dh for _, cy in observed]

    # solve  c_off + have * c_scale = want   per axis
    c_off_x, c_scale_x, res_x = _fit_axis(0.0, 1.0, have_x, want_x)
    c_off_y, c_scale_y, res_y = _fit_axis(0.0, 1.0, have_y, want_y)

    new_m = compose(c_scale_x, c_off_x, c_scale_y, c_off_y, old_m)
    diag = {
        "residual_x": res_x,
        "residual_y": res_y,
        "scale_x": c_scale_x,
        "scale_y": c_scale_y,
    }
    return new_m, diag


def residual_report(targets, observed):
    errs = [(abs(cx - tx), abs(cy - ty))
            for (tx, ty), (cx, cy) in zip(targets, observed)]
    return (sum(e[0] for e in errs) / len(errs),
            sum(e[1] for e in errs) / len(errs),
            max(max(*e) for e in errs))


# --------------------------------------------------------------------------- #
# applying
# --------------------------------------------------------------------------- #

def backup_config(path):
    if os.path.exists(path) and not os.path.exists(path + ".bak-pen-calibrate"):
        shutil.copy2(path, path + ".bak-pen-calibrate")
        return path + ".bak-pen-calibrate"
    return path + ".bak-pen-calibrate"


# --------------------------------------------------------------------------- #
# probe (re-derive the matrix layout)
# --------------------------------------------------------------------------- #

def probe(dev, opacity, bg):
    """
    Re-derive which slots of the string carry what, by measurement.

    Taps a fixed crosshair once per candidate slot; because the same physical
    spot is tapped every time, its normalised position is known, so the pointer's
    displacement is the slot's effect.  Slot 0 is tested with a 0.5 scale as a
    control: if that does not move the pointer, the matrix is not reaching the
    device and nothing else can be read from the run.
    """
    import tkinter as tk

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{sw}x{sh}+0+0")
    root.configure(bg=bg)
    canvas = tk.Canvas(root, bg=bg, highlightthickness=0,
                       width=sw, height=sh, cursor="crosshair")
    canvas.pack(fill="both", expand=True)
    mx, my = sw // 2, sh // 2
    for dx, dy in ((26, 0), (0, 26)):
        canvas.create_line(mx - dx, my - dy, mx + dx, my + dy,
                           fill="#ff0002", width=3)
    status = canvas.create_text(mx, my - 140, fill="#ffffff",
                                font=("Sans", 17, "bold"), justify="center")
    canvas.create_text(mx, my + 140, fill="#888888", font=("Sans", 12),
                       justify="center",
                       text="Tap the crosshair with the pen tip.\n"
                            "Ignore where the cursor goes.")
    root.update()

    tap = [None]
    root.bind("<ButtonPress>", lambda _e: tap.__setitem__(
        0, root.winfo_pointerxy()))

    def wait_tap(msg):
        tap[0] = None
        canvas.itemconfigure(status, text=msg)
        root.update()
        deadline = time.time() + 120
        while tap[0] is None and time.time() < deadline:
            root.update()
            time.sleep(0.02)
        return tap[0]

    tests = [("control: scale 0", I_XX, 0.5),
             ("slot 2", I_XT, 0.25),
             ("slot 6", I_YT, 0.25),
             ("slot 1", I_XY, 0.25),
             ("slot 4", I_YX, 0.25)]

    original = read_matrix(dev)
    try:
        write_matrix(dev, IDENTITY)
        base = wait_tap(f"1/{len(tests) + 1}  tap the crosshair (identity)")
        if base is None:
            print("probe: no tap received")
            return 1
        print(f"crosshair {mx},{my}   identity tap -> {base}  "
              f"(device offset {base[0] - mx:+d},{base[1] - my:+d} px)")

        found = {"xt": None, "yt": None}
        for n, (label, idx, val) in enumerate(tests, start=2):
            m = list(IDENTITY)
            m[idx] = val
            write_matrix(dev, m)
            p = wait_tap(f"{n}/{len(tests) + 1}  tap the crosshair ({label})")
            write_matrix(dev, IDENTITY)
            if p is None:
                print(f"probe: {label}: no tap")
                continue
            dx, dy = p[0] - base[0], p[1] - base[1]
            print(f"  {label:>18}  d=({dx:+5d},{dy:+5d}) px   "
                  f"{100 * dx / sw:+6.2f}%W {100 * dy / sh:+6.2f}%H")
            # a translation slot moves by ~val of the axis; a shear gives ~val*y0
            if abs(dx) > 0.6 * val * sw:
                found["xt"] = idx
            if abs(dy) > 0.6 * val * sh:
                found["yt"] = idx

        print()
        if found["xt"] is None and found["yt"] is None:
            print("probe: the matrix did not move the pointer at all — the "
                  "control slot failed,\n       so either the matrix is not "
                  "reaching the device or the tap did not register.")
            return 1
        print(f"probe: x-translation on slot {found['xt']}, "
              f"y-translation on slot {found['yt']}")
        if found["xt"] != I_XT or found["yt"] != I_YT:
            print(f"       WARNING: this differs from the built-in layout "
                  f"({I_XT}/{I_YT}); the script's I_* constants need "
                  f"updating for this KWin.")
            return 1
        print("probe: matches the built-in layout.")
        return 0
    finally:
        try:
            write_matrix(dev, original)
        except Exception:
            write_matrix(dev, IDENTITY)
        try:
            root.destroy()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# selftest
# --------------------------------------------------------------------------- #

def selftest(verbose=True):
    """
    Drive the solver with synthetic measurements and check it recovers a known,
    deliberately wrong matrix -- both with an ideal composition and with an extra
    gain between the matrix and the pointer, where convergence takes a few passes.
    """
    disp = (0.0, 0.0, 1920.0, 1280.0)
    targets = make_targets(disp, 50)

    failures = []
    for gain, offset, label in ((1.0, 0.0, "ideal composition"),
                                (1.12, 7.0, "extra compositor gain")):
        # a wrong starting matrix: 6% too small, nudged off-centre
        m = list(IDENTITY)
        m[I_XX], m[I_YY] = 0.94, 0.96
        m[I_XT], m[I_YT] = 0.02, -0.01
        if verbose:
            print(f"\n--- {label}: gain={gain} offset={offset} ---")
        for it in range(1, 6):
            observed = []
            for tx, ty in targets:
                # the pen sits under the mark, so its normalised position IS the
                # mark's fraction of the display
                ux, uy = (tx - disp[0]) / disp[2], (ty - disp[1]) / disp[3]
                # through the current matrix, then the unknown composition
                ox = m[I_XX] * ux + m[I_XY] * uy + m[I_XT]
                oy = m[I_YX] * ux + m[I_YY] * uy + m[I_YT]
                cx = disp[0] + ox * disp[2]
                cy = disp[1] + oy * disp[3]
                observed.append((gain * cx + offset, gain * cy + offset))

            ex, ey, emax = residual_report(targets, observed)
            if it == 1 and verbose:
                print(f"  before: mean |err| = {ex:6.1f}, {ey:6.1f} px"
                      f"   (max {emax:.1f})")
            if emax < 2.0:
                if verbose:
                    print(f"  converged after {it - 1} correction(s): "
                          f"mean |err| = {ex:.2f}, {ey:.2f} px")
                break
            m, _diag = solve_matrix(m, disp, targets, observed)
        else:
            failures.append(label)

    if not failures:
        print("\nselftest: PASS")
        return True
    print(f"\nselftest: FAIL ({', '.join(failures)})")
    return False


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def parse_display(text):
    """'WxH+X+Y' -> (left, top, width, height)."""
    try:
        size, x, y = text.lower().split("+")
        w, h = size.split("x")
        return (float(x), float(y), float(w), float(h))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected WxH+X+Y (e.g. 1920x1280+0+0), got {text!r}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="5-point calibrator for a built-in pen digitizer (KWin).")
    ap.add_argument("--device", metavar="NAME",
                    help="pick the pen tool whose name contains NAME")
    ap.add_argument("--display", type=parse_display, metavar="WxH+X+Y",
                    help="the rectangle the pen maps to "
                         "(default: the whole X screen)")
    ap.add_argument("--inset", type=float, default=50.0,
                    help="inset of the marks from the display corners, px")
    ap.add_argument("--opacity", type=float, default=0.55,
                    help="overlay opacity, 0..1 — how strongly the fill hides the "
                         "screen behind it (default: %(default)s)")
    ap.add_argument("--bg", type=parse_gray, default="#b0b0b0",
                    help="overlay fill: a 0-255 grey level or #rrggbb.  Light grey, "
                         "not black, so the stylus tip stays visible against it "
                         "(default: %(default)s)")
    ap.add_argument("--tolerance", type=float, default=2.0, metavar="PX",
                    help="max pointer error considered acceptable "
                         "(default: %(default)s)")
    ap.add_argument("--apply", action="store_true",
                    help="write the result and keep it (default: print the "
                         "proposal only)")
    ap.add_argument("--iterate", type=int, default=1, metavar="N",
                    help="with --apply, repeat measure+solve up to N times")
    ap.add_argument("--reset", action="store_true",
                    help="restore the identity matrix and exit")
    ap.add_argument("--probe", action="store_true",
                    help="re-derive the matrix layout by measurement and exit")
    ap.add_argument("--selftest", action="store_true",
                    help="run the solver against synthetic data and exit")
    args = ap.parse_args(argv)

    if args.selftest:
        return 0 if selftest() else 1

    dev = pick_device(list_devices(), args.device)

    if args.probe:
        return probe(dev, args.opacity, args.bg)

    if args.reset:
        write_matrix(dev, IDENTITY)
        print(f"{dev['name']}: matrix reset to identity.")
        return 0

    old_m = read_matrix(dev)
    t = terms(old_m)
    if not close_enough(old_m, IDENTITY):
        print(f"note: this device already carries a correction "
              f"(scale {t['xx']:.4f} x {t['yy']:.4f}, "
              f"offset {t['xt']:+.4f}, {t['yt']:+.4f}) — it will be built on.")

    if args.display is not None:
        disp = args.display
    else:
        import tkinter as tk
        probe_root = tk.Tk()
        probe_root.withdraw()
        disp = (0.0, 0.0, float(probe_root.winfo_screenwidth()),
                float(probe_root.winfo_screenheight()))
        probe_root.destroy()
        if dev["outputName"]:
            print(f"note: the pen is mapped to output {dev['outputName']!r}, "
                  f"not the whole desktop.\n"
                  f"      If that output is not the whole screen, pass "
                  f"--display WxH+X+Y.")

    print(f"device     : {dev['name']}  ({dev['sysName']})")
    print(f"display    : {disp[2]:.0f} x {disp[3]:.0f} px "
          f"@ left {disp[0]:.0f}, top {disp[1]:.0f}")
    print(f"matrix     : scale {t['xx']:.5f} x {t['yy']:.5f}, "
          f"offset {t['xt']:+.5f}, {t['yt']:+.5f}")

    targets = make_targets(disp, args.inset)
    print("\nTouch the five crosshairs in order: " + ", ".join(POINT_NAMES))
    print("A tap outside a crosshair is ignored, so a slip costs nothing.\n")

    for iteration in range(1, max(1, args.iterate) + 1):
        observed = Overlay(targets, args.opacity, args.bg).run()
        if observed is None:
            print("cancelled")
            return 1

        ex, ey, emax = residual_report(targets, observed)
        print(f"[{iteration}] measured error: mean {ex:.1f}/{ey:.1f} px, "
              f"max {emax:.1f} px")

        if emax < args.tolerance:
            print("        already within tolerance — leaving the matrix alone.")
            return 0

        new_m, diag = solve_matrix(old_m, disp, targets, observed)
        n = terms(new_m)
        print(f"        proposed matrix: scale {n['xx']:.5f} x {n['yy']:.5f}, "
              f"offset {n['xt']:+.5f}, {n['yt']:+.5f}")
        print(f"        correction {diag['scale_x']:.5f} x {diag['scale_y']:.5f}, "
              f"fit residual {diag['residual_x']:.2f}/{diag['residual_y']:.2f} px")

        if (abs(diag["scale_x"] - 1) > 0.25
                or abs(diag["scale_y"] - 1) > 0.25):
            print("        refusing: correction exceeds 25 %; the measurement "
                  "looks wrong.  Re-run and aim more carefully.")
            return 1

        if not args.apply:
            print("\ndry run — nothing written.  Re-run with --apply to use this, "
                  "then run once more to check the residual.")
            return 0

        backup = backup_config(KC_MINPUTRC)
        try:
            write_matrix(dev, new_m)
        except RuntimeError as exc:
            print(f"        error: {exc}")
            return 1
        print(f"        wrote {dev['name']} (config backup: {backup})")
        old_m = new_m
        if iteration == max(1, args.iterate):
            print("\ndone.  Run again to verify the residual.")
            return 0
        print("        re-measuring…\n")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
