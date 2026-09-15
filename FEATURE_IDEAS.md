# Feature Ideas

Working notes from brainstorming sessions on what to add to Screen Cast next.
Nothing here is implemented unless it is ticked off. See
`docs/superpowers/specs/` for approved, fully-speced work.

## ✅ Live stats overlay

Status: implemented.

Two things in the design below turned out to be wrong about ffmpeg, and
were measured rather than argued with:

- **ffmpeg reports no bitrate for an HLS output.** `bitrate=` and
  `total_size=` are `N/A` in every block for the whole run — it is
  writing a stream of files, so it has no total size to divide. The
  same command against `-f mpegts` fills both in. The panel weighs the
  segments listed in the playlist instead (`measured_bitrate_kbps`),
  which is the better number anyway: it is what the device is being
  asked to pull rather than what the encoder was aimed at.
- **A full progress pipe does not stall the cast.** ffmpeg 8.1 shrugs
  it off and keeps encoding at real time indefinitely (measured over
  four minutes, segments still landing on the half second); it is the
  *numbers* that are lost, everything after the roughly eleven seconds
  of blocks that fit in a 4KB Windows pipe. So the reader still has to
  start unconditionally and immediately, but because a late reader
  inherits figures that quietly stopped moving, not because the stream
  breaks.

One hazard found on the way out: closing the progress pipe while the
reader is still blocked on it needs the lock that read is holding, so
the close does not return until ffmpeg dies — which would hang Stop on
exactly the thing it was giving up on. `FfmpegProgress.stop()` leaves
the pipe alone if its reader has not finished, and a test holds that.

Shows live encode/delivery/audio stats in the GUI while casting: fps, encode
speed, actual bitrate, dropped/duplicated frames, rebuffer count, and audio
peak level. Most of this data (rebuffers, audio peak) is already tracked
internally by `CastSession` and `LoopbackCapture` for the one-shot warnings;
this mainly surfaces it continuously and adds the ffmpeg-side encode metrics
that aren't captured today.

CLI stays untouched — GUI only.

### Design

**Data flow.** ffmpeg already knows fps, output bitrate, and dropped/duplicated
frame counts — it just isn't asked to report them. Adding `-progress pipe:1`
to the ffmpeg command (`castlib/streaming.py`) makes it emit periodic
`key=value` blocks (`fps=`, `bitrate=`, `drop_frames=`, `dup_frames=`,
`speed=`) to a second pipe, separate from the HLS output files. A new
`FfmpegProgress` class reads that pipe on a background thread and exposes the
latest parsed values — same shape as `LoopbackCapture`'s pump thread. The
parsing itself should be a small pure function so it can be unit-tested by
feeding it sample ffmpeg text, no process required.

**Important wrinkle:** that pipe must always be drained, even when nobody's
watching it (CLI included), or ffmpeg eventually blocks trying to write to a
full, unread pipe and the cast silently stalls after enough hours. So the
reader thread must start unconditionally inside `CastSession`, not just when
the GUI is showing stats.

**Aggregation.** `CastSession._monitor()` already loops every 0.5s tracking
rebuffers and polling ffmpeg/the cast device. It gains one more step: build a
small frozen `CastStats` (elapsed time, fps, speed, bitrate, dropped/
duplicated frames, rebuffer count, audio peak dBFS or `None` if system audio
is off) and store it as `self._stats`. A `CastSession.stats` property exposes
the latest snapshot — no locking needed, since it's a plain reference swap.
The CLI ignores it entirely.

**GUI.** `screen_cast_gui.py` adds a `GroupBox("Stats")` (reusing the existing
win98 widgets, no new widget classes needed) with three lines of labels,
created alongside the other controls but only packed once a cast starts.
`AppWindow` gets a small `resize(width, height)` method; starting a cast
grows the window to fit the panel, returning to `IDLE`/`FAILED` shrinks it
back. The existing 100ms `_drain()` tick also reads `session.stats` and
refreshes the labels.

**Testing:** a pure-function test for progress-block parsing in
`test_streaming.py`, a test that `CastSession` produces a sane `CastStats`
from known inputs in `test_session.py`, and a geometry test in
`test_win98.py` confirming the window actually resizes.

---

## System tray icon

Status: designed, not yet implemented.

Decisions made:
- **Close (X) still quits** exactly as today (stops any cast, tears down).
  **Minimize (_) sends the window to the tray** instead of just the taskbar.
  The tray icon is a quicker way back in, not a new "hide on close" trap.
- **Balloon/toast notifications only for errors and disconnects** — silent
  otherwise. This is the moment you'd actually want to know something broke
  while the window is hidden.

### Design

**No new dependency.** The obvious library choice is `pystray` (+ Pillow for
the icon image), but this codebase already reaches for raw `ctypes` Win32
calls for exactly this kind of chrome integration (DPI awareness, taskbar
presence, minimize, in `castlib/win98.py`). A tray icon is `Shell_NotifyIconW`
from `shell32.dll` plus a `NOTIFYICONDATAW` struct — consistent with the
existing style and avoids pulling in Pillow just to build one small icon.

**Icon asset.** `screencast.ico` is currently only used at build time as
PyInstaller's `--icon` (baked into the exe's own resources, not bundled as a
loose file). Runtime needs an actual file to hand to `LoadImageW`, so
`build.py` should add it as a bundled data file; load it from `sys._MEIPASS`
when frozen, or the script directory otherwise.

**Receiving tray clicks — the one real risk.** `Shell_NotifyIcon` delivers
mouse events (double-click to restore, right-click for the context menu) as a
custom window message (`WM_APP + 1`) sent to a chosen `HWND`. Tk's event loop
doesn't dispatch arbitrary window messages to Python, so the Tk toplevel's
window procedure needs to be subclassed: swap it via
`SetWindowLongPtrW(hwnd, GWL_WNDPROC, new_proc)`, keep the `ctypes.WINFUNCTYPE`
callback alive as an instance attribute (it crashes if garbage collected), and
forward everything else to the original procedure via `CallWindowProcW`. This
is a step further than the existing ctypes usage in the codebase, which is
all one-way (set a style, query DPI, minimize) rather than receiving inbound
messages. **Worth a short throwaway spike to prove this works** — especially
alongside the window's existing `overrideredirect` + `WS_EX_APPWINDOW` custom
chrome — before building the rest, the same way the original design doc
verified PyAudioWPatch on 3.14 before depending on it.

**Plumbing.** A new `castlib/tray.py` (kept dependency-free of the rest of
the package, mirroring how `win98.py` depends on nothing else) exposing a
`TrayIcon` class: `show(hwnd, icon_path, tooltip)`, `set_menu(items)` (plain
native `CreatePopupMenu` / `TrackPopupMenu`, not a styled win98 menu — this
is the OS's own convention), `notify(title, message)` for balloons, and
`remove()` for cleanup on exit.

**Wiring in `screen_cast_gui.py`:**
- Minimize now calls `root.withdraw()` instead of `win98.minimize()`
  (`SW_MINIMIZE`), and ensures the tray icon is showing.
- Tray menu: "Show Screen Cast" (also the double-click action), a live
  "Start casting"/"Stop casting" item that reuses the existing `_toggle()`,
  and "Exit" (calls the existing `close()`).
- Tray tooltip mirrors the current status text, so hovering tells you what's
  happening without reopening the window.
- `_drain()` already reacts to `SessionState.FAILED`; when the window is
  hidden, that same branch also fires a balloon via `Shell_NotifyIcon` with
  `NIF_INFO`.
- `close()`/`_destroy()` must call `Shell_NotifyIcon(NIM_DELETE, ...)` before
  exit, or a stale icon lingers in the tray until moused over.

**Testing.** The message-loop plumbing itself isn't unit-testable (same as
the existing "win98.py rendering... verified by manual smoke test"
precedent), but the logic around it should be pulled into small pure
functions and tested the way `_audio_complaint` is: given a `SessionEvent`
and whether the window is currently hidden, should this fire a balloon, and
what should it say.

### Risks

| Risk | Mitigation |
|---|---|
| WNDPROC subclassing may misbehave combined with the existing `overrideredirect` + `WS_EX_APPWINDOW` chrome | De-risk with a throwaway spike before building the rest |
| A hidden `overrideredirect` window may not reliably restore/focus on all Windows versions | Verify manually on `deiconify()` + raise |

---

## Window picker via Windows.Graphics.Capture

Status: researched, not designed — needs a feasibility spike before a real
design is worth writing.

### Why this is harder than the monitor picker

Desktop Duplication (`ddagrab`, today's GPU capture path) has no concept of
a single window — it only exposes monitors. `gdigrab` can target a window by
title, but that's the same GDI/BitBlt path that measured 17.6fps for the
full desktop (README), unmeasured for a cropped window, and it captures
whatever is on top of an occluded window rather than the window's real
content. ffmpeg has no built-in Windows.Graphics.Capture (WGC) input as far
as this research found — the same modern, GPU-accelerated, occlusion-correct
API that OBS's "Windows Graphics Capture" source uses — so there's no simple
swap-the-lavfi-source option here the way the ddagrab→gdigrab fallback works
today. Worth re-verifying against whatever ffmpeg version is current when
this is picked up, in case that's changed.

### The likely shape of a real implementation

A Python-side WGC wrapper — `windows-capture` (PyO3/Rust-backed) is the
existing option this research turned up — captures a chosen window's frames
via a callback, as raw BGRA. Those frames would need to reach ffmpeg the
same way system audio does today: piped in from Python rather than pulled by
one of ffmpeg's own demuxers. Audio already goes in over `stdin`, so video
would need its own channel — most likely a Windows named pipe
(`\\.\pipe\...`) that ffmpeg opens as a second input, since stdin is spoken
for.

### The real risk: bandwidth, not API availability

Raw BGRA at 1920x1080 and 30fps is roughly 250MB/s having to cross from a
GPU texture into a CPU-side Python `bytes` object, through a pipe, and into
ffmpeg — every frame, continuously. That CPU round-trip is close to the same
tax that made `gdigrab` too slow for full-desktop capture in the first place
(the README's measured 17.6fps). `windows-capture`'s Rust core should be
fast at the capture side; whether the Python-to-ffmpeg leg keeps up at real
window sizes is genuinely unverified, and is the one thing worth measuring
before committing to this design — the project's own "measured rather than
assumed" standard (see the encoder benchmarking table in the README) applies
directly here.

### If the spike succeeds

WGC also captures monitors, and captures occluded/minimized windows
correctly (BitBlt/gdigrab does not) — so if the throughput holds up, one
backend could eventually replace `ddagrab` *and* `gdigrab` *and* add a
window mode, rather than the project carrying three capture paths. A nice
possible simplification, but speculative until the spike says the
throughput is real.

### If it doesn't

Fall back to the smaller, lower-risk options already on this list: a
monitor picker (ddagrab output index / gdigrab offset+size, no new
dependency), or a window picker via `gdigrab`'s window-title mode, accepting
its own performance ceiling.

### Suggested next step

A short throwaway spike, not a design: capture a real window with
`windows-capture` at its native size, pipe frames into a minimal ffmpeg
command over a named pipe, and measure sustained fps and CPU load — before
any of the pipe-topology or GUI work here is worth designing in detail.

---

## Per-application audio capture

Status: researched, not designed — needs a feasibility spike before a real
design is worth writing.

### Why this reopens a real non-goal

The original design doc ruled this out as "system-wide only." Windows 10
2004+ actually added exactly this capability — process loopback capture, via
`ActivateAudioInterfaceAsync` with
`AUDIOCLIENT_ACTIVATION_PARAMS_PROCESS_LOOPBACK_STREAM` (target PID,
include/exclude its process tree) — so the platform limitation that
justified the non-goal isn't there anymore on any reasonably current
Windows. Worth re-confirming the exact API name/shape against current docs
when this is picked up.

### Why it's not a drop-in addition

`PyAudioWPatch` (the loopback library already in use) captures the whole
default output endpoint; it has no per-process mode. Process loopback is a
much newer, less-wrapped Windows API — Microsoft's own C++ sample for it
exists, but there's no equivalent to PyAudioWPatch doing the COM interop for
it in Python. Implementing it means driving `ActivateAudioInterfaceAsync`'s
async COM activation (an `IActivateAudioInterfaceCompletionHandler`
callback) directly via `ctypes`/`comtypes` — a meaningfully bigger step up
in Windows COM interop than anything in the codebase today, which sticks to
flat, synchronous Win32 calls.

### The likely shape

- **Picking the app.** `pycaw` (a maintained, comtypes-based wrapper around
  Core Audio session APIs) already enumerates active audio sessions with
  their process IDs — a good fit for populating a device-style dropdown of
  "apps currently making sound," refreshed the same way the existing
  output/mic lists are. This would be a new dependency.
- **Capturing it.** A new `ProcessLoopbackCapture`, parallel to today's
  `LoopbackCapture`, feeding the same silence-padded real-time pump into
  ffmpeg's stdin — that part of the pipeline is solid and doesn't need to
  change, only what feeds it.
- **Edge case.** The target process can exit mid-cast. Should degrade like a
  muted endpoint does today (pad with silence, keep going) rather than fail
  the whole session.

### Suggested next step

A throwaway spike: get a raw PCM stream out of one process's audio via
`ActivateAudioInterfaceAsync` in isolation, before touching
`castlib/audio.py`. If the COM activation dance proves too fragile in
Python, this is worth reconsidering as a small native helper instead.

---

## Content-aware encoding

Status: designed, with one part of the original idea rejected as
impractical for this architecture.

### The obvious version doesn't fit ffmpeg's model

The natural-sounding idea — detect a static screen and swap to a slower,
better-compressing preset, then back to `ultrafast` the moment motion
returns — doesn't actually work here: `-preset`/`-tune` are fixed for the
life of an ffmpeg process. Changing them means restarting ffmpeg, which
means a visible stream hiccup on every scene change. Not worth it for the
gain.

### What's actually achievable inside the running encode

Two changes, both just parameters/filters on the single ffmpeg process
already running — no architecture change, no restarts:

1. **`mpdecimate` + `fps`.** ffmpeg already ships a filter that drops frames
   near-identical to the previous one. A desktop that hasn't changed
   (reading, a paused video, an idle screen) produces a stream of duplicate
   frames today that get encoded (cheaply, but not free) anyway;
   `mpdecimate` drops them before they reach the encoder, and a following
   `fps` filter restores a constant frame rate afterward so HLS's fixed
   segment-duration assumption still holds. This is the actual
   "content-aware" win here — it comes free from ffmpeg, not from custom
   motion-detection code.
2. **x264 adaptive quantization.** `-x264-params aq-mode=2:aq-strength=<n>`
   lets x264 itself allocate bits toward the parts of the frame that are
   actually changing rather than spreading them evenly — already a
   content-aware mechanism, just not switched on for `ultrafast`. Needs its
   own real-time-holding check, the same way every preset in the README's
   benchmarking table was checked, since AQ analysis isn't free.

### Risk

Both are cheap in theory but neither is free in practice, and this
codebase's whole encoder story is "measured, not assumed" (see the
preset/encoder table in the README) — `ultrafast` was chosen because
everything slower missed real time by a measurable margin. AQ and
`mpdecimate` need the same treatment: turn them on, re-run the same kind of
real-time-ratio measurement, and see if the margin survives.

### Suggested next step

Not a spike so much as a rerun of the existing benchmark: build the command
with `mpdecimate`+`fps` and `aq-mode=2` added, measure real-time ratio the
same way the current preset table was built, and only keep whichever piece
doesn't cost the real-time margin.

---

## True adaptive bitrate (ABR)

Status: researched, not designed — needs a feasibility spike before a real
design is worth writing.

### What changes vs. today

Today's Quality dropdown is one fixed bitrate, picked once before casting
starts; a struggling link produces the rebuffer warning and the user
manually steps down (see "Auto quality step-down" below, a smaller, related
idea). Real ABR means ffmpeg encodes several renditions from the same
capture at once (a `split` filter fans the scaled frame out to N encoder
branches), the `hls` muxer's built-in `-var_stream_map` produces a master
playlist alongside each rendition's own playlist, and the Chromecast's own
player switches renditions on its own as it measures throughput — the way
YouTube or Netflix behaves, no manual step-down needed.

### The real risk: this is the same CPU budget problem, times three

The README's own benchmarking exists because a single `ultrafast` x264
encode barely holds real time against GPU-captured 1080p30 on this
hardware — every faster-but-lower-quality preset already missed real time
by a measurable margin. ABR means running 2-3 simultaneous encodes instead
of one. Lower-resolution rungs (e.g. 720p, 480p) cost meaningfully less per
frame than 1080p, so this isn't literally 3x the load, but it's a real,
unmeasured multiple of today's already-tight budget on the same CPU.

### A second, separate risk

Live HLS ABR switching isn't guaranteed to behave as smoothly as VOD ABR on
every player — worth confirming the stock Chromecast Default Media Receiver
actually adapts renditions cleanly on a live/event stream before designing
the rest around the assumption that it will.

### Suggested next step

A throwaway spike, in the same spirit as the encoder table already in the
README: build a 2-3 rung `-var_stream_map` command, measure the real-time
ratio on this machine, and confirm the receiver actually switches
renditions on a live stream. If the CPU budget doesn't hold, the smaller
"auto quality step-down" idea gets most of the practical benefit for a
fraction of the cost.

---

## ✅ About dialog + version number

Status: implemented, at `1.0.0` — there were no tags and no version
anywhere to inherit one from.

Built as designed. The design was right about the shape and quiet about
the hard part: `transient()` + `grab_set()` is not enough to make a
*frameless* window modal on Windows.

- **Tk sets no owner on an overrideredirect Toplevel.** `transient()`
  gives it the tool-window style and leaves `GWLP_HWNDPARENT` at zero,
  so Windows does not know the two windows are related. Measured
  consequence: the dialog opens in front and stays there right up until
  anything raises the main window — Alt-Tab back to the app, or its
  taskbar button — and then the parent covers a dialog that is still
  holding the input grab. Nothing responds and nothing is visible to
  click: the app reads as hung. `win98.own_window()` sets the owner,
  which fixes it, and it has to run *after* the window is mapped
  because mapping a withdrawn frameless Toplevel destroys and recreates
  the handle it would be set on.
- **A dialog centred on the main window could open off screen.** The
  main window is frameless and drags without constraint, so it can be
  left far enough into a corner that a dialog centred on it lands past
  the edge — while grabbing every click. `center()` now clamps to the
  work area of whichever monitor the parent is on.
- **Position first, then map.** A transient Toplevel keeps the position
  it is given before it is shown, unlike the Dropdown popup, whose
  comment says the opposite and does not apply here. Mapping first put
  the dialog on screen in the corner for a whole event loop pass.
- **Closing it has to hand the focus back**, or the main window's
  drawn title bar stays greyed as though something else still had it.
- **A dialog destroyed from outside Tk** used to leave the caller
  holding a dead window and the ? button dead for the session. It now
  registers `WM_DELETE_WINDOW`, and the caller checks `winfo_exists`.

Two smaller ones:

- **The optional title bar buttons had a latent bug** that a third
  would have multiplied. `_button_boxes()` returned a box for minimize
  whether or not there was a handler, and `_redraw()` skipped painting
  it — so a window without one still had an invisible 16x16 patch that
  swallowed clicks and could not be dragged by.
- **The dialog sizes itself to its content** (`AppWindow.fit`). The
  first cut used a measured constant and clipped its own Close button,
  because text height follows the font, which follows the monitor.

`AppWindow.show_modal()` and the `parent` argument are the reusable
part: the Advanced/Settings dialog below now has chrome to sit in — and
`own_window()` is the piece the system tray idea will want too, for the
same reason.

Not done, and worth a decision: the built exe still carries no version
resource, so right-click → Properties → Details shows nothing. Fixing
that means generating a `VSVersionInfo` file in `build.py` and passing
`--version-file`; the cost is a regenerated `build/ScreenCast.spec` and
a 16MB binary diff in `dist/`, since neither directory is gitignored.

Not a streaming feature — this is about the app's own upkeep. There is
currently no version number anywhere: not in the window, not in a dialog,
nowhere to check "was this the build with the fix or not."

### Version number

A single `__version__` string in `castlib/__init__.py`, imported by the GUI,
the CLI, and `build.py` — one source of truth instead of three places that
can drift. `cast_screen.py` gains `--version` via argparse's built-in
`action="version"`. Deliberately kept out of the title bar — period-correct
Win98 apps put the version in About, not the caption — but worth stamping
into the log file once the diagnosability idea below exists, since "which
build was this" is exactly what a copied diagnostic report should carry.

### The dialog needs a small refactor first

`win98.AppWindow` currently hardcodes `self.root = tk.Tk()`. That's correct
for the one main window, but wrong for a second window in the same
process — only one real Tk root belongs per process; a second window should
be a `tk.Toplevel`. `AppWindow` needs to accept an optional `parent` and
build a `Toplevel` instead of a fresh `Tk()` when given one, skipping the
once-per-process DPI-awareness and taskbar setup. Small change, and it means
every *future* secondary dialog (an Advanced/Settings dialog, mentioned
below, gets more likely as more options accumulate) reuses the same chrome
instead of each one rolling its own.

### The dialog itself

- No minimize button — matches how the main window already omits maximize:
  a fixed, single-purpose dialog.
- Modal via `transient()` + `grab_set()`, the same idiom the existing
  Dropdown popup already uses for its own exclusivity.
- Content: app name, version, a one-line description, a Close button.
  Skipping a full tabbed "System Properties" pastiche — a cute reference,
  but real tabs are a lot of chrome to build for one static pane.
- Entry point: a small "?" button in the title bar, next to minimize and
  close — reuses the existing raised-square-button-with-glyph pattern
  already used for those two (`TitleBar._button_boxes`/`MINIMIZE`/`CLOSE`
  glyphs), needs one more pixel-grid glyph and an `on_about` callback
  threaded through the same way `on_minimize` already is.

---

## Other candidate improvements

Grouped by theme, unordered within each group. The existing design doc
(`docs/superpowers/specs/2026-09-13-audio-cast-win98-gui-design.md`) records
some of these as deliberate non-goals for v1 — worth re-reading before
picking one, since a few would reopen a decision made on purpose.

### Everyday usability
- **System tray icon** — minimize instead of closing, start/stop from the
  tray, a toast notification on disconnect/error.
- **Auto-connect to the last-used device** on launch instead of requiring
  Start every time.
- **Built-in network check** — a button that runs the ping/TCP-connection
  diagnostics the README currently tells you to do by hand.

### Capture scope (revisits stated non-goals)
- **Monitor picker** — cast a chosen display instead of always the whole
  desktop. Reuses ddagrab's output index / gdigrab's offset+size, no new
  dependency. Not yet designed in detail.
- **Window picker** — see the full write-up above (needs a feasibility spike
  first; ddagrab can't do this at all, and the two workarounds each carry
  real risk).
- **Multi-device casting** — cast to more than one Chromecast at once
  (multi-room). Bigger change: streaming/session would need to support N
  targets.
- **Live mic mute/unmute toggle** instead of restarting the cast to change
  it.

### Audio
- **Per-application audio capture** — see the full write-up above (needs a
  feasibility spike; the platform capability now exists, but there's no
  ready-made Python wrapper for it).

### Streaming quality
- **Auto quality step-down** when rebuffering crosses the threshold, instead
  of only warning and leaving it to the user. A smaller, lower-risk sibling
  of true ABR, below.
- **Content-aware encoding** — see the full write-up above (designed;
  `mpdecimate`+`fps` and x264 AQ, not live preset-swapping).
- **True adaptive bitrate** — see the full write-up above (needs a
  feasibility spike; multiple simultaneous encodes on an already-tight CPU
  budget).

### Packaging
- **Auto-update check**, or a proper installer with a Start Menu entry
  instead of a bare exe.

### App polish & diagnosability (not streaming functionality)
- **About dialog + version number** — see the full write-up above
  (designed).
- **Diagnosability** — a rotating log file, a "Copy diagnostic info" button
  (log tail, ffmpeg version, Windows build, GPU info, current settings), and
  a global unhandled-exception handler so a failure leaves a trace instead
  of vanishing into a console-less frozen exe. Not yet designed in detail.
- **In-app gotcha reminders** — surface the README's hard-won warnings
  (loopback taps post-volume, Quality/Delay trade-off, firewall blocking the
  port) next to the relevant control instead of leaving them to be
  rediscovered by getting burned. Not yet designed in detail.
- **CI** — run the existing `tests/` suite automatically on push; there is
  a real test suite today and nothing running it automatically.
- **Theme-appropriate polish** — classic system sounds on state changes
  (connect/error chimes), and remembering window position between launches
  instead of always re-centering.
- **Advanced/Settings dialog** — as more options accumulate (stats panel,
  presets, an ABR ladder, a monitor picker), the fixed 360x328 main window
  will run out of room. The About dialog's `AppWindow` refactor (accepting a
  `parent` for a `Toplevel`) is exactly the piece this would reuse.
