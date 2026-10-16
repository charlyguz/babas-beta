import struct
import sys
import zlib
from pathlib import Path

BG = (37, 99, 235)
FG = (255, 255, 255)
SS = 4


def inside_rounded(x, y, size, radius):
    cx = min(max(x, radius), size - radius)
    cy = min(max(y, radius), size - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def inside_b(x, y, size):
    u, v = x / size, y / size
    stem = 0.28 <= u <= 0.40 and 0.20 <= v <= 0.80
    def bowl(cy, ry, rx_out, rx_in):
        if not (cy - ry <= v <= cy + ry):
            return False
        dy = (v - cy) / ry
        reach_out = 0.40 + rx_out * (1 - dy * dy) ** 0.5
        reach_in = 0.40 + rx_in * max(0.0, 1 - (abs(dy) * ry / (ry - 0.06)) ** 2) ** 0.5 if abs(dy) * ry < ry - 0.06 else 0.40
        return 0.34 <= u <= reach_out and not (u > 0.40 and u < reach_in and abs(v - cy) < ry - 0.06)
    return stem or bowl(0.355, 0.155, 0.27, 0.14) or bowl(0.645, 0.155, 0.31, 0.18)


def render(size):
    big = size * SS
    radius = big * 0.22
    rows = []
    for py in range(size):
        row = bytearray([0])
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(SS):
                for sx in range(SS):
                    x, y = px * SS + sx + 0.5, py * SS + sy + 0.5
                    if not inside_rounded(x, y, big, radius):
                        continue
                    color = FG if inside_b(x, y, big) else BG
                    acc[0] += color[0]
                    acc[1] += color[1]
                    acc[2] += color[2]
                    acc[3] += 255
            n = SS * SS
            alpha = acc[3] // n
            if alpha:
                covered = acc[3] // 255
                row += bytes([acc[0] // covered, acc[1] // covered, acc[2] // covered, alpha])
            else:
                row += bytes([0, 0, 0, 0])
        rows.append(bytes(row))
    raw = b"".join(rows)
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "public/icons")
    out.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 48, 128):
        (out / f"icon{size}.png").write_bytes(render(size))
        print(f"wrote {out / f'icon{size}.png'}")
