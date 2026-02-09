"""FRANZ - Stateless Narrative Memory Agent with Python Execution"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as w
import json
import struct
import urllib.request
import zlib
from datetime import datetime
from pathlib import Path
from time import sleep
from typing import Any

try:
    ULONG_PTR = w.ULONG_PTR
except AttributeError:
    ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32

API_URL = "http://localhost:1234/v1/chat/completions"
MODEL_NAME = "qwen3-vl-2b-instruct-1m"
RES_W, RES_H = 536, 364

CONFIG = {
    "max_tokens": 400,
    "cycle_delay_ms": 500,
}

SAMPLING = {
    "temperature": 0.7,
    "top_p": 0.9,
    "max_tokens": CONFIG["max_tokens"],
}

PLAN_PROMPT = """You are operating a computer. Look at the screen and output commands.

Commands:
CLICK x y - Click at position (x and y are 0-1000, where 0 is left/top, 1000 is right/bottom)
DRAG x1 y1 x2 y2 - Drag from position 1 to position 2
TYPE text - Type text
KEY name - Press key (windows, enter, escape, tab, backspace, delete)
PYTHON_EXECUTE code - Execute Python code (single line, math/logic only)
WAIT milliseconds - Pause

Output one command per line. No explanations.

PYTHON_EXECUTE examples:
PYTHON_EXECUTE result = 2 * 6
PYTHON_EXECUTE values = [2*x for x in [1,2,3,4]]
PYTHON_EXECUTE answer = sum([1,2,3,4,5])

Execute tasks one step at a time.
If no task visible, output: WAIT 1000"""

REFLECT_PROMPT = """You see the screen after commands executed.

Previous observation: [see below]
Commands executed: [see below]
Execution results: [see below]
Current screen: [see image]

Write observation (50-100 words):
1. Commands executed
2. What you see now
3. What changed from previous observation
4. Next step, or "No active task - monitoring screen"

Be factual. Describe what changed."""

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

try:
    shcore = ctypes.WinDLL("shcore", use_last_error=True)
except Exception:
    shcore = None

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

def _enable_dpi_awareness() -> None:
    try:
        if hasattr(user32, "SetProcessDpiAwarenessContext"):
            user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
            return
    except Exception:
        pass
    try:
        if shcore is not None and hasattr(shcore, "SetProcessDpiAwareness"):
            shcore.SetProcessDpiAwareness(2)
            return
    except Exception:
        pass

def _virtual_screen() -> tuple[int, int, int, int]:
    x = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
    y = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
    wv = int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
    hv = int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
    return x, y, wv, hv

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", w.DWORD),
        ("biWidth", w.LONG),
        ("biHeight", w.LONG),
        ("biPlanes", w.WORD),
        ("biBitCount", w.WORD),
        ("biCompression", w.DWORD),
        ("biSizeImage", w.DWORD),
        ("biXPelsPerMeter", w.LONG),
        ("biYPelsPerMeter", w.LONG),
        ("biClrUsed", w.DWORD),
        ("biClrImportant", w.DWORD),
    ]

class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", w.DWORD * 3)]

def capture_screen(dw: int, dh: int) -> tuple[bytes, tuple[int, int, int, int]]:
    vx, vy, vw, vh = _virtual_screen()
    sdc = user32.GetDC(0)
    src_dc = gdi32.CreateCompatibleDC(sdc)
    dst_dc = gdi32.CreateCompatibleDC(sdc)
    src_bmp = None
    dst_bmp = None
    try:
        bmi_src = BITMAPINFO()
        bmi_src.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi_src.bmiHeader.biWidth = vw
        bmi_src.bmiHeader.biHeight = -vh
        bmi_src.bmiHeader.biPlanes = 1
        bmi_src.bmiHeader.biBitCount = 32
        src_bits = ctypes.c_void_p()
        src_bmp = gdi32.CreateDIBSection(sdc, ctypes.byref(bmi_src), 0, ctypes.byref(src_bits), None, 0)
        gdi32.SelectObject(src_dc, src_bmp)
        gdi32.BitBlt(src_dc, 0, 0, vw, vh, sdc, vx, vy, 0x40CC0020)
        bmi_dst = BITMAPINFO()
        bmi_dst.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi_dst.bmiHeader.biWidth = dw
        bmi_dst.bmiHeader.biHeight = -dh
        bmi_dst.bmiHeader.biPlanes = 1
        bmi_dst.bmiHeader.biBitCount = 32
        dst_bits = ctypes.c_void_p()
        dst_bmp = gdi32.CreateDIBSection(sdc, ctypes.byref(bmi_dst), 0, ctypes.byref(dst_bits), None, 0)
        gdi32.SelectObject(dst_dc, dst_bmp)
        gdi32.SetStretchBltMode(dst_dc, 4)
        gdi32.StretchBlt(dst_dc, 0, 0, dw, dh, src_dc, 0, 0, vw, vh, 0x00CC0020)
        size = dw * dh * 4
        bgra = bytes((ctypes.c_ubyte * size).from_address(dst_bits.value))
    finally:
        if dst_bmp:
            gdi32.DeleteObject(dst_bmp)
        if src_bmp:
            gdi32.DeleteObject(src_bmp)
        gdi32.DeleteDC(dst_dc)
        gdi32.DeleteDC(src_dc)
        user32.ReleaseDC(0, sdc)
    rgba = bytearray(len(bgra))
    rgba[0::4] = bgra[2::4]
    rgba[1::4] = bgra[1::4]
    rgba[2::4] = bgra[0::4]
    rgba[3::4] = b"\xff" * (size // 4)
    return _rgba_to_png(bytes(rgba), dw, dh), (vx, vy, vw, vh)

def _rgba_to_png(rgba: bytes, wv: int, hv: int) -> bytes:
    raw = bytearray()
    stride = wv * 4
    for y in range(hv):
        raw.append(0)
        raw.extend(rgba[y * stride : (y + 1) * stride])
    ihdr = struct.pack(">IIBBBBB", wv, hv, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw), 6)
    def _chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")

def call_vlm(system_prompt: str, user_text: str, images: list[bytes]) -> str:
    content = [{"type": "text", "text": user_text}]
    for img in images:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64.b64encode(img).decode()}"}})
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        **SAMPLING,
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(API_URL, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.load(resp)
    return data["choices"][0]["message"]["content"]

def parse_commands(content: str) -> list[tuple[str, list[str]]]:
    commands: list[tuple[str, list[str]]] = []
    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if not parts:
            continue
        cmd = parts[0].upper()
        args_str = parts[1] if len(parts) > 1 else ""
        
        if cmd == "CLICK":
            args = args_str.split()
            if len(args) >= 2:
                commands.append(("click", args[:2]))
        elif cmd == "DRAG":
            args = args_str.split()
            if len(args) >= 4:
                commands.append(("drag", args[:4]))
        elif cmd == "TYPE":
            commands.append(("type", [args_str]))
        elif cmd == "KEY":
            args = args_str.split()
            if args:
                commands.append(("key", [args[0].lower()]))
        elif cmd == "PYTHON_EXECUTE":
            commands.append(("python_execute", [args_str]))
        elif cmd == "WAIT":
            args = args_str.split()
            if args:
                commands.append(("wait", [args[0]]))
    return commands

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", w.LONG),
        ("dy", w.LONG),
        ("mouseData", w.DWORD),
        ("dwFlags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", w.WORD),
        ("wScan", w.WORD),
        ("dwFlags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]

class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]

class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

class INPUT(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("union", INPUTUNION)]

def _send_inputs(items: list[INPUT]) -> None:
    if not items:
        return
    user32.SendInput(len(items), (INPUT * len(items))(*items), ctypes.sizeof(INPUT))

def _mouse_abs(px: int, py: int, vx: int, vy: int, vw: int, vh: int) -> tuple[int, int]:
    x = max(vx, min(vx + vw - 1, px)) - vx
    y = max(vy, min(vy + vh - 1, py)) - vy
    ax = int(round(x * 65535 / (vw - 1))) if vw > 1 else 0
    ay = int(round(y * 65535 / (vh - 1))) if vh > 1 else 0
    return max(0, min(65535, ax)), max(0, min(65535, ay))

def _mouse_move(px: int, py: int, vs: tuple[int, int, int, int]) -> None:
    vx, vy, vw, vh = vs
    ax, ay = _mouse_abs(px, py, vx, vy, vw, vh)
    inp = INPUT(type=INPUT_MOUSE, union=INPUTUNION(mi=MOUSEINPUT(dx=ax, dy=ay, mouseData=0, dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, time=0, dwExtraInfo=0)))
    _send_inputs([inp])

def _mouse_click() -> None:
    _send_inputs([
        INPUT(type=INPUT_MOUSE, union=INPUTUNION(mi=MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=MOUSEEVENTF_LEFTDOWN, time=0, dwExtraInfo=0))),
        INPUT(type=INPUT_MOUSE, union=INPUTUNION(mi=MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=MOUSEEVENTF_LEFTUP, time=0, dwExtraInfo=0))),
    ])

def _key_unicode(ch: str, up: bool) -> INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    return INPUT(type=INPUT_KEYBOARD, union=INPUTUNION(ki=KEYBDINPUT(wVk=0, wScan=ord(ch), dwFlags=flags, time=0, dwExtraInfo=0)))

def _key_vk(vk_code: int) -> None:
    """Press and release a virtual key by VK code"""
    _send_inputs([
        INPUT(type=INPUT_KEYBOARD, union=INPUTUNION(ki=KEYBDINPUT(wVk=vk_code, wScan=0, dwFlags=0, time=0, dwExtraInfo=0))),
        INPUT(type=INPUT_KEYBOARD, union=INPUTUNION(ki=KEYBDINPUT(wVk=vk_code, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0, dwExtraInfo=0))),
    ])

def _type_text(text: str) -> None:
    items: list[INPUT] = []
    for ch in text:
        items.append(_key_unicode(ch, False))
        items.append(_key_unicode(ch, True))
    if items:
        _send_inputs(items)

def execute_python(code: str, context: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """
    Execute Python code in restricted environment.
    
    Returns: (result_string, updated_context)
    """
    # Restricted namespace - no imports, no dangerous builtins
    safe_builtins = {
        "abs": abs,
        "all": all,
        "any": any,
        "bin": bin,
        "bool": bool,
        "chr": chr,
        "dict": dict,
        "enumerate": enumerate,
        "filter": filter,
        "float": float,
        "hex": hex,
        "int": int,
        "len": len,
        "list": list,
        "map": map,
        "max": max,
        "min": min,
        "oct": oct,
        "ord": ord,
        "pow": pow,
        "range": range,
        "reversed": reversed,
        "round": round,
        "set": set,
        "sorted": sorted,
        "str": str,
        "sum": sum,
        "tuple": tuple,
        "zip": zip,
    }
    
    namespace = {"__builtins__": safe_builtins, **context}
    
    try:
        # Execute code (single statement or expression)
        exec(code, namespace)
        
        # Extract new variables created
        new_context = {k: v for k, v in namespace.items() 
                      if k not in safe_builtins and not k.startswith("__")}
        
        # Format result
        results = []
        for key, value in new_context.items():
            if key not in context:  # New variable
                results.append(f"{key} = {value}")
        
        result_str = "; ".join(results) if results else "OK"
        return result_str, new_context
        
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}", context

def execute_commands(commands: list[tuple[str, list[str]]], vs: tuple[int, int, int, int], 
                    python_context: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """
    Execute commands and return execution results + updated Python context.
    """
    vx, vy, vw, vh = vs
    def to_px(val: str, axis: str) -> int:
        v = int(float(val))
        if v <= 1000:
            return (vx if axis == "x" else vy) + int(round(v * (vw if axis == "x" else vh) / 1000))
        return v
    
    VK_MAP = {
        "enter": 0x0D,
        "escape": 0x1B,
        "esc": 0x1B,
        "tab": 0x09,
        "windows": 0x5B,
        "win": 0x5B,
        "backspace": 0x08,
        "delete": 0x2E,
        "del": 0x2E,
        "space": 0x20,
        "up": 0x26,
        "down": 0x28,
        "left": 0x25,
        "right": 0x27,
    }
    
    exec_results = []
    
    for action, args in commands:
        try:
            if action == "click" and len(args) >= 2:
                x, y = to_px(args[0], "x"), to_px(args[1], "y")
                _mouse_move(x, y, vs)
                sleep(0.02)
                _mouse_click()
                sleep(0.05)
            elif action == "drag" and len(args) >= 4:
                x1, y1 = to_px(args[0], "x"), to_px(args[1], "y")
                x2, y2 = to_px(args[2], "x"), to_px(args[3], "y")
                _mouse_move(x1, y1, vs)
                sleep(0.02)
                _send_inputs([INPUT(type=INPUT_MOUSE, union=INPUTUNION(mi=MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=MOUSEEVENTF_LEFTDOWN, time=0, dwExtraInfo=0)))])
                sleep(0.06)
                _mouse_move(x2, y2, vs)
                sleep(0.04)
                _send_inputs([INPUT(type=INPUT_MOUSE, union=INPUTUNION(mi=MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=MOUSEEVENTF_LEFTUP, time=0, dwExtraInfo=0)))])
                sleep(0.05)
            elif action == "type":
                _type_text(args[0])
                sleep(0.04)
            elif action == "key":
                key_name = args[0].lower()
                if key_name in VK_MAP:
                    _key_vk(VK_MAP[key_name])
                    sleep(0.05)
                else:
                    print(f"WARNING: Unknown key '{key_name}'")
            elif action == "python_execute":
                code = args[0]
                print(f"EXECUTING PYTHON: {code}")
                result, python_context = execute_python(code, python_context)
                exec_results.append(f"PYTHON: {code} → {result}")
                print(f"  RESULT: {result}")
            elif action == "wait":
                sleep(min(10.0, float(args[0])) / 1000.0)
        except Exception as e:
            error_msg = f"ERROR {action}: {e}"
            print(error_msg)
            exec_results.append(error_msg)
    
    return exec_results, python_context

def main() -> None:
    _enable_dpi_awareness()
    dump = Path("dump") / datetime.now().strftime("%Y%m%d_%H%M%S")
    dump.mkdir(parents=True, exist_ok=True)
    log = dump / "log.txt"
    
    observation = (
        "System started. Capable of: clicking, dragging, typing, pressing keys, executing Python code, waiting. "
        "Monitoring screen for tasks or instructions."
    )
    
    # Python execution context (persists across cycles)
    python_context: dict[str, Any] = {}
    
    print("=" * 80)
    print("FRANZ - Stateless Narrative Memory + Python Execution")
    print("=" * 80)
    print(f"Initial: {observation}")
    print(f"Logs: {dump}")
    print("=" * 80)
    print("⚠️  WARNING: PYTHON_EXECUTE enabled - agent can run arbitrary code!")
    print("=" * 80)
    
    with open(log, "w", encoding="utf-8") as f:
        f.write(f"START: {observation}\n\n")
    
    step = 0
    while True:
        if (dump / "STOP").exists():
            print("\nSTOP")
            break
        step += 1
        print(f"\n{'='*80}\nSTEP {step}\n{'='*80}")
        
        try:
            png_current, vs = capture_screen(RES_W, RES_H)
            (dump / f"{step:04d}_current.png").write_bytes(png_current)
        except Exception as e:
            print(f"ERROR capture: {e}")
            sleep(1)
            continue
        
        print(f"\nOBSERVATION:\n{observation}\n")
        print(f"PYTHON CONTEXT: {python_context}\n")
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"STEP {step}\nOBSERVATION: {observation}\n")
            f.write(f"PYTHON CONTEXT: {python_context}\n\n")
        
        try:
            print("Planning...")
            plan_user_prompt = (
                f"Current observation:\n{observation}\n\n"
                f"Available Python variables: {list(python_context.keys())}\n\n"
                "Look at the screen. Execute visible tasks one step at a time. "
                "Use PYTHON_EXECUTE for calculations. "
                "If no task present, output: WAIT 1000"
            )
            plan_text = call_vlm(PLAN_PROMPT, plan_user_prompt, [png_current])
            print(f"\nPLAN:\n{plan_text}\n")
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"PLAN:\n{plan_text}\n\n")
        except Exception as e:
            print(f"ERROR plan: {e}")
            sleep(1)
            continue
        
        commands = parse_commands(plan_text)
        if not commands:
            commands = [("wait", ["2000"])]
        
        print("COMMANDS:")
        for cmd, args in commands:
            print(f"  {cmd.upper()} {' '.join(args)}")
        print()
        
        with open(log, "a", encoding="utf-8") as f:
            f.write("COMMANDS:\n")
            for cmd, args in commands:
                f.write(f"  {cmd.upper()} {' '.join(args)}\n")
            f.write("\n")
        
        exec_results, python_context = execute_commands(commands, vs, python_context)
        
        if exec_results:
            print("EXECUTION RESULTS:")
            for result in exec_results:
                print(f"  {result}")
            print()
        
        try:
            png_after, vs = capture_screen(RES_W, RES_H)
            (dump / f"{step:04d}_after.png").write_bytes(png_after)
        except Exception as e:
            print(f"ERROR after: {e}")
            sleep(1)
            continue
        
        cmd_text = "\n".join(f"{cmd.upper()} {' '.join(args)}" for cmd, args in commands)
        results_text = "\n".join(exec_results) if exec_results else "No execution results"
        
        try:
            print("Reflecting...")
            reflect_text = call_vlm(
                REFLECT_PROMPT,
                f"Previous observation:\n{observation}\n\n"
                f"Commands executed:\n{cmd_text}\n\n"
                f"Execution results:\n{results_text}\n\n"
                f"Python variables available: {list(python_context.keys())}\n\n"
                "Look at current screen and write new observation:",
                [png_after]
            )
            print(f"\nREFLECT:\n{reflect_text}\n")
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"REFLECT:\n{reflect_text}\n\n")
            
            observation = reflect_text.strip()
        except Exception as e:
            print(f"ERROR reflect: {e}")
            observation = f"{observation} [Step {step} technical error]"
        
        print(f"NEW OBSERVATION:\n{observation}\n")
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"NEW OBSERVATION:\n{observation}\n\n")
        
        (dump / "observation.txt").write_text(observation, encoding="utf-8")
        sleep(CONFIG["cycle_delay_ms"] / 1000)

if __name__ == "__main__":
    main()
