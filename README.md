# Screen Cast

Casts the Windows desktop, with sound, to a Chromecast — as a small
Windows 98 flavoured window, or from the command line.

## Running it

Double-click `dist\ScreenCast.exe`. Pick the device, press **Start
casting**.

`ffmpeg` must be on PATH. Everything else is inside the exe.

## The one thing that will catch you out

Windows loopback capture taps the speaker endpoint **after** its volume
control. If the speakers you are capturing are muted or at zero volume,
the cast goes out silent even though everything looks fine — Windows'
own volume meter will still show the audio playing.

This matters because muting the PC is exactly what you would do to stop
the TV echoing back at you.

The same applies to the volume slider itself: at 29% the TV gets audio
roughly 15dB down, which across a room is close to nothing.

So: **keep Windows volume high and control the volume at the TV.**

### Hearing it only on the TV

Muting the laptop will not do it — that is the same post-volume tap,
and it silences the TV too. Route the sound somewhere inaudible
instead:

- **Send it to a device you cannot hear.** A wireless headset that is
  switched off still keeps its endpoint alive through the dongle. Set
  Windows output to it, set that device to 100%, and pick the same
  device under **System audio**. Nothing plays in the room; the TV gets
  everything. Switching the headset on undoes it.
- **Install a virtual cable** (VB-CABLE or similar) and capture that.
  It is a route that goes nowhere, so nothing can make it audible by
  accident.

The app watches for both cases. After 15 seconds it will tell you if
the capture is silent, or merely far too quiet, rather than leaving you
guessing.

## If it keeps buffering

First, the head start. A player joining a live playlist starts near its
end, and a stream produced in real time can never get ahead of a player
sitting at the live edge — it drains whatever exists, starves, and shows
a spinner every few seconds forever, at any bitrate. Handing over a
playlist that already holds several segments is what gives it somewhere
behind live to sit, which is why casting waits for
`PREROLL_SEGMENTS` before it starts.

Measured on a real device: casting the instant the first segment
appeared gave 12-18 dropouts a minute and the player was in PLAYING for
5-16% of samples. Waiting for four segments gave **zero** dropouts.

Everything below only matters once that head start is in place.

Then make sure the stream is served over persistent connections. The
built-in `SimpleHTTPRequestHandler` speaks HTTP/1.0 and hangs up after
every response, which makes a Chromecast open a fresh TCP connection per
segment. On a link with tens of milliseconds of round trip, each one
restarts TCP slow start at roughly 14KB, so the congestion window never
grows and the stream starves whatever the bitrate is. The symptom is a
buffering spinner roughly once a second, and hundreds of TIME_WAIT
sockets:

```
Get-NetTCPConnection -LocalPort 8765 | Group-Object State
```

Healthy is one or two `Established`. Many `TimeWait` and no
`Established` means connections are churning. `CastHttpHandler` sets
`protocol_version = "HTTP/1.1"` to prevent exactly this.

After that, bandwidth. Chromecasts are
often the weakest device on a network, and the one here answers pings
from its own LAN at 58ms average with peaks over 100ms — TCP throughput
is bounded by window over round-trip time, so a link that answers like
that cannot carry much more.

Check yours before blaming the encoder:

```
ping -n 40 <device ip>
```

Healthy is 1-5ms. Tens of milliseconds means the device's wireless link
is weak or it is dozing between packets. Moving it to 5GHz, closer to
the access point, or onto Ethernet does far more than any encoder
setting. Constant rebuffering also breaks audio, because playback never
runs long enough to keep a sound track going.

Then quality. Pick **Quality** in the window, or `--quality` on the
command line: Smooth (3 Mbps), Balanced (6 Mbps), Sharp (10 Mbps). Drop
a level if buffering returns, raise one if the picture looks soft.

**Quality and Delay pull against each other.** A higher bitrate means
bigger segments, and a shorter delay means less time to fetch them, so
raising one while lowering the other is the combination most likely to
stutter. Sharp on **Low** asks a Chromecast to pull 10 Mbps with barely
two seconds of cushion; if you want Sharp, pair it with **Smooth**.

## Capture and encoding

Screen capture uses **ddagrab** (Desktop Duplication API, on the GPU),
falling back to gdigrab where that is unavailable — over some remote
sessions, or without a D3D11 device.

This matters more than it sounds. gdigrab delivered only **17.6 frames
per second** here; ffmpeg then duplicates frames to reach the requested
30, so the output reports 30fps while the motion is barely half that.
ddagrab delivered a true 30.

Encoder choices were measured rather than assumed, with capture already
on the GPU:

| Setting | Speed | Note |
|---|---|---|
| libx264 ultrafast + zerolatency | 1.000x | what we use |
| libx264 ultrafast, no tune | 0.933x | cannot hold real time |
| libx264 superfast | 0.909x | too slow |
| libx264 veryfast | 0.888x | too slow |
| h264_nvenc p4 | 0.956x | hybrid graphics copies frames to the discrete GPU |
| h264_amf balanced | 0.998x | keeps up, but no better picture |

The AMD encoder scored within noise of x264 at the same bitrate (SSIM
0.979 vs 0.977, PSNR 32.16 vs 32.53), so it buys nothing while adding a
GPU dependency. Bitrate and capture rate are the levers, not the preset.

## Watching it run

While a cast is running the window grows a **Stats** panel:

```
30.0 fps at 1.00x, 5.9 Mbps
0 frames dropped, 12 duplicated
4:21, 0 rebuffers, peak -12 dB
```

The two worth watching are **speed** and **duplicated frames**. Speed is
how fast ffmpeg is encoding against the wall clock — 1.00x is holding
real time, and anything below it means the encode is falling behind for
as long as it stays there. Climbing duplicates mean capture is
delivering fewer frames than the 30 being encoded, which is the gdigrab
symptom above: the output claims 30fps while the motion is half that.

The bitrate is weighed from the segments in the playlist rather than
asked of ffmpeg, which reports none for a segmented output — it has no
single file to measure. That makes it the rate the device is actually
being asked to pull, so it sits well under the **Quality** setting on a
still desktop and climbs towards it on moving video.

**Rebuffers** is the same count behind the warning after four of them.
**Peak** is the loudest the captured audio has been, which is the
quickest way to catch the muted-endpoint trap at the top of this file:
`no sound` there means loopback is capturing digital silence.

The panel is the window only. The command line ignores it.

## Delay

Pick **Delay** in the window, or `--delay` on the command line.

A segment cannot be published until it is complete, and a player joins
the playlist a couple of segments from the end, so segment length sets
the delay. Measured against a real HLS client:

| Setting | Segments | Needs |
|---|---|---|
| Low (~2s) | 1s, 6 in playlist | a steady link |
| Balanced (~4s) | 2s, 8 in playlist | the default |
| Smooth (~8s) | 4s, 8 in playlist | a poor link |

Each step doubles the segment, because segment length is the only part
that decides whether a device keeps up: a player must fetch each
segment within its own duration or it stalls. **Buffering roughly once
a second means one-second segments are arriving late** — move up a
step. A device that stalls even on Smooth is past what it can carry;
lower **Quality** instead.

**The delay is the buffer.** A player joins a couple of segments from
the end of the playlist and stays there, so every second shaved off the
delay is a second less cushion against a slow segment fetch. There is
no setting that gives a short delay and a deep buffer, because they are
the same number.

That makes the right setting a property of the link, not a preference.
A Chromecast pinging at 58ms with 117ms spikes stalled once a second on
one-second segments and ran clean on two-second ones. Measure yours
before reaching for a lower setting:

```
ping -n 40 <device ip>
```

If a shorter delay stutters, try lowering **Quality** alongside it —
smaller segments transfer faster, so the smaller buffer has a better
chance of keeping up. Shorter segments themselves cost almost nothing:
6.12 Mbps at 1s against 6.36 Mbps at 2s.

The publishing side adds only about 0.19s, so there is nothing left to
win there. Going below roughly a second would need Low-Latency HLS,
which ffmpeg's muxer cannot produce.

The microphone stays off by default for the same reason: at any of
these delays an open mic near the TV sends a slapback of the TV's own
output back into the stream.

## Command line

```
python cast_screen.py                          # discover, then prompt
python cast_screen.py --name "Huiskamer 4K"    # skip the prompt
python cast_screen.py --host 192.168.1.42      # skip discovery
python cast_screen.py --quality "Sharp (10 Mbps)"
python cast_screen.py --no-audio               # video only
python cast_screen.py --mic "Logitech"         # mix in a microphone
python cast_screen.py --audio-device "Realtek" # capture a chosen endpoint
python cast_screen.py --list-audio             # show capturable devices
```

Discovery falls back to scanning the local `/24` for port 8009, because
mDNS finds nothing on networks that do not forward multicast between
clients.

If the device never requests the stream, Windows Firewall is blocking
inbound TCP on the serving port (8765 by default).

Casting takes the device over: if YouTube or anything else is playing,
it is closed first. Asking a busy Chromecast to play media makes it tear
the running app down and the following launch request is often lost in
the changeover, which shows up as the device refusing the stream — so
the other app is quit explicitly and given time to let go.

## Building the exe

```
%LOCALAPPDATA%\Programs\Python\Python314\python.exe build.py
```

PyInstaller cannot bundle the Microsoft Store build of Python, so the
build needs a python.org interpreter. `build.py` refuses to run under the
Store build rather than producing something broken.

The build runs the frozen exe's self-test afterwards: a one-file bundle
can silently lose a native library, and the first symptom would
otherwise be a window with an empty device list.

Dependencies:

```
python -m pip install pychromecast pyaudiowpatch pyinstaller
```

## Tests

```
python -m unittest discover -s tests -t .
```

The important one is `tests/test_audio.py`. Loopback capture delivers
nothing while the endpoint is idle, which would stall the pipe and drift
audio permanently behind video; the pump pads the gap with silence so
the stream always advances at exactly real time. Those tests hold that
property in place.

## Layout

| Path | Purpose |
|---|---|
| `screen_cast_gui.py` | The window |
| `cast_screen.py` | The command line |
| `castlib/session.py` | One cast, from discovery to teardown |
| `castlib/audio.py` | Loopback capture and device enumeration |
| `castlib/streaming.py` | ffmpeg command, HLS, HTTP server |
| `castlib/discovery.py` | Finding Cast devices |
| `castlib/win98.py` | The widget set |
| `build.py`, `make_icon.py` | Packaging |
