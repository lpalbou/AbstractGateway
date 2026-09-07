"""Tray icon renderer — the "gauge ring" (Pillow primitives only, no fonts).

Two concentric arcs on a faint track with a free centre: the OUTER arc is the
memory gauge (blue), the INNER arc the GPU gauge (amber), both filling
clockwise from 12 o'clock — at 100 % the ring closes. The centre is the state
slot: an activity dot while work is in flight, pause bars when paused, a
starting dot, an updating comet, or a red "!" when the gateway does not
answer. One icon, one message; the tooltip and the menu carry the rest.

Everything is drawn at 4x and downsampled (LANCZOS) so 2 px strokes at 16 px
stay crisp; every measure is a ratio of the canvas size S so the same object
renders at 16 (Windows 100 %), 22/24 (Linux panels, macOS 1x), 32 (Windows
master, downsampled by the shell) and 44 (macOS 2x Retina). Values are
quantized to 4 % steps before drawing so idle noise never re-encodes a PNG.

Three hues only: memory blue, GPU amber, alarm red. The neutral grey is
chosen to pass 3:1 on BOTH a white and a black bar so the icon needs no
light/dark detection to stay legible; when a platform does report its bar,
`mode="light"` / `mode="dark"` sharpen the contrast.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

STATES = ("running", "pausing", "paused", "starting", "restarting", "updating", "unreachable", "stopping")


@dataclass(frozen=True)
class Palette:
    memory: Tuple[int, int, int]
    gpu: Tuple[int, int, int]
    alarm: Tuple[int, int, int]
    neutral: Tuple[int, int, int]
    activity: Tuple[int, int, int]


def _hex(s: str) -> Tuple[int, int, int]:
    s = s.lstrip("#")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


PALETTES: Dict[str, Palette] = {
    # Works on any bar without knowing its colour.
    "neutral": Palette(memory=_hex("6ea8d8"), gpu=_hex("e8a54a"), alarm=_hex("e74c3c"), neutral=_hex("7f868f"), activity=_hex("7bc98c")),
    # A light bar was reported: the kit's darkened tokens (≥ 4.8:1 on white).
    "light": Palette(memory=_hex("2a6396"), gpu=_hex("8f5a0e"), alarm=_hex("dc2626"), neutral=_hex("1f1f1f"), activity=_hex("12883e")),
    # A dark bar was reported: the Observer Night hues, near-white neutral.
    "dark": Palette(memory=_hex("6ea8d8"), gpu=_hex("e8a54a"), alarm=_hex("e74c3c"), neutral=_hex("f0f0f0"), activity=_hex("7bc98c")),
}

QUANT_STEP = 4.0
MIN_SWEEP_DEG = 12.0
UPDATE_FRAMES = 8


def quantize(pct: Optional[float]) -> Optional[float]:
    """4 % steps; None stays None; a tiny non-zero value keeps a visible tick."""
    if pct is None:
        return None
    v = max(0.0, min(100.0, float(pct)))
    q = round(v / QUANT_STEP) * QUANT_STEP
    if v > 0 and q == 0:
        q = QUANT_STEP
    return float(q)


def icon_signature(*, size: int, state: str, mem_pct: Optional[float], gpu_pct: Optional[float], active: bool, frame: int, mode: str, wide: bool = False) -> tuple:
    """Cache key: identical signatures render identical images."""
    st = state if state in STATES else "running"
    if st in {"paused", "starting", "restarting", "unreachable", "stopping"}:
        # Gauges are not drawn in these states; do not thrash the cache on them.
        return (size, st, None, None, False, 0, mode, wide)
    if st == "updating":
        return (size, st, None, None, False, int(frame) % UPDATE_FRAMES, mode, wide)
    return (size, st, quantize(mem_pct), quantize(gpu_pct), bool(active), 0, mode, wide)


def _rgba(rgb: Tuple[int, int, int], alpha: float) -> Tuple[int, int, int, int]:
    return (rgb[0], rgb[1], rgb[2], int(round(255 * max(0.0, min(1.0, alpha)))))


def render_icon(
    size: int,
    *,
    state: str = "running",
    mem_pct: Optional[float] = None,
    gpu_pct: Optional[float] = None,
    active: bool = False,
    frame: int = 0,
    mode: str = "neutral",
    scale: int = 4,
):
    """Render one square icon of `size` px (RGBA). Pure: same inputs, same pixels."""
    from PIL import Image, ImageDraw

    pal = PALETTES.get(mode, PALETTES["neutral"])
    st = state if state in STATES else "running"
    S = int(size) * int(scale)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Geometry (ratios of the final canvas, scaled).
    px = lambda r: max(1, int(round(r * S)))  # noqa: E731
    inset = px(0.06)
    w1 = px(0.14)
    gap = px(0.06)
    w2 = px(0.12)
    inner_inset = inset + w1 + gap
    outer_box = (inset, inset, S - inset - 1, S - inset - 1)
    inner_box = (inner_inset, inner_inset, S - inner_inset - 1, S - inner_inset - 1)
    caps = size >= 22
    cx = cy = S / 2.0

    def track(box, width, alpha=0.30, color=pal.neutral):
        d.ellipse(box, outline=_rgba(color, alpha), width=width)

    def gauge(box, width, pct, color):
        if pct is None or pct <= 0:
            return
        sweep = max(MIN_SWEEP_DEG, 360.0 * float(pct) / 100.0)
        start = 270.0
        end = start + min(360.0, sweep)
        if sweep >= 359.5:
            d.ellipse(box, outline=_rgba(color, 1.0), width=width)
            return
        d.arc(box, start, end, fill=_rgba(color, 1.0), width=width)
        if caps:
            _cap(box, width, start, color)
            _cap(box, width, end, color)

    def _cap(box, width, angle_deg, color):
        import math

        r = (box[2] - box[0]) / 2.0
        rc = r - width / 2.0
        a = math.radians(angle_deg)
        x = cx + rc * math.cos(a)
        y = cy + rc * math.sin(a)
        rad = width / 2.0
        d.ellipse((x - rad, y - rad, x + rad, y + rad), fill=_rgba(color, 1.0))

    def dot(diameter_ratio, color, alpha=1.0):
        r = px(diameter_ratio) / 2.0
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=_rgba(color, alpha))

    def pause_bars(alpha):
        bw = px(0.12)
        bh = px(0.34)
        g = px(0.12)
        x0 = cx - g / 2.0 - bw
        x1 = cx + g / 2.0
        y0 = cy - bh / 2.0
        col = _rgba(pal.neutral, alpha)
        d.rounded_rectangle((x0, y0, x0 + bw, y0 + bh), radius=bw / 3.0, fill=col)
        d.rounded_rectangle((x1, y0, x1 + bw, y0 + bh), radius=bw / 3.0, fill=col)

    def bang(color):
        bw = px(0.12)
        bh = px(0.30)
        dd = px(0.12)
        g = px(0.06)
        total = bh + g + dd
        y0 = cy - total / 2.0
        col = _rgba(color, 1.0)
        d.rounded_rectangle((cx - bw / 2.0, y0, cx + bw / 2.0, y0 + bh), radius=bw / 3.0, fill=col)
        yd = y0 + bh + g
        d.ellipse((cx - dd / 2.0, yd, cx + dd / 2.0, yd + dd), fill=col)

    if st == "running":
        track(outer_box, w1)
        track(inner_box, w2)
        gauge(outer_box, w1, quantize(mem_pct), pal.memory)
        gauge(inner_box, w2, quantize(gpu_pct), pal.gpu)
        if active:
            dot(0.14, pal.activity)
    elif st == "pausing":
        track(outer_box, w1)
        track(inner_box, w2)
        gauge(outer_box, w1, quantize(mem_pct), pal.memory)
        gauge(inner_box, w2, quantize(gpu_pct), pal.gpu)
        pause_bars(0.60)
    elif st == "paused":
        track(outer_box, w1)
        pause_bars(1.0)
    elif st in {"starting", "restarting", "stopping"}:
        track(outer_box, w1)
        dot(0.20, pal.neutral, 0.60)
    elif st == "updating":
        track(outer_box, w1)
        start = 270.0 + 45.0 * (int(frame) % UPDATE_FRAMES)
        d.arc(outer_box, start, start + 90.0, fill=_rgba(pal.neutral, 1.0), width=w1)
        if caps:
            _cap(outer_box, w1, start, pal.neutral)
            _cap(outer_box, w1, start + 90.0, pal.neutral)
    elif st == "unreachable":
        d.ellipse(outer_box, outline=_rgba(pal.alarm, 1.0), width=w1)
        bang(pal.alarm)

    if scale != 1:
        img = img.resize((int(size), int(size)), Image.LANCZOS)
    return img


def render_wide_icon(
    height: int,
    *,
    mem_history: Tuple[Optional[float], ...],
    gpu_history: Tuple[Optional[float], ...],
    state: str = "running",
    mem_pct: Optional[float] = None,
    gpu_pct: Optional[float] = None,
    active: bool = False,
    frame: int = 0,
    mode: str = "neutral",
    samples: int = 60,
    scale: int = 4,
):
    """macOS opt-in wide variant: [gauge ring][gap][one graph panel]. Width =
    ring (height) + gap (4/22) + panel (26/22 of height). One panel, two
    series: memory as a filled area, GPU as a line over it."""
    from PIL import Image, ImageDraw

    pal = PALETTES.get(mode, PALETTES["neutral"])
    ring = render_icon(height, state=state, mem_pct=mem_pct, gpu_pct=gpu_pct, active=active, frame=frame, mode=mode, scale=scale)
    gap = max(2, int(round(height * 4 / 22)))
    panel_w = max(12, int(round(height * 26 / 22)))
    W = height + gap + panel_w
    img = Image.new("RGBA", (W, height), (0, 0, 0, 0))
    img.paste(ring, (0, 0), ring)

    S = scale
    pw, ph = panel_w * S, height * S
    panel = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    d = ImageDraw.Draw(panel)
    top = int(round(ph * 4 / 22))
    bottom = int(round(ph * 18 / 22))
    d.line((0, bottom, pw - 1, bottom), fill=_rgba(pal.neutral, 0.30), width=max(1, S))

    def points(series):
        vals = list(series)[-samples:]
        if len(vals) < samples:
            vals = [None] * (samples - len(vals)) + vals
        pts = []
        for i, v in enumerate(vals):
            if v is None:
                pts.append(None)
                continue
            x = (pw - 1) * i / max(1, samples - 1)
            y = bottom - (bottom - top) * max(0.0, min(100.0, float(v))) / 100.0
            pts.append((x, y))
        return pts

    def runs(pts):
        cur = []
        for p in pts:
            if p is None:
                if len(cur) > 1:
                    yield cur
                cur = []
            else:
                cur.append(p)
        if len(cur) > 1:
            yield cur

    for run in runs(points(mem_history)):
        poly = [(run[0][0], bottom)] + run + [(run[-1][0], bottom)]
        d.polygon(poly, fill=_rgba(pal.memory, 0.35))
        d.line(run, fill=_rgba(pal.memory, 1.0), width=max(1, int(1.5 * S)), joint="curve")
    for run in runs(points(gpu_history)):
        d.line(run, fill=_rgba(pal.gpu, 1.0), width=max(1, int(1.5 * S)), joint="curve")
    panel = panel.resize((panel_w, height), Image.LANCZOS)
    img.paste(panel, (height + gap, 0), panel)
    return img


def master_size_for_platform(platform: str) -> int:
    """The one square master each backend wants."""
    if platform == "darwin":
        return 44  # 2x of the 22 pt status bar; the darwin backend override sets the point size
    if platform.startswith("win"):
        return 32  # LoadImage(LR_DEFAULTSIZE) = 32, the shell downsamples to 16/24
    return 24
