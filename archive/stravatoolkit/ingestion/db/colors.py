"""Color generation utilities for athletes and activities."""
from __future__ import annotations


def athlete_color(athlete_id: int) -> list[int]:
    hash_value = (athlete_id * 2654435761) & 0xFFFFFFFF
    hue = hash_value % 360
    return hsl_to_rgb(hue / 360, 0.78, 0.62)


def activity_palette(index: int) -> list[int]:
    palette = [
        [97, 175, 255],
        [109, 211, 160],
        [244, 176, 89],
        [224, 123, 255],
        [255, 126, 126],
        [104, 205, 220],
        [188, 197, 90],
        [255, 155, 209],
    ]
    return palette[index % len(palette)]


def hsl_to_rgb(h: float, s: float, l: float) -> list[int]:
    def hue_to_rgb(p: float, q: float, t: float) -> float:
        if t < 0:
            t += 1
        if t > 1:
            t -= 1
        if t < 1 / 6:
            return p + (q - p) * 6 * t
        if t < 1 / 2:
            return q
        if t < 2 / 3:
            return p + (q - p) * (2 / 3 - t) * 6
        return p

    if s == 0:
        value = int(round(l * 255))
        return [value, value, value]

    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q
    r = hue_to_rgb(p, q, h + 1 / 3)
    g = hue_to_rgb(p, q, h)
    b = hue_to_rgb(p, q, h - 1 / 3)
    return [int(round(r * 255)), int(round(g * 255)), int(round(b * 255))]
