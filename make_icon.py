"""Write the application icon, without taking on an imaging dependency.

An .ico is simple enough to emit by hand: a directory of entries, each
pointing at a BMP whose height is doubled to hold its own mask.
"""

from __future__ import annotations

import struct
from pathlib import Path


PALETTE = {
    ".": None,
    "K": (0x00, 0x00, 0x00),
    "G": (0x80, 0x80, 0x80),
    "F": (0xC0, 0xC0, 0xC0),
    "W": (0xFF, 0xFF, 0xFF),
    "S": (0x00, 0x00, 0x80),
    "C": (0x00, 0xA8, 0xA8),
}

# A television, drawn at the size the era would have drawn it.
ART = (
    "................",
    ".....K....K.....",
    "......K..K......",
    ".......KK.......",
    "..KKKKKKKKKKKK..",
    "..KFFFFFFFFFFK..",
    "..KFSSSSSSSSFK..",
    "..KFSCCCCCCSFK..",
    "..KFSCCCCCCSFK..",
    "..KFSCCCCCCSFK..",
    "..KFSSSSSSSSFK..",
    "..KFFFFFFFFFFK..",
    "..KKKKKKKKKKKK..",
    "...K........K...",
    "................",
    "................",
)


def upscale(rows: tuple[str, ...], factor: int) -> list[str]:
    return [
        "".join(pixel * factor for pixel in row) for row in rows for _ in range(factor)
    ]


def encode(rows: list[str]) -> bytes:
    """One BMP image: header, bottom-up BGRA pixels, then the AND mask."""
    size = len(rows)

    header = struct.pack(
        "<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0
    )

    pixels = bytearray()

    for row in reversed(rows):
        for pixel in row:
            colour = PALETTE[pixel]

            if colour is None:
                pixels += b"\x00\x00\x00\x00"
            else:
                red, green, blue = colour
                pixels += bytes((blue, green, red, 0xFF))

    # 1bpp mask, rows padded to 4 bytes. Alpha already does the work, so
    # this only has to be present and consistent.
    stride = ((size + 31) // 32) * 4
    mask = bytearray()

    for row in reversed(rows):
        bits = bytearray(stride)

        for x, pixel in enumerate(row):
            if PALETTE[pixel] is None:
                bits[x // 8] |= 0x80 >> (x % 8)

        mask += bits

    return header + bytes(pixels) + bytes(mask)


def write_icon(path: Path, sizes: tuple[int, ...] = (1, 2)) -> Path:
    images = [encode(upscale(ART, factor)) for factor in sizes]

    offset = 6 + 16 * len(images)
    directory = struct.pack("<HHH", 0, 1, len(images))

    for factor, image in zip(sizes, images):
        edge = len(ART) * factor

        directory += struct.pack(
            "<BBBBHHII",
            edge if edge < 256 else 0,
            edge if edge < 256 else 0,
            0,
            0,
            1,
            32,
            len(image),
            offset,
        )

        offset += len(image)

    path.write_bytes(directory + b"".join(images))

    return path


if __name__ == "__main__":
    target = write_icon(Path(__file__).parent / "screencast.ico")

    print(f"wrote {target} ({target.stat().st_size} bytes)")
