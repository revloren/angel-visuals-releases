#!/usr/bin/env python3
"""Shrink PNG textures before building the Angel Visuals jar.

Minecraft reads only PNG, so the gains come from recompression:

  * RGBA pictures with no more than 256 distinct colours are rewritten as an
    exact palette PNG (lossless);
  * every other picture is recompressed losslessly at maximum zlib level;
  * with --lossy, large RGB pictures (menu backgrounds) are reduced to a
    256-colour palette with Floyd-Steinberg dithering. On the 0.5.14
    backgrounds this keeps PSNR at 40-45 dB (visually identical) and cuts
    16.0 MB to 5.8 MB.

A file is replaced only when the result is smaller. Use --dry-run first.

    python3 tools/optimize_textures.py src/main/resources/assets --dry-run
    python3 tools/optimize_textures.py src/main/resources/assets --lossy

Requires Pillow (pip install pillow); numpy is optional (for PSNR output).
"""
from __future__ import annotations

import argparse
import io
import math
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    sys.exit("Pillow is required: pip install pillow")

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

LOSSY_MIN_BYTES = 256 * 1024  # only big pictures are worth a palette


def encode(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True, compress_level=9)
    return buf.getvalue()


def exact_palette(img: Image.Image) -> Image.Image | None:
    """Palette image with exactly the same pixels, or None if > 256 colours."""
    rgba = img.convert("RGBA")
    colors = rgba.getcolors(256)
    if colors is None:
        return None
    index = {bytes(c): i for i, (_, c) in enumerate(colors)}
    raw = rgba.tobytes()
    pal = Image.frombytes("P", rgba.size, bytes(
        index[raw[i:i + 4]] for i in range(0, len(raw), 4)))
    flat = [ch for _, c in colors for ch in c[:3]]
    pal.putpalette(flat + [0] * (768 - len(flat)))
    alphas = bytes(c[3] for _, c in colors)
    if any(a != 255 for a in alphas):
        pal.info["transparency"] = alphas
    return pal


def psnr(a: Image.Image, b: Image.Image) -> float | None:
    if np is None:
        return None
    x = np.asarray(a.convert("RGBA"), dtype=np.float64)
    y = np.asarray(b.convert("RGBA"), dtype=np.float64)
    mse = float(((x - y) ** 2).mean())
    return math.inf if mse == 0 else 10 * math.log10(255 ** 2 / mse)


def optimize(path: Path, lossy: bool) -> tuple[bytes, str, float | None]:
    src = Image.open(path)
    src.load()
    best, how, quality = encode(src), "lossless", math.inf

    pal = exact_palette(src) if src.mode in ("RGBA", "LA", "P") else None
    if pal is not None:
        data = encode(pal)
        if len(data) < len(best):
            best, how = data, "exact palette"

    if lossy and src.mode == "RGB" and path.stat().st_size >= LOSSY_MIN_BYTES:
        q = src.quantize(256, method=Image.Quantize.MEDIANCUT,
                         dither=Image.Dither.FLOYDSTEINBERG)
        data = encode(q)
        if len(data) < len(best):
            best, how, quality = data, "palette 256 + dither", psnr(src, q)
    return best, how, quality


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("root", type=Path, help="assets folder or a single PNG")
    ap.add_argument("--lossy", action="store_true",
                    help="allow 256-colour palette for big RGB backgrounds")
    ap.add_argument("--dry-run", action="store_true", help="report only")
    args = ap.parse_args()

    files = [args.root] if args.root.is_file() else sorted(args.root.rglob("*.png"))
    before = after = 0
    for f in files:
        size = f.stat().st_size
        data, how, q = optimize(f, args.lossy)
        before += size
        if len(data) >= size:
            after += size
            continue
        after += len(data)
        qs = "" if q in (None, math.inf) else f"  PSNR {q:.1f} dB"
        print(f"{size / 1024:8.0f} KB -> {len(data) / 1024:8.0f} KB  {how:22s} {f}{qs}")
        if not args.dry_run:
            f.write_bytes(data)
    saved = before - after
    print(f"\n{len(files)} files: {before / 2**20:.2f} MB -> {after / 2**20:.2f} MB "
          f"(-{saved / 2**20:.2f} MB){' [dry run]' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
