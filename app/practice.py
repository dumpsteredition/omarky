"""Real, geometry-verifiable practice cards for OMarky first use.

The challenge image is deliberately rendered with Cairo so the exact same PNG
can be handed to the native annotation canvas and to the selected conversation.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import math
import random
import re
import secrets
from typing import Iterable, Mapping, Sequence

WIDTH = 1200
HEIGHT = 720
PRACTICE_NOTE = "Read the key I circled. Reply with only its label."
_LABEL_LETTERS = "BCDFGHJKLMNPQRSTVWXYZ"
_LABEL_DIGITS = "2346789"


@dataclass(frozen=True)
class Keycap:
    label: str
    x: float
    y: float
    width: float
    height: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2, self.y + self.height / 2)


@dataclass(frozen=True)
class PracticeCard:
    png: bytes
    width: int
    height: int
    keycaps: tuple[Keycap, ...]
    note: str = PRACTICE_NOTE

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(key.label for key in self.keycaps)


def _new_label(rng) -> str:
    """Return a 2–3 glyph token with both letters and digits."""
    length = rng.choice((2, 3))
    digit_at = rng.randrange(length)
    return "".join(
        rng.choice(_LABEL_DIGITS if index == digit_at else _LABEL_LETTERS)
        for index in range(length)
    )


def _rounded_rectangle(ctx, x, y, width, height, radius):
    radius = min(radius, width / 2, height / 2)
    ctx.new_sub_path()
    ctx.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    ctx.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    ctx.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    ctx.arc(x + radius, y + radius, radius, math.pi, math.pi * 1.5)
    ctx.close_path()


def _text(ctx, value, x, y, size, color, *, bold=False, centered=False):
    ctx.select_font_face(
        "Sans",
        0,
        1 if bold else 0,
    )
    ctx.set_font_size(size)
    if centered:
        extents = ctx.text_extents(value)
        x -= extents.x_bearing + extents.width / 2
    ctx.set_source_rgb(*color)
    ctx.move_to(x, y)
    ctx.show_text(value)


def _render_png(keycaps: Sequence[Keycap]) -> bytes:
    try:
        import cairo
    except ImportError as exc:  # Keep CLI help and route-only imports usable.
        raise RuntimeError("OMarky practice cards need GTK's Cairo bindings") from exc

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, WIDTH, HEIGHT)
    ctx = cairo.Context(surface)

    # A deep ink frame and warm paper panel make the three controls read as
    # one small physical object rather than three unrelated answer buttons.
    ctx.set_source_rgb(0.055, 0.078, 0.12)
    ctx.paint()
    _rounded_rectangle(ctx, 48, 42, 1104, 636, 24)
    ctx.set_source_rgb(0.965, 0.949, 0.902)
    ctx.fill()

    # The mint dot is the OMarky mark; the yellow rule anchors the card's eye.
    ctx.arc(92, 91, 12, 0, math.tau)
    ctx.set_source_rgb(0.23, 0.69, 0.60)
    ctx.fill()
    _text(ctx, "OMARKY  /  PRACTICE TURN", 119, 99, 19, (0.15, 0.20, 0.28), bold=True)
    _rounded_rectangle(ctx, 938, 70, 166, 38, 19)
    ctx.set_source_rgb(1.0, 0.84, 0.39)
    ctx.fill()
    _text(ctx, "ONE-TURN CHECK", 1021, 96, 14, (0.12, 0.16, 0.23), bold=True, centered=True)

    _text(ctx, "Circle any one key.", 92, 174, 39, (0.08, 0.13, 0.20), bold=True)
    _text(ctx, "Press Send. Your chat should answer with the key you circled.", 92, 214, 20, (0.27, 0.33, 0.40))
    _rounded_rectangle(ctx, 92, 239, 1016, 2, 1)
    ctx.set_source_rgb(0.82, 0.82, 0.77)
    ctx.fill()

    cap_colors = (
        (0.13, 0.40, 0.37),
        (0.20, 0.31, 0.52),
        (0.47, 0.22, 0.34),
    )
    for index, key in enumerate(keycaps):
        # A soft offset gives the keycap depth without reducing its large hit area.
        _rounded_rectangle(ctx, key.x, key.y + 9, key.width, key.height, 26)
        ctx.set_source_rgb(0.78, 0.79, 0.76)
        ctx.fill()
        _rounded_rectangle(ctx, key.x, key.y, key.width, key.height, 26)
        ctx.set_source_rgb(*cap_colors[index])
        ctx.fill()
        ctx.set_line_width(2)
        ctx.set_source_rgb(0.95, 0.94, 0.91)
        ctx.stroke()
        _text(ctx, key.label, *key.center, 58, (1.0, 1.0, 0.98), bold=True, centered=True)

    output = BytesIO()
    surface.write_to_png(output)
    surface.finish()
    return output.getvalue()


def new_practice_card(rng=None) -> PracticeCard:
    """Render a fresh three-key challenge with no target labels in its note."""
    rng = rng if rng is not None else secrets.SystemRandom()
    labels = []
    while len(labels) < 3:
        candidate = _new_label(rng)
        if candidate not in labels:
            labels.append(candidate)

    cap_width, cap_height, cap_y = 244.0, 164.0, 330.0
    cap_xs = (132.0, 478.0, 824.0)
    keycaps = tuple(
        Keycap(label, cap_xs[index], cap_y, cap_width, cap_height)
        for index, label in enumerate(labels)
    )
    return PracticeCard(_render_png(keycaps), WIDTH, HEIGHT, keycaps)


def _valid_points(stroke) -> list[tuple[float, float]]:
    points = []
    if not isinstance(stroke, (list, tuple)):
        return points
    for point in stroke:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            continue
        if math.isfinite(x) and math.isfinite(y):
            points.append((x, y))
    return points


def _circle_score(points: Sequence[tuple[float, float]], key: Keycap) -> float:
    """Score a single loop around a cap, rejecting lines and broad scribbles."""
    if len(points) < 12:
        return 0.0

    xs, ys = zip(*points)
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x, span_y = max_x - min_x, max_y - min_y
    if span_x < key.width * 0.48 or span_y < key.height * 0.48:
        return 0.0
    if span_x > key.width * 1.7 or span_y > key.height * 1.8:
        return 0.0

    center_x, center_y = key.center
    box_center_x, box_center_y = (min_x + max_x) / 2, (min_y + max_y) / 2
    if abs(box_center_x - center_x) > 72 or abs(box_center_y - center_y) > 54:
        return 0.0

    length = sum(
        math.hypot(x2 - x1, y2 - y1)
        for (x1, y1), (x2, y2) in zip(points, points[1:])
    )
    if length < 180:
        return 0.0

    # A key circle should travel around the cap, not just cross its center.
    radius_x = key.width / 2 + 28
    radius_y = key.height / 2 + 22
    radii = [
        math.hypot((x - center_x) / radius_x, (y - center_y) / radius_y)
        for x, y in points
    ]
    ring_fraction = sum(0.52 <= radius <= 1.55 for radius in radii) / len(radii)
    if ring_fraction < 0.60:
        return 0.0
    return ring_fraction


def marked_key(meta: Mapping, card: PracticeCard) -> str | None:
    """Return one uniquely circled label from canvas ORIGINAL-image strokes.

    Ambiguous marks (including two circled keycaps) return ``None`` and can
    never verify a route. The caller must only invoke this for the untouched
    practice image: this first-use lane disables crop in practice mode.
    """
    strokes = meta.get("strokes", ()) if isinstance(meta, Mapping) else ()
    best_by_label: dict[str, float] = {}
    for stroke in strokes or ():
        points = _valid_points(stroke)
        if not points:
            continue
        for key in card.keycaps:
            score = _circle_score(points, key)
            if score:
                best_by_label[key.label] = max(best_by_label.get(key.label, 0.0), score)
    if len(best_by_label) != 1:
        return None
    return next(iter(best_by_label))


def answer_matches(answer: str | None, expected: str, labels: Iterable[str]) -> bool:
    """Accept punctuation/explanation, but not a wrong or multiple key guess."""
    if not isinstance(answer, str) or not answer.strip():
        return False
    allowed = {label.upper() for label in labels}
    expected = expected.upper()
    if expected not in allowed:
        return False
    tokens = re.findall(r"(?<![A-Z0-9])[A-Z0-9]{2,3}(?![A-Z0-9])", answer.upper())
    named = [token for token in tokens if token in allowed]
    return bool(named) and set(named) == {expected}
