"""Windows background image matcher and clicker.

The application deliberately keeps every operation local: it reads one target
window with PrintWindow and sends mouse messages only to that window handle.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta
import json
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import cv2
import numpy as np


# PyInstaller extracts one-file bundles into a temporary folder. Assets are
# read from that bundle folder, while user-created debug captures must be
# written beside the executable so they survive after the program exits.
IS_FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
OUTPUT_ROOT = Path(sys.executable).resolve().parent if IS_FROZEN else BUNDLE_ROOT
ASSETS = BUNDLE_ROOT / "assets"
DEBUG = OUTPUT_ROOT / "debug"
POSITION_SETTINGS = OUTPUT_ROOT / "button_positions.json"
PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001
DIB_RGB_COLORS = 0
SRCCOPY = 0x00CC0020
# Steam games commonly render UI at a DPI-dependent scale.  Search the crop
# across this range instead of requiring its pixels to be exactly the same size.
# Start at native size so an exact current-resolution crop is detected without
# spending time on every other scale.  The remaining entries handle resolution
# changes for the other buttons.
MATCH_SCALES = (
    1.00, 0.975, 1.025, 0.95, 1.05, 0.90, 1.10, 0.80, 1.20,
    0.70, 1.30, 0.60, 1.40,
)
# Cancel and manual crops were made from a 2160 px-wide physical capture;
# start and ready crops were made from a 1920 px-wide lobby capture. Runtime
# captures use their own width to scale each reference-template family.
REFERENCE_CAPTURE_WIDTH = 2160
LOBBY_REFERENCE_CAPTURE_WIDTH = 1920
CALIBRATED_SCALE_TOLERANCE = (1.00, 0.975, 1.025, 0.95, 1.05, 0.90, 1.10)
# Coarse matching at a quarter of each dimension examines only 1/16 of the
# pixels. The promising locations are then verified at full resolution.
COARSE_SCALE = 0.25
COARSE_CANDIDATES = 3
# When a button has a calibrated click point, its search area is small enough
# to search at full resolution.  This avoids losing a 1–2 px detail while
# shrinking a small button to the quarter-size coarse image.
DIRECT_MATCH_MAX_PIXELS = 300_000
# If a game ignores a background message, retry the unchanged button without
# creating a rapid click loop.
RETRY_INTERVAL_SECONDS = 2.5

# Default percentage coordinates, measured from the game client area. Users
# can override these per calibrated resolution in the desktop interface.
BUTTON_DEFAULT_POSITIONS: dict[str, tuple[float, float]] = {
    "cancel_calibrated.png": (55.2, 54.3),
    "finish.png": (50.0, 50.0),
    "manual_calibrated.png": (85.7, 65.8),
    "ready_calibrated.png": (87.6, 78.1),
    "start_calibrated.png": (87.7, 77.4),
}


def calibrated_scales(
    capture_width: int, reference_width: int = REFERENCE_CAPTURE_WIDTH
) -> tuple[float, ...]:
    """Return scale candidates for crops calibrated at reference_width."""
    base_scale = capture_width / reference_width
    return tuple(round(base_scale * factor, 4) for factor in CALIBRATED_SCALE_TOLERANCE)


def calibration_description(capture_width: int, capture_height: int) -> str:
    scale = capture_width / REFERENCE_CAPTURE_WIDTH
    return f"目前擷取：{capture_width} × {capture_height}；校正樣板縮放：{scale:.1%}"


def default_button_position(name: str) -> tuple[float, float]:
    return BUTTON_DEFAULT_POSITIONS.get(name, (50.0, 50.0))


def profile_key(width: int, height: int) -> str:
    return f"{width}x{height}"


def load_position_profiles() -> dict[str, dict[str, tuple[float, float]]]:
    """Load user-defined percentage click positions, ignoring invalid entries."""
    try:
        raw = json.loads(POSITION_SETTINGS.read_text(encoding="utf-8"))
        profiles = raw.get("profiles", {})
        loaded: dict[str, dict[str, tuple[float, float]]] = {}
        for key, positions in profiles.items():
            if not isinstance(positions, dict):
                continue
            valid: dict[str, tuple[float, float]] = {}
            for name, pair in positions.items():
                if isinstance(pair, list) and len(pair) == 2:
                    x, y = float(pair[0]), float(pair[1])
                    if 0 <= x <= 100 and 0 <= y <= 100:
                        valid[name] = (x, y)
            loaded[key] = valid
        return loaded
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def fixed_click_point(
    name: str, image: np.ndarray, positions: dict[str, tuple[float, float]]
) -> tuple[int, int]:
    """Convert the stored client-area percentage to an integer click point."""
    x_percent, y_percent = positions.get(name, default_button_position(name))
    width, height = image.shape[1], image.shape[0]
    x = min(width - 1, max(0, round(width * x_percent / 100)))
    y = min(height - 1, max(0, round(height * y_percent / 100)))
    return x, y


def button_search_region(
    name: str, positions: dict[str, tuple[float, float]]
) -> tuple[float, float, float, float] | None:
    """Return a tight, resolution-independent search region for known buttons.

    The saved point is already the intended centre of the control.  Keeping
    the search near it makes matching resistant to similar UI art elsewhere
    on the screen and permits an original-resolution search.
    """
    # Finish may appear in more than one layout, so retain the broader search
    # for it and for user-added images.
    radii = {
        "cancel_calibrated.png": (16.0, 13.0),
        "manual_calibrated.png": (12.0, 10.0),
        "ready_calibrated.png": (13.0, 11.0),
        "start_calibrated.png": (13.0, 11.0),
    }
    radius = radii.get(name)
    if radius is None:
        return None
    x, y = positions.get(name, default_button_position(name))
    horizontal, vertical = radius
    return (
        max(0.0, (x - horizontal) / 100),
        max(0.0, (y - vertical) / 100),
        min(1.0, (x + horizontal) / 100),
        min(1.0, (y + vertical) / 100),
    )

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


def enable_per_monitor_dpi_awareness() -> None:
    """Use physical pixels for window bounds, screenshots, and click points."""
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (-4).  This must run
        # before Tk creates its first window.
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except AttributeError:
        pass
    # Fallback for older Windows versions.
    try:
        user32.SetProcessDPIAware()
    except AttributeError:
        pass


enable_per_monitor_dpi_awareness()

# ctypes defaults a native function return value to a 32-bit int.  Explicit
# pointer-sized return types are essential on 64-bit Windows.
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.restype = ctypes.c_int
user32.GetClientRect.restype = wintypes.BOOL
user32.PrintWindow.restype = wintypes.BOOL
user32.PostMessageW.restype = wintypes.BOOL
user32.GetCursorPos.restype = wintypes.BOOL
user32.ScreenToClient.restype = wintypes.BOOL
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.BitBlt.restype = wintypes.BOOL


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


# Explicit argument types prevent ctypes from silently converting the 64-bit
# HWND/HDC values to 32-bit C ints (the source of the reported OverflowError).
ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [ENUM_WINDOWS_PROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE, wintypes.DWORD,
]
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.BitBlt.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
]


def make_lparam(x: int, y: int) -> int:
    return (y & 0xFFFF) << 16 | (x & 0xFFFF)


def list_windows() -> list[tuple[int, str]]:
    """List visible, named top-level windows as (HWND, title)."""
    windows: list[tuple[int, str]] = []
    def visit(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            text = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, text, len(text))
            if text.value.strip():
                windows.append((hwnd, text.value))
        return True

    user32.EnumWindows(ENUM_WINDOWS_PROC(visit), 0)
    return windows


def find_window(title_part: str) -> int | None:
    """Return the first visible top-level window containing title_part."""
    wanted = title_part.casefold().strip()
    for hwnd, title in list_windows():
        if wanted in title.casefold():
            return hwnd
    return None


def window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    text = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, text, len(text))
    return text.value


def capture_client(hwnd: int) -> np.ndarray:
    """Capture a client area without requiring focus, returning a BGR image."""
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise OSError("無法取得遊戲視窗大小")
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width < 2 or height < 2:
        raise OSError("遊戲視窗目前最小化或大小無效")

    src_dc = user32.GetDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(src_dc)
    bits = ctypes.c_void_p()
    header = BITMAPINFOHEADER(
        ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0,
        width * height * 4, 0, 0, 0, 0,
    )
    info = BITMAPINFO(header, (wintypes.DWORD * 3)(0, 0, 0))
    bitmap = gdi32.CreateDIBSection(src_dc, ctypes.byref(info), DIB_RGB_COLORS,
                                   ctypes.byref(bits), None, 0)
    if not bitmap:
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, src_dc)
        raise OSError("無法建立影像緩衝區")
    old = gdi32.SelectObject(memory_dc, bitmap)
    try:
        def pixels() -> np.ndarray:
            raw = ctypes.string_at(bits, width * height * 4)
            return np.frombuffer(raw, dtype=np.uint8).reshape((height, width, 4))[:, :, :3].copy()

        # PW_RENDERFULLCONTENT helps DWM-composited games which return a blank
        # client-only PrintWindow frame.
        printed = user32.PrintWindow(hwnd, memory_dc, PW_CLIENTONLY | PW_RENDERFULLCONTENT)
        image = pixels()
        # Some DirectX games do not implement PrintWindow.  A client-DC BitBlt
        # works while the game is visible (even if it is not the focused app).
        # Prefer the more detailed result to avoid replacing a real white UI.
        if not printed or float(image.std()) < 2.0:
            copied = gdi32.BitBlt(memory_dc, 0, 0, width, height, src_dc, 0, 0, SRCCOPY)
            fallback = pixels()
            if copied and float(fallback.std()) > float(image.std()):
                image = fallback
        if float(image.std()) < 2.0:
            raise OSError("遊戲未提供可用畫面（PrintWindow 與 BitBlt 都是單色畫面）")
        return image
    finally:
        gdi32.SelectObject(memory_dc, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, src_dc)


def send_background_click(hwnd: int, x: int, y: int) -> None:
    """Send a client-coordinate left click without moving the real cursor."""
    point = make_lparam(x, y)
    user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, point)
    user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, point)
    user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, point)


def load_template(path: str) -> np.ndarray:
    image = cv2.imread(path, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"無法讀取圖片：{path}")
    return image


def match(
    screen: np.ndarray,
    template: np.ndarray,
    scales: tuple[float, ...] = MATCH_SCALES,
    region: tuple[float, float, float, float] | None = None,
) -> tuple[float, tuple[int, int], tuple[int, int]]:
    """Find the best scale-aware match, favouring stable luminance features."""
    best_score, best_pos, best_size = -1.0, (0, 0), (0, 0)
    offset_x, offset_y = 0, 0
    search = screen
    if region:
        left, top, right, bottom = region
        offset_x, offset_y = int(screen.shape[1] * left), int(screen.shape[0] * top)
        end_x, end_y = int(screen.shape[1] * right), int(screen.shape[0] * bottom)
        search = screen[offset_y:end_y, offset_x:end_x]
    source_height, source_width = template.shape[:2]
    # UI glow, HDR/colour profiles, and animation frames can change a button's
    # colour without changing its border and text.  Grayscale normalized
    # correlation is substantially less sensitive to those differences than
    # matching the three colour channels independently.
    search_gray = cv2.cvtColor(search, cv2.COLOR_BGR2GRAY)
    template_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)

    def scaled_template(width: int, height: int) -> np.ndarray:
        if (width, height) == (source_width, source_height):
            return template_gray
        interpolation = cv2.INTER_AREA if width < source_width else cv2.INTER_LINEAR
        return cv2.resize(template_gray, (width, height), interpolation=interpolation)

    # Known controls are searched in a small area around their calibrated
    # position.  At this size a full-resolution search is fast and, unlike the
    # old 1/4-size pre-search, cannot discard the true small-button location.
    if search_gray.size <= DIRECT_MATCH_MAX_PIXELS:
        for scale in scales:
            width, height = round(source_width * scale), round(source_height * scale)
            if width < 4 or height < 4 or width > search.shape[1] or height > search.shape[0]:
                continue
            result = cv2.matchTemplate(search_gray, scaled_template(width, height), cv2.TM_CCOEFF_NORMED)
            _min, score, _min_loc, top_left = cv2.minMaxLoc(result)
            if score > best_score:
                best_score = float(score)
                best_pos = (top_left[0] + offset_x, top_left[1] + offset_y)
                best_size = (width, height)
        return best_score, best_pos, best_size

    coarse_width = max(1, round(search.shape[1] * COARSE_SCALE))
    coarse_height = max(1, round(search.shape[0] * COARSE_SCALE))
    coarse_search = cv2.resize(search_gray, (coarse_width, coarse_height), interpolation=cv2.INTER_AREA)
    coarse_candidates: list[tuple[float, tuple[int, int], tuple[int, int]]] = []
    for scale in scales:
        width, height = round(source_width * scale), round(source_height * scale)
        if width < 4 or height < 4 or width > search.shape[1] or height > search.shape[0]:
            continue
        coarse_template = cv2.resize(
            template_gray,
            (max(4, round(width * COARSE_SCALE)), max(4, round(height * COARSE_SCALE))),
            interpolation=cv2.INTER_AREA,
        )
        if coarse_template.shape[0] > coarse_height or coarse_template.shape[1] > coarse_width:
            continue
        result = cv2.matchTemplate(coarse_search, coarse_template, cv2.TM_CCOEFF_NORMED)
        _min, score, _min_loc, top_left = cv2.minMaxLoc(result)
        coarse_candidates.append((float(score), top_left, (width, height)))

    # A lower-resolution image can choose a nearby false peak. Verify the
    # strongest few candidates against a small full-resolution neighbourhood.
    for _coarse_score, coarse_pos, (width, height) in sorted(coarse_candidates, reverse=True)[:COARSE_CANDIDATES]:
        scaled = scaled_template(width, height)
        estimate_x = round(coarse_pos[0] / COARSE_SCALE)
        estimate_y = round(coarse_pos[1] / COARSE_SCALE)
        margin = max(12, round(max(width, height) * 0.18))
        left = max(0, estimate_x - margin)
        top = max(0, estimate_y - margin)
        right = min(search.shape[1], estimate_x + width + margin)
        bottom = min(search.shape[0], estimate_y + height + margin)
        local = search_gray[top:bottom, left:right]
        if scaled.shape[0] > local.shape[0] or scaled.shape[1] > local.shape[1]:
            continue
        result = cv2.matchTemplate(local, scaled, cv2.TM_CCOEFF_NORMED)
        _min, score, _min_loc, local_pos = cv2.minMaxLoc(result)
        if score > best_score:
            best_score = float(score)
            best_pos = (local_pos[0] + left + offset_x, local_pos[1] + top + offset_y)
            best_size = (width, height)
    return best_score, best_pos, best_size


class ClickerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        # Do not include the game's default title here: otherwise the window
        # search could accidentally select this control panel itself.
        self.root.title("背景辨識點擊工具")
        self.root.resizable(False, False)
        self.window_choice_var = tk.StringVar()
        self.window_options: dict[str, tuple[int, str]] = {}
        # Priority order.  The loop clicks the first confirmed template, so
        # cancel must be checked before manual when both are visible.
        names = [
            "cancel_calibrated.png", "finish.png", "manual_calibrated.png",
            "ready_calibrated.png", "start_calibrated.png",
        ]
        self.template_paths = [ASSETS / name for name in names if (ASSETS / name).is_file()]
        self.delay_var = tk.StringVar(value="800")
        self.confidence_var = tk.StringVar(value="0.80")
        self.save_debug_var = tk.BooleanVar(value=False)
        self.schedule_enabled_var = tk.BooleanVar(value=False)
        self.schedule_time_var = tk.StringVar(value="23:59")
        self.calibration_var = tk.StringVar(value="尚未校正：開始監控前請先按「校正目前解析度」")
        self.calibration_hwnd: int | None = None
        self.calibration_capture_size: tuple[int, int] | None = None
        self.calibration_scales: tuple[float, ...] | None = None
        self.position_profiles = load_position_profiles()
        self.active_profile_key: str | None = None
        self.active_positions: dict[str, tuple[float, float]] | None = None
        self.position_x_var = tk.StringVar(value="50.0")
        self.position_y_var = tk.StringVar(value="50.0")
        self.pending_mouse_capture = False
        self.status_var = tk.StringVar(value="已停止")
        self.events: queue.Queue[str] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.debug_window: tk.Toplevel | None = None
        self.debug_text: tk.Text | None = None
        self._build()
        self.update_schedule_state()
        self.refresh_windows()
        self.root.after(100, self._drain_events)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _build(self) -> None:
        box = ttk.Frame(self.root, padding=14)
        box.grid(sticky="nsew")
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="遊戲視窗：").grid(row=0, column=0, sticky="w", pady=4)
        window_box = ttk.Frame(box)
        window_box.grid(row=0, column=1, columnspan=2, sticky="ew", pady=4)
        self.window_combo = ttk.Combobox(window_box, textvariable=self.window_choice_var, state="readonly", width=50)
        self.window_combo.pack(side="left", fill="x", expand=True)
        ttk.Button(window_box, text="重新掃描", command=self.refresh_windows).pack(side="left", padx=(6, 0))
        ttk.Label(box, text="偵測到即點擊的按鈕圖片：").grid(row=1, column=0, sticky="nw", pady=(7, 4))
        template_box = ttk.Frame(box)
        template_box.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(7, 4))
        self.template_list = tk.Listbox(template_box, height=6, width=57, selectmode=tk.EXTENDED)
        self.template_list.pack(side="left", fill="both", expand=True)
        self.template_list.bind("<<ListboxSelect>>", self.on_template_selected)
        buttons = ttk.Frame(template_box)
        buttons.pack(side="left", padx=(7, 0), fill="y")
        ttk.Button(buttons, text="加入…", command=self.add_templates).pack(fill="x")
        ttk.Button(buttons, text="移除", command=self.remove_templates).pack(fill="x", pady=(5, 0))
        self.refresh_template_list()
        position_box = ttk.Frame(box)
        position_box.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 4))
        ttk.Label(position_box, text="選取按鈕的固定點擊位置：X").pack(side="left")
        ttk.Entry(position_box, textvariable=self.position_x_var, width=7).pack(side="left", padx=(3, 0))
        ttk.Label(position_box, text="%  Y").pack(side="left")
        ttk.Entry(position_box, textvariable=self.position_y_var, width=7).pack(side="left", padx=(3, 0))
        ttk.Label(position_box, text="%").pack(side="left")
        self.capture_mouse_button = ttk.Button(
            position_box, text="3 秒後擷取滑鼠", command=self.capture_mouse_position
        )
        self.capture_mouse_button.pack(side="left", padx=(8, 0))
        ttk.Button(position_box, text="儲存此按鈕位置", command=self.save_selected_position).pack(side="left", padx=(8, 0))
        self._entry(box, 3, "掃描／點擊間隔（ms）：", self.delay_var)
        self._entry(box, 4, "相似度（0～1）：", self.confidence_var)
        schedule_box = ttk.Frame(box)
        schedule_box.grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 2))
        ttk.Checkbutton(
            schedule_box, text="定時停止監控", variable=self.schedule_enabled_var,
            command=self.update_schedule_state,
        ).pack(side="left")
        ttk.Label(schedule_box, text="時間（HH:MM）：").pack(side="left", padx=(12, 0))
        self.schedule_time_entry = ttk.Entry(schedule_box, textvariable=self.schedule_time_var, width=8)
        self.schedule_time_entry.pack(side="left")
        ttk.Checkbutton(box, text="偵測結果儲存除錯截圖", variable=self.save_debug_var).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(6, 5))
        controls = ttk.Frame(box)
        controls.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        self.start_button = ttk.Button(controls, text="開始監控", command=self.start)
        self.start_button.pack(side="left")
        ttk.Button(controls, text="停止", command=self.stop).pack(side="left", padx=7)
        ttk.Button(controls, text="校正目前解析度", command=self.calibrate_resolution).pack(side="left", padx=(0, 7))
        ttk.Button(controls, text="儲存目前畫面", command=self.capture_debug).pack(side="left", padx=(0, 7))
        ttk.Button(controls, text="開啟除錯視窗", command=self.open_debug).pack(side="left")
        ttk.Label(box, textvariable=self.calibration_var, foreground="#555555").grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(box, textvariable=self.status_var, foreground="#176b3a").grid(
            row=9, column=0, columnspan=3, sticky="w", pady=(4, 0))

    @staticmethod
    def _entry(parent: ttk.Frame, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=variable, width=52).grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)

    def update_schedule_state(self) -> None:
        state = "normal" if self.schedule_enabled_var.get() else "disabled"
        self.schedule_time_entry.configure(state=state)

    def scheduled_stop_datetime(self) -> datetime:
        """Return the next local HH:MM occurrence selected by the user."""
        selected_time = datetime.strptime(self.schedule_time_var.get().strip(), "%H:%M").time()
        now = datetime.now()
        target = now.replace(hour=selected_time.hour, minute=selected_time.minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    def refresh_windows(self) -> None:
        """Populate the selector with current visible top-level windows."""
        current = self.window_choice_var.get()
        self.window_options.clear()
        choices: list[str] = []
        for hwnd, title in list_windows():
            label = f"{title}  [0x{hwnd:016X}]"
            self.window_options[label] = (hwnd, title)
            choices.append(label)
        self.window_combo["values"] = choices
        if current in self.window_options:
            self.window_choice_var.set(current)
        elif choices:
            self.window_combo.current(0)
        else:
            self.window_choice_var.set("")

    def refresh_template_list(self) -> None:
        selected = self.template_list.curselection()
        selected_index = selected[0] if selected else 0
        self.template_list.delete(0, "end")
        for path in self.template_paths:
            # Bundled one-file executables extract assets to a temporary
            # _MEI... directory. Show a stable, readable label instead.
            self.template_list.insert("end", path.name)
        if self.template_paths:
            self.template_list.selection_set(min(selected_index, len(self.template_paths) - 1))
            self.on_template_selected()

    def selected_template_path(self) -> Path | None:
        selected = self.template_list.curselection()
        if not selected:
            return None
        return self.template_paths[selected[0]]

    def on_template_selected(self, _event: tk.Event | None = None) -> None:
        path = self.selected_template_path()
        if not path:
            return
        positions = self.active_positions or {}
        x, y = positions.get(path.name, default_button_position(path.name))
        self.position_x_var.set(f"{x:.2f}")
        self.position_y_var.set(f"{y:.2f}")

    def persist_position_profiles(self) -> None:
        serializable = {
            key: {name: [x, y] for name, (x, y) in positions.items()}
            for key, positions in self.position_profiles.items()
        }
        POSITION_SETTINGS.write_text(
            json.dumps({"version": 1, "profiles": serializable}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save_selected_position(self) -> None:
        path = self.selected_template_path()
        if not path:
            messagebox.showerror("尚未選擇按鈕", "請先在按鈕圖片清單選擇一張樣板。")
            return
        if not self.active_profile_key or self.active_positions is None:
            messagebox.showerror("尚未校正", "請先按「校正目前解析度」，再儲存固定點擊位置。")
            return
        try:
            x, y = float(self.position_x_var.get()), float(self.position_y_var.get())
            if not 0 <= x <= 100 or not 0 <= y <= 100:
                raise ValueError
        except ValueError:
            messagebox.showerror("位置格式錯誤", "X 與 Y 請輸入 0 到 100 之間的百分比。")
            return
        self.active_positions[path.name] = (x, y)
        self.position_profiles[self.active_profile_key] = self.active_positions
        try:
            self.persist_position_profiles()
        except OSError as exc:
            messagebox.showerror("儲存失敗", str(exc))
            return
        self.log(f"已儲存 {path.name} 的固定點擊位置：X={x:.2f}%，Y={y:.2f}%。")
        messagebox.showinfo("位置已儲存", f"{path.name}\nX={x:.2f}%  Y={y:.2f}%")

    def capture_mouse_position(self) -> None:
        """Give the user time to point at the game, then fill X/Y percentages."""
        if self.pending_mouse_capture:
            return
        if not self.selected_template_path():
            messagebox.showerror("尚未選擇按鈕", "請先在按鈕圖片清單選擇一張樣板。")
            return
        selected = self.window_options.get(self.window_choice_var.get())
        if not selected or self.calibration_hwnd != selected[0] or self.active_positions is None:
            messagebox.showerror("尚未校正", "請先選擇遊戲視窗並按「校正目前解析度」。")
            return
        self.pending_mouse_capture = True
        self.capture_mouse_button.configure(state="disabled", text="請移動滑鼠…")
        self.status_var.set("請在 3 秒內將滑鼠移到遊戲內想點擊的位置。")
        self.root.after(3000, self.finish_mouse_capture)

    def finish_mouse_capture(self) -> None:
        self.pending_mouse_capture = False
        self.capture_mouse_button.configure(state="normal", text="3 秒後擷取滑鼠")
        selected = self.window_options.get(self.window_choice_var.get())
        if not selected:
            self.status_var.set("擷取滑鼠位置失敗：遊戲視窗已取消選取。")
            return
        point = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)) or not user32.ScreenToClient(selected[0], ctypes.byref(point)):
            self.status_var.set("擷取滑鼠位置失敗。")
            return
        rect = wintypes.RECT()
        if not user32.GetClientRect(selected[0], ctypes.byref(rect)):
            self.status_var.set("擷取滑鼠位置失敗：無法取得遊戲視窗大小。")
            return
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width <= 0 or height <= 0 or not (0 <= point.x < width and 0 <= point.y < height):
            self.status_var.set("滑鼠不在選取的遊戲視窗內，未更新位置。")
            return
        x_percent, y_percent = point.x * 100 / width, point.y * 100 / height
        self.position_x_var.set(f"{x_percent:.2f}")
        self.position_y_var.set(f"{y_percent:.2f}")
        self.status_var.set(f"已擷取滑鼠位置：X={x_percent:.2f}%，Y={y_percent:.2f}%；請按「儲存此按鈕位置」。")
        self.log(self.status_var.get())

    def add_templates(self) -> None:
        paths = filedialog.askopenfilenames(
            title="加入要點擊的按鈕圖片",
            filetypes=[("圖片", "*.png *.jpg *.jpeg *.bmp"), ("所有檔案", "*.*")],
        )
        for value in paths:
            path = Path(value)
            if path not in self.template_paths:
                self.template_paths.append(path)
                if self.active_positions is not None:
                    self.active_positions.setdefault(path.name, default_button_position(path.name))
        self.refresh_template_list()

    def remove_templates(self) -> None:
        selected = set(self.template_list.curselection())
        self.template_paths = [path for index, path in enumerate(self.template_paths) if index not in selected]
        self.refresh_template_list()

    def capture_debug(self) -> None:
        """Save exactly what PrintWindow sees, for diagnosing game render APIs."""
        selected = self.window_options.get(self.window_choice_var.get())
        if not selected:
            messagebox.showerror("尚未選擇視窗", "請先重新掃描並選擇遊戲視窗。")
            return
        try:
            image = capture_client(selected[0])
            DEBUG.mkdir(exist_ok=True)
            filename = DEBUG / f"capture_{datetime.now():%Y%m%d_%H%M%S_%f}.png"
            if not cv2.imwrite(str(filename), image):
                raise OSError("無法寫入圖片")
            self.log(f"已儲存目前畫面：{filename.name}")
            messagebox.showinfo("截圖完成", f"已儲存到：\n{filename}")
        except Exception as exc:
            messagebox.showerror("截圖失敗", str(exc))

    def calibrate_resolution(self) -> None:
        """Capture selected window once and lock this session's UI scale."""
        selected = self.window_options.get(self.window_choice_var.get())
        if not selected:
            messagebox.showerror("尚未選擇視窗", "請先重新掃描並選擇遊戲視窗。")
            return
        try:
            image = capture_client(selected[0])
            width, height = image.shape[1], image.shape[0]
            self.calibration_hwnd = selected[0]
            self.calibration_capture_size = (width, height)
            self.calibration_scales = calibrated_scales(width)
            self.active_profile_key = profile_key(width, height)
            self.active_positions = self.position_profiles.setdefault(self.active_profile_key, {})
            for path in self.template_paths:
                self.active_positions.setdefault(path.name, default_button_position(path.name))
            self.on_template_selected()
            description = calibration_description(width, height)
            self.calibration_var.set("已校正：" + description + f"；位置設定檔：{self.active_profile_key}")
            self.log("解析度校正完成；" + description + f"；位置設定檔：{self.active_profile_key}")
            messagebox.showinfo(
                "校正完成",
                description + f"\n已載入位置設定檔：{self.active_profile_key}\n現在可以開始監控。",
            )
        except Exception as exc:
            messagebox.showerror("校正失敗", str(exc))

    def open_debug(self) -> None:
        if self.debug_window and self.debug_window.winfo_exists():
            self.debug_window.lift()
            return
        self.debug_window = tk.Toplevel(self.root)
        self.debug_window.title("除錯紀錄")
        self.debug_window.geometry("720x340")
        self.debug_text = tk.Text(self.debug_window, state="disabled", wrap="word")
        self.debug_text.pack(fill="both", expand=True, padx=8, pady=8)
        self.debug_window.protocol("WM_DELETE_WINDOW", self._close_debug)

    def _close_debug(self) -> None:
        if self.debug_window:
            self.debug_window.destroy()
        self.debug_window = None
        self.debug_text = None

    def log(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.events.put(f"[{stamp}] {text}")

    def _drain_events(self) -> None:
        try:
            while True:
                line = self.events.get_nowait()
                if self.debug_text and self.debug_text.winfo_exists():
                    self.debug_text.configure(state="normal")
                    self.debug_text.insert("end", line + "\n")
                    self.debug_text.see("end")
                    self.debug_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            delay = max(100, int(self.delay_var.get())) / 1000
            confidence = float(self.confidence_var.get())
            if not 0 < confidence <= 1:
                raise ValueError
            templates = [(path.name, load_template(str(path))) for path in self.template_paths]
        except (ValueError, OSError):
            messagebox.showerror("設定錯誤", "請確認延遲、相似度與所有按鈕圖片都有效。")
            return
        if not templates:
            messagebox.showerror("設定錯誤", "請至少加入一張要點擊的按鈕圖片。")
            return
        scheduled_stop_at: datetime | None = None
        if self.schedule_enabled_var.get():
            try:
                scheduled_stop_at = self.scheduled_stop_datetime()
            except ValueError:
                messagebox.showerror("定時格式錯誤", "請輸入 24 小時制 HH:MM，例如 23:30。")
                return
        selected_window = self.window_options.get(self.window_choice_var.get())
        if not selected_window:
            messagebox.showerror("設定錯誤", "請先按「重新掃描」並從下拉選單選擇遊戲視窗。")
            return
        if (
            self.calibration_hwnd != selected_window[0]
            or not self.calibration_scales
            or not self.calibration_capture_size
            or self.active_positions is None
        ):
            messagebox.showerror("尚未校正", "每次開啟工具後，請先對目前選取的遊戲視窗按「校正目前解析度」。")
            return
        self.stop_event.clear()
        if scheduled_stop_at:
            self.status_var.set(f"監控中；將於 {scheduled_stop_at:%Y-%m-%d %H:%M} 自動停止")
        else:
            self.status_var.set("監控中（可切換到其他視窗）")
        self.start_button.configure(state="disabled")
        hwnd, title = selected_window
        click_positions = {
            name: self.active_positions.get(name, default_button_position(name))
            for name, _template in templates
        }
        settings = (
            hwnd, title, templates, delay, confidence, self.save_debug_var.get(),
            self.calibration_scales, self.calibration_capture_size, click_positions, scheduled_stop_at,
        )
        self.worker = threading.Thread(target=self.run_loop, args=settings, daemon=True)
        self.worker.start()
        self.log("開始監控。")

    def stop(self) -> None:
        self.stop_event.set()
        self.status_var.set("已停止")
        self.start_button.configure(state="normal")
        self.log("已停止。")

    def run_loop(self, hwnd: int, title: str, templates: list[tuple[str, np.ndarray]],
                 delay: float, confidence: float, save_debug: bool,
                 calibration_scales_for_run: tuple[float, ...], calibration_size: tuple[int, int],
                 click_positions: dict[str, tuple[float, float]], scheduled_stop_at: datetime | None) -> None:
        last_click_key: tuple[str, int, int] | None = None
        last_click_at = 0.0
        click_attempt = 0
        last_missing_log = 0.0
        last_score_log = 0.0
        last_debug_save = 0.0
        last_size_warning = 0.0
        lobby_scales_for_run = calibrated_scales(calibration_size[0], LOBBY_REFERENCE_CAPTURE_WIDTH)
        stop_reason: str | None = None
        while not self.stop_event.is_set():
            if scheduled_stop_at and datetime.now() >= scheduled_stop_at:
                stop_reason = f"已依定時設定於 {scheduled_stop_at:%H:%M} 停止"
                self.log(stop_reason + "。")
                self.stop_event.set()
                break
            if not user32.IsWindow(hwnd):
                hwnd = find_window(title)
            if not hwnd:
                if time.monotonic() - last_missing_log > 5:
                    self.log(f"選取的視窗已關閉，且找不到標題為「{title}」的新視窗。")
                    last_missing_log = time.monotonic()
                self.stop_event.wait(delay)
                continue
            try:
                screen = capture_client(hwnd)
                capture_size = (screen.shape[1], screen.shape[0])
                if capture_size != calibration_size and time.monotonic() - last_size_warning > 5:
                    self.log("遊戲視窗尺寸已變更；請停止監控後重新按「校正目前解析度」。")
                    last_size_warning = time.monotonic()
                candidates: list[tuple[float, str, tuple[int, int], tuple[int, int]]] = []
                scores: list[tuple[str, float]] = []
                for name, template in templates:
                    stable_region = button_search_region(name, click_positions)
                    if name == "cancel_calibrated.png":
                        # The saved click point identifies the centre-dialog
                        # control and allows a full-resolution local search.
                        score, top_left, size = match(
                            screen, template, calibration_scales_for_run, stable_region
                        )
                    elif name == "manual_calibrated.png":
                        # The auto/manual control can glow or change colour;
                        # constrain it to its calibrated action-strip position.
                        score, top_left, size = match(
                            screen, template, calibration_scales_for_run, stable_region
                        )
                    elif name in {"ready_calibrated.png", "start_calibrated.png"}:
                        # Both lobby controls share a separate 1920 px-wide
                        # reference, but use their saved position for search.
                        score, top_left, size = match(
                            screen, template, lobby_scales_for_run, stable_region
                        )
                    else:
                        score, top_left, size = match(screen, template)
                    scores.append((name, score))
                    if score >= confidence:
                        candidates.append((score, name, top_left, size))
                        # One confirmed button is enough to act on this frame;
                        # avoiding needless template searches keeps scans fast.
                        break
                if not candidates:
                    # A later matching button starts a fresh click sequence.
                    last_click_key = None
                    click_attempt = 0
                    if time.monotonic() - last_score_log > 3:
                        summary = ", ".join(f"{name}={score:.3f}" for name, score in scores)
                        self.log(f"未達門檻 {confidence:.2f}；最高比對分數：{summary}")
                        last_score_log = time.monotonic()
                    if save_debug and time.monotonic() - last_debug_save > 3:
                        self.save_debug_image(screen, (0, 0), (0, 0), "no_match", max(score for _, score in scores))
                        last_debug_save = time.monotonic()
                    self.stop_event.wait(delay)
                    continue
                button_score, name, top_left, size = max(candidates, key=lambda item: item[0])
                # A template confirms which action is visible. Clicking uses
                # the user-configured, resolution-relative point instead of
                # the possibly imperfect template-match centre.
                x, y = fixed_click_point(name, screen, click_positions)
                click_key = (name, x, y)
                now = time.monotonic()
                if click_key != last_click_key or now - last_click_at >= RETRY_INTERVAL_SECONDS:
                    send_background_click(hwnd, x, y)
                    if click_key != last_click_key:
                        click_attempt = 1
                    else:
                        click_attempt += 1
                    last_click_key = click_key
                    last_click_at = now
                    self.log(
                        f"偵測到 {name}，已依固定位置背景點擊「{window_title(hwnd)}」座標 ({x}, {y})；"
                        f"相似度 {button_score:.3f}，第 {click_attempt} 次嘗試。"
                    )
                    if save_debug:
                        self.save_debug_image(screen, top_left, size, name, button_score)
                self.stop_event.wait(delay)
            except Exception as exc:  # keep the UI usable if a game window changes state
                self.log(f"處理失敗：{exc}")
                self.stop_event.wait(delay)
        self.root.after(0, self._worker_finished, stop_reason)

    def save_debug_image(self, image: np.ndarray, top_left: tuple[int, int], size: tuple[int, int], name: str, score: float) -> None:
        DEBUG.mkdir(exist_ok=True)
        x, y = top_left
        annotated = image.copy()
        cv2.rectangle(annotated, (x, y), (x + size[0], y + size[1]), (0, 255, 0), 2)
        cv2.putText(annotated, f"{name} score={score:.3f}", (10, 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 0), 2)
        filename = DEBUG / f"click_{datetime.now():%Y%m%d_%H%M%S_%f}.png"
        cv2.imwrite(str(filename), annotated)
        self.log(f"已儲存除錯截圖：{filename.name}")

    def _worker_finished(self, stop_reason: str | None = None) -> None:
        self.start_button.configure(state="normal")
        if stop_reason:
            self.status_var.set(stop_reason)
        elif self.stop_event.is_set():
            self.status_var.set("已停止")

    def close(self) -> None:
        self.stop_event.set()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    ClickerApp(root)
    root.mainloop()
