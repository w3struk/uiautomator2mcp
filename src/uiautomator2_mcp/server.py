"""MCP server for Android device automation via uiautomator2."""

from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import re
import time
from typing import Any, cast
from xml.etree import ElementTree as ET

from mcp import types

try:  # mcp 2.x (mcp.server.fastmcp was removed)
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x fallback
    from mcp.server.fastmcp import FastMCP

from uiautomator2_mcp.adb_tools import (
    list_avds as list_available_avds,
    start_emulator as start_android_emulator,
)
from uiautomator2_mcp.device_manager import device_manager
from uiautomator2_mcp.logcat import (
    LogQuery,
    clear_logs as clear_device_logs,
    get_logs as get_device_logs,
)

mcp = FastMCP(
    "uiautomator2",
    instructions="Android device automation via uiautomator2. Use list_devices() to choose a target, connect() one or more devices, then pass device_id when multiple devices are connected.",
)

_KEYGUARD_HINTS = (
    "keyguard_root_view",
    "keyguard_panel_view",
    "com.android.systemui:id/keyguard",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_selector(
    text: str | None = None,
    resource_id: str | None = None,
    class_name: str | None = None,
    description: str | None = None,
) -> dict[str, str]:
    """Build a uiautomator2 selector dict from optional parameters."""
    selector: dict[str, str] = {}
    if text is not None:
        selector["text"] = text
    if resource_id is not None:
        selector["resourceId"] = resource_id
    if class_name is not None:
        selector["className"] = class_name
    if description is not None:
        selector["description"] = description
    return selector


def _resolve_locator(
    text: str | None = None,
    resource_id: str | None = None,
    class_name: str | None = None,
    description: str | None = None,
    xpath: str | None = None,
    *,
    error_message: str = "Error: provide at least one selector.",
) -> tuple[str, str | dict[str, str]]:
    """Resolve a query into either xpath or selector mode."""
    if xpath:
        return "xpath", xpath
    selector = _build_selector(text, resource_id, class_name, description)
    if not selector:
        raise ValueError(error_message)
    return "selector", selector


def _locate_element(
    d: Any,
    *,
    text: str | None = None,
    resource_id: str | None = None,
    class_name: str | None = None,
    description: str | None = None,
    xpath: str | None = None,
    error_message: str = "Error: provide at least one selector.",
) -> tuple[str, str | dict[str, str], Any]:
    """Locate an element using selector fields or XPath."""
    mode, query = _resolve_locator(
        text=text,
        resource_id=resource_id,
        class_name=class_name,
        description=description,
        xpath=xpath,
        error_message=error_message,
    )
    element = d.xpath(query) if mode == "xpath" else d(**query)
    return mode, query, element


def _locator_label(mode: str, query: str | dict[str, str]) -> str:
    """Render a human-readable locator description."""
    return f"xpath: {query}" if mode == "xpath" else f"selector: {query}"


def _element_exists(element: Any) -> bool:
    """Return whether a resolved element exists."""
    return bool(element.exists)


def _element_info(element: Any, mode: str) -> dict[str, Any]:
    """Fetch element info for selector or XPath elements."""
    if mode == "xpath":
        node = element.get()
        if node is None or not hasattr(node, "info"):
            raise ValueError("Cannot resolve element info.")
        return node.info
    return element.info


def _wait_on_element(element: Any, mode: str, *, timeout: float, gone: bool = False) -> bool:
    """Wait for an element to appear or disappear."""
    if mode == "xpath":
        return element.wait_gone(timeout=timeout) if gone else element.wait(timeout=timeout)
    return element.wait_gone(timeout=timeout) if gone else element.wait(timeout=timeout)


def _has_keyguard_overlay(d: Any) -> bool:
    """Best-effort check whether lockscreen/keyguard nodes still exist in hierarchy."""
    try:
        xml = d.dump_hierarchy()
    except Exception:
        return False
    return any(marker in xml for marker in _KEYGUARD_HINTS)


def _tap_resolved_element(d: Any, element: Any, mode: str, *, double: bool = False) -> None:
    """Tap or double-tap the center of a resolved element."""
    info = _element_info(element, mode)
    x, y = _center_from_info(info, action="double tap" if double else "tap")
    if double:
        d.double_click(x, y)
    else:
        d.click(x, y)


def _sleep_seconds(seconds: float) -> None:
    """Sleep for a non-negative number of seconds."""
    time.sleep(max(0.0, seconds))


def _format_element_info(info: dict[str, Any]) -> str:
    """Format element info as readable JSON."""
    return json.dumps(info, indent=2, ensure_ascii=False)


def _normalize_image_format(image_format: str | None, *, save_path: str | None = None) -> str:
    """Normalize requested image format to a Pillow-friendly value."""
    format_hint = image_format
    if format_hint is None and save_path:
        suffix = os.path.splitext(save_path)[1].lower()
        format_hint = {
            ".png": "png",
            ".jpg": "jpeg",
            ".jpeg": "jpeg",
        }.get(suffix)
    normalized = (format_hint or "png").strip().lower()
    if normalized == "jpg":
        normalized = "jpeg"
    if normalized not in {"png", "jpeg"}:
        raise ValueError("image_format must be one of: png, jpeg, jpg")
    return normalized


def _resize_image(img: Any, *, max_width: int | None = None, max_height: int | None = None) -> Any:
    """Resize an image to fit within the provided bounds while preserving aspect ratio."""
    if max_width is not None and max_width <= 0:
        raise ValueError("max_width must be positive")
    if max_height is not None and max_height <= 0:
        raise ValueError("max_height must be positive")
    if max_width is None and max_height is None:
        return img

    width, height = img.size
    scale_candidates = [1.0]
    if max_width is not None:
        scale_candidates.append(max_width / width)
    if max_height is not None:
        scale_candidates.append(max_height / height)
    scale = min(scale_candidates)
    if scale >= 1.0:
        return img

    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    resample = getattr(getattr(img, "Resampling", None), "LANCZOS", None)
    if resample is None:
        resample = getattr(img, "LANCZOS", 1)
    return img.resize(new_size, resample)


def _encode_image_bytes(img: Any, *, image_format: str, quality: int = 85) -> bytes:
    """Encode a PIL image into PNG or JPEG bytes."""
    if not 1 <= quality <= 100:
        raise ValueError("quality must be between 1 and 100")

    buffer = io.BytesIO()
    save_kwargs: dict[str, Any] = {"format": image_format.upper()}
    if image_format == "jpeg":
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        save_kwargs.update({"quality": quality, "optimize": True})
    img.save(buffer, **save_kwargs)
    return buffer.getvalue()


def _mime_type_for_format(image_format: str) -> str:
    """Map normalized image format to MIME type."""
    return "image/jpeg" if image_format == "jpeg" else "image/png"


def _parse_bounds(bounds: str) -> dict[str, int] | None:
    """Parse Android bounds string like [0,0][100,200]."""
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds.strip())
    if not match:
        return None
    left, top, right, bottom = (int(value) for value in match.groups())
    return {"left": left, "top": top, "right": right, "bottom": bottom}


def _parse_bounds_tuple(raw_bounds: Any, *, error_message: str) -> tuple[int, int, int, int]:
    """Parse bounds from dict or Android bounds string into a fixed 4-tuple."""
    if isinstance(raw_bounds, dict):
        keys = ("left", "top", "right", "bottom")
        if all(isinstance(raw_bounds.get(key), int) for key in keys):
            return cast(
                tuple[int, int, int, int],
                tuple(raw_bounds[key] for key in keys),
            )
        raise ValueError(error_message)

    if isinstance(raw_bounds, str):
        parsed = _parse_bounds(raw_bounds)
        if parsed is None:
            raise ValueError(error_message)
        return parsed["left"], parsed["top"], parsed["right"], parsed["bottom"]

    raise ValueError(error_message)


def _ui_tree_elements(xml_str: str) -> list[dict[str, Any]]:
    """Parse UI hierarchy XML into a structured list of elements."""
    root = ET.fromstring(xml_str)
    elements: list[dict[str, Any]] = []
    for order, node in enumerate(root.iter("node")):
        raw_index = node.get("index")
        elements.append({
            "text": node.get("text", ""),
            "resource_id": node.get("resource-id", ""),
            "class_name": node.get("class", ""),
            "content_desc": node.get("content-desc", ""),
            "bounds": _parse_bounds(node.get("bounds", "")),
            "clickable": node.get("clickable", "false") == "true",
            "enabled": node.get("enabled", "false") == "true",
            "focused": node.get("focused", "false") == "true",
            "selected": node.get("selected", "false") == "true",
            "checked": node.get("checked", "false") == "true",
            "scrollable": node.get("scrollable", "false") == "true",
            "index": int(raw_index) if raw_index and raw_index.isdigit() else order,
        })
    return elements


def _is_xpath_clear_failure(error: Exception) -> bool:
    """Return True when XPath set_text failed in known ADB clear-text paths."""
    message = str(error)
    return any(
        marker in message
        for marker in ("ADB_KEYBOARD_CLEAR_TEXT", "ExtractedText", "AdbBroadcastError")
    )


def _xpath_element_info(elem: Any) -> dict[str, Any]:
    """Best-effort fetch of XPath element info."""
    node = elem.get()
    return node.info if node is not None and hasattr(node, "info") else {}


def _xpath_resource_id(elem: Any) -> str | None:
    """Extract a usable resource-id from an XPath element, if present."""
    info = _xpath_element_info(elem)
    for key in ("resourceName", "resourceId", "resource-id"):
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _xpath_text_is_empty(elem: Any) -> bool:
    """Check whether the XPath element has an empty text value."""
    info = _xpath_element_info(elem)
    text_value = info.get("text")
    return isinstance(text_value, str) and text_value == ""


def _xpath_selector_from_info(elem: Any) -> dict[str, str]:
    """Build a best-effort selector from XPath element info."""
    info = _xpath_element_info(elem)
    selector: dict[str, str] = {}

    resource_id = info.get("resourceName") or info.get("resourceId") or info.get("resource-id")
    if isinstance(resource_id, str) and resource_id.strip():
        selector["resourceId"] = resource_id.strip()

    class_name = info.get("className") or info.get("class")
    if isinstance(class_name, str) and class_name.strip():
        selector["className"] = class_name.strip()

    description = info.get("contentDescription") or info.get("description")
    if isinstance(description, str) and description.strip():
        selector["description"] = description.strip()

    text_value = info.get("text")
    if isinstance(text_value, str):
        selector["text"] = text_value

    return selector


def _selector_match_count(d: Any, selector: dict[str, str]) -> int | None:
    """Best-effort count of matches for a selector.

    Returns:
        Number of matches if available, otherwise None when the backend cannot
        provide a reliable count.
    """
    try:
        return int(d(**selector).count)
    except Exception:
        return None


def _center_from_info(info: dict[str, Any], *, action: str = "element action") -> tuple[int, int]:
    """Extract element center coordinates from uiautomator2 info bounds.

    Supports common uiautomator2 formats:
    - visibleBounds or bounds as dict: {"left": 0, "top": 100, "right": 1080, "bottom": 240}
    - visibleBounds or bounds as string: "[0,100][1080,240]"

    Prefers visibleBounds when available, then falls back to bounds.
    """
    error_message = f"Cannot resolve element bounds for {action}"

    for raw_bounds in (info.get("visibleBounds"), info.get("bounds")):
        if raw_bounds is None:
            continue
        try:
            left, top, right, bottom = _parse_bounds_tuple(
                raw_bounds,
                error_message=error_message,
            )
        except ValueError:
            continue
        if right < left or bottom < top:
            continue
        return (left + right) // 2, (top + bottom) // 2

    raise ValueError(error_message)


def _get_device(device_id: str | None = None) -> Any:
    """Resolve a connected device, requiring device_id when ambiguous."""
    return device_manager.get_device(device_id)


def _resolve_adb_serial(device_id: str | None = None) -> str:
    """Resolve the target ADB serial for diagnostics tools."""
    if device_id is not None and device_id.strip():
        return device_id.strip()
    return device_manager.get_serial(device_id)


# ---------------------------------------------------------------------------
# Connection tools
# ---------------------------------------------------------------------------

@mcp.tool()
def connect(serial: str | None = None) -> str:
    """Connect to an Android device.

    Args:
        serial: Device serial number or IP address (e.g. "emulator-5554" or "192.168.1.100").
                If not provided, connects to the only available ready adb device.
    """
    try:
        resolved_serial, info = device_manager.connect(serial)
        return (
            f"Connected to {resolved_serial}.\n\n"
            f"Connected devices in session: {device_manager.connected_device_ids()}\n\n"
            f"Device info:\n{_format_element_info(info)}"
        )
    except Exception as e:
        return f"Failed to connect: {e}"


@mcp.tool()
def disconnect(device_id: str | None = None) -> str:
    """Disconnect from one connected Android device.

    Args:
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        serial = device_manager.disconnect(device_id)
        remaining = device_manager.connected_device_ids()
        return (
            f"Disconnected {serial}. Remaining connected devices: {remaining or '(none)'}"
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def list_devices() -> str:
    """List devices currently visible to adb."""
    try:
        devices = device_manager.list_devices()
        session_devices = set(device_manager.connected_device_ids())
        enriched = []
        for device in devices:
            enriched.append(
                {
                    **device,
                    "connected_in_mcp_session": device.get("serial") in session_devices,
                }
            )
        return json.dumps(enriched, indent=2, ensure_ascii=False)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def list_avds() -> str:
    """List configured Android Virtual Devices available to the emulator."""
    try:
        return json.dumps(list_available_avds(), indent=2, ensure_ascii=False)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def start_emulator(
    avd_name: str,
    no_window: bool = False,
    wipe_data: bool = False,
) -> str:
    """Start an Android emulator in the background.

    Args:
        avd_name: Name of the configured Android Virtual Device.
        no_window: If True, starts the emulator without a visible window.
        wipe_data: If True, starts the emulator with a wiped data partition.
    """
    try:
        result = start_android_emulator(
            avd_name,
            no_window=no_window,
            wipe_data=wipe_data,
        )
        return json.dumps(result, indent=2, ensure_ascii=False)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def device_info(device_id: str | None = None) -> str:
    """Get detailed information about a connected device (model, screen size, Android version, etc.).

    Args:
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        return json.dumps(
            device_manager.get_device_details(device_id),
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# UI interaction tools
# ---------------------------------------------------------------------------

@mcp.tool()
def tap(x: int, y: int, device_id: str | None = None) -> str:
    """Tap at the given screen coordinates.

    Args:
        x: Horizontal coordinate (pixels from left).
        y: Vertical coordinate (pixels from top).
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        d.click(x, y)
        return f"Tapped at ({x}, {y})."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def double_tap(x: int, y: int, device_id: str | None = None) -> str:
    """Double-tap at the given screen coordinates.

    Args:
        x: Horizontal coordinate.
        y: Vertical coordinate.
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        d.double_click(x, y)
        return f"Double-tapped at ({x}, {y})."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def long_tap(
    x: int,
    y: int,
    duration: float = 1.0,
    device_id: str | None = None,
) -> str:
    """Long-press at the given screen coordinates.

    Args:
        x: Horizontal coordinate.
        y: Vertical coordinate.
        duration: Hold duration in seconds (default 1.0).
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        d.long_click(x, y, duration=duration)
        return f"Long-tapped at ({x}, {y}) for {duration}s."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def swipe(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    duration: float = 0.5,
    device_id: str | None = None,
) -> str:
    """Swipe from one point to another.

    Args:
        start_x: Start horizontal coordinate.
        start_y: Start vertical coordinate.
        end_x: End horizontal coordinate.
        end_y: End vertical coordinate.
        duration: Swipe duration in seconds (default 0.5).
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        d.swipe(start_x, start_y, end_x, end_y, duration=duration)
        return f"Swiped from ({start_x}, {start_y}) to ({end_x}, {end_y})."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def drag(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    duration: float = 0.5,
    device_id: str | None = None,
) -> str:
    """Drag from one point to another.

    Args:
        start_x: Start horizontal coordinate.
        start_y: Start vertical coordinate.
        end_x: End horizontal coordinate.
        end_y: End vertical coordinate.
        duration: Drag duration in seconds (default 0.5).
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        d.drag(start_x, start_y, end_x, end_y, duration=duration)
        return f"Dragged from ({start_x}, {start_y}) to ({end_x}, {end_y})."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def input_text(text: str, device_id: str | None = None) -> str:
    """Type text using the keyboard. The focused input field will receive the text.

    Args:
        text: The text to type.
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        d.send_keys(text)
        return f"Typed: {text!r}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def press_key(key: str, device_id: str | None = None) -> str:
    """Press a device key.

    Args:
        key: Key name — one of: home, back, enter, menu, recent,
             volume_up, volume_down, power, delete, tab, space.
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        d.press(key)
        return f"Pressed key: {key}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def multi_tap(
    x: int,
    y: int,
    count: int,
    interval_ms: int = 120,
    device_id: str | None = None,
) -> str:
    """Tap the same coordinates multiple times.

    Args:
        x: Horizontal coordinate.
        y: Vertical coordinate.
        count: Number of taps to perform. Must be at least 1.
        interval_ms: Delay between taps in milliseconds (default 120).
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        if count < 1:
            return "Error: count must be at least 1."
        if interval_ms < 0:
            return "Error: interval_ms must be non-negative."
        d = _get_device(device_id)
        for index in range(count):
            d.click(x, y)
            if index < count - 1:
                _sleep_seconds(interval_ms / 1000)
        return f"Tapped ({x}, {y}) {count} times with {interval_ms}ms interval."
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Element tools
# ---------------------------------------------------------------------------

@mcp.tool()
def find_element(
    text: str | None = None,
    resource_id: str | None = None,
    class_name: str | None = None,
    description: str | None = None,
    xpath: str | None = None,
    device_id: str | None = None,
) -> str:
    """Find a UI element and return its properties (bounds, text, class, etc.).

    Provide at least one selector. If xpath is given, it takes precedence.

    Args:
        text: Exact text of the element.
        resource_id: Resource ID (e.g. "com.example:id/button").
        class_name: Class name (e.g. "android.widget.Button").
        description: Content description.
        xpath: XPath expression (e.g. "//android.widget.TextView[@text='Hello']").
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        mode, query, elem = _locate_element(
            d,
            text=text,
            resource_id=resource_id,
            class_name=class_name,
            description=description,
            xpath=xpath,
            error_message=(
                "Error: provide at least one selector (text, resource_id, class_name, "
                "description, or xpath)."
            ),
        )
        if _element_exists(elem):
            return _format_element_info(_element_info(elem, mode))
        return f"Element not found with {_locator_label(mode, query)}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def tap_element(
    text: str | None = None,
    resource_id: str | None = None,
    class_name: str | None = None,
    description: str | None = None,
    xpath: str | None = None,
    device_id: str | None = None,
) -> str:
    """Find a UI element and tap on it.

    Provide at least one selector. If xpath is given, it takes precedence.

    Args:
        text: Exact text of the element.
        resource_id: Resource ID (e.g. "com.example:id/button").
        class_name: Class name (e.g. "android.widget.Button").
        description: Content description.
        xpath: XPath expression.
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        mode, query, elem = _locate_element(
            d,
            text=text,
            resource_id=resource_id,
            class_name=class_name,
            description=description,
            xpath=xpath,
        )
        if _element_exists(elem):
            _tap_resolved_element(d, elem, mode)
            return f"Tapped element ({_locator_label(mode, query)})."
        return f"Element not found with {_locator_label(mode, query)}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def double_tap_element(
    text: str | None = None,
    resource_id: str | None = None,
    class_name: str | None = None,
    description: str | None = None,
    xpath: str | None = None,
    device_id: str | None = None,
) -> str:
    """Find a UI element and double-tap its center.

    Provide at least one selector. If xpath is given, it takes precedence.

    Args:
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        mode, query, elem = _locate_element(
            d,
            text=text,
            resource_id=resource_id,
            class_name=class_name,
            description=description,
            xpath=xpath,
        )
        if _element_exists(elem):
            _tap_resolved_element(d, elem, mode, double=True)
            return f"Double-tapped element ({_locator_label(mode, query)})."
        return f"Element not found with {_locator_label(mode, query)}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def set_element_text(
    value: str,
    text: str | None = None,
    resource_id: str | None = None,
    class_name: str | None = None,
    description: str | None = None,
    xpath: str | None = None,
    device_id: str | None = None,
) -> str:
    """Set text in an input field identified by a selector.

    Args:
        value: The text to set.
        text: Current text of the element.
        resource_id: Resource ID of the element.
        class_name: Class name of the element.
        description: Content description of the element.
        xpath: XPath expression.
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        if xpath:
            elem = d.xpath(xpath)
            if elem.exists:
                try:
                    elem.set_text(value)
                    return f"Set text to {value!r} (xpath: {xpath})."
                except Exception as xpath_error:
                    if not _is_xpath_clear_failure(xpath_error):
                        raise

                    fallback_selector = _xpath_selector_from_info(elem)
                    if fallback_selector:
                        selector_count = _selector_match_count(d, fallback_selector)
                        if selector_count is None:
                            return (
                                "Error: XPath clear-text path failed and selector fallback could not "
                                "verify selector uniqueness."
                            )
                        if selector_count != 1:
                            return (
                                "Error: XPath clear-text path failed and selector fallback matched "
                                f"{selector_count} elements, so writing was skipped to avoid editing "
                                "the wrong field."
                            )

                        try:
                            d(**fallback_selector).set_text(value)
                            return (
                                f"Set text to {value!r} (xpath: {xpath}). "
                                "Used selector fallback derived from XPath node info after "
                                "uniqueness check."
                            )
                        except Exception as selector_error:
                            return (
                                "Error: XPath clear-text path failed and unique selector fallback "
                                f"derived from XPath node info also failed: {selector_error}"
                            )

                    fallback_resource_id = _xpath_resource_id(elem)
                    if fallback_resource_id:
                        return (
                            "Error: XPath clear-text path failed and selector fallback could not find "
                            f"resource-id {fallback_resource_id!r}."
                        )

                    if _xpath_text_is_empty(elem):
                        elem.click()
                        d.send_keys(value)
                        return (
                            f"Set text to {value!r} (xpath: {xpath}). "
                            "Used click+send_keys fallback for empty field."
                        )

                    return (
                        "Error: XPath field text replacement failed after all fallbacks (direct XPath, "
                        "selector from node info, empty-field send_keys). Provide selector/resource-id or "
                        "custom replace strategy."
                    )
            return f"Element not found with xpath: {xpath}"
        selector = _build_selector(text, resource_id, class_name, description)
        if not selector:
            return "Error: provide at least one selector."
        elem = d(**selector)
        if elem.exists:
            elem.set_text(value)
            return f"Set text to {value!r} on element: {selector}"
        return f"Element not found with selector: {selector}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def element_exists(
    text: str | None = None,
    resource_id: str | None = None,
    class_name: str | None = None,
    description: str | None = None,
    xpath: str | None = None,
    device_id: str | None = None,
) -> str:
    """Check whether a UI element exists on screen.

    Args:
        text: Exact text of the element.
        resource_id: Resource ID.
        class_name: Class name.
        description: Content description.
        xpath: XPath expression.
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        mode, query, elem = _locate_element(
            d,
            text=text,
            resource_id=resource_id,
            class_name=class_name,
            description=description,
            xpath=xpath,
        )
        return json.dumps({"exists": _element_exists(elem), mode: query})
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def wait_element(
    text: str | None = None,
    resource_id: str | None = None,
    class_name: str | None = None,
    description: str | None = None,
    xpath: str | None = None,
    timeout: float = 10.0,
    device_id: str | None = None,
) -> str:
    """Wait for a UI element to appear on screen.

    Args:
        text: Exact text of the element.
        resource_id: Resource ID.
        class_name: Class name.
        description: Content description.
        xpath: XPath expression.
        timeout: Maximum wait time in seconds (default 10).
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        mode, query, elem = _locate_element(
            d,
            text=text,
            resource_id=resource_id,
            class_name=class_name,
            description=description,
            xpath=xpath,
        )
        found = _wait_on_element(elem, mode, timeout=timeout)
        if found:
            return f"Element appeared ({_locator_label(mode, query)})."
        return f"Timeout ({timeout}s): element not found ({_locator_label(mode, query)})."
    except Exception as e:
        return f"Error: {e}"


def _has_locator_fields(step: dict[str, Any]) -> bool:
    """Return whether a step contains selector fields or XPath."""
    return any(
        step.get(key) is not None
        for key in ("text", "resource_id", "class_name", "description", "xpath")
    )


def _validate_tap_sequence_steps(steps: list[dict[str, Any]]) -> None:
    """Validate a tap sequence before execution starts."""
    if not steps:
        raise ValueError("steps must contain at least one action.")

    allowed_actions = {"tap", "tap_element", "wait", "input_text", "press_key", "swipe"}
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"Step {index} must be an object.")
        action = step.get("action")
        if not isinstance(action, str) or action not in allowed_actions:
            raise ValueError(
                f"Step {index} has invalid action {action!r}. "
                f"Allowed actions: {', '.join(sorted(allowed_actions))}"
            )
        if action == "tap":
            if "x" not in step or "y" not in step:
                raise ValueError(f"Step {index} (tap) requires x and y.")
        elif action == "tap_element":
            if not _has_locator_fields(step):
                raise ValueError(f"Step {index} (tap_element) requires selector fields or xpath.")
        elif action == "wait":
            has_seconds = "seconds" in step
            has_locator = _has_locator_fields(step)
            if has_seconds == has_locator:
                raise ValueError(
                    f"Step {index} (wait) requires exactly one of seconds or locator fields."
                )
        elif action == "input_text":
            if "text" not in step:
                raise ValueError(f"Step {index} (input_text) requires text.")
        elif action == "press_key":
            if "key" not in step:
                raise ValueError(f"Step {index} (press_key) requires key.")
        elif action == "swipe":
            required = {"start_x", "start_y", "end_x", "end_y"}
            missing = sorted(required - step.keys())
            if missing:
                raise ValueError(
                    f"Step {index} (swipe) is missing required fields: {', '.join(missing)}."
                )


def _execute_tap_sequence_step(d: Any, step: dict[str, Any]) -> str:
    """Execute one validated tap-sequence step and return a short summary."""
    action = step["action"]
    if action == "tap":
        d.click(step["x"], step["y"])
        return f"tap({step['x']}, {step['y']})"
    if action == "tap_element":
        mode, query, elem = _locate_element(
            d,
            text=step.get("text"),
            resource_id=step.get("resource_id"),
            class_name=step.get("class_name"),
            description=step.get("description"),
            xpath=step.get("xpath"),
        )
        if not _element_exists(elem):
            raise RuntimeError(f"Element not found with {_locator_label(mode, query)}")
        _tap_resolved_element(d, elem, mode)
        return f"tap_element({_locator_label(mode, query)})"
    if action == "wait":
        if "seconds" in step:
            seconds = float(step["seconds"])
            if seconds < 0:
                raise RuntimeError("wait seconds must be non-negative")
            _sleep_seconds(seconds)
            return f"wait({seconds}s)"
        mode, query, elem = _locate_element(
            d,
            text=step.get("text"),
            resource_id=step.get("resource_id"),
            class_name=step.get("class_name"),
            description=step.get("description"),
            xpath=step.get("xpath"),
        )
        timeout = float(step.get("timeout", 10.0))
        gone = bool(step.get("gone", False))
        matched = _wait_on_element(elem, mode, timeout=timeout, gone=gone)
        if not matched:
            verb = "disappear" if gone else "appear"
            raise RuntimeError(
                f"Timeout ({timeout}s): element did not {verb} ({_locator_label(mode, query)})"
            )
        return (
            f"wait_gone({_locator_label(mode, query)})"
            if gone
            else f"wait_element({_locator_label(mode, query)})"
        )
    if action == "input_text":
        d.send_keys(step["text"])
        return f"input_text({step['text']!r})"
    if action == "press_key":
        d.press(step["key"])
        return f"press_key({step['key']})"
    if action == "swipe":
        d.swipe(
            step["start_x"],
            step["start_y"],
            step["end_x"],
            step["end_y"],
            duration=float(step.get("duration", 0.5)),
        )
        return (
            "swipe("
            f"{step['start_x']}, {step['start_y']} -> {step['end_x']}, {step['end_y']}"
            ")"
        )
    raise RuntimeError(f"Unsupported action: {action}")


@mcp.tool()
def tap_and_wait(
    text: str | None = None,
    resource_id: str | None = None,
    class_name: str | None = None,
    description: str | None = None,
    xpath: str | None = None,
    wait_for_text: str | None = None,
    wait_for_resource_id: str | None = None,
    wait_for_class_name: str | None = None,
    wait_for_description: str | None = None,
    wait_for_xpath: str | None = None,
    wait_until_gone: bool = False,
    timeout: float = 10.0,
    settle_time: float = 0.5,
    compact: bool = True,
    device_id: str | None = None,
) -> str:
    """Tap an element, wait for UI stabilization, and return a fresh hierarchy snapshot.

    Wait strategy priority:
    1. Wait for an expected next element to appear if any wait_for_* locator is provided.
    2. Otherwise wait for the tapped element to disappear if wait_until_gone is True.
    3. Otherwise sleep for settle_time seconds as a fallback.
    """
    try:
        if wait_until_gone and any(
            value is not None
            for value in (
                wait_for_text,
                wait_for_resource_id,
                wait_for_class_name,
                wait_for_description,
                wait_for_xpath,
            )
        ):
            return "Error: use either wait_for_* selectors or wait_until_gone, not both."

        d = _get_device(device_id)
        tap_mode, tap_query, tap_elem = _locate_element(
            d,
            text=text,
            resource_id=resource_id,
            class_name=class_name,
            description=description,
            xpath=xpath,
        )
        if not _element_exists(tap_elem):
            return f"Element not found with {_locator_label(tap_mode, tap_query)}"

        _tap_resolved_element(d, tap_elem, tap_mode)
        wait_reason = f"fallback settle_time={settle_time}s"

        if any(
            value is not None
            for value in (
                wait_for_text,
                wait_for_resource_id,
                wait_for_class_name,
                wait_for_description,
                wait_for_xpath,
            )
        ):
            wait_mode, wait_query, wait_elem = _locate_element(
                d,
                text=wait_for_text,
                resource_id=wait_for_resource_id,
                class_name=wait_for_class_name,
                description=wait_for_description,
                xpath=wait_for_xpath,
                error_message="Error: provide at least one wait_for selector.",
            )
            if not _wait_on_element(wait_elem, wait_mode, timeout=timeout):
                return (
                    f"Timeout ({timeout}s): expected next element not found "
                    f"({_locator_label(wait_mode, wait_query)})."
                )
            wait_reason = f"waited for appearance of {_locator_label(wait_mode, wait_query)}"
        elif wait_until_gone:
            if not _wait_on_element(tap_elem, tap_mode, timeout=timeout, gone=True):
                return (
                    f"Timeout ({timeout}s): tapped element still present "
                    f"({_locator_label(tap_mode, tap_query)})."
                )
            wait_reason = f"waited for disappearance of {_locator_label(tap_mode, tap_query)}"
        else:
            _sleep_seconds(settle_time)

        snapshot = dump_hierarchy(compact=compact, device_id=device_id)
        return (
            f"Tapped {_locator_label(tap_mode, tap_query)} and {wait_reason}.\n\n"
            f"UI snapshot:\n{snapshot}"
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def tap_sequence(
    steps: list[dict[str, Any]],
    compact: bool = True,
    device_id: str | None = None,
) -> str:
    """Execute a validated sequence of UI actions and return the final hierarchy snapshot.

    Supported step actions:
    - tap: {action: "tap", x: int, y: int}
    - tap_element: {action: "tap_element", text/resource_id/class_name/description/xpath: ...}
    - wait: {action: "wait", seconds: float} OR
            {action: "wait", text/resource_id/class_name/description/xpath: ..., timeout?: float, gone?: bool}
    - input_text: {action: "input_text", text: str}
    - press_key: {action: "press_key", key: str}
    - swipe: {action: "swipe", start_x: int, start_y: int, end_x: int, end_y: int, duration?: float}
    """
    try:
        _validate_tap_sequence_steps(steps)
        d = _get_device(device_id)
        summaries: list[str] = []
        for index, step in enumerate(steps):
            try:
                summaries.append(f"{index}: {_execute_tap_sequence_step(d, step)}")
            except Exception as step_error:
                return f"Error at step {index}: {step_error}"
        snapshot = dump_hierarchy(compact=compact, device_id=device_id)
        return (
            f"Executed {len(steps)} steps successfully.\n"
            f"Steps:\n- " + "\n- ".join(summaries) + f"\n\nUI snapshot:\n{snapshot}"
        )
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Scroll & fling tools
# ---------------------------------------------------------------------------

@mcp.tool()
def scroll_to_element(
    text: str | None = None,
    resource_id: str | None = None,
    class_name: str | None = None,
    description: str | None = None,
    direction: str = "vertical",
    max_swipes: int = 50,
    reset_first: bool = True,
    device_id: str | None = None,
) -> str:
    """Scroll a scrollable container until a target element is found.

    Uses Android's native UiScrollable scrollTo, which is the most reliable
    way to find elements in long lists. Optionally resets scroll position
    to the beginning first for maximum reliability.

    Args:
        text: Exact text of the target element.
        resource_id: Resource ID of the target element.
        class_name: Class name of the target element.
        description: Content description of the target element.
        direction: Scroll direction — "vertical" (default) or "horizontal".
        max_swipes: Maximum number of swipes when resetting to beginning (default 50).
        reset_first: If True (default), scroll to beginning before searching.
                     Set to False if you know the element is ahead of current position.
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        selector = _build_selector(text, resource_id, class_name, description)
        if not selector:
            return "Error: provide at least one selector (text, resource_id, class_name, description)."
        d = _get_device(device_id)
        scrollable = d(scrollable=True)
        if not scrollable.exists:
            return "Error: no scrollable container found on screen."
        is_vertical = direction.lower() != "horizontal"
        scroll_obj = scrollable.scroll.vert if is_vertical else scrollable.scroll.horiz
        if reset_first:
            scroll_obj.toBeginning(max_swipes=max_swipes)
        scroll_obj.to(**selector)
        target = d(**selector)
        if target.exists:
            return f"Found element after scrolling: {_format_element_info(target.info)}"
        return f"Element not found after scrolling through the list: {selector}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def scroll(
    direction: str = "forward",
    orientation: str = "vertical",
    steps: int = 55,
    device_id: str | None = None,
) -> str:
    """Scroll the screen in a given direction.

    Uses Android's native UiScrollable for reliable scrolling.

    Args:
        direction: "forward" (default) or "backward".
        orientation: "vertical" (default) or "horizontal".
        steps: Number of steps (higher = slower scroll). Default 55.
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        scrollable = d(scrollable=True)
        if not scrollable.exists:
            return "Error: no scrollable container found on screen."
        scroll_obj = scrollable.scroll.vert if orientation.lower() != "horizontal" else scrollable.scroll.horiz
        if direction.lower() == "backward":
            reached_end = scroll_obj.backward(steps=steps)
        else:
            reached_end = scroll_obj.forward(steps=steps)
        return f"Scrolled {direction}. Reached end: {reached_end}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def fling(
    direction: str = "forward",
    orientation: str = "vertical",
    max_swipes: int = 500,
    device_id: str | None = None,
) -> str:
    """Fling (fast scroll) the screen. Faster than scroll, but less precise.

    Args:
        direction: "forward", "backward", "toBeginning", or "toEnd". Default "forward".
        orientation: "vertical" (default) or "horizontal".
        max_swipes: Maximum swipes for toBeginning/toEnd (default 500).
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        scrollable = d(scrollable=True)
        if not scrollable.exists:
            return "Error: no scrollable container found on screen."
        fling_obj = scrollable.fling.vert if orientation.lower() != "horizontal" else scrollable.fling.horiz
        dir_lower = direction.lower()
        if dir_lower == "backward":
            result = fling_obj.backward()
        elif dir_lower == "tobeginning":
            result = fling_obj.toBeginning(max_swipes=max_swipes)
        elif dir_lower == "toend":
            result = fling_obj.toEnd(max_swipes=max_swipes)
        elif dir_lower == "forward":
            result = fling_obj.forward()
        else:
            return f"Error: invalid direction '{direction}'. Use: forward, backward, toBeginning, toEnd."
        return f"Flung {direction}. Reached end: {result}"
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Wait tools
# ---------------------------------------------------------------------------

@mcp.tool()
def wait_element_gone(
    text: str | None = None,
    resource_id: str | None = None,
    class_name: str | None = None,
    description: str | None = None,
    xpath: str | None = None,
    timeout: float = 10.0,
    device_id: str | None = None,
) -> str:
    """Wait for a UI element to disappear from the screen.

    Useful for waiting for loading indicators, dialogs, or splash screens to go away.

    Args:
        text: Exact text of the element.
        resource_id: Resource ID.
        class_name: Class name.
        description: Content description.
        xpath: XPath expression.
        timeout: Maximum wait time in seconds (default 10).
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        if xpath:
            gone = d.xpath(xpath).wait_gone(timeout=timeout)
            if gone:
                return f"Element gone (xpath: {xpath})."
            return f"Timeout ({timeout}s): element still present (xpath: {xpath})."
        selector = _build_selector(text, resource_id, class_name, description)
        if not selector:
            return "Error: provide at least one selector."
        gone = d(**selector).wait_gone(timeout=timeout)
        if gone:
            return f"Element gone: {selector}"
        return f"Timeout ({timeout}s): element still present: {selector}"
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Toast tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_toast(
    wait_timeout: float = 10.0,
    reset_first: bool = True,
    device_id: str | None = None,
) -> str:
    """Wait for and capture a toast message on screen.

    Call this BEFORE triggering the action that shows the toast, or immediately after.
    Toasts are ephemeral — they disappear quickly.

    Args:
        wait_timeout: Maximum seconds to wait for a toast (default 10).
        reset_first: If True (default), clear any previously cached toast first.
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        if reset_first:
            d.toast.reset()
        message = d.toast.get_message(wait_timeout=wait_timeout, default=None)
        if message is None:
            return f"No toast detected within {wait_timeout}s."
        return json.dumps({"toast_message": message})
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Watcher tools
# ---------------------------------------------------------------------------

@mcp.tool()
def watcher_add(
    name: str,
    xpath_conditions: list[str],
    action: str = "click",
    press_key: str | None = None,
    device_id: str | None = None,
) -> str:
    """Register a watcher that auto-handles UI elements when they appear.

    Watchers run in the background and automatically respond to system dialogs,
    permission prompts, popups, etc. Call watcher_start() after adding watchers.

    Args:
        name: Unique name for this watcher (used to remove it later).
        xpath_conditions: List of XPath expressions that must ALL match to trigger.
                          For simple text matching, use "//*[@text='Allow']".
                          The action is performed on the LAST element in the list.
        action: What to do when triggered — "click" (default) or "press".
        press_key: Key to press if action is "press" (e.g. "back", "home").
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        w = d.watcher(name)
        for xpath in xpath_conditions:
            w = w.when(xpath)
        if action.lower() == "press":
            if not press_key:
                return "Error: press_key is required when action is 'press'."
            w.press(press_key)
        else:
            w.click()
        return f"Watcher '{name}' added: conditions={xpath_conditions}, action={action}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def watcher_start(interval: float = 2.0, device_id: str | None = None) -> str:
    """Start the background watcher that polls for registered conditions.

    Must be called after adding watchers with watcher_add.

    Args:
        interval: Polling interval in seconds (default 2.0). Lower = more responsive but more CPU.
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        if d.watcher.running():
            return "Watcher is already running."
        d.watcher.start(interval=interval)
        return f"Watcher started (polling every {interval}s)."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def watcher_stop(device_id: str | None = None) -> str:
    """Stop the background watcher.

    Args:
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        if not d.watcher.running():
            return "Watcher is not running."
        d.watcher.stop()
        return "Watcher stopped."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def watcher_remove(name: str | None = None, device_id: str | None = None) -> str:
    """Remove registered watchers.

    Args:
        name: Name of a specific watcher to remove. If not provided, removes ALL watchers.
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        if name:
            d.watcher.remove(name)
            return f"Watcher '{name}' removed."
        else:
            d.watcher.remove()
            return "All watchers removed."
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Screenshot & hierarchy
# ---------------------------------------------------------------------------

@mcp.tool()
def screenshot(
    save_path: str | None = None,
    inline: bool = False,
    image_format: str | None = None,
    quality: int = 85,
    max_width: int | None = None,
    max_height: int | None = None,
    device_id: str | None = None,
) -> Any:
    """Take a screenshot either as a saved file or inline JSON image data.

    Args:
        save_path: File path for saved screenshots. When inline is False, None defaults to
            "/tmp/screenshot.png". When inline is True, None avoids writing a file.
        inline: If True, return a JSON object with base64 image data for vision-ready clients.
        image_format: Optional output format override: png (default) or jpeg/jpg. When omitted,
            the save_path extension is used for saved files and png is used for inline mode.
        quality: JPEG quality from 1-100. Ignored for PNG except for validation.
        max_width: Optional maximum output width in pixels while preserving aspect ratio.
        max_height: Optional maximum output height in pixels while preserving aspect ratio.
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        effective_save_path = save_path or (None if inline else "/tmp/screenshot.png")
        img = _resize_image(
            d.screenshot(),
            max_width=max_width,
            max_height=max_height,
        )
        normalized_format = _normalize_image_format(image_format, save_path=effective_save_path)
        encoded = _encode_image_bytes(img, image_format=normalized_format, quality=quality)
        width, height = img.size

        if effective_save_path is not None:
            Path(effective_save_path).parent.mkdir(parents=True, exist_ok=True)
            with open(effective_save_path, "wb") as f:
                f.write(encoded)

        if inline:
            payload: dict[str, Any] = {
                "data": base64.b64encode(encoded).decode("ascii"),
                "mime_type": _mime_type_for_format(normalized_format),
                "width": width,
                "height": height,
                "byte_size": len(encoded),
                "format": normalized_format,
            }
            if effective_save_path is not None:
                payload["save_path"] = effective_save_path
            return payload

        if effective_save_path is None:
            return "Error: save_path cannot be None when inline is False."

        file_size = os.path.getsize(effective_save_path)
        return (
            f"Screenshot saved to {effective_save_path} "
            f"({width}x{height}, {file_size // 1024}KB, format={normalized_format})"
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def dump_hierarchy(
    compact: bool = True,
    device_id: str | None = None,
) -> str:
    """Dump the current UI hierarchy.

    Args:
        compact: If True (default), return a concise one-line-per-element format.
                 If False, return the full XML. Compact mode is lightweight but omits
                 stateful attributes that get_ui_tree() preserves.
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        xml_str = d.dump_hierarchy()
        if not compact:
            return xml_str
        return _parse_hierarchy_compact(xml_str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_ui_tree(
    structured: bool = True,
    device_id: str | None = None,
) -> Any:
    """Return the current UI tree with richer per-element state for agent analysis.

    Args:
        structured: If True, return the JSON array as structured MCP output.
            If False, return a JSON string containing the same array.
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        elements = _ui_tree_elements(d.dump_hierarchy())
        rendered = json.dumps(elements, ensure_ascii=False, indent=2)
        if structured:
            return ([types.TextContent(type="text", text=rendered)], elements)
        return rendered
    except Exception as e:
        return f"Error: {e}"


def _parse_hierarchy_compact(xml_str: str) -> str:
    """Parse UI hierarchy XML into a compact text format."""
    root = ET.fromstring(xml_str)
    lines: list[str] = []
    idx = 0
    for node in root.iter("node"):
        text = node.get("text", "")
        resource_id = node.get("resource-id", "")
        class_name = node.get("class", "")
        description = node.get("content-desc", "")
        bounds = node.get("bounds", "")
        clickable = node.get("clickable", "false")

        # Skip empty containers that add noise
        if _is_noise_container(text, resource_id, description, class_name):
            continue

        parts = [f"[{idx}]"]
        # Short class name (strip android.widget. prefix)
        short_class = class_name.replace("android.widget.", "").replace(
            "android.view.", ""
        )
        parts.append(short_class)
        if text:
            parts.append(f'"{text}"')
        if description:
            parts.append(f'desc="{description}"')
        if resource_id:
            # Shorten com.package.name:id/foo → id/foo
            short_id = resource_id.split(":id/")[-1] if ":id/" in resource_id else resource_id
            parts.append(f"#{short_id}")
        if clickable == "true":
            parts.append("[clickable]")
        parts.append(bounds)

        lines.append(" ".join(parts))
        idx += 1

    return "\n".join(lines)


def _is_noise_container(
    text: str,
    resource_id: str,
    description: str,
    class_name: str,
) -> bool:
    """Return whether a node is an empty container that adds little signal."""
    if text or resource_id or description:
        return False
    return class_name in {
        "android.view.View",
        "android.widget.FrameLayout",
        "android.widget.LinearLayout",
        "android.widget.RelativeLayout",
    }


# ---------------------------------------------------------------------------
# App management tools
# ---------------------------------------------------------------------------

@mcp.tool()
def app_start(
    package: str,
    activity: str | None = None,
    device_id: str | None = None,
) -> str:
    """Launch an application.

    Args:
        package: Package name (e.g. "com.android.settings").
        activity: Activity name (optional). If omitted, launches the default activity.
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        if activity:
            d.app_start(package, activity)
        else:
            d.app_start(package)
        return f"Started {package}" + (f"/{activity}" if activity else "") + "."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def app_stop(package: str, device_id: str | None = None) -> str:
    """Force-stop an application.

    Args:
        package: Package name (e.g. "com.android.settings").
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        d.app_stop(package)
        return f"Stopped {package}."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def app_install(path: str, device_id: str | None = None) -> str:
    """Install an APK file on the device.

    Args:
        path: Local path to the APK file.
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        d.app_install(path)
        return f"Installed APK from {path}."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def app_uninstall(package: str, device_id: str | None = None) -> str:
    """Uninstall an application from the device.

    Args:
        package: Package name to uninstall.
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        d.app_uninstall(package)
        return f"Uninstalled {package}."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def app_clear(package: str, device_id: str | None = None) -> str:
    """Clear all data for an application.

    Args:
        package: Package name.
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        d.app_clear(package)
        return f"Cleared data for {package}."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def app_info(package: str, device_id: str | None = None) -> str:
    """Get information about an installed application.

    Args:
        package: Package name.
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        info = d.app_info(package)
        return json.dumps(info, indent=2, ensure_ascii=False, default=str)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def app_list_running(device_id: str | None = None) -> str:
    """List all currently running applications. Returns a list of package names.

    Args:
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        apps = d.app_list_running()
        return json.dumps(apps, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def current_app(device_id: str | None = None) -> str:
    """Get information about the currently focused application (package name and activity).

    Args:
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        info = d.app_current()
        return json.dumps(info, indent=2, ensure_ascii=False, default=str)
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Device control tools
# ---------------------------------------------------------------------------

@mcp.tool()
def screen_on(device_id: str | None = None) -> str:
    """Turn the screen on (wake up the device).

    Args:
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        d.screen_on()
        return "Screen turned on."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def screen_off(device_id: str | None = None) -> str:
    """Turn the screen off.

    Args:
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        d.screen_off()
        return "Screen turned off."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def unlock(device_id: str | None = None) -> str:
    """Unlock the device using upstream uiautomator2 behavior and fallback gestures.

    Args:
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        # Keep the call order compatible with openatx/uiautomator2: do not call
        # screen_on() first, because Device.unlock() only swipes when screenOn=False.
        d.unlock()
        time.sleep(0.2)

        if _has_keyguard_overlay(d):
            # Upstream unlock gesture (left-bottom -> right-top) as fallback for
            # OEM lock screens that still show keyguard after d.unlock().
            d.swipe(0.1, 0.9, 0.9, 0.1)
            time.sleep(0.2)

        if _has_keyguard_overlay(d):
            return (
                "Android lock-state is unlocked, but keyguard UI still appears on screen. "
                "Additional authentication or OEM-specific gesture may be required."
            )
        return "Device unlocked and keyguard overlay cleared."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def open_notification(device_id: str | None = None) -> str:
    """Open the notification panel.

    Args:
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        d.open_notification()
        return "Notification panel opened."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def open_quick_settings(device_id: str | None = None) -> str:
    """Open the quick settings panel.

    Args:
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        d.open_quick_settings()
        return "Quick settings opened."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_clipboard(device_id: str | None = None) -> str:
    """Get the current clipboard content.

    Args:
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        content = d.clipboard
        return content or "(clipboard is empty)"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def set_clipboard(text: str, device_id: str | None = None) -> str:
    """Set the device clipboard content.

    Args:
        text: Text to put in the clipboard.
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        d.set_clipboard(text)
        return f"Clipboard set to: {text!r}"
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Diagnostics tools
# ---------------------------------------------------------------------------

@mcp.tool()
def clear_logs(device_id: str | None = None) -> str:
    """Clear logcat buffers for the target device.

    Args:
        device_id: Optional ADB serial/device ID. If omitted, uses the currently connected device.
    """
    try:
        serial = _resolve_adb_serial(device_id)
        return clear_device_logs(serial)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def get_logs(
    package: str | None = None,
    level: str | None = None,
    since: str | None = None,
    lines: int = 200,
    device_id: str | None = None,
) -> str:
    """Get filtered logcat output for the target device.

    Args:
        package: Optional Android package name to filter logs for.
        level: Minimum log priority to include. Supports V/D/I/W/E/F/A and full names.
        since: Optional timestamp filter. Supports ISO-8601, YYYY-MM-DD HH:MM:SS(.sss),
               or MM-DD HH:MM:SS.sss.
        lines: Maximum number of matching log lines to return (default 200).
        device_id: Optional ADB serial/device ID. If omitted, uses the currently connected device.
    """
    try:
        serial = _resolve_adb_serial(device_id)
        query = LogQuery(
            serial=serial,
            package=package,
            level=level,
            since=since,
            lines=lines,
        )
        return get_device_logs(query)
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Shell & file tools
# ---------------------------------------------------------------------------

@mcp.tool()
def shell(command: str, device_id: str | None = None) -> str:
    """Execute a shell command on the device and return the output.

    Args:
        command: Shell command to execute (e.g. "ls /sdcard").
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        result = device_manager.execute_shell(command, device_id)
        details = result.output
        if result.stderr:
            details = (
                f"{details}\n[stderr]\n{result.stderr}"
                if details
                else f"[stderr]\n{result.stderr}"
            )
        if result.exit_code != 0:
            return f"Exit code {result.exit_code}:\n{details}" if details else f"Exit code {result.exit_code}."
        return details
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def push_file(local: str, remote: str, device_id: str | None = None) -> str:
    """Push a local file to the device.

    Args:
        local: Local file path.
        remote: Destination path on the device (e.g. "/sdcard/file.txt").
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        d.push(local, remote)
        return f"Pushed {local} -> {remote}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def pull_file(remote: str, local: str, device_id: str | None = None) -> str:
    """Pull a file from the device to local filesystem.

    Args:
        remote: File path on the device (e.g. "/sdcard/file.txt").
        local: Local destination path.
        device_id: Optional device serial/device ID. Required when multiple devices are connected.
    """
    try:
        d = _get_device(device_id)
        d.pull(remote, local)
        return f"Pulled {remote} -> {local}"
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the MCP server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
