#!/usr/bin/python3
"""OMarky's GTK3/Cairo marking canvas.

The drawing controls and outlined freehand renderer are ported from the
installed Rare Tools annotator. This module deliberately keeps capture,
marked PNGs, and metadata in memory; routing belongs to the caller.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import selectors
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping

try:
    from chat_card import save_card
except ModuleNotFoundError:  # Support package import by the local MCP server.
    from .chat_card import save_card

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango  # noqa: E402

try:
    gi.require_version("GtkLayerShell", "0.1")
    from gi.repository import GtkLayerShell  # noqa: E402
except (ImportError, ValueError):  # GTK fullscreen remains available without layer-shell.
    GtkLayerShell = None

APP_NAME = "OMarky"
INK_OUTLINE_EXTENT_LOGICAL = 1.4
INK_PALETTE = {
    "Coral": (1.00, 0.25, 0.23, 1.0),
    "Cyan": (0.00, 0.62, 0.83, 1.0),
    "Amber": (0.98, 0.69, 0.14, 1.0),
    "White": (0.98, 0.98, 0.95, 1.0),
}
BRUSH_MIN_LOGICAL = 2.5
BRUSH_MAX_LOGICAL = 13.0
BRUSH_DEFAULT_LOGICAL = 6.5
COMMAND_TIMEOUT = 1.25
CLIPBOARD_WRITE_TIMEOUT = 2.0
ALLOWED_SEND_STATUSES = {"ok", "wrong", "busy", "gone", "uncertain", "error"}


def bounded_json(command: list[str], runner: Any = subprocess.run,
                 timeout: float = COMMAND_TIMEOUT) -> Any | None:
    """Read compositor output metadata without hanging on a missing socket."""
    try:
        completed = runner(command, check=True, capture_output=True, text=True,
                           timeout=timeout)
        return json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def _finite_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def read_focused_output(runner: Any = subprocess.run) -> dict[str, Any]:
    """Resolve one named, focused Hyprland output or fail closed."""
    outputs = bounded_json(["hyprctl", "-j", "monitors"], runner=runner)
    if not isinstance(outputs, list) or not outputs:
        raise RuntimeError("could not resolve a focused display; refusing an all-display capture")
    focused = [item for item in outputs
               if isinstance(item, dict) and item.get("focused") is True]
    if len(focused) == 1:
        output = focused[0]
    elif not focused and len(outputs) == 1 and isinstance(outputs[0], dict):
        output = outputs[0]
    else:
        raise RuntimeError("focused display is ambiguous; refusing an all-display capture")
    name = output.get("name")
    geometry = [output.get(key) for key in ("x", "y", "width", "height")]
    if (not isinstance(name, str) or not name.strip()
            or any(not _finite_number(value) for value in geometry)
            or geometry[2] <= 0 or geometry[3] <= 0):
        raise RuntimeError("focused display has incomplete name/geometry; capture stopped")
    scale = output.get("scale", 1.0)
    if not _finite_number(scale) or scale <= 0:
        raise RuntimeError("focused display has invalid scale; capture stopped")
    return dict(output)


def resolve_output_monitor(output: dict[str, Any], display: Any | None = None) -> Any:
    """Bind a compositor output to a GDK monitor by geometry, never list order."""
    display = display or Gdk.Display.get_default()
    if display is None:
        raise RuntimeError("no graphical display is available for the annotation canvas")
    count = display.get_n_monitors()
    if count <= 0:
        raise RuntimeError("no graphical monitors are available for the annotation canvas")
    monitors = [display.get_monitor(index) for index in range(count)]
    monitors = [monitor for monitor in monitors if monitor is not None]

    x, y, width, height = (output.get(key) for key in ("x", "y", "width", "height"))
    scale = output.get("scale", 1.0)
    expected_sizes = {(float(width), float(height)),
                      (float(width) / float(scale), float(height) / float(scale))}
    matches = []
    for monitor in monitors:
        geometry = monitor.get_geometry()
        if abs(float(geometry.x) - float(x)) > 1 or abs(float(geometry.y) - float(y)) > 1:
            continue
        if any(abs(float(geometry.width) - expected_w) <= 1
               and abs(float(geometry.height) - expected_h) <= 1
               for expected_w, expected_h in expected_sizes):
            matches.append(monitor)
    if len(matches) != 1:
        raise RuntimeError("focused output could not be uniquely matched by geometry; capture stopped")
    return matches[0]


def read_screenshot(output: dict[str, Any], runner: Any = subprocess.run) -> bytes:
    """Freeze only the explicitly named output; never invoke grim without -o."""
    name = output.get("name") if isinstance(output, dict) else None
    if not isinstance(name, str) or not name.strip():
        raise RuntimeError("focused display has no output name; refusing an all-display capture")
    try:
        result = runner(["grim", "-o", name, "-"], check=True,
                        capture_output=True, timeout=15)
    except FileNotFoundError as exc:
        raise RuntimeError("grim is required for focused-display capture") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("grim timed out while capturing the focused display") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", "replace").strip() if exc.stderr else ""
        raise RuntimeError(f"focused-display capture failed{': ' + detail if detail else ''}") from exc
    png = result.stdout
    if not isinstance(png, bytes) or not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("grim returned no PNG for the focused display")
    return png


def decode_pixbuf(png: bytes) -> GdkPixbuf.Pixbuf:
    if not isinstance(png, bytes) or not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("source image must be PNG bytes")
    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
    try:
        loader.write(png)
        loader.close()
    except Exception as exc:
        raise ValueError("could not decode source PNG") from exc
    pixbuf = loader.get_pixbuf()
    if pixbuf is None:
        raise ValueError("could not decode source PNG")
    return pixbuf.copy()


def prepare_source(sample_png: bytes | None) -> tuple[bytes, dict[str, Any] | None]:
    """Use supplied practice bytes as-is; routine capture is a separate branch."""
    if sample_png is not None:
        if not isinstance(sample_png, bytes):
            raise TypeError("sample_png must be bytes or None")
        decode_pixbuf(sample_png)
        return sample_png, None
    output = read_focused_output()
    png = read_screenshot(output)
    decode_pixbuf(png)
    return png, output


def normalize_strokes(strokes: list[Any]) -> list[dict[str, Any]]:
    """Validate independent freehand strokes and retain their individual pen state."""
    if not strokes:
        return []
    if isinstance(strokes[0], dict):
        result = []
        for stroke in strokes:
            if not isinstance(stroke, dict):
                raise ValueError("stroke records must not be mixed with point lists")
            points = stroke.get("points")
            width = stroke.get("width", BRUSH_DEFAULT_LOGICAL)
            color = stroke.get("color", INK_PALETTE["Coral"])
            if not isinstance(points, (list, tuple)) or not points:
                raise ValueError("each stroke must contain at least one point")
            if not _finite_number(width) or width <= 0:
                raise ValueError("each stroke must have a finite positive width")
            if (not isinstance(color, (list, tuple)) or len(color) != 4
                    or any(not _finite_number(value) or value < 0 or value > 1
                           for value in color)):
                raise ValueError("each stroke must have a finite RGBA color")
            parsed_points = []
            for point in points:
                if (not isinstance(point, (tuple, list)) or len(point) != 2
                        or any(not _finite_number(value) for value in point)):
                    raise ValueError("stroke points must be finite x/y pairs")
                parsed_points.append((float(point[0]), float(point[1])))
            record = {"points": parsed_points, "width": float(width),
                      "color": tuple(float(value) for value in color)}
            if isinstance(stroke.get("color_name"), str):
                record["color_name"] = stroke["color_name"]
            result.append(record)
        return result
    points = strokes
    return normalize_strokes([{"points": points}])


def trace_stroke(context: cairo.Context, points: list[tuple[float, float]]) -> None:
    """Draw a smoothed path whose curves remain inside the sampled-point hull."""
    if len(points) == 1:
        context.arc(points[0][0], points[0][1], 0.5, 0, math.tau)
        return
    context.move_to(*points[0])
    if len(points) == 2:
        context.line_to(*points[1])
        return

    def quadratic_to(control: tuple[float, float], end: tuple[float, float]) -> None:
        current = context.get_current_point()
        first = (current[0] + (control[0] - current[0]) * (2.0 / 3.0),
                 current[1] + (control[1] - current[1]) * (2.0 / 3.0))
        second = (end[0] + (control[0] - end[0]) * (2.0 / 3.0),
                  end[1] + (control[1] - end[1]) * (2.0 / 3.0))
        context.curve_to(*first, *second, *end)

    for index in range(1, len(points) - 1):
        control = points[index]
        following = points[index + 1]
        midpoint = ((control[0] + following[0]) / 2.0,
                    (control[1] + following[1]) / 2.0)
        quadratic_to(control, midpoint)
    quadratic_to(points[-2], points[-1])


def paint_strokes(context: cairo.Context, strokes: list[Any]) -> None:
    """Render the donor's outlined, high-contrast marks in preview and export."""
    context.set_line_cap(cairo.LINE_CAP_ROUND)
    context.set_line_join(cairo.LINE_JOIN_ROUND)
    for stroke in normalize_strokes(strokes):
        points = stroke["points"]
        width = stroke["width"]
        context.save()
        if len(points) == 1:
            context.set_source_rgba(0.03, 0.045, 0.065, 0.88)
            context.arc(points[0][0], points[0][1], width / 2 + INK_OUTLINE_EXTENT_LOGICAL,
                        0, math.tau)
            context.fill()
            context.set_source_rgba(*stroke["color"])
            context.arc(points[0][0], points[0][1], width / 2, 0, math.tau)
            context.fill()
        else:
            context.set_line_width(width + 2 * INK_OUTLINE_EXTENT_LOGICAL)
            context.set_source_rgba(0.96, 0.97, 0.98, 0.88)
            trace_stroke(context, points)
            context.stroke()
            context.set_line_width(width + 0.8)
            context.set_source_rgba(0.025, 0.035, 0.05, 0.82)
            trace_stroke(context, points)
            context.stroke()
            context.set_line_width(width)
            context.set_source_rgba(*stroke["color"])
            trace_stroke(context, points)
            context.stroke()
        context.restore()


def fit_image_rect(image_width: int, image_height: int,
                   canvas_width: int, canvas_height: int) -> tuple[float, float, float, float]:
    """Aspect-fit an entire card in a canvas and center any letterboxing."""
    if min(image_width, image_height, canvas_width, canvas_height) <= 0:
        return 0.0, 0.0, 0.0, 0.0
    scale = min(canvas_width / image_width, canvas_height / image_height)
    width = float(max(1, min(canvas_width, round(image_width * scale))))
    height = float(max(1, min(canvas_height, round(image_height * scale))))
    return (canvas_width - width) / 2.0, (canvas_height - height) / 2.0, width, height


def canvas_to_image_point(point: tuple[float, float], image_rect: tuple[float, float, float, float],
                          image_width: int, image_height: int) -> tuple[float, float]:
    x0, y0, width, height = image_rect
    if width <= 0 or height <= 0:
        raise ValueError("image is not laid out on the canvas")
    x = max(0.0, min(float(image_width), (float(point[0]) - x0) * image_width / width))
    y = max(0.0, min(float(image_height), (float(point[1]) - y0) * image_height / height))
    return x, y


def canvas_strokes_to_image(strokes: list[Any], image_rect: tuple[float, float, float, float],
                            image_width: int, image_height: int) -> list[dict[str, Any]]:
    """Convert canvas-logical marks into original-image physical pixels."""
    x0, y0, rect_width, rect_height = image_rect
    if rect_width <= 0 or rect_height <= 0:
        raise ValueError("image is not laid out on the canvas")
    pixels_per_logical = ((image_width / rect_width) + (image_height / rect_height)) / 2.0
    mapped = []
    for stroke in normalize_strokes(strokes):
        mapped.append({
            "points": [canvas_to_image_point(point, image_rect, image_width, image_height)
                       for point in stroke["points"]],
            "width": stroke["width"] * pixels_per_logical,
            "color": stroke["color"],
            "color_name": stroke.get("color_name"),
        })
    return mapped


def build_metadata(image_width: int, image_height: int,
                   image_strokes: list[Any]) -> dict[str, Any]:
    """Export only original-frame dimensions and per-stroke original pixel points."""
    records = normalize_strokes(image_strokes)
    return {
        "width": int(image_width),
        "height": int(image_height),
        "strokes": [[[float(x), float(y)] for x, y in stroke["points"]]
                    for stroke in records],
    }


def validate_capture_bounds(bounds: dict[str, Any] | None,
                            pixel_width: int, pixel_height: int) -> dict[str, int]:
    if pixel_width <= 0 or pixel_height <= 0:
        raise ValueError("invalid source image dimensions")
    if bounds is None:
        return {"x": 0, "y": 0, "width": pixel_width, "height": pixel_height}
    if not isinstance(bounds, dict):
        raise ValueError("crop bounds must be a rectangle")
    values = [bounds.get(key) for key in ("x", "y", "width", "height")]
    if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
        raise ValueError("crop bounds must use integer source-image pixels")
    x, y, width, height = values
    if (x < 0 or y < 0 or width <= 0 or height <= 0
            or x + width > pixel_width or y + height > pixel_height):
        raise ValueError("crop bounds fall outside the original image")
    return {"x": x, "y": y, "width": width, "height": height}


def manual_crop_bounds(start: tuple[float, float], end: tuple[float, float],
                       pixel_width: int, pixel_height: int,
                       minimum_pixels: float = 8.0) -> dict[str, int]:
    """Map a crop drag in original-image pixels to a clamped source rectangle."""
    if (not isinstance(start, (tuple, list)) or len(start) != 2
            or not isinstance(end, (tuple, list)) or len(end) != 2):
        raise ValueError("crop selection must have two x/y points")
    values = (pixel_width, pixel_height, minimum_pixels, *start, *end)
    if any(not _finite_number(value) for value in values):
        raise ValueError("crop selection coordinates must be finite numbers")
    if pixel_width <= 0 or pixel_height <= 0 or minimum_pixels <= 0:
        raise ValueError("invalid crop dimensions")
    left, right = sorted((max(0.0, min(float(pixel_width), float(start[0]))),
                          max(0.0, min(float(pixel_width), float(end[0])))))
    top, bottom = sorted((max(0.0, min(float(pixel_height), float(start[1]))),
                          max(0.0, min(float(pixel_height), float(end[1])))))
    if right - left < minimum_pixels or bottom - top < minimum_pixels:
        raise ValueError("crop selection is blank or too small")
    x0, y0 = max(0, math.floor(left)), max(0, math.floor(top))
    x1, y1 = min(pixel_width, math.ceil(right)), min(pixel_height, math.ceil(bottom))
    if x1 <= x0 or y1 <= y0:
        raise ValueError("crop selection is blank or outside the original image")
    return {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}


def crop_png(original_png: bytes, bounds: dict[str, int]) -> bytes:
    source = cairo.ImageSurface.create_from_png(io.BytesIO(original_png))
    checked = validate_capture_bounds(bounds, source.get_width(), source.get_height())
    crop = cairo.ImageSurface(cairo.FORMAT_ARGB32, checked["width"], checked["height"])
    context = cairo.Context(crop)
    context.set_source_surface(source, -checked["x"], -checked["y"])
    context.paint()
    crop.flush()
    encoded = io.BytesIO()
    crop.write_to_png(encoded)
    return encoded.getvalue()


def render_marked_png(original_png: bytes, image_strokes: list[Any],
                      bounds: dict[str, int] | None = None) -> bytes:
    """Render the marked source and optional crop entirely in memory."""
    records = normalize_strokes(image_strokes)
    base = cairo.ImageSurface.create_from_png(io.BytesIO(original_png))
    width, height = base.get_width(), base.get_height()
    checked = validate_capture_bounds(bounds, width, height)
    full = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    context = cairo.Context(full)
    context.set_source_surface(base, 0, 0)
    context.paint()
    paint_strokes(context, records)
    full.flush()
    crop = cairo.ImageSurface(cairo.FORMAT_ARGB32, checked["width"], checked["height"])
    crop_context = cairo.Context(crop)
    crop_context.set_source_surface(full, -checked["x"], -checked["y"])
    crop_context.paint()
    crop.flush()
    encoded = io.BytesIO()
    crop.write_to_png(encoded)
    return encoded.getvalue()


def _stop_clipboard_owner(owner: subprocess.Popen[bytes]) -> None:
    if owner.poll() is None:
        owner.terminate()
        try:
            owner.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            owner.kill()
            owner.wait(timeout=1.0)


def publish_clipboard(png: bytes) -> None:
    """Explicit Copy path: offer image/png and verify exact Wayland readback."""
    try:
        owner = subprocess.Popen(
            ["wl-copy", "--type", "image/png"], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("wl-copy is required to copy the marked PNG") from exc
    try:
        if owner.stdin is None:
            raise RuntimeError("wl-copy did not provide an input pipe")
        descriptor = owner.stdin.fileno()
        os.set_blocking(descriptor, False)
        pending = memoryview(png)
        deadline = time.monotonic() + CLIPBOARD_WRITE_TIMEOUT
        with selectors.DefaultSelector() as writable:
            writable.register(owner.stdin, selectors.EVENT_WRITE)
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not writable.select(remaining):
                    raise TimeoutError("wl-copy did not consume the marked PNG in time")
                try:
                    written = os.write(descriptor, pending)
                except BlockingIOError:
                    continue
                if written <= 0:
                    raise BrokenPipeError("wl-copy closed its input before receiving the PNG")
                pending = pending[written:]
        owner.stdin.close()
        try:
            code = owner.wait(timeout=0.3)
        except subprocess.TimeoutExpired:
            code = None  # A live wl-copy process owns the Wayland offer.
        if code not in (None, 0):
            raise RuntimeError(f"wl-copy exited with status {code}")
        pasted = subprocess.run(["wl-paste", "--type", "image/png"],
                                capture_output=True, timeout=5)
        if pasted.returncode:
            detail = pasted.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"clipboard readback failed{': ' + detail if detail else ''}")
        if pasted.stdout != png:
            raise RuntimeError("clipboard readback differs from the marked PNG")
    except (OSError, subprocess.SubprocessError) as exc:
        _stop_clipboard_owner(owner)
        raise RuntimeError(f"could not copy the marked PNG: {exc}") from exc
    except Exception:
        _stop_clipboard_owner(owner)
        raise


def publish_handoff_clipboard(text: str) -> None:
    """Offer one inert text handoff and verify exact text clipboard readback."""
    if not isinstance(text, str):
        raise TypeError("For chat clipboard content must be text")
    payload = text.encode("utf-8", "strict")
    try:
        owner = subprocess.Popen(
            ["wl-copy", "--type", "text/plain;charset=utf-8"], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("wl-copy is required for the For chat handoff") from exc
    try:
        if owner.stdin is None:
            raise RuntimeError("wl-copy did not provide an input pipe")
        descriptor = owner.stdin.fileno()
        os.set_blocking(descriptor, False)
        pending = memoryview(payload)
        deadline = time.monotonic() + CLIPBOARD_WRITE_TIMEOUT
        with selectors.DefaultSelector() as writable:
            writable.register(owner.stdin, selectors.EVENT_WRITE)
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not writable.select(remaining):
                    raise TimeoutError("wl-copy did not consume the For chat text in time")
                try:
                    written = os.write(descriptor, pending)
                except BlockingIOError:
                    continue
                if written <= 0:
                    raise BrokenPipeError("wl-copy closed its input before receiving the handoff")
                pending = pending[written:]
        owner.stdin.close()
        try:
            code = owner.wait(timeout=0.3)
        except subprocess.TimeoutExpired:
            code = None  # A live wl-copy process owns the Wayland text offer.
        if code not in (None, 0):
            raise RuntimeError(f"wl-copy exited with status {code}")
        pasted = subprocess.run(
            ["wl-paste", "--no-newline", "--type", "text/plain;charset=utf-8"],
            capture_output=True, timeout=5,
        )
        if pasted.returncode:
            detail = pasted.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"clipboard readback failed{': ' + detail if detail else ''}")
        if pasted.stdout != payload:
            raise RuntimeError("clipboard readback differs from the For chat text")
    except (OSError, subprocess.SubprocessError) as exc:
        _stop_clipboard_owner(owner)
        raise RuntimeError(f"could not copy the For chat handoff: {exc}") from exc
    except Exception:
        _stop_clipboard_owner(owner)
        raise


def build_handoff_text(card: Mapping[str, Any]) -> str:
    """Format one human-readable paste request without shell or submit commands."""
    if not isinstance(card, Mapping):
        raise ValueError("card storage returned no card details")
    card_id = card.get("id")
    image_path = card.get("image_path")
    note = card.get("note")
    if (not isinstance(card_id, str) or len(card_id) != 32
            or any(char not in "0123456789abcdef" for char in card_id)):
        raise ValueError("card storage returned an invalid ID")
    if (not isinstance(image_path, str) or not os.path.isabs(image_path)
            or any(ord(char) < 32 or ord(char) == 127 for char in image_path)):
        raise ValueError("card storage returned an invalid image path")
    if not isinstance(note, str):
        raise ValueError("card storage returned an invalid note")
    return (
        f"Use OMarky card {card_id} in this conversation. "
        f"If the OMarky tool is available, call read_annotation with that ID. "
        f"Otherwise open this local image: {image_path}\nMy note: {note}"
    )


def invoke_for_chat(marked_png: bytes, note: str, *, card_saver: Any = None,
                    clipboard_writer: Callable[[str], None] | None = None) -> dict[str, str]:
    """Save one explicit marked card, then copy its request; never send a chat."""
    saver = card_saver if card_saver is not None else save_card
    writer = clipboard_writer if clipboard_writer is not None else publish_handoff_clipboard
    try:
        card = saver(marked_png, note)
        request = build_handoff_text(card)
    except Exception as exc:
        return {
            "status": "error",
            "message": f"Could not export the marked image path; nothing copied or sent. {type(exc).__name__}: {exc}",
        }
    try:
        writer(request)
    except Exception as exc:
        return {
            "status": "error",
            "message": (f"Card {card['id']} is saved at {card['image_path']}, but clipboard text "
                        f"was not verified; nothing was sent. {type(exc).__name__}: {exc}"),
        }
    return {
        "status": "ok",
        "message": f"Ready to paste · nothing sent · card {card['id']} expires in 30 minutes",
    }


def validated_send_result(result: Any) -> dict[str, str]:
    if not isinstance(result, dict):
        return {"status": "error", "message": "send callback returned no result"}
    status = result.get("status")
    if not isinstance(status, str) or status not in ALLOWED_SEND_STATUSES:
        return {"status": "error", "message": "send callback returned an invalid status"}
    message = result.get("message", "")
    if not isinstance(message, str):
        message = str(message)
    return {"status": status, "message": message}


def invoke_send(callback: Callable[[bytes, str, dict[str, Any]], dict[str, Any]],
                marked_png: bytes, note: str, meta: dict[str, Any]) -> dict[str, str]:
    """Invoke the integration callback once; caller owns the worker thread."""
    return validated_send_result(callback(marked_png, note, meta))


def invoke_copy(writer: Callable[[bytes], None], marked_png: bytes) -> dict[str, str]:
    """Report Copy truthfully without losing the in-memory marked image on failure."""
    try:
        writer(marked_png)
    except Exception as exc:
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
    return {"status": "ok", "message": "Copied; not sent"}


def selected_target(current: str, choice: Any) -> str:
    """Keep the displayed target unchanged when the chooser cancels."""
    if choice is None:
        return current
    if not isinstance(choice, str):
        raise TypeError("target chooser must return a label or None")
    return choice


def action_allowed(kind: str, *, target_label: str, has_ink: bool,
                   crop_mode: bool, busy: bool,
                   terminal_state: str | None = None) -> bool:
    """For chat and Copy need marks, not a paired chat; Send additionally needs a target."""
    if kind not in {"copy", "for_chat", "send"} or busy or crop_mode or not has_ink:
        return False
    if kind == "send":
        return bool(target_label.strip()) and terminal_state not in {"ok", "uncertain"}
    return True


def set_accessible_name(widget: Gtk.Widget, name: str,
                        description: str | None = None) -> None:
    try:
        accessible = widget.get_accessible()
        accessible.set_name(name)
        if description:
            accessible.set_description(description)
    except Exception:
        pass


def install_overlay_styles() -> None:
    css = b"""
    .omarky-toolbar {
        background-color: rgba(18, 24, 33, 0.96);
        color: #f4f7fb;
        border: 1px solid rgba(237, 244, 255, 0.18);
        border-radius: 12px;
        padding: 6px 8px;
    }
    .omarky-toolbar label { color: #edf2f8; }
    .omarky-status { color: #d4dde7; font-size: 11px; }
    .omarky-button {
        background-image: none; background-color: rgba(255,255,255,0.075);
        color: #f4f7fb; border: 1px solid rgba(235,243,255,0.17);
        border-radius: 7px; padding: 3px 7px; min-height: 24px;
    }
    .omarky-button:hover { background-color: rgba(255,255,255,0.15); }
    .omarky-button:focus { border: 1px solid #76d9e5; }
    .omarky-button:disabled { color: rgba(244,247,251,0.45); }
    .omarky-primary-button {
        background-image: none; background-color: #57c9d8; color: #09141a;
        border: 1px solid #7edbe5; border-radius: 7px; padding: 3px 9px;
        min-height: 24px; font-weight: 700;
    }
    .omarky-primary-button:hover { background-color: #7bd9e3; }
    .omarky-primary-button:focus { border: 2px solid #ffffff; }
    .omarky-primary-button:disabled {
        background-color: rgba(87,201,216,0.35); color: rgba(8,20,26,0.55);
    }
    .omarky-entry {
        background-color: rgba(255,255,255,0.075); color: #f4f7fb;
        border: 1px solid rgba(235,243,255,0.17); border-radius: 7px;
        padding: 4px 8px; min-height: 26px; caret-color: #7bd9e3;
    }
    .omarky-swatch-button {
        background-image: none; background-color: transparent;
        border: 1px solid transparent; border-radius: 7px; padding: 2px;
        min-width: 24px; min-height: 24px;
    }
    .omarky-swatch-button:hover { background-color: rgba(255,255,255,0.11); }
    .omarky-size-scale trough {
        min-height: 4px; background-color: rgba(255,255,255,0.24); border-radius: 3px;
    }
    .omarky-size-scale highlight { background-color: #70d4df; border-radius: 3px; }
    .omarky-size-scale slider {
        min-width: 13px; min-height: 13px; background-color: #f4f7fb;
        border: 1px solid #263542; border-radius: 99px;
    }
    """
    screen = Gdk.Screen.get_default()
    if screen is not None:
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )


class AnnotateOverlay:
    """The Rare Tools freehand canvas adapted to OMarky's in-memory contract."""
    def __init__(self, screenshot: bytes, target_label: str,
                 on_send: Callable[[bytes, str, dict[str, Any]], dict[str, Any]],
                 on_choose: Callable[[], str | None] | None,
                 monitor: Any, loop: GLib.MainLoop,
                 *, practice: bool, note: str,
                 clipboard_writer: Callable[[bytes], None] = publish_clipboard,
                 handoff_writer: Callable[[str], None] = publish_handoff_clipboard):
        self.screenshot = screenshot
        self.pixbuf = decode_pixbuf(screenshot)
        self._scaled_backgrounds: dict[tuple[int, int], GdkPixbuf.Pixbuf] = {}
        self.target_label = target_label
        self.on_send_callback = on_send
        self.on_choose_callback = on_choose
        self.monitor = monitor
        self.loop = loop
        self.practice = practice
        self.clipboard_writer = clipboard_writer
        self.handoff_writer = handoff_writer
        self.strokes: list[dict[str, Any]] = []
        self.drawing = False
        self.crop_mode = False
        self.crop_dragging = False
        self.crop_drag_start: tuple[float, float] | None = None
        self.crop_drag_end: tuple[float, float] | None = None
        self.capture_bounds: dict[str, int] | None = None
        self.busy = False
        self.action_kind: str | None = None
        self.terminal_send_state: str | None = None
        self.selected_color_name = "Coral"
        self.selected_color = INK_PALETTE[self.selected_color_name]
        self.brush_width = BRUSH_DEFAULT_LOGICAL
        self.toolbar_position = "bottom"
        self.note_expanded = bool(note)
        self._note_focus_pending = False
        self.last_marked_png: bytes | None = None
        self.closed = False
        self.window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.window.set_title(f"{APP_NAME} — Mark")
        self.window.set_name("omarky-annotate")
        self.window.set_decorated(False)
        self.window.set_modal(True)
        self.window.set_app_paintable(True)
        self.window.set_resizable(False)
        geometry = monitor.get_geometry()
        self.window.set_default_size(geometry.width, geometry.height)
        self._bind_window_to_monitor(monitor)
        install_overlay_styles()
        self.overlay = Gtk.Overlay()
        self.area = Gtk.DrawingArea()
        self.area.set_can_focus(True)
        self.area.set_hexpand(True)
        self.area.set_vexpand(True)
        self.area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                             Gdk.EventMask.POINTER_MOTION_MASK)
        self.area.connect("draw", self.on_draw)
        self.area.connect("button-press-event", self.on_press)
        self.area.connect("motion-notify-event", self.on_motion)
        self.area.connect("button-release-event", self.on_release)
        self.overlay.add(self.area)
        self._build_toolbar(note)
        self.overlay.add_overlay(self.toolbar)
        self.window.add(self.overlay)
        self.window.connect("key-press-event", self.on_key)
        self.window.connect("delete-event", self.on_delete)
        self.window.connect("destroy", self.on_destroy)
        self.window.show_all()
        if self.practice:
            self.for_chat_button.hide()
        if not self.note_expanded:
            self.note_row.hide()
        self.area.grab_focus()
        self._update_controls()

    def _bind_window_to_monitor(self, monitor: Any) -> None:
        if GtkLayerShell is not None:
            try:
                GtkLayerShell.init_for_window(self.window)
                GtkLayerShell.set_layer(self.window, GtkLayerShell.Layer.OVERLAY)
                GtkLayerShell.set_keyboard_mode(self.window, GtkLayerShell.KeyboardMode.EXCLUSIVE)
                GtkLayerShell.set_exclusive_zone(self.window, -1)
                for edge in (GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.BOTTOM,
                             GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT):
                    GtkLayerShell.set_anchor(self.window, edge, True)
                GtkLayerShell.set_monitor(self.window, monitor)
                return
            except Exception:
                pass
        screen = Gdk.Screen.get_default()
        if screen is None:
            raise RuntimeError("could not bind the annotation canvas to a display")
        self.window.fullscreen_on_monitor(screen, monitor.get_number())

    def _build_toolbar(self, note: str) -> None:
        self.toolbar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.toolbar.set_name("omarky-toolbar")
        self.toolbar.get_style_context().add_class("omarky-toolbar")
        self.toolbar.set_size_request(720, -1)
        self.toolbar.set_halign(Gtk.Align.CENTER)
        self.toolbar.set_valign(Gtk.Align.END)
        self.toolbar.set_margin_start(10)
        self.toolbar.set_margin_end(10)
        self.toolbar.set_margin_bottom(12)
        set_accessible_name(self.toolbar, "Annotation controls")

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self.toolbar.pack_start(top, False, False, 0)
        self.position_button = self._button("↑", "Move controls to the top edge")
        self.position_button.connect("clicked", self.on_position_clicked)
        top.pack_start(self.position_button, False, False, 0)

        self.status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self.toolbar.pack_start(self.status_row, False, False, 0)
        self.status_label = Gtk.Label(label="Ready to mark")
        self.status_label.set_xalign(0.0)
        self.status_label.set_hexpand(True)
        self.status_label.set_width_chars(14)
        self.status_label.set_max_width_chars(36)
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.status_label.get_style_context().add_class("omarky-status")
        set_accessible_name(self.status_label, "Annotation status")
        self.status_row.pack_start(self.status_label, True, True, 2)

        self.target_label_widget = Gtk.Label()
        self.target_label_widget.set_ellipsize(Pango.EllipsizeMode.END)
        self.target_label_widget.set_max_width_chars(20)
        self.target_label_widget.set_tooltip_text(self.target_label or "Copy only; no chat selected")
        top.pack_start(self.target_label_widget, False, False, 0)
        self.choose_button = None
        if self.on_choose_callback is not None:
            self.choose_button = self._button("Choose", "Choose another chat; keep all current marks")
            self.choose_button.connect("clicked", self.on_choose_clicked)
            top.pack_start(self.choose_button, False, False, 0)

        self.color_buttons: dict[str, Gtk.Button] = {}
        palette = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=1)
        set_accessible_name(palette, "Pen colors")
        top.pack_start(palette, False, False, 0)
        for color_name, rgba in INK_PALETTE.items():
            button = Gtk.Button()
            button.set_relief(Gtk.ReliefStyle.NONE)
            button.set_size_request(26, 26)
            button.get_style_context().add_class("omarky-swatch-button")
            button.set_tooltip_text(f"Ink color: {color_name}")
            set_accessible_name(button, f"Ink color {color_name}",
                                "Select color for the next mark only")
            swatch = Gtk.DrawingArea()
            swatch.set_size_request(17, 17)
            swatch.connect("draw", self.draw_swatch, color_name, rgba)
            button.add(swatch)
            button.connect("clicked", self.on_color_clicked, color_name)
            palette.pack_start(button, False, False, 0)
            self.color_buttons[color_name] = button

        size_group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        set_accessible_name(size_group, "Brush width controls")
        top.pack_start(size_group, False, False, 0)
        self.size_preview = Gtk.DrawingArea()
        self.size_preview.set_size_request(18, 18)
        set_accessible_name(self.size_preview, "Live brush-width preview")
        size_group.pack_start(self.size_preview, False, False, 0)
        self.size_preview.connect("draw", self.draw_size_preview)
        self.size_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, BRUSH_MIN_LOGICAL, BRUSH_MAX_LOGICAL, 0.5
        )
        self.size_scale.set_name("omarky-brush-size")
        self.size_scale.set_size_request(82, 22)
        self.size_scale.set_draw_value(False)
        self.size_scale.set_digits(1)
        self.size_scale.set_value(BRUSH_DEFAULT_LOGICAL)
        self.size_scale.get_style_context().add_class("omarky-size-scale")
        self.size_scale.connect("value-changed", self.on_size_changed)
        set_accessible_name(self.size_scale, "Brush size", "Adjust the width used by the next mark")
        self.size_scale.set_tooltip_text(f"Brush width: {self.brush_width:.1f} px")
        size_group.pack_start(self.size_scale, False, False, 0)
        self.undo_button = self._button("↶", "Undo the latest mark (Ctrl+Z)")
        self.undo_button.connect("clicked", self.on_undo_clicked)
        top.pack_start(self.undo_button, False, False, 0)

        self.crop_button = self._button("Crop", "Drag a rectangle to choose a manual crop")
        self.crop_button.connect("clicked", self.on_crop_clicked)
        set_accessible_name(self.crop_button, "Crop", "Choose an optional manual crop")
        top.pack_start(self.crop_button, False, False, 0)
        self.note_toggle_button = self._button("Note", "Add an optional note")
        self.note_toggle_button.connect("clicked", self.on_note_toggle_clicked)
        top.pack_start(self.note_toggle_button, False, False, 0)
        self.action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self.action_row.set_halign(Gtk.Align.END)
        self.toolbar.pack_start(self.action_row, False, False, 0)
        self.cancel_button = self._button("Cancel", "Discard this annotation · Esc")
        self.cancel_button.connect("clicked", self.on_cancel_clicked)
        self.action_row.pack_start(self.cancel_button, False, False, 0)
        self.copy_button = self._button("Copy", "Copy the marked PNG only; never send")
        self.copy_button.connect("clicked", self.on_copy_clicked)
        self.action_row.pack_start(self.copy_button, False, False, 0)
        self.for_chat_button = self._button(
            "For chat", "Prepare a paste request for the chat you choose; nothing is sent"
        )
        self.for_chat_button.connect("clicked", self.on_for_chat_clicked)
        self.action_row.pack_start(self.for_chat_button, False, False, 0)
        self.send_button = Gtk.Button(label="Send")
        self.send_button.get_style_context().add_class("omarky-primary-button")
        self.send_button.set_tooltip_text("Send the marked image to the selected Codex chat")
        set_accessible_name(self.send_button, "Send", "Explicitly send to the selected Codex chat")
        self.send_button.connect("clicked", self.on_send_clicked)
        self.action_row.pack_start(self.send_button, False, False, 0)

        self.note_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.note_row.set_name("omarky-note-row")
        self.toolbar.pack_start(self.note_row, False, False, 0)
        self.note_entry = Gtk.Entry()
        self.note_entry.set_name("omarky-note")
        self.note_entry.set_placeholder_text("Add a note (optional)")
        self.note_entry.set_hexpand(True)
        self.note_entry.set_width_chars(24)
        self.note_entry.get_style_context().add_class("omarky-entry")
        self.note_entry.set_tooltip_text("Optional note included literally with Send or For chat")
        set_accessible_name(self.note_entry, "Optional note")
        self.note_entry.set_text(note)
        self.note_entry.connect("map", self.on_note_entry_map)
        self.note_row.pack_start(self.note_entry, True, True, 0)
        self._update_controls()

    def _button(self, label: str, tooltip: str) -> Gtk.Button:
        button = Gtk.Button(label=label)
        button.get_style_context().add_class("omarky-button")
        button.set_tooltip_text(tooltip)
        set_accessible_name(button, label, tooltip)
        return button

    def _image_rect(self) -> tuple[float, float, float, float]:
        return fit_image_rect(self.pixbuf.get_width(), self.pixbuf.get_height(),
                              self.area.get_allocated_width(), self.area.get_allocated_height())

    def _inside_image(self, point: tuple[float, float]) -> bool:
        x, y, width, height = self._image_rect()
        return x <= point[0] <= x + width and y <= point[1] <= y + height

    def _clamp_to_image(self, point: tuple[float, float]) -> tuple[float, float]:
        x, y, width, height = self._image_rect()
        return (max(x, min(x + width, point[0])), max(y, min(y + height, point[1])))

    def on_position_clicked(self, _button: Gtk.Button) -> None:
        if self._editing_locked():
            return
        self.toolbar_position = "top" if self.toolbar_position == "bottom" else "bottom"
        at_top = self.toolbar_position == "top"
        self.toolbar.set_valign(Gtk.Align.START if at_top else Gtk.Align.END)
        self.toolbar.set_margin_top(12 if at_top else 0)
        self.toolbar.set_margin_bottom(0 if at_top else 12)
        destination = "bottom" if at_top else "top"
        self.position_button.set_label("↓" if at_top else "↑")
        self.position_button.set_tooltip_text(f"Move controls to the {destination} edge")
        set_accessible_name(self.position_button, f"Move controls to the {destination} edge")

    def on_note_toggle_clicked(self, _button: Gtk.Button) -> None:
        if self._editing_locked():
            return
        self.note_expanded = not self.note_expanded
        if self.note_expanded:
            self._note_focus_pending = True
            self.note_entry.show()
            self.note_row.show()
        else:
            self._note_focus_pending = False
            self.note_row.hide()
        self.note_toggle_button.set_label("Hide note" if self.note_expanded else "Note")

    def on_note_entry_map(self, _entry: Gtk.Entry) -> None:
        if self._note_focus_pending:
            self._note_focus_pending = False
            self.note_entry.grab_focus()

    def _set_status(self, message: str) -> None:
        self.status_label.set_text(message)
        self.status_label.set_tooltip_text(message)

    def _editing_locked(self) -> bool:
        return self.busy or self.terminal_send_state in {"ok", "uncertain"}

    def _update_controls(self) -> None:
        locked = self._editing_locked()
        has_ink = bool(self.strokes)
        self.target_label_widget.set_text(self.target_label or "No chat selected")
        self.target_label_widget.set_tooltip_text(
            self.target_label or "No Codex chat selected · For chat prepares a paste request"
        )
        self.position_button.set_sensitive(not locked)
        self.note_toggle_button.set_sensitive(not locked)
        self.crop_button.set_sensitive(not locked and not self.practice)
        if self.practice:
            self.crop_button.set_label("Crop off")
            self.crop_button.set_tooltip_text("Cropping is disabled for practice cards")
        elif self.crop_mode or self.capture_bounds is not None:
            self.crop_button.set_label("Full image")
            self.crop_button.set_tooltip_text("Restore the entire original image")
        else:
            self.crop_button.set_label("Crop")
            self.crop_button.set_tooltip_text("Drag a rectangle on the image to choose a manual crop")
        self.undo_button.set_sensitive(has_ink and not locked)
        can_publish = has_ink and not self.busy and not self.crop_mode
        self.copy_button.set_sensitive(can_publish)
        self.for_chat_button.set_sensitive(can_publish and not self.practice)
        self.send_button.set_sensitive(can_publish and bool(self.target_label.strip())
                                       and self.terminal_send_state not in {"ok", "uncertain"})
        self.cancel_button.set_sensitive(not self.busy)
        self.note_entry.set_editable(not locked)
        if self.choose_button is not None:
            self.choose_button.set_sensitive(not locked)
        for button in self.color_buttons.values():
            button.set_sensitive(not locked)
        self.size_scale.set_sensitive(not locked)
        self.copy_button.set_label("Copying…" if self.busy and self.action_kind == "copy" else "Copy")
        self.for_chat_button.set_label(
            "Preparing…" if self.busy and self.action_kind == "for_chat" else "For chat"
        )
        if self.busy and self.action_kind == "send":
            self.send_button.set_label("Sending…")
        elif self.terminal_send_state == "ok":
            self.send_button.set_label("Sent")
            self.cancel_button.set_label("Done")
        elif self.terminal_send_state == "uncertain":
            self.send_button.set_label("Not confirmed")
            self.send_button.set_tooltip_text("Check the chat before attempting another send")
        else:
            self.send_button.set_label("Send")
            self.cancel_button.set_label("Cancel")

    def draw_swatch(self, widget: Gtk.Widget, context: cairo.Context,
                    color_name: str, rgba: tuple[float, float, float, float]) -> bool:
        width, height = widget.get_allocated_width(), widget.get_allocated_height()
        if width <= 0 or height <= 0:
            return False
        x, y = width / 2.0, height / 2.0
        selected = color_name == self.selected_color_name
        context.set_source_rgba(0.02, 0.035, 0.05, 0.9)
        context.arc(x, y, min(width, height) * 0.47, 0, math.tau)
        context.fill()
        context.set_source_rgba(*rgba)
        context.arc(x, y, min(width, height) * 0.38, 0, math.tau)
        context.fill()
        context.set_line_width(2.0 if selected else 1.0)
        context.set_source_rgba(0.97, 0.98, 1.0, 0.98 if selected else 0.42)
        context.arc(x, y, min(width, height) * (0.47 if selected else 0.44), 0, math.tau)
        context.stroke()
        if selected:
            context.set_line_width(1.8)
            context.set_line_cap(cairo.LINE_CAP_ROUND)
            context.set_line_join(cairo.LINE_JOIN_ROUND)
            context.set_source_rgba(0.02, 0.035, 0.05, 0.95)
            context.move_to(x - 3, y)
            context.line_to(x - 0.5, y + 2.5)
            context.line_to(x + 4, y - 3)
            context.stroke()
        return False

    def draw_size_preview(self, widget: Gtk.Widget, context: cairo.Context) -> bool:
        width, height = widget.get_allocated_width(), widget.get_allocated_height()
        if width <= 0 or height <= 0:
            return False
        radius = max(1.0, min(self.brush_width / 2.0, min(width, height) / 2.0 - 1))
        context.set_source_rgba(0.96, 0.97, 0.98, 0.95)
        context.arc(width / 2.0, height / 2.0, radius, 0, math.tau)
        context.fill()
        return False

    def _crop_canvas_rect(self) -> tuple[float, float, float, float] | None:
        if self.crop_mode and self.crop_dragging and self.crop_drag_start and self.crop_drag_end:
            left, right = sorted((self.crop_drag_start[0], self.crop_drag_end[0]))
            top, bottom = sorted((self.crop_drag_start[1], self.crop_drag_end[1]))
            return left, top, right - left, bottom - top
        if self.capture_bounds is None:
            return None
        image_x, image_y, image_w, image_h = self._image_rect()
        sx, sy = image_w / self.pixbuf.get_width(), image_h / self.pixbuf.get_height()
        return (image_x + self.capture_bounds["x"] * sx,
                image_y + self.capture_bounds["y"] * sy,
                self.capture_bounds["width"] * sx,
                self.capture_bounds["height"] * sy)

    def _draw_crop_overlay(self, context: cairo.Context,
                           rect: tuple[float, float, float, float]) -> None:
        canvas_width = self.area.get_allocated_width()
        canvas_height = self.area.get_allocated_height()
        x, y, width, height = rect
        x0, y0 = max(0.0, min(float(canvas_width), x)), max(0.0, min(float(canvas_height), y))
        x1 = max(x0, min(float(canvas_width), x + width))
        y1 = max(y0, min(float(canvas_height), y + height))
        if x1 <= x0 or y1 <= y0:
            return
        context.save()
        context.set_source_rgba(0.015, 0.025, 0.04, 0.60)
        context.rectangle(0, 0, canvas_width, y0)
        context.rectangle(0, y1, canvas_width, canvas_height - y1)
        context.rectangle(0, y0, x0, y1 - y0)
        context.rectangle(x1, y0, canvas_width - x1, y1 - y0)
        context.fill()
        context.set_line_width(4.0)
        context.set_source_rgba(0.02, 0.035, 0.05, 0.95)
        context.rectangle(x0 + 1, y0 + 1, max(0.0, x1 - x0 - 2), max(0.0, y1 - y0 - 2))
        context.stroke()
        context.set_line_width(2.0)
        context.set_source_rgba(0.27, 0.94, 0.88, 1.0)
        context.rectangle(x0 + 2, y0 + 2, max(0.0, x1 - x0 - 4), max(0.0, y1 - y0 - 4))
        context.stroke()
        tick = min(12.0, (x1 - x0) / 3, (y1 - y0) / 3)
        context.set_line_width(3.0)
        context.set_source_rgba(0.98, 0.99, 1.0, 0.98)
        for cx, cy, dx, dy in ((x0, y0, 1, 1), (x1, y0, -1, 1),
                               (x0, y1, 1, -1), (x1, y1, -1, -1)):
            context.move_to(cx, cy + dy * tick)
            context.line_to(cx, cy)
            context.line_to(cx + dx * tick, cy)
        context.stroke()
        context.restore()

    def on_crop_clicked(self, _button: Gtk.Button) -> None:
        if self._editing_locked() or self.practice:
            return
        if self.crop_mode or self.capture_bounds is not None:
            self.crop_mode = False
            self.crop_dragging = False
            self.crop_drag_start = None
            self.crop_drag_end = None
            self.capture_bounds = None
            self._set_status("Full image · existing marks kept")
        else:
            self.crop_mode = True
            self.crop_dragging = False
            self.crop_drag_start = None
            self.crop_drag_end = None
            self._set_status("Select a crop · Copy/For chat/Send disabled")
        self.area.grab_focus()
        self.area.queue_draw()
        self._update_controls()

    def on_map(self, _widget: Gtk.Widget, _event: Gdk.Event) -> bool:
        return False

    def on_draw(self, widget: Gtk.Widget, context: cairo.Context) -> bool:
        width, height = widget.get_allocated_width(), widget.get_allocated_height()
        if width <= 0 or height <= 0:
            return False
        context.set_source_rgb(0.015, 0.025, 0.04)
        context.paint()
        image_x, image_y, image_w, image_h = self._image_rect()
        if image_w <= 0 or image_h <= 0:
            return False
        key = (round(image_w), round(image_h))
        scaled = self._scaled_backgrounds.get(key)
        if scaled is None:
            scaled = self.pixbuf.scale_simple(key[0], key[1], GdkPixbuf.InterpType.BILINEAR)
            if scaled is None:
                return False
            self._scaled_backgrounds[key] = scaled
        Gdk.cairo_set_source_pixbuf(context, scaled, image_x, image_y)
        context.paint()
        paint_strokes(context, self.strokes)
        crop_rect = self._crop_canvas_rect()
        if crop_rect is not None:
            self._draw_crop_overlay(context, crop_rect)
        return False

    def on_press(self, _widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        if event.button != 1 or self._editing_locked():
            return False
        self.area.grab_focus()
        point = (event.x, event.y)
        if not self._inside_image(point):
            return False
        if self.crop_mode:
            self.crop_drag_start = point
            self.crop_drag_end = point
            self.crop_dragging = True
            self.area.queue_draw()
            return True
        self.drawing = True
        self.strokes.append({
            "points": [point], "color": tuple(self.selected_color),
            "color_name": self.selected_color_name, "width": float(self.brush_width),
        })
        self.area.queue_draw()
        self._update_controls()
        return True

    def on_motion(self, _widget: Gtk.Widget, event: Gdk.EventMotion) -> bool:
        point = (event.x, event.y)
        if self.crop_mode and self.crop_dragging and not self.busy:
            self.crop_drag_end = self._clamp_to_image(point)
            self.area.queue_draw()
            return True
        if not self.drawing or self.busy:
            return False
        point = self._clamp_to_image(point)
        points = self.strokes[-1]["points"]
        if not points or math.dist(point, points[-1]) >= 0.45:
            points.append(point)
            self.area.queue_draw()
        return True

    def on_release(self, _widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        if (event.button == 1 and self.crop_mode and self.crop_dragging
                and self.crop_drag_start is not None and not self.busy):
            end = self._clamp_to_image((event.x, event.y))
            self.crop_drag_end = end
            image_rect = self._image_rect()
            start_pixels = canvas_to_image_point(self.crop_drag_start, image_rect,
                                                 self.pixbuf.get_width(), self.pixbuf.get_height())
            end_pixels = canvas_to_image_point(end, image_rect,
                                               self.pixbuf.get_width(), self.pixbuf.get_height())
            min_px = 8.0 * ((self.pixbuf.get_width() / image_rect[2]
                             + self.pixbuf.get_height() / image_rect[3]) / 2.0)
            try:
                self.capture_bounds = manual_crop_bounds(
                    start_pixels, end_pixels, self.pixbuf.get_width(), self.pixbuf.get_height(), min_px
                )
            except ValueError:
                self._set_status("Crop too small · previous selection kept")
            else:
                self._set_status(f"Manual crop · {len(self.strokes)} marks")
            self.crop_mode = False
            self.crop_dragging = False
            self.crop_drag_start = None
            self.crop_drag_end = None
            self.area.queue_draw()
            self._update_controls()
            return True
        if event.button != 1 or not self.drawing or self.busy:
            return False
        self.drawing = False
        last = self._clamp_to_image((event.x, event.y))
        points = self.strokes[-1]["points"]
        if not points or math.dist(last, points[-1]) > 0.01:
            points.append(last)
        self.area.queue_draw()
        mode = "manual crop" if self.capture_bounds is not None else "full image"
        self._set_status(f"{len(self.strokes)} mark{'s' if len(self.strokes) != 1 else ''} · {mode}")
        self._update_controls()
        return True

    def on_color_clicked(self, _button: Gtk.Button, color_name: str) -> None:
        if self._editing_locked():
            return
        self.selected_color_name = color_name
        self.selected_color = INK_PALETTE[color_name]
        for button in self.color_buttons.values():
            child = button.get_child()
            if child is not None:
                child.queue_draw()

    def on_size_changed(self, scale: Gtk.Scale) -> None:
        self.brush_width = float(scale.get_value())
        self.size_scale.set_tooltip_text(f"Brush width: {self.brush_width:.1f} px")
        self.size_preview.set_tooltip_text(f"Brush preview: {self.brush_width:.1f} px")
        self.size_preview.queue_draw()

    def undo_last_stroke(self) -> None:
        if self._editing_locked() or not self.strokes:
            return
        self.drawing = False
        self.strokes.pop()
        self.area.queue_draw()
        mode = "manual crop" if self.capture_bounds is not None else "full image"
        self._set_status(f"{len(self.strokes)} marks · {mode}" if self.strokes
                         else "Ready to mark")
        self._update_controls()

    def on_undo_clicked(self, _button: Gtk.Button) -> None:
        self.undo_last_stroke()

    def on_choose_clicked(self, _button: Gtk.Button) -> None:
        if self._editing_locked() or self.on_choose_callback is None:
            return
        try:
            choice = self.on_choose_callback()
            self.target_label = selected_target(self.target_label, choice)
        except Exception as exc:
            self._set_status(f"Chat unchanged · {type(exc).__name__}")
        self._update_controls()

    def on_copy_clicked(self, _button: Gtk.Button) -> None:
        self._start_action("copy")

    def on_for_chat_clicked(self, _button: Gtk.Button) -> None:
        self._start_action("for_chat")

    def on_send_clicked(self, _button: Gtk.Button) -> None:
        self._start_action("send")

    def on_cancel_clicked(self, _button: Gtk.Button) -> None:
        if self.busy:
            self._set_status("Working · wait for this action to finish")
            return
        self.window.destroy()

    def _start_action(self, kind: str) -> None:
        if kind == "for_chat" and self.practice:
            self._set_status("For chat is available on routine marking only")
            return
        if not action_allowed(kind, target_label=self.target_label,
                              has_ink=bool(self.strokes), crop_mode=self.crop_mode,
                              busy=self.busy, terminal_state=self.terminal_send_state):
            if self.busy or (kind == "send" and self.terminal_send_state in {"ok", "uncertain"}):
                return
            if self.crop_mode:
                self._set_status("Select a crop or choose Full image first")
                return
            if not self.strokes:
                self._set_status("Add a mark first")
                return
            if kind == "send" and not self.target_label.strip():
                self._set_status("Choose a chat first · For chat works without pairing")
                return
            return
        image_rect = self._image_rect()
        image_strokes = canvas_strokes_to_image(
            self.strokes, image_rect, self.pixbuf.get_width(), self.pixbuf.get_height()
        )
        bounds = validate_capture_bounds(self.capture_bounds,
                                         self.pixbuf.get_width(), self.pixbuf.get_height())
        marked_png = render_marked_png(self.screenshot, image_strokes, bounds)
        meta = build_metadata(self.pixbuf.get_width(), self.pixbuf.get_height(), image_strokes)
        self.last_marked_png = marked_png
        note = self.note_entry.get_text() if kind in {"send", "for_chat"} else ""
        target_label = self.target_label
        self.busy = True
        self.action_kind = kind
        mode = "manual crop" if self.capture_bounds is not None else "full image"
        if kind == "copy":
            self._set_status(f"Copying marked {mode}…")
        elif kind == "for_chat":
            self._set_status(f"Preparing For chat · {mode}…")
        else:
            self._set_status(f"Sending marked {mode}…")
        self._update_controls()

        def work() -> None:
            try:
                if kind == "copy":
                    result = invoke_copy(self.clipboard_writer, marked_png)
                elif kind == "for_chat":
                    result = invoke_for_chat(marked_png, note, clipboard_writer=self.handoff_writer)
                else:
                    result = invoke_send(self.on_send_callback, marked_png, note, meta)
            except Exception as exc:
                result = {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
            if not self.closed:
                GLib.idle_add(self._action_complete, kind, result, target_label)

        threading.Thread(target=work, name=f"omarky-{kind}", daemon=True).start()

    def _action_complete(self, kind: str, result: dict[str, str], target_label: str) -> bool:
        if getattr(self, "closed", False):
            return GLib.SOURCE_REMOVE
        self.busy = False
        self.action_kind = None
        status, message = result["status"], result["message"]
        if kind == "copy" and status == "ok":
            self._set_status("Copied marked image · not sent")
        elif kind == "copy":
            self._set_status(f"Copy failed · image and marks kept · {message or status}")
        elif kind == "for_chat" and status == "ok":
            self._set_status(message)
        elif kind == "for_chat":
            self._set_status(f"For chat failed · {message or status}")
        elif status == "ok":
            self.terminal_send_state = "ok"
            destination = target_label or "selected chat"
            self._set_status(f"Sent to {destination}" + (f" · {message}" if message else ""))
        elif status == "uncertain":
            self.terminal_send_state = "uncertain"
            self._set_status("No confirmation · check the chat before retrying"
                             + (f" · {message}" if message else ""))
        else:
            self._set_status(f"Send {status} · marks and note kept · Copy and For chat remain available"
                             + (f" · {message}" if message else ""))
        self._update_controls()
        return GLib.SOURCE_REMOVE

    def on_key(self, _widget: Gtk.Widget, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            if self.busy:
                self._set_status("Working · this action cannot be cancelled safely")
                return True
            self.window.destroy()
            return True
        focus = self.window.get_focus()
        if isinstance(focus, Gtk.Entry):
            return False
        if (event.state & Gdk.ModifierType.CONTROL_MASK
                and event.keyval in (Gdk.KEY_z, Gdk.KEY_Z)):
            self.undo_last_stroke()
            return True
        return False

    def on_delete(self, _widget: Gtk.Widget, _event: Gdk.Event) -> bool:
        if self.busy:
            self._set_status("Working · wait for this action to finish")
            return True
        return False

    def on_destroy(self, _widget: Gtk.Widget) -> None:
        self.closed = True
        if self.loop.is_running():
            self.loop.quit()


def annotate(*, sample_png: bytes | None, note: str, target_label: str,
             on_send: Callable[[bytes, str, dict[str, Any]], dict[str, Any]],
             on_choose: Callable[[], str | None] | None = None) -> None:
    """Open one modal annotation loop; routine capture freezes only focused output."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("annotate must be called on the GTK main thread")
    if not isinstance(note, str) or not isinstance(target_label, str):
        raise TypeError("note and target_label must be strings")
    if not callable(on_send):
        raise TypeError("on_send must be callable")
    if on_choose is not None and not callable(on_choose):
        raise TypeError("on_choose must be callable or None")
    screenshot, output = prepare_source(sample_png)
    initialized, _argv = Gtk.init_check(None)
    if not initialized:
        raise RuntimeError("GTK could not connect to a display")
    display = Gdk.Display.get_default()
    if display is None:
        raise RuntimeError("no graphical display is available for the annotation canvas")
    if output is not None:
        monitor = resolve_output_monitor(output, display)
    else:
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        if monitor is None:
            raise RuntimeError("no monitor is available for the practice canvas")
    loop = GLib.MainLoop.new(None, False)
    overlay = AnnotateOverlay.__new__(AnnotateOverlay)
    try:
        overlay.__init__(screenshot, target_label, on_send, on_choose, monitor, loop,
                         practice=sample_png is not None, note=note)
        loop.run()
    finally:
        window = getattr(overlay, "window", None)
        if window is not None:
            window.destroy()
        if loop.is_running():
            loop.quit()
    return None


def self_test() -> None:
    rect = fit_image_rect(400, 200, 1000, 600)
    assert rect == (0.0, 50.0, 1000.0, 500.0)
    point = canvas_to_image_point((500.0, 300.0), rect, 400, 200)
    assert point == (200.0, 100.0)
    bounds = manual_crop_bounds((20, 10), (180, 90), 200, 100)
    assert bounds == {"x": 20, "y": 10, "width": 160, "height": 80}
    meta = build_metadata(200, 100, [{"points": [(50, 25)], "width": 5,
                                     "color": INK_PALETTE["Coral"]}])
    assert meta == {"width": 200, "height": 100, "strokes": [[[50.0, 25.0]]]}
    print("self-test: PASS (letterbox mapping, crop bounds, original-frame metadata)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OMarky in-memory marking canvas")
    parser.add_argument("--self-test", action="store_true",
                        help="check image geometry without opening a screen capture")
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    parser.error("canvas is launched by OMarky through annotate(); --self-test is standalone")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
