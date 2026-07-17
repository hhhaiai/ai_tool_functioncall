"""
gateway_computer_use.py — Real implementations for computer_use, image_generation,
click, type_text, press_key, scroll tools.

No placeholders. All tools perform real actions:
- Screenshots via macOS Quartz / cross-platform pyautogui
- Mouse/keyboard via Quartz CGEvents
- Image generation via Pollinations.ai (free, no key) or configured API.
  If every real provider fails, returns ok=false instead of a local placeholder.
"""

from __future__ import annotations

import base64
import datetime as _dt
import io
import json
import os
import pathlib
import sys
import time
from typing import Any

from .gateway_file_ops import atomic_write_bytes

Json = dict[str, Any]

# ---------------------------------------------------------------------------
# Platform detection & imports
# ---------------------------------------------------------------------------

_IS_MACOS = sys.platform == "darwin"

_QUARTZ = None
_PIL_Image = None
_PYAUTOGUI = None


def _lazy_imports():
    global _QUARTZ, _PIL_Image, _PYAUTOGUI
    if _IS_MACOS and _QUARTZ is None:
        try:
            import Quartz as _Q
            _QUARTZ = _Q
        except ImportError:
            pass
    if _PIL_Image is None:
        try:
            from PIL import Image as _Img
            _PIL_Image = _Img
        except ImportError:
            pass
    if _PYAUTOGUI is None and not _IS_MACOS:
        try:
            import pyautogui as _pg
            _PYAUTOGUI = _pg
        except ImportError:
            pass


def _ensure_screenshot_dir() -> pathlib.Path:
    d = pathlib.Path(os.environ.get("GATEWAY_SCREENSHOT_DIR", "/tmp/gateway_screenshots"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _image_generation_url_policy() -> Json:
    allow_private = os.environ.get("GATEWAY_ALLOW_PRIVATE_NETWORK_TOOLS", "").strip().lower() in {"1", "true", "yes", "on"}
    try:
        from .gateway_config import _gateway_config
        allow_private = allow_private or bool(_gateway_config().get("allow_private_network_tools", False))
    except Exception:
        pass
    return {"allow_private_network": allow_private}


def _open_image_provider_url(req: Any, *, timeout: float) -> Any:
    from .gateway_http_actions import _http_action_opener, _validate_action_url

    policy = _image_generation_url_policy()
    _validate_action_url(getattr(req, "full_url", ""), policy)
    return _http_action_opener(policy).open(req, timeout=timeout)


def _image_generation_size(size: str) -> tuple[int, int]:
    try:
        configured_max = int(os.environ.get("GATEWAY_IMAGE_MAX_DIMENSION", "2048"))
    except ValueError:
        configured_max = 2048
    max_dimension = max(64, min(configured_max, 4096))
    width, height = 1024, 1024
    if "x" in size:
        parts = size.lower().split("x", 1)
        width, height = int(parts[0]), int(parts[1])
    width = max(64, min(width, max_dimension))
    height = max(64, min(height, max_dimension))
    return width, height


# ---------------------------------------------------------------------------
# computer_use — take screenshot + return path/base64
# ---------------------------------------------------------------------------

def _tool_computer_use(args: Json) -> str:
    """Take a screenshot of the current display. Returns file path and optional base64."""
    _lazy_imports()
    output_dir = _ensure_screenshot_dir()
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = output_dir / f"screenshot_{ts}.png"
    width = height = 0

    if _QUARTZ is not None:
        main_id = _QUARTZ.CGMainDisplayID()
        img = _QUARTZ.CGWindowListCreateImage(
            _QUARTZ.CGRectInfinite,
            _QUARTZ.kCGWindowListOptionOnScreenOnly,
            _QUARTZ.kCGNullWindowID,
            _QUARTZ.kCGWindowImageDefault,
        )
        if img is None:
            return json.dumps({"ok": False, "error": "CGWindowListCreateImage returned null"}, ensure_ascii=False)
        width = _QUARTZ.CGImageGetWidth(img)
        height = _QUARTZ.CGImageGetHeight(img)
        if _PIL_Image is not None:
            buf = _QUARTZ.CFDataCreateMutable(None, 0)
            dest = _QUARTZ.CGImageDestinationCreateWithData(buf, "public.png", 1, None)
            _QUARTZ.CGImageDestinationAddImage(dest, img, None)
            _QUARTZ.CGImageDestinationFinalize(dest)
            data = _QUARTZ.CFDataGetBytes(buf, _QUARTZ.CFRangeMake(0, _QUARTZ.CFDataGetLength(buf)))
            pil_img = _PIL_Image.open(io.BytesIO(bytes(data)))
            pil_img.save(str(out_path))
        else:
            return json.dumps({"ok": False, "error": "Pillow required for screenshot saving on macOS. pip install Pillow"}, ensure_ascii=False)
    elif _PYAUTOGUI is not None:
        pil_img = _PYAUTOGUI.screenshot()
        width, height = pil_img.size
        pil_img.save(str(out_path))
    else:
        return json.dumps({"ok": False, "error": "No screenshot backend available. Install Pillow (macOS) or pyautogui (Linux/Windows)."}, ensure_ascii=False)

    result: Json = {
        "ok": True,
        "path": str(out_path),
        "width": width,
        "height": height,
        "size_bytes": out_path.stat().st_size,
        "timestamp": ts,
    }
    if args.get("include_base64") or args.get("base64"):
        b64 = base64.b64encode(out_path.read_bytes()).decode("ascii")
        result["base64_png"] = b64[:200000] + ("..." if len(b64) > 200000 else "")
    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# click — real mouse click via Quartz / pyautogui
# ---------------------------------------------------------------------------

def _tool_click(args: Json) -> str:
    """Click at (x, y) on screen. Supports left/right/middle and double-click."""
    _lazy_imports()
    x = int(args.get("x", 0))
    y = int(args.get("y", 0))
    button = str(args.get("button", "left")).lower()
    double = bool(args.get("double") or args.get("double_click"))

    if _QUARTZ is not None:
        point = _QUARTZ.CGPointMake(float(x), float(y))
        if button == "right":
            down_type = _QUARTZ.kCGEventRightMouseDown
            up_type = _QUARTZ.kCGEventRightMouseUp
            btn = _QUARTZ.kCGMouseButtonRight
        elif button == "middle":
            down_type = _QUARTZ.kCGEventOtherMouseDown
            up_type = _QUARTZ.kCGEventOtherMouseUp
            btn = _QUARTZ.kCGMouseButtonCenter
        else:
            down_type = _QUARTZ.kCGEventLeftMouseDown
            up_type = _QUARTZ.kCGEventLeftMouseUp
            btn = _QUARTZ.kCGMouseButtonLeft

        click_count = 2 if double else 1
        for _ in range(click_count):
            down = _QUARTZ.CGEventCreateMouseEvent(None, down_type, point, btn)
            up = _QUARTZ.CGEventCreateMouseEvent(None, up_type, point, btn)
            _QUARTZ.CGEventPost(_QUARTZ.kCGHIDEventTap, down)
            time.sleep(0.02)
            _QUARTZ.CGEventPost(_QUARTZ.kCGHIDEventTap, up)
            if click_count > 1:
                time.sleep(0.05)
    elif _PYAUTOGUI is not None:
        if double:
            _PYAUTOGUI.doubleClick(x, y, button=button)
        else:
            _PYAUTOGUI.click(x, y, button=button)
    else:
        return json.dumps({"ok": False, "error": "No click backend. Install Quartz (macOS) or pyautogui."}, ensure_ascii=False)

    return json.dumps({"ok": True, "x": x, "y": y, "button": button, "double_click": double}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# type_text — type a string with real keyboard events
# ---------------------------------------------------------------------------

def _tool_type_text(args: Json) -> str:
    """Type text string character by character via real keyboard events."""
    _lazy_imports()
    text = str(args.get("text") or args.get("input") or "")
    if not text:
        return json.dumps({"ok": False, "error": "missing text"}, ensure_ascii=False)
    interval = float(args.get("interval") or args.get("delay") or 0.03)

    if _QUARTZ is not None:
        for char in text:
            event_down = _QUARTZ.CGEventCreateKeyboardEvent(None, 0, True)
            event_up = _QUARTZ.CGEventCreateKeyboardEvent(None, 0, False)
            _QUARTZ.CGEventKeyboardSetUnicodeString(
                event_down, len(char), char
            )
            _QUARTZ.CGEventKeyboardSetUnicodeString(
                event_up, len(char), char
            )
            _QUARTZ.CGEventPost(_QUARTZ.kCGHIDEventTap, event_down)
            _QUARTZ.CGEventPost(_QUARTZ.kCGHIDEventTap, event_up)
            time.sleep(interval)
    elif _PYAUTOGUI is not None:
        _PYAUTOGUI.typewrite(text, interval=interval) if text.isascii() else _PYAUTOGUI.write(text)
    else:
        return json.dumps({"ok": False, "error": "No keyboard backend."}, ensure_ascii=False)

    return json.dumps({"ok": True, "length": len(text), "typed": text[:200]}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# press_key — press a named key (Enter, Tab, Escape, arrows, combos)
# ---------------------------------------------------------------------------

# macOS Quartz keycodes
_KEYCODE_MAP = {
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51,
    "backspace": 51, "escape": 53, "esc": 53, "command": 55, "cmd": 55,
    "shift": 56, "option": 58, "alt": 58, "control": 59, "ctrl": 59,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96,
    "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109,
    "f11": 103, "f12": 111,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
}

_MODIFIER_FLAGS = {
    "command": 1 << 20, "cmd": 1 << 20,
    "shift": 1 << 17,
    "option": 1 << 19, "alt": 1 << 19,
    "control": 1 << 18, "ctrl": 1 << 18,
}


def _tool_press_key(args: Json) -> str:
    """Press a key or key combo (e.g., 'command+a', 'ctrl+shift+s')."""
    _lazy_imports()
    key = str(args.get("key") or args.get("keys") or "").strip().lower()
    if not key:
        return json.dumps({"ok": False, "error": "missing key"}, ensure_ascii=False)

    parts = [p.strip() for p in key.replace("-", "+").split("+") if p.strip()]
    modifiers = [p for p in parts if p in _MODIFIER_FLAGS]
    main_key = [p for p in parts if p not in _MODIFIER_FLAGS]
    main = main_key[-1] if main_key else parts[-1]

    if _QUARTZ is not None:
        keycode = _KEYCODE_MAP.get(main)
        if keycode is None and len(main) == 1:
            # For single chars, use unicode event
            event_down = _QUARTZ.CGEventCreateKeyboardEvent(None, 0, True)
            event_up = _QUARTZ.CGEventCreateKeyboardEvent(None, 0, False)
            flags = 0
            for mod in modifiers:
                flags |= _MODIFIER_FLAGS.get(mod, 0)
            if flags:
                _QUARTZ.CGEventSetFlags(event_down, flags)
                _QUARTZ.CGEventSetFlags(event_up, flags)
            _QUARTZ.CGEventKeyboardSetUnicodeString(event_down, len(main), main)
            _QUARTZ.CGEventKeyboardSetUnicodeString(event_up, len(main), main)
            _QUARTZ.CGEventPost(_QUARTZ.kCGHIDEventTap, event_down)
            _QUARTZ.CGEventPost(_QUARTZ.kCGHIDEventTap, event_up)
        elif keycode is not None:
            event_down = _QUARTZ.CGEventCreateKeyboardEvent(None, keycode, True)
            event_up = _QUARTZ.CGEventCreateKeyboardEvent(None, keycode, False)
            flags = 0
            for mod in modifiers:
                flags |= _MODIFIER_FLAGS.get(mod, 0)
            if flags:
                _QUARTZ.CGEventSetFlags(event_down, flags)
                _QUARTZ.CGEventSetFlags(event_up, flags)
            _QUARTZ.CGEventPost(_QUARTZ.kCGHIDEventTap, event_down)
            time.sleep(0.02)
            _QUARTZ.CGEventPost(_QUARTZ.kCGHIDEventTap, event_up)
        else:
            return json.dumps({"ok": False, "error": f"unknown key: {main}"}, ensure_ascii=False)
    elif _PYAUTOGUI is not None:
        combo = "+".join(parts)
        _PYAUTOGUI.hotkey(*parts) if len(parts) > 1 else _PYAUTOGUI.press(main)
    else:
        return json.dumps({"ok": False, "error": "No keyboard backend."}, ensure_ascii=False)

    return json.dumps({"ok": True, "key": key, "modifiers": modifiers, "main_key": main}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# scroll — real scroll via Quartz / pyautogui
# ---------------------------------------------------------------------------

def _tool_scroll(args: Json) -> str:
    """Scroll the mouse wheel. dx=horizontal, dy=vertical. Positive dy=scroll up."""
    _lazy_imports()
    dx = int(args.get("dx") or args.get("delta_x") or 0)
    dy = int(args.get("dy") or args.get("delta_y") or args.get("amount") or 3)
    x = args.get("x")
    y = args.get("y")

    if _QUARTZ is not None:
        if x is not None and y is not None:
            point = _QUARTZ.CGPointMake(float(x), float(y))
            move = _QUARTZ.CGEventCreateMouseEvent(None, _QUARTZ.kCGEventMouseMoved, point, 0)
            _QUARTZ.CGEventPost(_QUARTZ.kCGHIDEventTap, move)
            time.sleep(0.02)

        scroll_event = _QUARTZ.CGEventCreateScrollWheelEvent(
            None, _QUARTZ.kCGScrollEventUnitLine, 2, dy, dx
        )
        if scroll_event:
            _QUARTZ.CGEventPost(_QUARTZ.kCGHIDEventTap, scroll_event)
        else:
            return json.dumps({"ok": False, "error": "CGEventCreateScrollWheelEvent failed"}, ensure_ascii=False)
    elif _PYAUTOGUI is not None:
        if x is not None and y is not None:
            _PYAUTOGUI.moveTo(int(x), int(y))
        _PYAUTOGUI.scroll(dy, dx)
    else:
        return json.dumps({"ok": False, "error": "No scroll backend."}, ensure_ascii=False)

    return json.dumps({"ok": True, "dx": dx, "dy": dy}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# image_generation — real image generation via Pollinations.ai / HF / OpenAI
# ---------------------------------------------------------------------------

def _tool_image_generation(args: Json) -> str:
    """Generate an image from a text prompt.
    Provider priority: 1) OpenAI (if key set) 2) Pollinations.ai (free) 3) HuggingFace (if key set).
    Returns base64 PNG and saves to disk.
    """
    prompt = str(args.get("prompt") or args.get("input") or "")
    if not prompt:
        return json.dumps({"ok": False, "error": "missing prompt"}, ensure_ascii=False)

    size = str(args.get("size") or "1024x1024")
    output_dir = _ensure_screenshot_dir()
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = output_dir / f"generated_{ts}.png"
    provider_errors: list[str] = []

    # --- Provider 1: OpenAI DALL-E ---
    openai_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("IMAGE_GEN_API_KEY")
    if openai_key:
        try:
            import urllib.request
            import urllib.error
            body = json.dumps({
                "model": os.environ.get("IMAGE_GEN_MODEL", "dall-e-3"),
                "prompt": prompt,
                "n": 1,
                "size": args.get("size") or "1024x1024",
                "response_format": "b64_json",
            }).encode()
            base_url = os.environ.get("IMAGE_GEN_BASE_URL", "https://api.openai.com").rstrip("/")
            req = urllib.request.Request(
                base_url + "/v1/images/generations",
                data=body,
                headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"},
            )
            with _open_image_provider_url(req, timeout=120) as resp:
                data = json.loads(resp.read().decode())
            b64_data = data["data"][0]["b64_json"]
            img_bytes = base64.b64decode(b64_data)
            atomic_write_bytes(out_path, img_bytes)
            return json.dumps({
                "ok": True, "provider": "openai", "path": str(out_path),
                "size_bytes": len(img_bytes),
                "base64_png": b64_data[:200000] + ("..." if len(b64_data) > 200000 else ""),
            }, ensure_ascii=False)
        except Exception as e:
            provider_errors.append(f"openai: {e}")

    # --- Provider 2: Pollinations.ai (free, no key) ---
    try:
        import urllib.request
        import urllib.parse
        encoded_prompt = urllib.parse.quote(prompt)
        width, height = _image_generation_size(size)
        url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width={width}&height={height}&nologo=true&seed={int(time.time())}"
        req = urllib.request.Request(url, headers={"User-Agent": "Gateway/1.0"})
        with _open_image_provider_url(req, timeout=120) as resp:
            img_bytes = resp.read()
        if len(img_bytes) < 1000:
            raise ValueError(f"Response too small ({len(img_bytes)} bytes), likely error")
        atomic_write_bytes(out_path, img_bytes)
        b64_str = base64.b64encode(img_bytes).decode("ascii")
        return json.dumps({
            "ok": True, "provider": "pollinations", "path": str(out_path),
            "size_bytes": len(img_bytes),
            "base64_png": b64_str[:200000] + ("..." if len(b64_str) > 200000 else ""),
            "prompt": prompt,
        }, ensure_ascii=False)
    except Exception as e:
        provider_errors.append(f"pollinations: {e}")

    # --- Provider 3: HuggingFace Inference API ---
    hf_key = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
    if hf_key:
        try:
            import urllib.request
            model = os.environ.get("IMAGE_GEN_HF_MODEL", "stabilityai/stable-diffusion-xl-base-1.0")
            body = json.dumps({"inputs": prompt}).encode()
            req = urllib.request.Request(
                f"https://api-inference.huggingface.co/models/{model}",
                data=body,
                headers={"Authorization": f"Bearer {hf_key}", "Content-Type": "application/json"},
            )
            with _open_image_provider_url(req, timeout=120) as resp:
                img_bytes = resp.read()
            atomic_write_bytes(out_path, img_bytes)
            b64_str = base64.b64encode(img_bytes).decode("ascii")
            return json.dumps({
                "ok": True, "provider": "huggingface", "path": str(out_path),
                "size_bytes": len(img_bytes),
                "base64_png": b64_str[:200000],
                "prompt": prompt,
            }, ensure_ascii=False)
        except Exception as e:
            provider_errors.append(f"huggingface: {e}")

    return json.dumps(
        {
            "ok": False,
            "error": "No real image generation provider succeeded. Configure OPENAI_API_KEY/HF_TOKEN or ensure Pollinations.ai is reachable.",
            "provider_errors": provider_errors[-5:],
        },
        ensure_ascii=False,
    )
