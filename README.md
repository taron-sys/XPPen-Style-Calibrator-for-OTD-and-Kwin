# XPPen-Style Pen Calibrator Scripts

> [!WARNING] 
> Attention Attention! This project has been LLM assisted. Use with caution
> These tools working for me doesn't mean they will work for you.
> This is a personal tool and findings I put together for myself,
> And if it helps others, I'd love to hear about it.

Guided **5-point screen calibrators**, in the style of the calibration tool in XP-Pen's Linux
driver: five crosshairs, touched in order, from which the tool solves the correction and (optionally)
writes it for you.

There are two, because a pen reaches the screen by one of two routes:

| script | drives | use it for |
|---|---|---|
| `otd-calibrate.py` | OpenTabletDriver | an external tablet — XP-Pen, Wacom, anything OTD drives — configured through OTD's `settings.json` |
| `pen-calibrate.py` | KWin (libinput) | a digitizer built into the machine — i.e. a pen-enabled convertible, where there is no vendor driver and nothing to adjust but the compositor's matrix |

Both are single-file, Python standard library only, no dependencies to install.

---

## The problem they solve

**General Scope**
I wanted to get a calibration about as accurate as the one XP-Pen does for other devices or without the XP-Pen Driver.

**With OTD,** 
absolute-mode calibration is a tablet-area rectangle you adjust by hand — `X`, `Y`,
`Width`, `Height` — and you tune it by watching the pointer and nudging numbers. That is hard to do
well on a pen display, because a small scale error is invisible at the centre and gross at the
edges: the pointer sits right where you look and drifts as you move out. Eyeballing it tends to
converge slowly, if at all.

**With a built-in digitizer,** 
the equivalent knob is a calibration matrix KWin keeps per device,
normally reached through System Settings → Tablet. It is easier to nudge than OTD's rectangle, but
it is still a nudge: nothing tells you *where* the error is, only that the pen feels off near the
edges of the panel.

Both tools replace the guesswork with the same guided measurement. Touch five marks, and the
correction is computed.

---

## Requirements

Both:

- **Python 3.8+**, standard library only (Tk is used for the overlay).
- A session where a full-screen X11/XWayland window can be placed over the desktop. Verified on KDE
  Plasma (Wayland, via XWayland).
- A pen that moves the pointer in absolute mode.
- No other driver holding the device.

`otd-calibrate.py` additionally needs:

- **OTD configured and running**, and actually driving the tablet.
- **No other driver holding the device.** Anything else that has claimed the tablet must be stopped
  first — on XP-Pen tablets that means the vendor `PenTablet` driver, which holds the pen interface
  exclusively and leaves OTD with nothing to read. The tool warns if it sees it running.

`pen-calibrate.py` additionally needs:

- **KWin on a Wayland session**, and `busctl` on `PATH` — the tool talks to KWin over the session
  bus and needs no root.
- A pen tool that KWin reports as calibratable (`supportsCalibrationMatrix`). The tool lists what it
  sees if it cannot find one.

---

## `otd-calibrate.py` — OpenTabletDriver

### Usage

```bash
# measure and print the proposed change — writes nothing
python3 otd-calibrate.py

# write it into settings.json and restart the daemon
python3 otd-calibrate.py --apply

# apply, re-measure and refine, up to 3 passes (usually converges in 2)
python3 otd-calibrate.py --apply --iterate 3

# exercise the solver offline against synthetic data — needs no display
python3 otd-calibrate.py --selftest
```

If KDE pins OTD's virtual tablet to one output (System Settings → Tablet, with OTD's display
area set to the whole desktop), the tool asks KWin which output that is and calibrates
against it rather than OTD's display area. This is the XP-Pen driver's arrangement, and it
keeps working when other monitors are switched off. Pass `--screen DP-3` to choose the output
yourself.

Esc or a right-click (the pen's barrel button) cancels the overlay.

`--apply` copies your config to `settings.json.bak-otd-calibrate` before the first write.

### Options

| option | default | meaning |
|---|---|---|
| `--config PATH` | `~/.config/OpenTabletDriver/settings.json` | where OTD's settings live |
| `--profile N` | `0` | which profile to calibrate |
| `--inset PX` | `50` | how far the marks sit inside the display-area corners |
| `--bg LEVEL\|#rrggbb` | `#b0b0b0` | overlay fill; a 0–255 grey level or a hex colour |
| `--opacity F` | `0.55` | how strongly the fill hides the screen behind it |
| `--tolerance PX` | `2.0` | worst error still considered acceptable |
| `--apply` | off | write the result and restart the daemon |
| `--iterate N` | `1` | with `--apply`, repeat measure → solve up to N times |
| `--selftest` | off | run the solver against synthetic data and exit |

### How it works

#### The measurement

Five marks, placed **inside OTD's display area** — so they land on the screen OTD is mapping the
tablet to, not merely wherever a full-screen window happens to go. They are touched in this order:

| # | mark |
|---|---|
| 1 | top-left |
| 2 | bottom-left |
| 3 | bottom-right |
| 4 | top-right |
| 5 | centre |

A tap is accepted only if it lands within a ±26 px box around the current mark; anything else is
ignored and flashed amber. A slip therefore costs nothing and does not advance the sequence.
`Esc` cancels.

For each mark the tool records **where the pointer actually went** (`observed`) as well as where
the mark was (`target`). The difference between the two is the error it has to remove.

#### The solve

OTD maps a *tablet area* rectangle onto a *display area* rectangle, and the tablet area has exactly
**four free parameters** — `X`/`Y` are the rectangle's centre, `Width`/`Height` its size. Five points
over-determine those four, so the solution fits OTD's own model exactly rather than approximately,
and no extra correction layer is needed.

Per axis, working in fractions across the display:

```
want_i = normalize_display( target_i )     # where the mark is
have_i = normalize_display( observed_i )   # where the pointer actually went
t_i    = area_left + have_i * area_width   # the pen position that produced that pointer

solve   area_left' + want_i * area_width' = t_i      (least squares over the 5 points)
```

In words: *re-map the area so that the pen position which currently lands at `have` instead lands
at `want`.*

A correct calibration is a **fixed point** of that equation — when the pointer already lands on the
marks, `have == want` and the solved area is unchanged, so an already-good tablet is left alone.
That property is what makes the tool safe to re-run.

#### Why the two-pass behaviour

The composition — OTD → uinput → the compositor → the screen — is affine, but its **gain is not
knowable a priori**, because the window system rescales the virtual device's absolute range on its
way to the screen. The solver therefore works in the space it can actually observe, and treats any
residual as something the *next* measurement will see and remove:

- under an ideal composition it recovers the correct area **exactly, in one pass**;
- with an extra unknown gain it converges in **two or three** passes.

`--iterate N` runs that loop internally; running the tool again does the same thing manually.

#### The overlay

The fill is **translucent light grey**, not black, and that is deliberate: a black fill makes the
stylus tip hard to see against the panel, which is itself a source of mis-taps. Tune it with `--bg`
and `--opacity` — the effective background is `(1 − opacity) · screen + opacity · bg`, so raising
either makes it lighter and more uniform.

The annotation text is white over a black outline, so it stays legible whether the fill lands on a
dark or a light part of the screen, and it is positioned from the marks themselves (between the two
top marks, just below them) so it appears on the tablet's screen area even in a multi-monitor
layout. The crosshairs are 2 px red.

#### Safety rails

- Writes nothing unless `--apply` is given; the first write makes a backup.
- **Refuses a correction larger than 25 %**, which means the measurement is wrong, rather than
  writing it.
- **Warns if another driver is running**, which would make every measurement garbage.
- **Refuses to start if any mark falls outside the overlay window**, since a mark you cannot reach
  would stall the sequence.
- A tap outside the hit box is ignored rather than recorded.

---

## `pen-calibrate.py` — built-in pen digitizer (KWin)

### Usage

```bash
# measure and print the proposed change — writes nothing
python3 pen-calibrate.py

# write the matrix and keep it
python3 pen-calibrate.py --apply

# apply, re-measure and refine, up to 3 passes
python3 pen-calibrate.py --apply --iterate 3

# back to the identity matrix (same as System Settings' Reset)
python3 pen-calibrate.py --reset

# re-derive the slot layout by measurement, if a future KWin moves it
python3 pen-calibrate.py --probe

# exercise the solver offline against synthetic data — needs no display
python3 pen-calibrate.py --selftest
```

It finds the pen itself; `--device NAME` picks a different one by name. `--apply` copies
`~/.config/kcminputrc` to `kcminputrc.bak-pen-calibrate` before the first write.

### Options

| option | default | meaning |
|---|---|---|
| `--device NAME` | the first pen tool found | pick the device whose name contains NAME |
| `--display WxH+X+Y` | the whole X screen | the rectangle the pen is mapped to |
| `--inset PX` | `50` | how far the marks sit inside the display corners |
| `--bg LEVEL\|#rrggbb` | `#b0b0b0` | overlay fill; a 0–255 grey level or a hex colour |
| `--opacity F` | `0.55` | how strongly the fill hides the screen behind it |
| `--tolerance PX` | `2.0` | worst error still considered acceptable |
| `--apply` | off | write the matrix and keep it |
| `--iterate N` | `1` | with `--apply`, repeat measure → solve up to N times |
| `--reset` | off | restore the identity matrix and exit |
| `--probe` | off | re-derive the matrix layout by measurement and exit |
| `--selftest` | off | run the solver against synthetic data and exit |

### How it works

#### The measurement

Identical to the OTD tool: five marks in XP-Pen's order, the same ±26 px hit box, the same
"a tap that misses is ignored" behaviour, the same overlay and `Esc` to cancel. `Overlay`,
`make_targets` and `residual_report` are shared between the two scripts, near enough verbatim.

The marks are placed inside **the rectangle the pen is mapped to** — the whole desktop by default,
since a pen tool with no output bound to it spans the virtual screen. If KWin has the pen bound to a
particular output, pass `--display` with that output's geometry.

For each mark it records where the pointer went, not where the pen's raw digitiser reading was: that
reading lives in `/dev/input/event*`, which is `root:input` and deliberately not readable here.

#### The matrix

KWin keeps a 4×4 matrix per input device and pushes its top-left 3×3 into libinput, which applies it
to the device's **normalised** coordinates `[0,1]`. For a pen that means:

```
screen_fraction = M · pen_normalised        (as a column vector)
```

The value lives at `/org/kde/KWin/InputDevice/<sysName>` on the session bus, interface
`org.kde.KWin.InputDevice`, property `calibrationMatrix` — a 16-value comma-separated string. Setting
it applies **immediately**, and KWin persists it to `~/.config/kcminputrc` under
`[Libinput][<vendor>][<product>][<name>]` by itself. No root, no file editing, no daemon restart —
which is why the whole apply/restart third of the OTD tool has no counterpart here.

**The layout, established by measurement rather than assumption** (`--probe` re-derives it):

| index | carries | |
|---|---|---|
| 0, 5 | x scale, y scale | |
| 1, 4 | x-from-y shear, y-from-x shear | |
| **2, 6** | **x translation, y translation** | |

so the top-left 3×3 is the ordinary affine map

```
x' = m[0]·x + m[1]·y + m[2]
y' = m[4]·x + m[5]·y + m[6]
```

and the identity is `1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1`.

> **It must be exactly 16 values.** KWin silently substitutes the identity for anything else — no
> error, no warning — so a malformed write looks like a working one. The tool therefore reads the
> matrix back after writing and fails loudly if it did not take.

#### The solve

Per axis, working in fractions across the display, exactly as the OTD tool does — only the thing
being solved for is different:

```
want_i  = normalize_display( target_i )     # where the mark is
have_i  = normalize_display( observed_i )   # where the pointer actually went

solve   c_off + have_i * c_scale = want_i   (least squares over the 5 points)
compose M_new = C · M_old
```

In words: *find the correction that sends the pen position which currently lands at `have` to
`want`, and fold it into the matrix that is already there.*

This is the same least-squares fit and the same fixed-point property as the OTD tool: when the
pointer already lands on the marks, `have == want`, `C` is the identity, and the matrix is left
alone. Because the correction is *composed* rather than replaced, a rotation or shear already in the
matrix is carried through instead of being wiped.

The matrix is a strictly more general target than OTD's four parameters, and the five points could
support a full six-parameter affine — but the solver deliberately fits scale and offset per axis, so
that it behaves the same way as its sibling. See **Limitations**.

#### Why the two-pass behaviour

If anything between the matrix and the pointer is not exactly linear over the rectangle the pen is
bound to — an output scale, a compositor gain — the first pass is approximate, and the residual is
measured again on the next run.

- under an ideal chain it recovers the correct matrix **exactly, in one pass**;
- with an extra gain it converges in **two or three** passes.

`--iterate N` runs that loop internally; running the tool again does the same thing manually.

#### The overlay

As the OTD tool: translucent light grey rather than black so the stylus tip stays visible, white
text over a black outline, 2 px red crosshairs, and the annotation placed from the marks themselves
so it lands on the digitizer's own screen area.

#### Safety rails

- Writes nothing unless `--apply` is given; the first write makes a backup of `kcminputrc`.
- **Refuses a correction larger than 25 %**, which means the measurement is wrong, rather than
  writing it.
- **Reads the matrix back after writing** and fails if KWin did not accept it — because KWin's own
  fallback for a malformed matrix is silent.
- **Refuses to start if any mark falls outside the overlay window**, since a mark you cannot reach
  would stall the sequence.
- A tap outside the hit box is ignored rather than recorded.
- The value is the same one System Settings shows, so its Reset button undoes anything written here.

---

## Testing

```bash
python3 otd-calibrate.py --selftest
python3 pen-calibrate.py --selftest
```

Each drives its solver with synthetic measurements and checks two cases:

1. **Ideal composition** — recovers a deliberately wrong setting *exactly*: 0.00 px residual after
   one correction. This is the check that the algebra is right.
2. **An extra unknown gain** (1.12 plus a 7 px offset) — the case that actually happens, where the
   gain is not knowable. Converges to sub-pixel in 2 corrections.

`pen-calibrate.py --probe` is a third, different check: it re-derives the matrix layout by
measurement — tapping a fixed crosshair once per candidate slot and reading the pointer's
displacement, with a 0.5 scale on slot 0 as a control that must move the pointer if the matrix is
reaching the device at all. If a future KWin reorders the slots, that is how you find out.

There is no automated test for either overlay; they are exercised by running the tool.

---

## Limitations

Shared:

- **Five points fix scale and offset only.** No rotation, no shear, no non-linearity. This is the
  same limitation the vendor tool has, so it is a fair match — but it is a real limit.
- **They correct the composed chain**, not any one stage of it.
- **They assume the tablet maps to one display region** and that you can see and reach the marks.
- **The overlay is an X11/XWayland window.** A session with no X11 compatibility at all (pure
  Wayland with XWayland disabled) is untested and likely will not work.

`otd-calibrate.py`:

- **Exercised on one tablet so far** — an XP-Pen Artist Pro 16TP under KDE Plasma on Wayland. The
  code is written to be device-agnostic, but that is an expectation, not a tested claim.

`pen-calibrate.py`:

- **KWin only.** It writes KWin's device properties over D-Bus; other compositors keep their own.
- **The slot layout is a KWin implementation detail**, verified by measurement on 6.7.5 and
  re-checkable with `--probe`, not a documented interface.
- **The solve is scale + offset per axis.** The matrix could express rotation and shear, and the
  five points could fit them, but this solver does not attempt it — it is kept deliberately
  identical in behaviour to the OTD tool. A rotation or shear already present is carried through.
- **It calibrates the pen, not the finger.** The touchscreen is a separate device with its own
  matrix, and does not have `tabletTool` set, so it is not selected.
- **System Settings edits the same value.** Close its Tablet page while calibrating, so nothing
  writes an older value back over the result.
- **Exercised on one machine so far** — an HP convertible with a `Wacom HID 48CE` digitiser (i2c-hid,
  259.2 × 172.8 mm) under KWin 6.7.5 / Plasma 6.7.5 on Wayland, one scaled panel.

---

## Provenance

Independent implementations. They contain **no XP-Pen code, assets or strings**, and they target
different programs: OpenTabletDriver and its `settings.json`, and KWin and its device properties.

They are *behaviourally* inspired by XP-Pen's calibrator — the guided five-point flow, the mark
order, the 50 px inset, and the "ignore a tap that misses the mark" behaviour are all borrowed as
*interface* choices, because familiarity is the point of the exercise. The **solver is a different
algorithm**: a least-squares fit over the target program's own model, not the vendor's ratio
factors.

The matrix layout documented above was not taken from documentation — there is none — but
established from the local KWin by reading `setCalibrationMatrix` and `deserializeMatrix` out of
`libkwin`, and then settled by direct measurement, which is the version the `--probe` mode performs.

---

## Status

Personal tools, no warranty, barebones. `otd-calibrate.py` is written against OTD 0.6.7.
`pen-calibrate.py` is written against KWin 6.7.5.
