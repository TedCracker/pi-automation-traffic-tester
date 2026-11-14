#!/usr/bin/env python3
"""Draw the app icons (teal rounded square + white pulse line, same as the header logo) into traffic_tester/static/icons/.
Run with: venv/bin/python scripts/make_icons.py   (Pillow). Outputs icon-192.png, icon-512.png, icon-maskable-512.png, apple-touch-icon.png."""
import os
from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "traffic_tester", "static", "icons")
TEAL = (15, 118, 110, 255)
PULSE = [(6, 17), (11, 17), (14, 9), (18, 23), (21, 17), (26, 17)]      # the logo's polyline, in its 32 x 32 box


def draw(size, rounded, inner=1.0, ss=4):
    """rounded: corner radius as in the logo (8/32) or None for a full-bleed square; inner: scale of the pulse inside the square (maskable keeps it in the central 80 % safe zone)."""
    n = size * ss
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    if rounded: d.rounded_rectangle([0, 0, n - 1, n - 1], radius=int(n * 8 / 32), fill=TEAL)
    else: d.rectangle([0, 0, n, n], fill=TEAL)
    k = n / 32 * inner; off = n * (1 - inner) / 2
    pts = [(off + x * k, off + y * k) for x, y in PULSE]
    w = int(2.5 * k)
    d.line(pts, fill="white", width=w, joint="curve")
    for x, y in (pts[0], pts[-1]): d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill="white")      # round caps
    return im.resize((size, size), Image.LANCZOS)


os.makedirs(OUT, exist_ok=True)
draw(192, True).save(os.path.join(OUT, "icon-192.png"), optimize=True)
draw(512, True).save(os.path.join(OUT, "icon-512.png"), optimize=True)
draw(512, None, inner=0.72).save(os.path.join(OUT, "icon-maskable-512.png"), optimize=True)
draw(180, None, inner=0.86).convert("RGB").save(os.path.join(OUT, "apple-touch-icon.png"), optimize=True)    # iOS rounds the corners itself
print("icons written to", OUT)
