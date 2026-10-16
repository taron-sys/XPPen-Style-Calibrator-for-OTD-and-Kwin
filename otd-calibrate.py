#!/usr/bin/env python3
"""
otd-calibrate — a 5-point screen calibrator for OpenTabletDriver.

Shows the same five crosshairs XP-Pen's own calibrator uses (top-left, bottom-left,
bottom-right, top-right, centre), measures where the pointer actually lands when you
tap each one, solves for the tablet-area rectangle that removes the error, and writes
it into OTD's settings.json.

Why this works
--------------
OTD maps a *tablet area* rectangle onto a *display area* rectangle, and the tablet
area has exactly four free parameters (X, Y are the rectangle's centre; Width, Height
its size).  A five-point measurement over-determines those four, so the calibration
fits OTD's model exactly rather than approximately.

The measurement model, per axis:

    cursor = composition( normalize( tablet_area, pen_position ) )

The composition (OTD -> uinput -> compositor -> screen) is affine but its gain is not
knowable a priori, because the X11/compositor layer rescales the virtual device's
absolute range.  The solver therefore works in the space it can observe:

    want_i = normalize_display( target_i )     # where the mark is, as a fraction 0..1
    have_i = normalize_display( observed_i )   # where the pointer actually went
    t_i    = area_left + have_i * area_width   # the pen position that produced that
    solve  area_left' + want_i * area_width' = t_i   (least squares over the 5 points)

i.e. "re-map the area so that the pen position which currently lands at `have` instead
lands at `want`".  A perfect calibration is a fixed point of that equation: when the
pointer already lands on the marks, have == want and the solved area is unchanged, so
an already-correct tablet is left alone.

If the unmodelled compositor gain differs from 1 the first pass is approximate; the
residual is then measured directly on the next run and driven down.  `--iterate N`
does that loop internally.

Requirements: Python 3.8+, standard library only (Tk for the overlay).
Run it with OTD's daemon active and actually driving the tablet; any other driver that
has claimed the device must be stopped first.  (On XP-Pen tablets that means the vendor
`PenTablet` driver, which holds the pen interface exclusively and will leave OTD with
nothing to read.)

The overlay is filled with a translucent *grey* rather than black: a black fill makes
the stylus tip hard to see against the panel, which is itself a source of mis-taps.
Tune the fill with --bg and how much it hides the screen with --opacity.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

DEFAULT_CONFIG = os.path.expanduser("~/.config/OpenTabletDriver/settings.json")
SERVICE = "opentabletdriver"

# XP-Pen's order: top-left, bottom-left, bottom-right, top-right, centre.
POINT_NAMES = [
    "top-left",
    "bottom-left",
    "bottom-right",
    "top-right",
    "centre",
]


# --------------------------------------------------------------------------- #
# settings.json
# --------------------------------------------------------------------------- #

def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def pick_profile(settings, index):
    profiles = settings.get("Profiles") or []
    if not profiles:
        sys.exit("error: settings.json has no Profiles")
    if index >= len(profiles):
        sys.exit(f"error: profile {index} does not exist (found {len(profiles)})")
    return profiles[index]


def area_rect(area):
    """OTD Area -> (left, top, width, height).  X/Y is the rectangle's CENTRE."""
    w = float(area["Width"])
    h = float(area["Height"])
    return (float(area["X"]) - w / 2.0, float(area["Y"]) - h / 2.0, w, h)


def rect_to_area(left, top, width, height, rotation):
    return {
        "Width": width,
        "Height": height,
        "X": left + width / 2.0,
        "Y": top + height / 2.0,
        "Rotation": rotation,
    }


# --------------------------------------------------------------------------- #
# the screen the pen lands on
# --------------------------------------------------------------------------- #
#
# Normally that is OTD's display area.  But KDE can also pin OTD's virtual tablet to
# one output (System Settings -> Tablet), with OTD mapped to the whole desktop -- the
# XP-Pen driver's way round, which survives monitors coming and going.  Then OTD's
# display area spans screens the pen never reaches, and the real target is the
# output KWin maps the device to.  The solver is unchanged: it only needs the
# rectangle the tablet area's 0..1 range ends up on.

def _busctl(*args):
    out = subprocess.run(["busctl", "--user", *args],
                         capture_output=True, text=True, timeout=5)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return out.stdout.strip()


def _busctl_str(path, name):
    out = _busctl("get-property", "org.kde.KWin", path,
                  "org.kde.KWin.InputDevice", name)
    return out.partition(" ")[2].strip().strip('"')


def kwin_mapped_output():
    """The output KWin pins OTD's virtual tablet to, or None."""
    try:
        out = _busctl("get-property", "org.kde.KWin", "/org/kde/KWin/InputDevice",
                      "org.kde.KWin.InputDeviceManager", "devicesSysNames")
        names = [t.strip('"') for t in out.split()[1:] if t.startswith('"')]
        for n in names:
            path = f"/org/kde/KWin/InputDevice/{n}"
            if _busctl_str(path, "name").startswith("OpenTabletDriver"):
                output = _busctl_str(path, "outputName")
                if output:
                    return output
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        pass
    return None


def output_rect(name):
    """(left, top, width, height) of an output in desktop pixels, or None."""
    try:
        out = subprocess.run(["kscreen-doctor", "-j"], capture_output=True,
                             text=True, timeout=10)
        outputs = [o for o in json.loads(out.stdout).get("outputs", [])
                   if o.get("enabled") and o.get("connected")]
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if not outputs:
        return None
    # XWayland's root window starts at the desktop's top-left corner.
    min_x = min(o["pos"]["x"] for o in outputs)
    min_y = min(o["pos"]["y"] for o in outputs)
    for o in outputs:
        if o["name"] != name:
            continue
        w, h = o["size"]["width"], o["size"]["height"]
        if o.get("rotation") in (2, 8):          # rotated left / right
            w, h = h, w
        scale = float(o.get("scale") or 1.0)
        return (float(o["pos"]["x"] - min_x), float(o["pos"]["y"] - min_y),
                w / scale, h / scale)
    return None


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
                     f"size {sw}x{sh}; OTD's display area does not fit.\n"
                     "       Check that OTD's display area matches your monitor "
                     "layout, then re-run.")

        # Put the annotation inside the tablet's own screen area, not at the top of the
        # whole workspace: with several monitors those are different places, and the
        # crosshairs the user is looking at are in the former.  Derived from the marks
        # themselves -- horizontally between the two top marks and just below them,
        # which is empty space (only the corners and the centre carry crosshairs).
        cx = (targets[0][0] + targets[3][0]) / 2.0
        cy = min(targets[0][1] + 90.0, targets[1][1] - 60.0)
        cx -= self.origin[0]
        cy -= self.origin[1]

        # White text over a black outline, so it stays legible whether the fill lands on
        # a dark or a light part of the screen -- no single flat colour can manage that,
        # and the fill is deliberately light now.
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
        root.bind("<Motion>", self._motion)
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
        label = (f"Screen calibration — point {self.index + 1} of 5\n"
                 f"Touch the crosshair: {POINT_NAMES[self.index]}\n"
                 f"(Esc or right-click cancels)")
        for item in self.status_halo + [self.status]:
            self.canvas.itemconfigure(item, text=label)

    def _motion(self, event):
        # Nudge the text out of the way is unnecessary; just keep the cursor free.
        pass

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

def _fit_axis(old_left, old_size, ws, ts):
    """
    Least squares for  new_left + w * new_size = t  over the sample set.
    Returns (new_left, new_size, max_residual).
    """
    n = len(ws)
    sw = sum(ws)
    sww = sum(w * w for w in ws)
    sb = sum(t for t in ts)
    swb = sum(w * t for w, t in zip(ws, ts))
    det = n * sww - sw * sw
    if abs(det) < 1e-12:
        return old_left, old_size, 0.0
    new_size = (n * swb - sw * sb) / det
    new_left = (sb - sw * new_size) / n
    resid = max(abs(new_left + w * new_size - t) for w, t in zip(ws, ts))
    return new_left, new_size, resid


def solve_area(old_tablet, disp, targets, observed):
    """Return (new_tablet_dict, diagnostics)."""
    tl, tt, tw, th = old_tablet
    dl, dt, dw, dh = disp

    # Where each mark sits, as a fraction across the display: the coefficient the new
    # area must reproduce.
    want_x = [(tx - dl) / dw for tx, _ in targets]
    want_y = [(ty - dt) / dh for _, ty in targets]

    # Where the pen actually is, as a fraction of the OLD area, recovered from the
    # pointer position the old mapping produced.  (This assumes the composition from
    # OTD's output to the pointer is the identity across the display rect to first
    # order; any residual is what the next iteration measures and removes.)
    have_x = [(cx - dl) / dw for cx, _ in observed]
    have_y = [(cy - dt) / dh for _, cy in observed]

    # The pen's raw position is therefore old_left + have*old_size, and the new area
    # must place that same position at fraction `want`:
    #     new_left + want * new_size = old_left + have * old_size
    ts_x = [tl + u * tw for u in have_x]
    ts_y = [tt + u * th for u in have_y]

    new_left, new_w, res_x = _fit_axis(tl, tw, want_x, ts_x)
    new_top, new_h, res_y = _fit_axis(tt, th, want_y, ts_y)

    diag = {
        "residual_x": res_x,
        "residual_y": res_y,
        "scale_x": new_w / tw if tw else 1.0,
        "scale_y": new_h / th if th else 1.0,
    }
    return (new_left, new_top, new_w, new_h), diag


def residual_report(targets, observed):
    errs = [(abs(cx - tx), abs(cy - ty))
            for (tx, ty), (cx, cy) in zip(targets, observed)]
    return (sum(e[0] for e in errs) / len(errs),
            sum(e[1] for e in errs) / len(errs),
            max(max(*e) for e in errs))


# --------------------------------------------------------------------------- #
# applying
# --------------------------------------------------------------------------- #

def vendor_driver_running():
    try:
        out = subprocess.run(["pgrep", "-x", "PenTablet"],
                             capture_output=True, text=True)
        return out.returncode == 0
    except OSError:
        return False


def restart_daemon():
    subprocess.run(["systemctl", "--user", "restart", SERVICE], check=False)
    # Wait for the daemon to come back and re-open the device.
    for _ in range(30):
        time.sleep(0.5)
        try:
            out = subprocess.run(["pgrep", "-x", "OpenTabletDriver.Daemon"],
                                 capture_output=True, text=True)
            if out.returncode == 0:
                time.sleep(1.5)
                return True
        except OSError:
            return False
    return False


def write_area(config_path, profile_index, new_area):
    settings = load_config(config_path)
    profile = pick_profile(settings, profile_index)
    profile["AbsoluteModeSettings"]["Tablet"] = new_area
    backup = config_path + ".bak-otd-calibrate"
    if not os.path.exists(backup):
        shutil.copy2(config_path, backup)
    tmp = config_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, config_path)
    return backup


# --------------------------------------------------------------------------- #
# selftest
# --------------------------------------------------------------------------- #

def selftest(verbose=True):
    """
    Drive the solver with synthetic measurements and check it recovers a known,
    deliberately wrong tablet area -- both with an ideal composition (gain 1) and
    with an extra compositor gain, where convergence takes a few passes.
    """
    disp = (1307.0, 1168.0, 2194.0, 1234.0)       # left, top, w, h (DP-3)
    true_tablet = (0.0, 0.0, 340.0, 190.0)        # the "physically correct" area
    targets = make_targets(disp, 50)

    failures = []
    for gain, offset, label in ((1.0, 0.0, "ideal composition"),
                                (1.12, 7.0, "extra compositor gain")):
        area = (2.0, -3.0, 350.0, 196.0)         # a wrong starting area
        if verbose:
            print(f"\n--- {label}: gain={gain} offset={offset} ---")
        for it in range(1, 6):
            # Simulate: pen placed under each mark, driven through `area`, then the
            # (unknown) composition turns OTD's output into the cursor position.
            observed = []
            for tx, ty in targets:
                wx = (tx - disp[0]) / disp[2]
                wy = (ty - disp[1]) / disp[3]
                # pen's tablet coordinate for the physical point under the mark
                pen_x = true_tablet[0] + wx * true_tablet[2]
                pen_y = true_tablet[1] + wy * true_tablet[3]
                # what OTD believes, given the current area
                ux = (pen_x - area[0]) / area[2]
                uy = (pen_y - area[1]) / area[3]
                # OTD output, then the compositor's affine
                ox = disp[0] + ux * disp[2]
                oy = disp[1] + uy * disp[3]
                cx = gain * ox + offset
                cy = gain * oy + offset
                observed.append((cx, cy))

            ex, ey, emax = residual_report(targets, observed)
            if it == 1 and verbose:
                print(f"  before: mean |err| = {ex:6.1f}, {ey:6.1f} px"
                      f"   (max {emax:.1f})")
            if emax < 2.0:
                if verbose:
                    print(f"  converged after {it - 1} correction(s): "
                          f"mean |err| = {ex:.2f}, {ey:.2f} px")
                break
            (nl, nt, nw, nh), _diag = solve_area(area, disp, targets, observed)
            area = (nl, nt, nw, nh)
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

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="5-point screen calibrator for OpenTabletDriver.")
    ap.add_argument("--config", default=DEFAULT_CONFIG,
                    help="path to settings.json (default: %(default)s)")
    ap.add_argument("--profile", type=int, default=0,
                    help="profile index to calibrate (default: %(default)s)")
    ap.add_argument("--inset", type=float, default=50.0,
                    help="inset of the marks from the display corners, px")
    ap.add_argument("--opacity", type=float, default=0.55,
                    help="overlay opacity, 0..1 — how strongly the fill hides the "
                         "screen behind it (default: %(default)s)")
    ap.add_argument("--bg", type=parse_gray, default="#b0b0b0",
                    help="overlay fill: a 0-255 grey level or #rrggbb.  Light grey, not "
                         "black, so the stylus tip stays visible against it "
                         "(default: %(default)s)")
    ap.add_argument("--tolerance", type=float, default=2.0, metavar="PX",
                    help="max pointer error considered acceptable "
                         "(default: %(default)s)")
    ap.add_argument("--apply", action="store_true",
                    help="write the result to settings.json and restart the daemon "
                         "(default: print the proposal only)")
    ap.add_argument("--iterate", type=int, default=1, metavar="N",
                    help="with --apply, repeat measure+solve up to N times")
    ap.add_argument("--screen", metavar="OUTPUT",
                    help="output the pen lands on, e.g. DP-3 (default: the one KDE "
                         "pins OTD's tablet to, else OTD's display area)")
    ap.add_argument("--selftest", action="store_true",
                    help="run the solver against synthetic data and exit")
    args = ap.parse_args(argv)

    if args.selftest:
        return 0 if selftest() else 1

    if not os.path.exists(args.config):
        sys.exit(f"error: {args.config} not found — is OTD configured?")

    if vendor_driver_running():
        print("warning: the XP-Pen vendor driver (PenTablet) is running.\n"
              "         It holds USB interface 2, so OTD cannot drive the pen and\n"
              "         this measurement will be garbage.  Stop it first.")

    settings = load_config(args.config)
    profile = pick_profile(settings, args.profile)
    tablet = area_rect(profile["AbsoluteModeSettings"]["Tablet"])
    disp = area_rect(profile["AbsoluteModeSettings"]["Display"])

    screen = args.screen or kwin_mapped_output()
    if screen:
        rect = output_rect(screen)
        if rect is None:
            sys.exit(f"error: output {screen!r} not found or not enabled")
        print(f"KDE maps the pen to {screen}; calibrating against that output "
              "rather than OTD's display area")
        disp = rect

    print(f"profile   : {profile.get('Tablet', '?')}")
    print(f"tablet area: {tablet[2]:.3f} x {tablet[3]:.3f} mm "
          f"@ left {tablet[0]:.3f}, top {tablet[1]:.3f}")
    print(f"display    : {disp[2]:.0f} x {disp[3]:.0f} px "
          f"@ left {disp[0]:.0f}, top {disp[1]:.0f}")

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
            print("        already within tolerance — leaving the area alone.")
            return 0

        new_tablet, diag = solve_area(tablet, disp, targets, observed)
        print(f"        proposed tablet area: {new_tablet[2]:.3f} x "
              f"{new_tablet[3]:.3f} mm @ left {new_tablet[0]:.3f}, "
              f"top {new_tablet[1]:.3f}")
        print(f"        scale {diag['scale_x']:.5f} x {diag['scale_y']:.5f}, "
              f"fit residual {diag['residual_x']:.2f}/{diag['residual_y']:.2f} px")

        if abs(diag["scale_x"] - 1) > 0.25 or abs(diag["scale_y"] - 1) > 0.25:
            print("        refusing: correction exceeds 25 %; the measurement "
                  "looks wrong.  Re-run and aim more carefully.")
            return 1

        if not args.apply:
            print("\ndry run — nothing written.  Re-run with --apply to use this, "
                  "then run once more to check the residual.")
            return 0

        area_dict = rect_to_area(*new_tablet,
                                 profile["AbsoluteModeSettings"]["Tablet"]
                                 .get("Rotation", 0.0))
        backup = write_area(args.config, args.profile, area_dict)
        print(f"        wrote {args.config} (backup: {backup})")
        if not restart_daemon():
            print("        warning: could not confirm the daemon restarted; "
                  "restart it yourself if the pen stops responding.")
        tablet = new_tablet
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
