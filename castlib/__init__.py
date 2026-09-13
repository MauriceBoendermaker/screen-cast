"""Desktop casting to Chromecast devices over HLS."""

from __future__ import annotations

# One place, read by the window, the command line and the build, so
# "was this the build with the fix" has a single answer rather than
# three that can drift apart.
__version__ = "1.0.0"

__all__ = ["audio", "discovery", "session", "streaming"]
