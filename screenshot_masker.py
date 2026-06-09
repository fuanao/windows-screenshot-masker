from __future__ import annotations

import ctypes
import ctypes.wintypes
import asyncio
import io
import json
import math
import os
import re
import sys
import threading
import time
import tempfile
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk
from urllib.parse import quote
from urllib.request import urlopen

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageGrab, ImageTk
import pystray

try:
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.globalization import Language
    from winrt.windows.storage import StorageFile, FileAccessMode
    from winrt.windows.graphics.imaging import BitmapDecoder
except Exception:
    OcrEngine = None
    Language = None
    StorageFile = None
    FileAccessMode = None
    BitmapDecoder = None


APP_NAME = "截图打码工具"
HOTKEY_ID = 0xA51
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
WM_HOTKEY = 0x0312
CF_DIB = 8
GMEM_MOVEABLE = 0x0002

CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ScreenshotMasker"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_HOTKEY = {"ctrl": True, "shift": True, "alt": False, "win": False, "key": "A"}
KEY_OPTIONS = {
    **{chr(code): code for code in range(ord("A"), ord("Z") + 1)},
    **{str(num): ord(str(num)) for num in range(0, 10)},
    **{f"F{num}": 0x6F + num for num in range(1, 13)},
    "Print Screen": 0x2C,
    "Insert": 0x2D,
    "Home": 0x24,
    "End": 0x23,
    "Page Up": 0x21,
    "Page Down": 0x22,
}


def load_config() -> dict:
    config = {"hotkey": DEFAULT_HOTKEY.copy(), "single_window": True}
    try:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("hotkey"), dict):
                hotkey = DEFAULT_HOTKEY.copy()
                hotkey.update(data["hotkey"])
                if hotkey.get("key") not in KEY_OPTIONS:
                    hotkey["key"] = DEFAULT_HOTKEY["key"]
                config["hotkey"] = hotkey
            if isinstance(data, dict) and "single_window" in data:
                config["single_window"] = bool(data.get("single_window"))
    except Exception:
        pass
    return config


def save_config(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def hotkey_modifiers(hotkey: dict) -> int:
    modifiers = 0
    if hotkey.get("alt"):
        modifiers |= MOD_ALT
    if hotkey.get("ctrl"):
        modifiers |= MOD_CONTROL
    if hotkey.get("shift"):
        modifiers |= MOD_SHIFT
    if hotkey.get("win"):
        modifiers |= MOD_WIN
    return modifiers


def hotkey_label(hotkey: dict) -> str:
    parts = []
    if hotkey.get("ctrl"):
        parts.append("Ctrl")
    if hotkey.get("shift"):
        parts.append("Shift")
    if hotkey.get("alt"):
        parts.append("Alt")
    if hotkey.get("win"):
        parts.append("Win")
    parts.append(str(hotkey.get("key", "A")))
    return " + ".join(parts)


def set_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def virtual_screen_rect() -> tuple[int, int, int, int]:
    user32 = ctypes.windll.user32
    left = user32.GetSystemMetrics(76)
    top = user32.GetSystemMetrics(77)
    width = user32.GetSystemMetrics(78)
    height = user32.GetSystemMetrics(79)
    return left, top, width, height


def clamp_region(x: float, y: float, w: float, h: float, max_w: int, max_h: int) -> tuple[int, int, int, int]:
    x0 = max(0, min(int(round(x)), max_w))
    y0 = max(0, min(int(round(y)), max_h))
    x1 = max(0, min(int(round(x + w)), max_w))
    y1 = max(0, min(int(round(y + h)), max_h))
    return min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)


@dataclass
class Region:
    x: int
    y: int
    width: int
    height: int
    effect: str
    shape: str
    color: str = "#000000"
    text: str = ""
    font_size: int = 28
    stroke_width: int = 4
    points: list[tuple[int, int]] | None = None


@dataclass
class CaptureState:
    title: str
    original: Image.Image
    regions: list[Region]


@dataclass
class WordBox:
    text: str
    x: int
    y: int
    width: int
    height: int


def create_tray_image() -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 12, 56, 50), radius=8, fill=(33, 116, 255), outline=(255, 255, 255), width=3)
    draw.rectangle((20, 22, 44, 40), outline=(255, 255, 255), width=4)
    draw.line((14, 14, 50, 50), fill=(255, 255, 255), width=4)
    return image


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts" / "msyh.ttc",
        Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts" / "simhei.ttf",
        Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts" / "arial.ttf",
    ]
    for path in candidates:
        try:
            if path.exists():
                return ImageFont.truetype(str(path), size=max(10, int(size)))
        except Exception:
            pass
    return ImageFont.load_default()


def copy_png_to_clipboard(image: Image.Image) -> None:
    dib_output = io.BytesIO()
    image.convert("RGB").save(dib_output, "BMP")
    dib_data = dib_output.getvalue()[14:]
    dib_output.close()

    png_output = io.BytesIO()
    image.convert("RGBA").save(png_output, "PNG")
    png_data = png_output.getvalue()
    png_output.close()

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [ctypes.wintypes.HWND]
    user32.OpenClipboard.restype = ctypes.wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = ctypes.wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.wintypes.BOOL
    user32.SetClipboardData.argtypes = [ctypes.wintypes.UINT, ctypes.wintypes.HANDLE]
    user32.SetClipboardData.restype = ctypes.wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [ctypes.wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [ctypes.wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = ctypes.wintypes.BOOL
    user32.RegisterClipboardFormatW.argtypes = [ctypes.wintypes.LPCWSTR]
    user32.RegisterClipboardFormatW.restype = ctypes.wintypes.UINT

    def write_clipboard_format(format_id: int, data: bytes) -> None:
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise RuntimeError("无法分配剪贴板内存")
        locked = kernel32.GlobalLock(handle)
        if not locked:
            raise RuntimeError("无法锁定剪贴板内存")
        ctypes.memmove(locked, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(format_id, handle):
            raise RuntimeError("写入剪贴板失败")

    opened = False
    for _ in range(8):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.05)
    if not opened:
        raise RuntimeError("无法打开剪贴板")
    try:
        if not user32.EmptyClipboard():
            raise RuntimeError("清空剪贴板失败")
        write_clipboard_format(CF_DIB, dib_data)
        png_format = user32.RegisterClipboardFormatW("PNG")
        if png_format:
            try:
                write_clipboard_format(png_format, png_data)
            except Exception:
                pass
    finally:
        user32.CloseClipboard()


async def recognize_text_async(image_path: str) -> str:
    if OcrEngine is None:
        raise RuntimeError("当前安装包缺少 Windows OCR 组件")
    file = await StorageFile.get_file_from_path_async(str(Path(image_path).resolve()))
    stream = await file.open_async(FileAccessMode.READ)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    engine = None
    for lang_tag in ("zh-Hans-CN", "en-US"):
        try:
            engine = OcrEngine.try_create_from_language(Language(lang_tag))
            if engine:
                break
        except Exception:
            pass
    if not engine:
        engine = OcrEngine.try_create_from_user_profile_languages()
    if not engine:
        raise RuntimeError("系统没有可用 OCR 语言，请在 Windows 语言设置里安装 OCR 语言包")
    result = await engine.recognize_async(bitmap)
    return result.text.strip()


async def recognize_word_boxes_async(image_path: str) -> list[WordBox]:
    if OcrEngine is None:
        raise RuntimeError("Windows OCR is unavailable.")
    file = await StorageFile.get_file_from_path_async(str(Path(image_path).resolve()))
    stream = await file.open_async(FileAccessMode.READ)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    engine = None
    for lang_tag in ("zh-Hans-CN", "en-US"):
        try:
            engine = OcrEngine.try_create_from_language(Language(lang_tag))
            if engine:
                break
        except Exception:
            pass
    if not engine:
        engine = OcrEngine.try_create_from_user_profile_languages()
    if not engine:
        raise RuntimeError("No Windows OCR language is available.")
    result = await engine.recognize_async(bitmap)
    words: list[WordBox] = []
    for line in result.lines:
        for word in line.words:
            rect = word.bounding_rect
            words.append(
                WordBox(
                    text=str(word.text),
                    x=int(round(rect.x)),
                    y=int(round(rect.y)),
                    width=max(1, int(round(rect.width))),
                    height=max(1, int(round(rect.height))),
                )
            )
    return words


def recognize_text(image: Image.Image) -> str:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        path = tmp.name
    try:
        image.convert("RGB").save(path, "PNG")
        return asyncio.run(recognize_text_async(path))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def recognize_word_boxes(image: Image.Image) -> list[WordBox]:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        path = tmp.name
    try:
        image.convert("RGB").save(path, "PNG")
        return asyncio.run(recognize_word_boxes_async(path))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


SENSITIVE_PATTERNS = [
    re.compile(r"1[3-9]\d{9}"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    re.compile(r"\b\d{17}[\dXx]\b"),
    re.compile(r"\b\d{15}\b"),
]


def looks_sensitive(text: str) -> bool:
    compact = re.sub(r"\s+", "", text).replace("-", "").replace("_", "")
    return any(pattern.search(compact) for pattern in SENSITIVE_PATTERNS)


def translate_text(text: str, target: str = "zh-CN") -> str:
    text = text.strip()
    if not text:
        return ""
    url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl={target}&dt=t&q={quote(text)}"
    with urlopen(url, timeout=12) as response:
        data = json.loads(response.read().decode("utf-8"))
    return "".join(part[0] for part in data[0] if part and part[0]).strip()


class HotkeyThread(threading.Thread):
    def __init__(self, on_hotkey, hotkey: dict):
        super().__init__(daemon=True)
        self.on_hotkey = on_hotkey
        self.hotkey = hotkey.copy()
        self.thread_id = None
        self.registration_error = None
        self.ready_event = threading.Event()
        self._stop_event = threading.Event()

    def run(self) -> None:
        user32 = ctypes.windll.user32
        self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        label = hotkey_label(self.hotkey)
        registered = user32.RegisterHotKey(None, HOTKEY_ID, hotkey_modifiers(self.hotkey), KEY_OPTIONS[self.hotkey["key"]])
        if not registered:
            self.registration_error = f"快捷键 {label} 注册失败，可能已被其他程序占用。请在托盘菜单里更换快捷键。"
            self.ready_event.set()
            self.on_hotkey(error=self.registration_error)
            return
        self.ready_event.set()
        msg = ctypes.wintypes.MSG()
        try:
            while not self._stop_event.is_set() and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self.on_hotkey()
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)

    def stop(self) -> None:
        self._stop_event.set()
        if self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)


class CaptureOverlay:
    def __init__(self, app: "ScreenshotMaskerApp", screenshot: Image.Image, on_region=None, tip_text: str | None = None, allow_fullscreen: bool = True):
        self.app = app
        self.screenshot = screenshot
        self.on_region = on_region
        self.allow_fullscreen = allow_fullscreen
        self.left, self.top, self.width, self.height = virtual_screen_rect()
        self.start_x = 0
        self.start_y = 0
        self.rect_id = None
        self.cursor_ids: list[int] = []
        self.toolbar_bounds: tuple[int, int, int, int] | None = None
        self.full_button_bounds: tuple[int, int, int, int] | None = None

        self.window = tk.Toplevel(app.root)
        self.window.title("选择截图区域")
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.geometry(f"{self.width}x{self.height}{self.left:+d}{self.top:+d}")
        self.window.focus_force()

        self.canvas = tk.Canvas(self.window, width=self.width, height=self.height, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.photo = ImageTk.PhotoImage(screenshot)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.canvas.create_rectangle(0, 0, self.width, self.height, fill="#000000", stipple="gray50", outline="")
        default_tip = f"拖动选择截图区域，Esc 取消。快捷键：{app.hotkey_label()}"
        if allow_fullscreen:
            self.draw_capture_toolbar()
            default_tip = f"默认区域截图：按住鼠标拖动选择范围。Enter/F 全屏截图，Esc 取消。"
        self.tip = self.canvas.create_text(
            self.width // 2,
            96 if allow_fullscreen else 36,
            text=tip_text or default_tip,
            fill="white",
            font=("Microsoft YaHei UI", 16, "bold"),
        )

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<Motion>", self.on_motion)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.window.bind("<Escape>", lambda _event: self.cancel())
        if self.allow_fullscreen and self.on_region is None:
            self.window.bind("<Return>", self.capture_fullscreen)
            self.window.bind("<f>", self.capture_fullscreen)
            self.window.bind("<F>", self.capture_fullscreen)

    def draw_capture_toolbar(self) -> None:
        bar_w = min(700, max(460, self.width - 80))
        bar_h = 54
        bar_x0 = (self.width - bar_w) // 2
        bar_y0 = 22
        bar_x1 = bar_x0 + bar_w
        bar_y1 = bar_y0 + bar_h
        self.toolbar_bounds = (bar_x0, bar_y0, bar_x1, bar_y1)
        self.canvas.create_rectangle(bar_x0, bar_y0, bar_x1, bar_y1, fill="#111827", outline="#ffffff", width=1)

        region_x0 = bar_x0 + 14
        region_x1 = region_x0 + 150
        self.canvas.create_rectangle(region_x0, bar_y0 + 10, region_x1, bar_y1 - 10, fill="#2563eb", outline="")
        self.canvas.create_text((region_x0 + region_x1) // 2, (bar_y0 + bar_y1) // 2, text="区域截图 默认", fill="white", font=("Microsoft YaHei UI", 11, "bold"))

        full_x0 = region_x1 + 10
        full_x1 = full_x0 + 110
        self.full_button_bounds = (full_x0, bar_y0 + 10, full_x1, bar_y1 - 10)
        self.canvas.create_rectangle(full_x0, bar_y0 + 10, full_x1, bar_y1 - 10, fill="#374151", outline="", tags=("fullscreen_button",))
        self.canvas.create_text((full_x0 + full_x1) // 2, (bar_y0 + bar_y1) // 2, text="全屏", fill="white", font=("Microsoft YaHei UI", 11), tags=("fullscreen_button",))
        self.canvas.tag_bind("fullscreen_button", "<Button-1>", self.capture_fullscreen)

        hint = "拖动鼠标框选范围"
        self.canvas.create_text(full_x1 + 28, (bar_y0 + bar_y1) // 2, anchor="w", text=hint, fill="#d1d5db", font=("Microsoft YaHei UI", 10))

    def draw_pointer(self, x: float, y: float) -> None:
        for item_id in self.cursor_ids:
            self.canvas.delete(item_id)
        size = 16
        self.cursor_ids = [
            self.canvas.create_line(x - size, y, x + size, y, fill="white", width=5),
            self.canvas.create_line(x, y - size, x, y + size, fill="white", width=5),
            self.canvas.create_line(x - size, y, x + size, y, fill="#ff3344", width=2),
            self.canvas.create_line(x, y - size, x, y + size, fill="#ff3344", width=2),
            self.canvas.create_oval(x - 4, y - 4, x + 4, y + 4, outline="white", width=2),
        ]

    def on_motion(self, event) -> None:
        self.draw_pointer(self.canvas.canvasx(event.x), self.canvas.canvasy(event.y))

    def on_press(self, event) -> None:
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        if self.full_button_bounds:
            x0, y0, x1, y1 = self.full_button_bounds
            if x0 <= x <= x1 and y0 <= y <= y1:
                self.capture_fullscreen()
                return
        if self.toolbar_bounds:
            x0, y0, x1, y1 = self.toolbar_bounds
            if x0 <= x <= x1 and y0 <= y <= y1:
                return
        self.start_x = x
        self.start_y = y
        self.draw_pointer(self.start_x, self.start_y)
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(
            self.start_x,
            self.start_y,
            self.start_x,
            self.start_y,
            outline="#ff3344",
            width=3,
        )

    def on_drag(self, event) -> None:
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        self.draw_pointer(x, y)
        if not self.rect_id:
            return
        self.canvas.coords(self.rect_id, self.start_x, self.start_y, x, y)

    def on_release(self, event) -> None:
        end_x = self.canvas.canvasx(event.x)
        end_y = self.canvas.canvasy(event.y)
        x0, y0 = min(self.start_x, end_x), min(self.start_y, end_y)
        x1, y1 = max(self.start_x, end_x), max(self.start_y, end_y)
        if x1 - x0 < 8 or y1 - y0 < 8:
            self.cancel()
            return
        if self.on_region:
            self.window.destroy()
            self.app.release_capture_lock()
            self.on_region((int(x0), int(y0), int(x1), int(y1)))
            return
        crop = self.screenshot.crop((int(x0), int(y0), int(x1), int(y1)))
        self.window.destroy()
        self.app.release_capture_lock()
        self.app.copy_capture_and_open_editor(crop)

    def capture_fullscreen(self, _event=None):
        if not self.allow_fullscreen or self.on_region is not None:
            return "break"
        self.window.destroy()
        self.app.release_capture_lock()
        self.app.copy_capture_and_open_editor(self.screenshot.convert("RGBA"))
        return "break"

    def cancel(self) -> None:
        self.window.destroy()
        self.app.release_capture_lock()


class EditorWindow:
    def __init__(self, app: "ScreenshotMaskerApp", image: Image.Image):
        self.app = app
        self.captures: list[CaptureState] = [CaptureState("截图 1", image.convert("RGBA"), [])]
        self.active_capture_index = 0
        self.original = self.captures[0].original
        self.regions = self.captures[0].regions
        self.current_effect = tk.StringVar(value="outline")
        self.current_shape = tk.StringVar(value="rect")
        self.current_color = tk.StringVar(value="#ff3344")
        self.font_size = tk.IntVar(value=28)
        self.stroke_width = tk.IntVar(value=4)
        self.status_text = tk.StringVar(value="默认框选工具。每次修改都会自动更新剪贴板，可直接到聊天窗口 Ctrl+V。")
        self.scale = 1.0
        self.start = None
        self.preview_id = None
        self.text_entry = None
        self.text_entry_window = None
        self.text_anchor = None
        self.editing_text_index = None
        self.effect_buttons = {}
        self.shape_buttons = {}
        self.color_swatches = []
        self.capture_listbox = None
        self.image_offset_x = 0
        self.image_offset_y = 0
        self.display_photo = None
        self.display_image = None

        self.window = tk.Toplevel(app.root)
        self.window.title(f"{APP_NAME} - 编辑截图")
        self.window.geometry("1180x800")
        self.window.minsize(920, 620)
        self.window.configure(bg="#f4f6f8")
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        self.build_ui()
        self.refresh_capture_list()
        self.fit_to_window()
        self.window.update_idletasks()
        self.render()

    def build_ui(self) -> None:
        style = ttk.Style(self.window)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("App.TSpinbox", arrowsize=12)

        shell = tk.Frame(self.window, bg="#f4f6f8")
        shell.pack(fill="both", expand=True)

        header = tk.Frame(shell, bg="#ffffff", height=64)
        header.pack(side="top", fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="截图编辑", bg="#ffffff", fg="#111827", font=("Microsoft YaHei UI", 15, "bold")).pack(side="left", padx=(18, 6))
        tk.Label(header, text="截图已在剪贴板，可直接粘贴；编辑后按 Ctrl+C 复制最终图片", bg="#ffffff", fg="#6b7280", font=("Microsoft YaHei UI", 9)).pack(side="left")

        action_bar = tk.Frame(header, bg="#ffffff")
        action_bar.pack(side="right", padx=14)
        tk.Button(action_bar, text="OCR", command=self.run_ocr, bg="#eef2f7", fg="#111827", activebackground="#e5e7eb", relief="flat", padx=12, pady=7, font=("Microsoft YaHei UI", 10), cursor="hand2").pack(side="left", padx=(0, 8))
        tk.Button(action_bar, text="翻译", command=self.run_translate_from_ocr, bg="#eef2f7", fg="#111827", activebackground="#e5e7eb", relief="flat", padx=12, pady=7, font=("Microsoft YaHei UI", 10), cursor="hand2").pack(side="left", padx=(0, 8))
        tk.Button(action_bar, text="智能打码", command=self.run_smart_mask, bg="#111827", fg="white", activebackground="#374151", activeforeground="white", relief="flat", padx=12, pady=7, font=("Microsoft YaHei UI", 10), cursor="hand2").pack(side="left", padx=(0, 8))
        tk.Button(action_bar, text="复制到剪贴板", command=self.copy_image, bg="#2563eb", fg="white", activebackground="#1d4ed8", activeforeground="white", relief="flat", padx=14, pady=7, font=("Microsoft YaHei UI", 10, "bold"), cursor="hand2").pack(side="left", padx=(0, 8))
        tk.Button(action_bar, text="保存 PNG", command=self.save_png, bg="#eef2f7", fg="#111827", activebackground="#e5e7eb", relief="flat", padx=12, pady=7, font=("Microsoft YaHei UI", 10), cursor="hand2").pack(side="left")
        tk.Button(action_bar, text="关闭", command=self.close, bg="#fee2e2", fg="#991b1b", activebackground="#fecaca", relief="flat", padx=12, pady=7, font=("Microsoft YaHei UI", 10), cursor="hand2").pack(side="left", padx=(8, 0))

        toolbar = tk.Frame(shell, bg="#f4f6f8", padx=14, pady=10)
        toolbar.pack(side="top", fill="x")

        def group(parent, title: str) -> tk.Frame:
            outer = tk.Frame(parent, bg="#ffffff", highlightbackground="#d7dee8", highlightthickness=1)
            outer.pack(side="left", padx=(0, 10))
            tk.Label(outer, text=title, bg="#ffffff", fg="#6b7280", font=("Microsoft YaHei UI", 8, "bold")).pack(anchor="w", padx=10, pady=(6, 0))
            inner = tk.Frame(outer, bg="#ffffff", padx=8, pady=7)
            inner.pack()
            return inner

        def tool_button(parent, icon: str, label: str, value: str) -> None:
            btn = tk.Button(
                parent,
                text=f"{icon}\n{label}",
                command=lambda v=value: self.select_effect(v),
                bg="#f8fafc",
                fg="#111827",
                relief="flat",
                width=7,
                height=2,
                padx=4,
                pady=4,
                activebackground="#e5edf8",
                activeforeground="#111827",
                font=("Microsoft YaHei UI", 9),
                cursor="hand2",
            )
            btn.pack(side="left", padx=3)
            self.effect_buttons[value] = btn

        def shape_button(parent, icon: str, label: str, value: str) -> None:
            btn = tk.Button(
                parent,
                text=f"{icon}\n{label}",
                command=lambda v=value: self.select_shape(v),
                bg="#f8fafc",
                fg="#111827",
                relief="flat",
                width=6,
                height=2,
                padx=4,
                pady=4,
                activebackground="#e5edf8",
                activeforeground="#111827",
                font=("Microsoft YaHei UI", 9),
                cursor="hand2",
            )
            btn.pack(side="left", padx=3)
            self.shape_buttons[value] = btn

        def color_swatch(parent, color: str) -> None:
            btn = tk.Button(parent, text="", command=lambda c=color: self.set_color(c), bg=color, activebackground=color, relief="flat", width=2, cursor="hand2")
            btn.pack(side="left", padx=3, ipady=8)
            self.color_swatches.append(btn)

        shape_group = group(toolbar, "形状")
        shape_button(shape_group, "▭", "矩形", "rect")
        shape_button(shape_group, "◯", "圆形", "ellipse")

        effect_group = group(toolbar, "工具")
        tool_button(effect_group, "■", "遮挡", "black")
        tool_button(effect_group, "≈", "模糊", "blur")
        tool_button(effect_group, "▦", "马赛克", "mosaic")
        tool_button(effect_group, "□", "框选", "outline")
        tool_button(effect_group, "→", "箭头", "arrow")
        tool_button(effect_group, "／", "直线", "line")
        tool_button(effect_group, "✎", "画笔", "pen")
        tool_button(effect_group, "▰", "高亮", "highlight")
        tool_button(effect_group, "T", "文字", "text")

        style_group = group(toolbar, "样式")
        for swatch in ("#000000", "#ff3344", "#2563eb", "#16a34a", "#f59e0b", "#ffffff"):
            color_swatch(style_group, swatch)
        self.color_button = tk.Button(style_group, text="更多", command=self.choose_color, bg="#f8fafc", fg="#111827", activebackground="#e5e7eb", relief="flat", padx=8, pady=5, font=("Microsoft YaHei UI", 9), cursor="hand2")
        self.color_button.pack(side="left", padx=(0, 8), ipady=8)
        tk.Label(style_group, text="字号", bg="#ffffff", fg="#374151", font=("Microsoft YaHei UI", 9)).pack(side="left")
        ttk.Spinbox(style_group, from_=10, to=96, width=4, textvariable=self.font_size, style="App.TSpinbox").pack(side="left", padx=(5, 0))
        tk.Label(style_group, text="线宽", bg="#ffffff", fg="#374151", font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(8, 0))
        ttk.Spinbox(style_group, from_=1, to=40, width=4, textvariable=self.stroke_width, style="App.TSpinbox").pack(side="left", padx=(5, 0))

        edit_group = group(toolbar, "操作")
        tk.Button(edit_group, text="撤销", command=self.undo, bg="#f8fafc", fg="#111827", activebackground="#e5e7eb", relief="flat", padx=10, pady=5, font=("Microsoft YaHei UI", 9), cursor="hand2").pack(side="left", padx=2)
        tk.Button(edit_group, text="清空", command=self.clear, bg="#f8fafc", fg="#111827", activebackground="#e5e7eb", relief="flat", padx=10, pady=5, font=("Microsoft YaHei UI", 9), cursor="hand2").pack(side="left", padx=2)
        tk.Button(edit_group, text="放大", command=lambda: self.zoom(1.2), bg="#f8fafc", fg="#111827", activebackground="#e5e7eb", relief="flat", padx=10, pady=5, font=("Microsoft YaHei UI", 9), cursor="hand2").pack(side="left", padx=2)
        tk.Button(edit_group, text="缩小", command=lambda: self.zoom(1 / 1.2), bg="#f8fafc", fg="#111827", activebackground="#e5e7eb", relief="flat", padx=10, pady=5, font=("Microsoft YaHei UI", 9), cursor="hand2").pack(side="left", padx=2)
        self.refresh_tool_buttons()

        content = tk.Frame(shell, bg="#f4f6f8")
        content.pack(fill="both", expand=True, padx=10, pady=(0, 0))

        sidebar = tk.Frame(content, bg="#ffffff", width=150, highlightbackground="#d7dee8", highlightthickness=1)
        sidebar.pack(side="left", fill="y", padx=(0, 10))
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="截图列表", bg="#ffffff", fg="#6b7280", font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w", padx=10, pady=(10, 6))
        self.capture_listbox = tk.Listbox(
            sidebar,
            bg="#ffffff",
            fg="#111827",
            activestyle="none",
            selectbackground="#dbeafe",
            selectforeground="#1d4ed8",
            relief="flat",
            highlightthickness=0,
            font=("Microsoft YaHei UI", 10),
            exportselection=False,
        )
        self.capture_listbox.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.capture_listbox.bind("<<ListboxSelect>>", self.on_capture_selected)
        tk.Button(sidebar, text="删除当前截图", command=self.delete_current_capture, bg="#f8fafc", fg="#991b1b", activebackground="#fee2e2", relief="flat", padx=8, pady=6, font=("Microsoft YaHei UI", 9), cursor="hand2").pack(fill="x", padx=8, pady=(0, 8))

        frame = tk.Frame(content, bg="#eef2f7", padx=1, pady=1)
        frame.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(frame, bg="#f8fafc", highlightthickness=0, cursor="crosshair")
        hbar = ttk.Scrollbar(frame, orient="horizontal", command=self.canvas.xview)
        vbar = ttk.Scrollbar(frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=hbar.set, yscrollcommand=vbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Configure>", lambda _event: self.render() if self.display_image is not None else None)
        self.window.bind("<Control-c>", lambda _event: self.copy_image())
        self.window.bind("<Control-C>", lambda _event: self.copy_image())
        self.window.bind("<Return>", self.on_window_return)
        self.window.bind("<Control-s>", lambda _event: self.save_png())
        self.window.bind("<Control-S>", lambda _event: self.save_png())
        self.window.bind("<Control-z>", self.on_shortcut_undo)
        self.window.bind("<Control-Z>", self.on_shortcut_undo)
        self.window.bind("<Control-plus>", self.on_shortcut_zoom_in)
        self.window.bind("<Control-equal>", self.on_shortcut_zoom_in)
        self.window.bind("<Control-KP_Add>", self.on_shortcut_zoom_in)
        self.window.bind("<Control-minus>", self.on_shortcut_zoom_out)
        self.window.bind("<Control-KP_Subtract>", self.on_shortcut_zoom_out)
        self.window.bind("<Control-0>", self.on_shortcut_fit)
        self.window.bind("<Control-MouseWheel>", self.on_shortcut_mousewheel_zoom)

        status = tk.Frame(shell, bg="#ffffff", height=34)
        status.pack(side="bottom", fill="x")
        status.pack_propagate(False)
        tk.Label(status, textvariable=self.status_text, bg="#ffffff", fg="#374151", font=("Microsoft YaHei UI", 9)).pack(side="left", padx=14)

    def close(self) -> None:
        self.app.editor_window = None
        self.window.destroy()

    def refresh_capture_list(self) -> None:
        if not self.capture_listbox:
            return
        self.capture_listbox.delete(0, "end")
        for index, capture in enumerate(self.captures):
            suffix = " *" if capture.regions else ""
            self.capture_listbox.insert("end", f"{index + 1}. {capture.title}{suffix}")
        self.capture_listbox.selection_clear(0, "end")
        self.capture_listbox.selection_set(self.active_capture_index)
        self.capture_listbox.activate(self.active_capture_index)

    def on_capture_selected(self, _event) -> None:
        if not self.capture_listbox:
            return
        selection = self.capture_listbox.curselection()
        if not selection:
            return
        self.switch_capture(selection[0])

    def switch_capture(self, index: int) -> None:
        if index == self.active_capture_index or not (0 <= index < len(self.captures)):
            return
        self.commit_text_entry(silent=True)
        self.active_capture_index = index
        active = self.captures[index]
        self.original = active.original
        self.regions = active.regions
        self.start = None
        self.preview_id = None
        self.fit_to_window()
        self.render()
        self.refresh_capture_list()
        self.sync_clipboard("已切换截图，并已更新剪贴板。")

    def delete_current_capture(self) -> None:
        if not self.captures:
            self.close()
            return
        self.commit_text_entry(silent=True)
        self.captures.pop(self.active_capture_index)
        if not self.captures:
            self.close()
            return
        self.active_capture_index = min(self.active_capture_index, len(self.captures) - 1)
        active = self.captures[self.active_capture_index]
        self.original = active.original
        self.regions = active.regions
        self.start = None
        self.preview_id = None
        self.fit_to_window()
        self.render()
        self.refresh_capture_list()
        self.sync_clipboard("已删除当前截图，并已切换到列表中的图片。")

    def add_capture(self, image: Image.Image, copied: bool = False, error: Exception | None = None) -> None:
        self.commit_text_entry(silent=True)
        title = f"截图 {len(self.captures) + 1}"
        self.captures.append(CaptureState(title, image.convert("RGBA"), []))
        self.active_capture_index = len(self.captures) - 1
        active = self.captures[self.active_capture_index]
        self.original = active.original
        self.regions = active.regions
        self.start = None
        self.preview_id = None
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()
        self.fit_to_window()
        self.render()
        self.refresh_capture_list()
        if copied:
            self.status_text.set("新截图已加入列表，并已复制到剪贴板。")
        elif error:
            self.status_text.set(f"新截图已加入列表，但自动复制失败：{error}")
        else:
            self.sync_clipboard("新截图已加入列表，并已更新剪贴板。")

    def choose_color(self) -> None:
        color = colorchooser.askcolor(color=self.current_color.get(), parent=self.window, title="选择颜色")
        if color and color[1]:
            self.set_color(color[1])

    def set_color(self, color: str) -> None:
        self.current_color.set(color)
        self.color_button.configure(bg=color, activebackground=color, fg="white" if self.is_dark_color(color) else "black")
        self.refresh_tool_buttons()

    def select_effect(self, effect: str) -> None:
        self.commit_text_entry(silent=True)
        self.current_effect.set(effect)
        if effect == "text":
            self.status_text.set("文字工具：在图片上点击后直接输入，Enter 落字，Esc 取消。")
        else:
            self.status_text.set("在图片上拖动添加区域。完成后按 Ctrl+C 复制最终图片。")
        self.refresh_tool_buttons()

    def select_shape(self, shape: str) -> None:
        self.commit_text_entry(silent=True)
        self.current_shape.set(shape)
        self.refresh_tool_buttons()

    def refresh_tool_buttons(self) -> None:
        for value, button in self.effect_buttons.items():
            selected = value == self.current_effect.get()
            button.configure(
                bg="#dbeafe" if selected else "#f8fafc",
                fg="#1d4ed8" if selected else "#111827",
                activebackground="#bfdbfe" if selected else "#e5edf8",
            )
        for value, button in self.shape_buttons.items():
            selected = value == self.current_shape.get()
            button.configure(
                bg="#dbeafe" if selected else "#f8fafc",
                fg="#1d4ed8" if selected else "#111827",
                activebackground="#bfdbfe" if selected else "#e5edf8",
            )

    def is_dark_color(self, color: str) -> bool:
        color = color.lstrip("#")
        if len(color) != 6:
            return True
        r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
        return (r * 299 + g * 587 + b * 114) / 1000 < 150

    def find_text_region_at(self, x: int, y: int) -> int | None:
        probe = Image.new("RGBA", (1, 1))
        draw = ImageDraw.Draw(probe)
        for index in range(len(self.regions) - 1, -1, -1):
            region = self.regions[index]
            if region.effect != "text" or not region.text.strip():
                continue
            font = load_font(region.font_size)
            bbox = draw.textbbox((region.x, region.y), region.text, font=font)
            padding = max(6, region.font_size // 4)
            if bbox[0] - padding <= x <= bbox[2] + padding and bbox[1] - padding <= y <= bbox[3] + padding:
                return index
        return None

    def start_text_entry(self, event) -> None:
        self.cancel_text_entry()
        original_x, original_y = self.display_to_original(event)
        existing_index = self.find_text_region_at(original_x, original_y)
        existing_text = ""
        if existing_index is not None:
            region = self.regions[existing_index]
            original_x, original_y = region.x, region.y
            existing_text = region.text
            self.font_size.set(region.font_size)
            self.set_color(region.color)
            self.editing_text_index = existing_index
        else:
            self.editing_text_index = None
        display_x = original_x * self.scale + self.image_offset_x
        display_y = original_y * self.scale + self.image_offset_y
        display_font_size = max(10, int(self.font_size.get() * self.scale))
        self.text_anchor = (original_x, original_y)
        self.text_entry = tk.Entry(
            self.canvas,
            font=("Microsoft YaHei UI", display_font_size),
            fg=self.current_color.get(),
            bg="white",
            insertbackground=self.current_color.get(),
            relief="solid",
            bd=1,
        )
        self.text_entry_window = self.canvas.create_window(display_x, display_y, anchor="nw", window=self.text_entry, width=260)
        self.text_entry.insert(0, existing_text)
        self.text_entry.selection_range(0, "end")
        self.text_entry.focus_set()
        self.status_text.set("输入文字后点其他位置会自动保存，Esc 取消。")
        self.text_entry.bind("<Return>", self.on_text_entry_return)
        self.text_entry.bind("<Escape>", self.on_text_entry_escape)
        self.text_entry.bind("<FocusOut>", lambda _event: self.commit_text_entry(silent=True))
        self.window.after(50, self.text_entry.focus_force)

    def on_text_entry_return(self, _event):
        self.commit_text_entry()
        return "break"

    def on_text_entry_escape(self, _event):
        self.cancel_text_entry()
        self.status_text.set("已取消文字输入。")
        return "break"

    def commit_text_entry(self, silent: bool = False) -> None:
        if not self.text_entry or not self.text_anchor:
            return
        text = self.text_entry.get().strip()
        x, y = self.text_anchor
        editing_index = self.editing_text_index
        self.cancel_text_entry(render=False)
        if not text:
            if editing_index is not None and 0 <= editing_index < len(self.regions):
                self.regions.pop(editing_index)
                self.render()
                self.sync_clipboard("文字已删除，并已更新剪贴板。")
            else:
                if not silent:
                    self.status_text.set("文字为空，已取消。")
            return
        size = max(10, min(96, int(self.font_size.get() or 28)))
        new_region = Region(
            x=x,
            y=y,
            width=1,
            height=1,
            effect="text",
            shape="rect",
            color=self.current_color.get(),
            text=text,
            font_size=size,
        )
        if editing_index is not None and 0 <= editing_index < len(self.regions):
            self.regions[editing_index] = new_region
        else:
            self.regions.append(new_region)
        self.render()
        self.sync_clipboard("文字已保存，并已更新剪贴板。")

    def cancel_text_entry(self, render: bool = False) -> None:
        entry = self.text_entry
        entry_window = self.text_entry_window
        self.text_entry = None
        self.text_entry_window = None
        self.text_anchor = None
        self.editing_text_index = None
        if entry_window:
            self.canvas.delete(entry_window)
        if entry:
            try:
                entry.destroy()
            except tk.TclError:
                pass
        if render:
            self.render()

    def fit_to_window(self) -> None:
        max_w = 1040
        max_h = 640
        self.scale = min(1.0, max_w / self.original.width, max_h / self.original.height)

    def zoom(self, factor: float) -> None:
        self.scale = max(0.1, min(4.0, self.scale * factor))
        self.render()

    def on_shortcut_undo(self, event=None):
        if self.text_entry and self.text_entry.focus_get() == self.text_entry:
            return None
        self.undo()
        return "break"

    def on_shortcut_zoom_in(self, event=None):
        self.commit_text_entry(silent=True)
        self.zoom(1.2)
        self.status_text.set(f"已放大到 {round(self.scale * 100)}%。")
        return "break"

    def on_shortcut_zoom_out(self, event=None):
        self.commit_text_entry(silent=True)
        self.zoom(1 / 1.2)
        self.status_text.set(f"已缩小到 {round(self.scale * 100)}%。")
        return "break"

    def on_shortcut_fit(self, event=None):
        self.commit_text_entry(silent=True)
        self.fit_to_window()
        self.render()
        self.status_text.set(f"已适应窗口，当前缩放 {round(self.scale * 100)}%。")
        return "break"

    def on_shortcut_mousewheel_zoom(self, event):
        if event.delta > 0:
            return self.on_shortcut_zoom_in(event)
        return self.on_shortcut_zoom_out(event)

    def render_final(self) -> Image.Image:
        image = self.original.copy()
        for region in self.regions:
            self.apply_region(image, region)
        return image.convert("RGBA")

    def apply_region(self, image: Image.Image, region: Region) -> None:
        if region.effect in {"line", "arrow"}:
            draw = ImageDraw.Draw(image)
            width = max(1, int(region.stroke_width or 4))
            x0 = max(0, min(int(region.x), image.width))
            y0 = max(0, min(int(region.y), image.height))
            x1 = max(0, min(int(region.x + region.width), image.width))
            y1 = max(0, min(int(region.y + region.height), image.height))
            draw.line((x0, y0, x1, y1), fill=region.color, width=width)
            if region.effect == "arrow":
                self.draw_arrow_head(draw, x0, y0, x1, y1, region.color, width)
            return

        if region.effect in {"pen", "highlight"} and region.points:
            width = max(1, int(region.stroke_width or 4))
            points = [(max(0, min(int(x), image.width)), max(0, min(int(y), image.height))) for x, y in region.points]
            if len(points) < 2:
                return
            if region.effect == "highlight":
                overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
                overlay_draw = ImageDraw.Draw(overlay)
                overlay_draw.line(points, fill=self.hex_to_rgba(region.color, 95), width=max(8, width * 4), joint="curve")
                image.alpha_composite(overlay)
                return
            draw = ImageDraw.Draw(image)
            draw.line(points, fill=region.color, width=width, joint="curve")
            return

        x, y, w, h = clamp_region(region.x, region.y, region.width, region.height, image.width, image.height)
        if region.effect == "text":
            if not region.text.strip():
                return
            draw = ImageDraw.Draw(image)
            font = load_font(region.font_size)
            draw.text((x, y), region.text, fill=region.color, font=font)
            return
        if w <= 0 or h <= 0:
            return
        box = (x, y, x + w, y + h)
        mask = Image.new("L", (w, h), 0)
        mask_draw = ImageDraw.Draw(mask)
        if region.shape == "ellipse":
            mask_draw.ellipse((0, 0, w - 1, h - 1), fill=255)
        else:
            mask_draw.rectangle((0, 0, w, h), fill=255)

        if region.effect == "black":
            fill = Image.new("RGBA", (w, h), region.color)
            image.paste(fill, box, mask)
            return

        if region.effect == "blur":
            crop = image.crop(box).filter(ImageFilter.GaussianBlur(radius=max(8, min(w, h) // 6)))
            image.paste(crop, box, mask)
            return

        if region.effect == "mosaic":
            crop = image.crop(box)
            block = max(6, min(24, min(w, h) // 6 or 6))
            small_w = max(1, math.ceil(w / block))
            small_h = max(1, math.ceil(h / block))
            mosaic = crop.resize((small_w, small_h), Image.Resampling.BILINEAR).resize((w, h), Image.Resampling.NEAREST)
            image.paste(mosaic, box, mask)
            return

        draw = ImageDraw.Draw(image)
        outline_width = max(1, int(region.stroke_width or self.stroke_width.get() or 4))
        if region.shape == "ellipse":
            draw.ellipse((x, y, x + w, y + h), outline=region.color, width=outline_width)
        else:
            draw.rectangle((x, y, x + w, y + h), outline=region.color, width=outline_width)

    def hex_to_rgba(self, color: str, alpha: int) -> tuple[int, int, int, int]:
        color = color.lstrip("#")
        if len(color) != 6:
            return 255, 230, 0, alpha
        return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16), alpha

    def draw_arrow_head(self, draw: ImageDraw.ImageDraw, x0: int, y0: int, x1: int, y1: int, color: str, width: int) -> None:
        angle = math.atan2(y1 - y0, x1 - x0)
        length = max(12, width * 4)
        spread = math.radians(28)
        left = (
            x1 - length * math.cos(angle - spread),
            y1 - length * math.sin(angle - spread),
        )
        right = (
            x1 - length * math.cos(angle + spread),
            y1 - length * math.sin(angle + spread),
        )
        draw.line((x1, y1, left[0], left[1]), fill=color, width=width)
        draw.line((x1, y1, right[0], right[1]), fill=color, width=width)

    def render(self) -> None:
        final = self.render_final()
        display_size = (max(1, round(final.width * self.scale)), max(1, round(final.height * self.scale)))
        self.display_image = final.resize(display_size, Image.Resampling.LANCZOS)
        self.display_photo = ImageTk.PhotoImage(self.display_image)
        self.canvas.delete("all")
        canvas_w = max(self.canvas.winfo_width(), 1)
        canvas_h = max(self.canvas.winfo_height(), 1)
        self.image_offset_x = max(0, (canvas_w - display_size[0]) // 2)
        self.image_offset_y = max(0, (canvas_h - display_size[1]) // 2)
        self.canvas.create_image(self.image_offset_x, self.image_offset_y, anchor="nw", image=self.display_photo)
        self.canvas.config(scrollregion=(0, 0, max(display_size[0] + self.image_offset_x * 2, canvas_w), max(display_size[1] + self.image_offset_y * 2, canvas_h)))

    def display_to_original(self, event) -> tuple[int, int]:
        x = (self.canvas.canvasx(event.x) - self.image_offset_x) / self.scale
        y = (self.canvas.canvasy(event.y) - self.image_offset_y) / self.scale
        x = max(0, min(x, self.original.width))
        y = max(0, min(y, self.original.height))
        return int(round(x)), int(round(y))

    def on_press(self, event) -> None:
        if self.current_effect.get() == "text":
            if self.text_entry:
                x, y = self.display_to_original(event)
                editing_index = self.editing_text_index
                if editing_index is not None and 0 <= editing_index < len(self.regions):
                    hit_index = self.find_text_region_at(x, y)
                    if hit_index == editing_index:
                        return
                self.commit_text_entry(silent=True)
            self.start_text_entry(event)
            return
        self.commit_text_entry(silent=True)
        point = self.display_to_original(event)
        if self.current_effect.get() in {"pen", "highlight"}:
            self.start = [point]
        else:
            self.start = point
        if self.preview_id:
            self.canvas.delete(self.preview_id)
            self.preview_id = None
        self.canvas.delete("preview")

    def on_drag(self, event) -> None:
        if self.current_effect.get() == "text":
            return
        if not self.start:
            return
        if self.current_effect.get() in {"pen", "highlight"}:
            points = self.start
            if not isinstance(points, list):
                return
            x1, y1 = self.display_to_original(event)
            x0, y0 = points[-1]
            points.append((x1, y1))
            sx0 = x0 * self.scale + self.image_offset_x
            sy0 = y0 * self.scale + self.image_offset_y
            sx1 = x1 * self.scale + self.image_offset_x
            sy1 = y1 * self.scale + self.image_offset_y
            width = max(1, int(self.stroke_width.get() * self.scale))
            if self.current_effect.get() == "highlight":
                width = max(8, width * 4)
            self.canvas.create_line(sx0, sy0, sx1, sy1, fill=self.current_color.get(), width=width, capstyle="round", smooth=True, tags="preview")
            return
        if isinstance(self.start, list):
            return
        x0, y0 = self.start
        x1, y1 = self.display_to_original(event)
        sx0 = x0 * self.scale + self.image_offset_x
        sy0 = y0 * self.scale + self.image_offset_y
        sx1 = x1 * self.scale + self.image_offset_x
        sy1 = y1 * self.scale + self.image_offset_y
        if self.preview_id:
            self.canvas.delete(self.preview_id)
        if self.current_effect.get() in {"line", "arrow"}:
            arrow = tk.LAST if self.current_effect.get() == "arrow" else ""
            self.preview_id = self.canvas.create_line(sx0, sy0, sx1, sy1, fill=self.current_color.get(), width=max(1, int(self.stroke_width.get() * self.scale)), arrow=arrow, tags="preview")
        else:
            create = self.canvas.create_oval if self.current_shape.get() == "ellipse" else self.canvas.create_rectangle
            self.preview_id = create(sx0, sy0, sx1, sy1, outline="#ff3344", width=2, dash=(6, 4), tags="preview")

    def on_release(self, event) -> None:
        if self.current_effect.get() == "text":
            return
        if not self.start:
            return
        if self.current_effect.get() in {"pen", "highlight"}:
            points = self.start if isinstance(self.start, list) else []
            self.start = None
            self.canvas.delete("preview")
            if len(points) < 2:
                return
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            self.regions.append(
                Region(
                    x=min(xs),
                    y=min(ys),
                    width=max(xs) - min(xs),
                    height=max(ys) - min(ys),
                    effect=self.current_effect.get(),
                    shape="rect",
                    color=self.current_color.get(),
                    stroke_width=max(1, int(self.stroke_width.get() or 4)),
                    points=points,
                )
            )
            self.render()
            self.sync_clipboard("已添加标注，并已更新剪贴板。")
            return
        if isinstance(self.start, list):
            self.start = None
            return
        x0, y0 = self.start
        x1, y1 = self.display_to_original(event)
        self.start = None
        if self.preview_id:
            self.canvas.delete(self.preview_id)
            self.preview_id = None
        self.canvas.delete("preview")
        if self.current_effect.get() in {"line", "arrow"}:
            if abs(x1 - x0) < 5 and abs(y1 - y0) < 5:
                return
            self.regions.append(
                Region(
                    x=x0,
                    y=y0,
                    width=x1 - x0,
                    height=y1 - y0,
                    effect=self.current_effect.get(),
                    shape="rect",
                    color=self.current_color.get(),
                    stroke_width=max(1, int(self.stroke_width.get() or 4)),
                )
            )
            self.render()
            self.sync_clipboard("已添加标注，并已更新剪贴板。")
            return
        x, y, w, h = clamp_region(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0), self.original.width, self.original.height)
        if w < 5 or h < 5:
            return
        self.regions.append(
            Region(
                x=x,
                y=y,
                width=w,
                height=h,
                effect=self.current_effect.get(),
                shape=self.current_shape.get(),
                color=self.current_color.get(),
                stroke_width=max(1, int(self.stroke_width.get() or 4)),
            )
        )
        self.status_text.set("已添加区域。按 Ctrl+C 或 Enter 复制最终图片，然后到聊天窗口按 Ctrl+V。")
        self.render()
        self.sync_clipboard("已添加区域，并已更新剪贴板。可直接到聊天窗口按 Ctrl+V。")

    def undo(self) -> None:
        if self.regions:
            self.regions.pop()
            self.render()
            self.sync_clipboard("已撤销上一处区域，并已更新剪贴板。")

    def clear(self) -> None:
        if self.regions:
            self.regions.clear()
            self.render()
            self.sync_clipboard("已清空全部区域，并已更新剪贴板。")

    def on_window_return(self, _event):
        if self.text_entry:
            return "break"
        self.copy_image()
        return "break"

    def save_png(self) -> None:
        self.commit_text_entry(silent=True)
        path = filedialog.asksaveasfilename(
            parent=self.window,
            title="保存打码后的图片",
            defaultextension=".png",
            filetypes=[("PNG 图片", "*.png")],
            initialfile="masked_screenshot.png",
        )
        if not path:
            return
        try:
            self.render_final().save(path, "PNG")
            self.status_text.set("已保存 PNG。")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"保存失败：{exc}", parent=self.window)

    def copy_image(self) -> None:
        self.commit_text_entry(silent=True)
        try:
            copy_png_to_clipboard(self.render_final())
            self.status_text.set("已复制到剪贴板。现在可以回到聊天窗口按 Ctrl+V 粘贴。")
            self.window.bell()
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"复制失败：{exc}", parent=self.window)

    def sync_clipboard(self, success_message: str) -> None:
        self.refresh_capture_list()
        try:
            copy_png_to_clipboard(self.render_final())
            self.status_text.set(success_message)
        except Exception as exc:
            self.status_text.set(f"图片已更新，但剪贴板同步失败：{exc}")

    def run_smart_mask(self) -> None:
        self.commit_text_entry(silent=True)
        self.status_text.set("正在识别敏感信息并打码...")
        threading.Thread(target=self._smart_mask_worker, daemon=True).start()

    def _smart_mask_worker(self) -> None:
        try:
            words = recognize_word_boxes(self.render_final())
            boxes = self.find_sensitive_boxes(words)
            self.window.after(0, lambda: self.apply_sensitive_boxes(boxes))
        except Exception as exc:
            self.window.after(0, lambda e=exc: messagebox.showerror(APP_NAME, f"智能打码失败：{e}", parent=self.window))
            self.window.after(0, lambda: self.status_text.set("智能打码失败。"))

    def find_sensitive_boxes(self, words: list[WordBox]) -> list[tuple[int, int, int, int]]:
        boxes: list[tuple[int, int, int, int]] = []
        for word in words:
            if looks_sensitive(word.text):
                boxes.append((word.x, word.y, word.width, word.height))

        lines: list[list[WordBox]] = []
        for word in sorted(words, key=lambda item: (item.y, item.x)):
            if not lines:
                lines.append([word])
                continue
            current = lines[-1]
            avg_y = sum(item.y for item in current) / len(current)
            avg_h = max(1, sum(item.height for item in current) / len(current))
            if abs(word.y - avg_y) <= max(8, avg_h * 0.7):
                current.append(word)
            else:
                lines.append([word])

        for line in lines:
            line = sorted(line, key=lambda item: item.x)
            line_text = "".join(item.text for item in line)
            if not looks_sensitive(line_text):
                continue
            x0 = min(item.x for item in line)
            y0 = min(item.y for item in line)
            x1 = max(item.x + item.width for item in line)
            y1 = max(item.y + item.height for item in line)
            boxes.append((x0, y0, x1 - x0, y1 - y0))

        merged: list[tuple[int, int, int, int]] = []
        for x, y, w, h in boxes:
            pad = max(4, h // 3)
            x = max(0, x - pad)
            y = max(0, y - pad)
            w = min(self.original.width - x, w + pad * 2)
            h = min(self.original.height - y, h + pad * 2)
            box = (x, y, w, h)
            if box not in merged:
                merged.append(box)
        return merged

    def apply_sensitive_boxes(self, boxes: list[tuple[int, int, int, int]]) -> None:
        if not boxes:
            self.status_text.set("未识别到手机号、邮箱、身份证或 IP。")
            return
        for x, y, w, h in boxes:
            self.regions.append(
                Region(
                    x=x,
                    y=y,
                    width=w,
                    height=h,
                    effect="mosaic",
                    shape="rect",
                    color=self.current_color.get(),
                    stroke_width=max(1, int(self.stroke_width.get() or 4)),
                )
            )
        self.render()
        self.sync_clipboard(f"已智能打码 {len(boxes)} 处敏感信息，并已更新剪贴板。")

    def run_ocr(self) -> None:
        self.commit_text_entry(silent=True)
        self.status_text.set("正在 OCR 识别...")
        threading.Thread(target=self._ocr_worker, daemon=True).start()

    def _ocr_worker(self) -> None:
        try:
            text = recognize_text(self.render_final())
            self.window.after(0, lambda: self.show_text_result("OCR 识别结果", text, allow_translate=True))
        except Exception as exc:
            self.window.after(0, lambda e=exc: messagebox.showerror(APP_NAME, f"OCR 失败：{e}", parent=self.window))
            self.window.after(0, lambda: self.status_text.set("OCR 失败。"))

    def run_translate_from_ocr(self) -> None:
        self.commit_text_entry(silent=True)
        self.status_text.set("正在 OCR 并翻译...")
        threading.Thread(target=self._ocr_translate_worker, daemon=True).start()

    def _ocr_translate_worker(self) -> None:
        try:
            source = recognize_text(self.render_final())
            translated = translate_text(source)
            body = f"【原文】\n{source}\n\n【翻译】\n{translated}"
            self.window.after(0, lambda: self.show_text_result("OCR 翻译结果", body, allow_translate=False))
        except Exception as exc:
            self.window.after(0, lambda e=exc: messagebox.showerror(APP_NAME, f"翻译失败：{e}", parent=self.window))
            self.window.after(0, lambda: self.status_text.set("翻译失败。"))

    def show_text_result(self, title: str, text: str, allow_translate: bool = False) -> None:
        win = tk.Toplevel(self.window)
        win.title(title)
        win.geometry("620x440")
        win.transient(self.window)
        frame = tk.Frame(win, bg="#ffffff", padx=12, pady=12)
        frame.pack(fill="both", expand=True)
        box = tk.Text(frame, wrap="word", font=("Microsoft YaHei UI", 10), relief="solid", bd=1)
        box.pack(fill="both", expand=True)
        box.insert("1.0", text or "未识别到文字")
        buttons = tk.Frame(frame, bg="#ffffff")
        buttons.pack(fill="x", pady=(10, 0))

        def copy_text():
            win.clipboard_clear()
            win.clipboard_append(box.get("1.0", "end").strip())
            self.status_text.set("文本已复制。")

        def translate_current():
            source = box.get("1.0", "end").strip()
            if not source:
                return
            self.status_text.set("正在翻译文本...")

            def worker():
                try:
                    translated = translate_text(source)
                    win.after(0, lambda: box.delete("1.0", "end"))
                    win.after(0, lambda: box.insert("1.0", translated))
                    win.after(0, lambda: self.status_text.set("翻译完成。"))
                except Exception as exc:
                    win.after(0, lambda e=exc: messagebox.showerror(APP_NAME, f"翻译失败：{e}", parent=win))

            threading.Thread(target=worker, daemon=True).start()

        tk.Button(buttons, text="复制文本", command=copy_text, bg="#2563eb", fg="white", activebackground="#1d4ed8", activeforeground="white", relief="flat", padx=14, pady=7, cursor="hand2").pack(side="right", padx=(8, 0))
        if allow_translate:
            tk.Button(buttons, text="翻译为中文", command=translate_current, bg="#eef2f7", fg="#111827", activebackground="#e5e7eb", relief="flat", padx=14, pady=7, cursor="hand2").pack(side="right")
        tk.Button(buttons, text="关闭", command=win.destroy, bg="#f8fafc", fg="#111827", activebackground="#e5e7eb", relief="flat", padx=14, pady=7, cursor="hand2").pack(side="left")
        self.status_text.set(title)


class HotkeySettingsWindow:
    def __init__(self, app: "ScreenshotMaskerApp"):
        self.app = app
        self.window = tk.Toplevel(app.root)
        self.window.title("设置截图快捷键")
        self.window.geometry("360x250")
        self.window.resizable(False, False)
        self.window.attributes("-topmost", True)
        self.window.focus_force()

        hotkey = app.hotkey_config.copy()
        self.ctrl_var = tk.BooleanVar(value=bool(hotkey.get("ctrl")))
        self.shift_var = tk.BooleanVar(value=bool(hotkey.get("shift")))
        self.alt_var = tk.BooleanVar(value=bool(hotkey.get("alt")))
        self.win_var = tk.BooleanVar(value=bool(hotkey.get("win")))
        self.key_var = tk.StringVar(value=str(hotkey.get("key", "A")))
        self.single_window_var = tk.BooleanVar(value=bool(app.config.get("single_window", True)))
        self.status_var = tk.StringVar(value=f"当前快捷键：{app.hotkey_label()}")

        body = ttk.Frame(self.window, padding=16)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="选择组合键").pack(anchor="w")
        modifier_frame = ttk.Frame(body)
        modifier_frame.pack(fill="x", pady=(8, 12))
        ttk.Checkbutton(modifier_frame, text="Ctrl", variable=self.ctrl_var).pack(side="left")
        ttk.Checkbutton(modifier_frame, text="Shift", variable=self.shift_var).pack(side="left", padx=(10, 0))
        ttk.Checkbutton(modifier_frame, text="Alt", variable=self.alt_var).pack(side="left", padx=(10, 0))
        ttk.Checkbutton(modifier_frame, text="Win", variable=self.win_var).pack(side="left", padx=(10, 0))

        ttk.Label(body, text="主键").pack(anchor="w")
        key_box = ttk.Combobox(body, textvariable=self.key_var, values=list(KEY_OPTIONS.keys()), state="readonly")
        key_box.pack(fill="x", pady=(8, 12))

        ttk.Checkbutton(
            body,
            text="单窗口编辑：新截图加入左侧列表，不重复打开编辑窗口",
            variable=self.single_window_var,
        ).pack(anchor="w", pady=(0, 12))

        ttk.Label(body, textvariable=self.status_var, foreground="#444").pack(anchor="w", pady=(0, 14))

        button_frame = ttk.Frame(body)
        button_frame.pack(fill="x")
        ttk.Button(button_frame, text="保存并生效", command=self.save).pack(side="right")
        ttk.Button(button_frame, text="取消", command=self.window.destroy).pack(side="right", padx=(0, 8))

        self.window.bind("<Return>", lambda _event: self.save())
        self.window.bind("<Escape>", lambda _event: self.window.destroy())

    def save(self) -> None:
        hotkey = {
            "ctrl": self.ctrl_var.get(),
            "shift": self.shift_var.get(),
            "alt": self.alt_var.get(),
            "win": self.win_var.get(),
            "key": self.key_var.get(),
        }
        if hotkey_modifiers(hotkey) == 0:
            self.status_var.set("请至少选择 Ctrl、Shift、Alt、Win 中的一个。")
            return
        if hotkey["key"] not in KEY_OPTIONS:
            self.status_var.set("请选择一个有效主键。")
            return
        try:
            self.app.config["single_window"] = self.single_window_var.get()
            save_config(self.app.config)
            self.app.update_hotkey(hotkey)
            self.window.destroy()
            messagebox.showinfo(APP_NAME, f"快捷键已改为：{hotkey_label(hotkey)}", parent=self.app.root)
        except Exception as exc:
            self.status_var.set(f"保存失败：{exc}")


class ScreenshotMaskerApp:
    def __init__(self):
        set_dpi_awareness()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        self.config = load_config()
        self.hotkey_config = self.config["hotkey"].copy()
        self.hotkey: HotkeyThread | None = None
        self.tray_icon = None
        self.capture_lock = threading.Lock()
        self.settings_window = None
        self.editor_window: EditorWindow | None = None

    def start(self) -> None:
        self.start_hotkey()
        self.start_tray()
        self.root.mainloop()

    def hotkey_label(self) -> str:
        return hotkey_label(self.hotkey_config)

    def start_hotkey(self) -> str | None:
        self.hotkey = HotkeyThread(self.on_hotkey_event, self.hotkey_config)
        self.hotkey.start()
        self.hotkey.ready_event.wait(timeout=1)
        return self.hotkey.registration_error

    def stop_hotkey(self) -> None:
        if self.hotkey:
            self.hotkey.stop()
            self.hotkey.join(timeout=1)
            self.hotkey = None

    def update_hotkey(self, hotkey: dict) -> None:
        old_hotkey = self.hotkey_config.copy()
        self.stop_hotkey()
        self.hotkey_config = hotkey.copy()
        error = self.start_hotkey()
        if error:
            self.stop_hotkey()
            self.hotkey_config = old_hotkey
            self.config["hotkey"] = old_hotkey
            save_config(self.config)
            self.start_hotkey()
            raise RuntimeError("新快捷键注册失败，已恢复原快捷键。")
        self.config["hotkey"] = self.hotkey_config.copy()
        save_config(self.config)
        if self.tray_icon:
            self.tray_icon.menu = self.build_tray_menu()
            self.tray_icon.update_menu()

    def on_hotkey_event(self, error: str | None = None) -> None:
        if error:
            self.root.after(0, lambda: messagebox.showwarning(APP_NAME, error))
            return
        self.root.after(0, self.capture_screen)

    def start_tray(self) -> None:
        self.tray_icon = pystray.Icon(
            "ScreenshotMasker",
            create_tray_image(),
            APP_NAME,
            menu=self.build_tray_menu(),
        )
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def build_tray_menu(self):
        def menu_capture(_icon, _item):
            self.root.after(0, self.capture_screen)

        def menu_full_capture(_icon, _item):
            self.root.after(0, self.capture_full_screen)

        def menu_long_capture(_icon, _item):
            self.root.after(0, self.start_long_capture_setup)

        def menu_settings(_icon, _item):
            self.root.after(0, self.open_hotkey_settings)

        def menu_exit(_icon, _item):
            self.root.after(0, self.quit)

        return pystray.Menu(
            pystray.MenuItem(f"区域截图 {self.hotkey_label()}", menu_capture, default=True),
            pystray.MenuItem("全屏截图", menu_full_capture),
            pystray.MenuItem("长截图", menu_long_capture),
            pystray.MenuItem("设置快捷键", menu_settings),
            pystray.MenuItem("退出", menu_exit),
        )

    def open_hotkey_settings(self) -> None:
        if self.settings_window and self.settings_window.window.winfo_exists():
            self.settings_window.window.focus_force()
            return
        self.settings_window = HotkeySettingsWindow(self)

    def capture_screen(self) -> None:
        if not self.capture_lock.acquire(blocking=False):
            return
        try:
            screenshot = ImageGrab.grab(all_screens=True)
            CaptureOverlay(self, screenshot)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"截图失败：{exc}")
            self.capture_lock.release()
            return

    def capture_full_screen(self) -> None:
        if not self.capture_lock.acquire(blocking=False):
            return
        try:
            image = ImageGrab.grab(all_screens=True).convert("RGBA")
            self.copy_capture_and_open_editor(image)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"全屏截图失败：{exc}")
        finally:
            self.release_capture_lock()

    def start_long_capture_setup(self) -> None:
        if self.capture_lock.locked():
            return
        count = simpledialog.askinteger(APP_NAME, "长截图连续抓取几张？\n建议 4-8 张，期间手动滚动页面。", initialvalue=5, minvalue=2, maxvalue=20, parent=self.root)
        if not count:
            return
        delay = simpledialog.askfloat(APP_NAME, "每张间隔几秒？\n建议 0.8-1.2 秒。", initialvalue=1.0, minvalue=0.3, maxvalue=5.0, parent=self.root)
        if not delay:
            return
        self.long_capture_count = count
        self.long_capture_delay_ms = int(delay * 1000)
        if not self.capture_lock.acquire(blocking=False):
            return
        try:
            screenshot = ImageGrab.grab(all_screens=True)
            CaptureOverlay(
                self,
                screenshot,
                on_region=self.begin_long_capture,
                tip_text="拖动选择长截图区域，松开后请开始滚动页面，Esc 取消。",
                allow_fullscreen=False,
            )
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"长截图启动失败：{exc}")
            self.release_capture_lock()

    def begin_long_capture(self, region: tuple[int, int, int, int]) -> None:
        self.long_capture_region = region
        self.long_capture_frames: list[Image.Image] = []
        self.long_capture_status = tk.Toplevel(self.root)
        self.long_capture_status.title("长截图")
        self.long_capture_status.geometry("320x110")
        self.long_capture_status.attributes("-topmost", True)
        self.long_capture_label = tk.Label(
            self.long_capture_status,
            text="准备开始，请滚动页面...",
            font=("Microsoft YaHei UI", 11),
            padx=18,
            pady=18,
        )
        self.long_capture_label.pack(fill="both", expand=True)
        self.root.after(500, lambda: self.capture_long_frame(0))

    def capture_long_frame(self, index: int) -> None:
        try:
            if not hasattr(self, "long_capture_status") or not self.long_capture_status.winfo_exists():
                return
            total = self.long_capture_count
            self.long_capture_label.config(text=f"正在长截图 {index + 1}/{total}\n请继续滚动页面")
            image = ImageGrab.grab(all_screens=True).crop(self.long_capture_region)
            self.long_capture_frames.append(image.convert("RGBA"))
            if index + 1 >= total:
                self.finish_long_capture()
                return
            self.root.after(self.long_capture_delay_ms, lambda: self.capture_long_frame(index + 1))
        except Exception as exc:
            if hasattr(self, "long_capture_status") and self.long_capture_status.winfo_exists():
                self.long_capture_status.destroy()
            messagebox.showerror(APP_NAME, f"长截图失败：{exc}")

    def finish_long_capture(self) -> None:
        if hasattr(self, "long_capture_status") and self.long_capture_status.winfo_exists():
            self.long_capture_status.destroy()
        frames = getattr(self, "long_capture_frames", [])
        if not frames:
            return
        width = max(frame.width for frame in frames)
        height = sum(frame.height for frame in frames)
        stitched = Image.new("RGBA", (width, height), "white")
        y = 0
        for frame in frames:
            stitched.paste(frame, (0, y))
            y += frame.height
        try:
            copy_png_to_clipboard(stitched)
            copied = True
            error = None
        except Exception as exc:
            copied = False
            error = exc
        self.copy_capture_and_open_editor(stitched)
        if self.editor_window:
            if copied:
                self.editor_window.status_text.set("长截图已拼接完成，并已复制到剪贴板。")
            elif error:
                self.editor_window.status_text.set(f"长截图已拼接完成，但复制失败：{error}")

    def release_capture_lock(self) -> None:
        if self.capture_lock.locked():
            self.capture_lock.release()

    def open_editor(self, image: Image.Image) -> None:
        if self.config.get("single_window", True) and self.editor_window and self.editor_window.window.winfo_exists():
            self.editor_window.add_capture(image)
            return
        self.editor_window = EditorWindow(self, image)

    def copy_capture_and_open_editor(self, image: Image.Image) -> None:
        copied = False
        error = None
        try:
            copy_png_to_clipboard(image)
            copied = True
        except Exception as exc:
            error = exc
        if self.config.get("single_window", True) and self.editor_window and self.editor_window.window.winfo_exists():
            self.editor_window.add_capture(image, copied=copied, error=error)
            return
        editor = EditorWindow(self, image)
        self.editor_window = editor
        if copied:
            editor.status_text.set("截图已自动复制到剪贴板。可直接到聊天窗口按 Ctrl+V，或编辑后按 Ctrl+C 复制最终图片。")
        elif error:
            editor.status_text.set(f"截图已打开，但自动复制失败：{error}")

    def quit(self) -> None:
        self.stop_hotkey()
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.quit()
        self.root.destroy()


def main() -> int:
    if sys.platform != "win32":
        print("This tool is intended for Windows.")
        return 1
    app = ScreenshotMaskerApp()
    app.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
