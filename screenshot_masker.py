from __future__ import annotations

import ctypes
import ctypes.wintypes
import io
import os
import sys
import tempfile
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from urllib.parse import quote
from urllib.request import urlopen

from PIL import Image, ImageDraw, ImageFilter, ImageGrab, ImageTk
import pystray

try:
    import asyncio
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.globalization import Language
    from winrt.windows.storage import StorageFile, FileAccessMode
    from winrt.windows.graphics.imaging import BitmapDecoder
except Exception:
    asyncio = None
    OcrEngine = None
    Language = None
    StorageFile = None
    FileAccessMode = None
    BitmapDecoder = None

APP_NAME = "截图打码工具"
HOTKEY_ID = 0xA51
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
WM_HOTKEY = 0x0312
CF_DIB = 8
GMEM_MOVEABLE = 0x0002


def set_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def create_tray_image() -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 12, 56, 50), radius=8, fill=(35, 110, 255), outline=(255, 255, 255), width=3)
    draw.rectangle((20, 22, 44, 40), outline=(255, 255, 255), width=4)
    draw.line((14, 14, 50, 50), fill=(255, 255, 255), width=4)
    return image


def copy_image_to_clipboard(image: Image.Image) -> None:
    output = io.BytesIO()
    image.convert("RGB").save(output, "BMP")
    data = output.getvalue()[14:]
    output.close()

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

    opened = False
    for _ in range(10):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.05)
    if not opened:
        raise RuntimeError("无法打开剪贴板")

    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise RuntimeError("无法分配剪贴板内存")
        locked = kernel32.GlobalLock(handle)
        if not locked:
            raise RuntimeError("无法锁定剪贴板内存")
        ctypes.memmove(locked, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_DIB, handle):
            raise RuntimeError("写入剪贴板失败")
    finally:
        user32.CloseClipboard()


async def _recognize_text_async(image_path: str) -> str:
    if OcrEngine is None:
        raise RuntimeError("当前环境缺少 Windows OCR 组件")
    file = await StorageFile.get_file_from_path_async(str(Path(image_path).resolve()))
    stream = await file.open_async(FileAccessMode.READ)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    engine = None
    for tag in ("zh-Hans-CN", "en-US"):
        try:
            engine = OcrEngine.try_create_from_language(Language(tag))
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


def recognize_text(image: Image.Image) -> str:
    if asyncio is None:
        raise RuntimeError("当前 Python 环境缺少 asyncio 或 Windows OCR 依赖")
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        path = tmp.name
    try:
        image.convert("RGB").save(path, "PNG")
        return asyncio.run(_recognize_text_async(path))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def translate_text(text: str, target: str = "zh-CN") -> str:
    text = text.strip()
    if not text:
        return ""
    url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl={target}&dt=t&q={quote(text)}"
    with urlopen(url, timeout=10) as response:
        raw = response.read().decode("utf-8", errors="replace")
    import json
    data = json.loads(raw)
    return "".join(item[0] for item in data[0] if item and item[0])


@dataclass
class Region:
    x: int
    y: int
    w: int
    h: int
    effect: str
    text: str = ""


class HotkeyThread(threading.Thread):
    def __init__(self, callback):
        super().__init__(daemon=True)
        self.callback = callback
        self.stop_event = threading.Event()
        self.thread_id = None

    def run(self) -> None:
        user32 = ctypes.windll.user32
        self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_SHIFT, ord("A")):
            self.callback(error="快捷键 Ctrl + Shift + A 注册失败，可能已被其他程序占用")
            return
        msg = ctypes.wintypes.MSG()
        try:
            while not self.stop_event.is_set():
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0:
                    break
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self.callback(error=None)
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)


class CaptureOverlay:
    def __init__(self, app, screenshot: Image.Image):
        self.app = app
        self.screenshot = screenshot.convert("RGBA")
        self.start_x = self.start_y = 0
        self.rect_id = None

        self.window = tk.Toplevel(app.root)
        self.window.attributes("-fullscreen", True)
        self.window.attributes("-topmost", True)
        self.window.configure(bg="black")
        self.window.bind("<Escape>", self.cancel)

        self.screen_w = self.window.winfo_screenwidth()
        self.screen_h = self.window.winfo_screenheight()
        preview = self.screenshot.resize((self.screen_w, self.screen_h))
        dark = Image.new("RGBA", preview.size, (0, 0, 0, 90))
        preview = Image.alpha_composite(preview, dark)
        self.tk_img = ImageTk.PhotoImage(preview)

        self.canvas = tk.Canvas(self.window, cursor="cross", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_image(0, 0, anchor="nw", image=self.tk_img)
        self.canvas.create_text(
            self.screen_w // 2,
            36,
            text="拖动选择截图区域，Esc 取消",
            fill="white",
            font=("Microsoft YaHei UI", 14),
        )
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)

    def on_press(self, event) -> None:
        self.start_x, self.start_y = event.x, event.y
        self.rect_id = self.canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#00d4ff", width=2)

    def on_drag(self, event) -> None:
        if self.rect_id:
            self.canvas.coords(self.rect_id, self.start_x, self.start_y, event.x, event.y)

    def on_release(self, event) -> None:
        x0, y0 = min(self.start_x, event.x), min(self.start_y, event.y)
        x1, y1 = max(self.start_x, event.x), max(self.start_y, event.y)
        self.window.destroy()
        if x1 - x0 < 5 or y1 - y0 < 5:
            self.app.release_capture_lock()
            return
        scale_x = self.screenshot.width / self.screen_w
        scale_y = self.screenshot.height / self.screen_h
        crop = self.screenshot.crop((int(x0 * scale_x), int(y0 * scale_y), int(x1 * scale_x), int(y1 * scale_y)))
        self.app.open_editor(crop)
        self.app.release_capture_lock()

    def cancel(self, _event=None) -> None:
        self.window.destroy()
        self.app.release_capture_lock()


class EditorWindow:
    def __init__(self, app, image: Image.Image):
        self.app = app
        self.original = image.convert("RGBA")
        self.regions: list[Region] = []
        self.mode = tk.StringVar(value="mosaic")
        self.status = tk.StringVar(value="截图已打开。选择工具后拖动区域即可打码，编辑后会自动复制到剪贴板。")
        self.start_x = self.start_y = 0
        self.temp_rect = None

        self.window = tk.Toplevel(app.root)
        self.window.title(APP_NAME)
        self.window.geometry("1100x720")
        self.window.minsize(760, 520)
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        toolbar = ttk.Frame(self.window, padding=8)
        toolbar.pack(side="top", fill="x")
        buttons = [
            ("马赛克", "mosaic"),
            ("模糊", "blur"),
            ("黑色遮罩", "black"),
            ("红框", "rect"),
            ("文字", "text"),
        ]
        for text, value in buttons:
            ttk.Radiobutton(toolbar, text=text, value=value, variable=self.mode).pack(side="left", padx=4)
        ttk.Button(toolbar, text="撤销", command=self.undo).pack(side="left", padx=10)
        ttk.Button(toolbar, text="复制", command=self.copy).pack(side="left", padx=4)
        ttk.Button(toolbar, text="保存", command=self.save).pack(side="left", padx=4)
        ttk.Button(toolbar, text="OCR", command=self.ocr).pack(side="left", padx=4)
        ttk.Button(toolbar, text="OCR+翻译", command=self.ocr_translate).pack(side="left", padx=4)
        ttk.Label(toolbar, textvariable=self.status).pack(side="left", padx=12)

        self.canvas = tk.Canvas(self.window, bg="#1f2937", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self.render())
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.window.bind("<Control-c>", lambda _e: self.copy())
        self.window.bind("<Control-s>", lambda _e: self.save())
        self.window.bind("<Control-z>", lambda _e: self.undo())
        self.render()
        self.copy()

    def current_image(self) -> Image.Image:
        image = self.original.copy()
        draw = ImageDraw.Draw(image)
        for r in self.regions:
            box = (r.x, r.y, r.x + r.w, r.y + r.h)
            if r.effect == "black":
                draw.rectangle(box, fill="black")
            elif r.effect == "rect":
                draw.rectangle(box, outline="red", width=4)
            elif r.effect == "blur":
                patch = image.crop(box).filter(ImageFilter.GaussianBlur(10))
                image.paste(patch, box)
            elif r.effect == "mosaic":
                patch = image.crop(box)
                small_w = max(1, patch.width // 14)
                small_h = max(1, patch.height // 14)
                patch = patch.resize((small_w, small_h), Image.Resampling.BILINEAR)
                patch = patch.resize((r.w, r.h), Image.Resampling.NEAREST)
                image.paste(patch, box)
            elif r.effect == "text":
                try:
                    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 28)
                except Exception:
                    font = ImageFont.load_default()
                draw.text((r.x, r.y), r.text, fill="red", font=font)
        return image

    def render(self) -> None:
        image = self.current_image()
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        scale = min(cw / image.width, ch / image.height, 1.0)
        self.scale = scale
        self.offset_x = int((cw - image.width * scale) / 2)
        self.offset_y = int((ch - image.height * scale) / 2)
        preview = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))))
        self.tk_img = ImageTk.PhotoImage(preview)
        self.canvas.delete("all")
        self.canvas.create_image(self.offset_x, self.offset_y, anchor="nw", image=self.tk_img)

    def canvas_to_image(self, x: int, y: int) -> tuple[int, int]:
        return int((x - self.offset_x) / self.scale), int((y - self.offset_y) / self.scale)

    def on_press(self, event) -> None:
        self.start_x, self.start_y = event.x, event.y
        self.temp_rect = self.canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#00d4ff", width=2)

    def on_drag(self, event) -> None:
        if self.temp_rect:
            self.canvas.coords(self.temp_rect, self.start_x, self.start_y, event.x, event.y)

    def on_release(self, event) -> None:
        if self.temp_rect:
            self.canvas.delete(self.temp_rect)
            self.temp_rect = None
        x0, y0 = self.canvas_to_image(min(self.start_x, event.x), min(self.start_y, event.y))
        x1, y1 = self.canvas_to_image(max(self.start_x, event.x), max(self.start_y, event.y))
        x0 = max(0, min(x0, self.original.width))
        y0 = max(0, min(y0, self.original.height))
        x1 = max(0, min(x1, self.original.width))
        y1 = max(0, min(y1, self.original.height))
        if x1 - x0 < 3 or y1 - y0 < 3:
            return
        effect = self.mode.get()
        text = ""
        if effect == "text":
            text = simpledialog.askstring(APP_NAME, "请输入要添加的文字：", parent=self.window) or ""
            if not text:
                return
        self.regions.append(Region(x0, y0, x1 - x0, y1 - y0, effect, text))
        self.render()
        self.copy()

    def undo(self) -> None:
        if self.regions:
            self.regions.pop()
            self.render()
            self.copy()

    def copy(self) -> None:
        try:
            copy_image_to_clipboard(self.current_image())
            self.status.set("已复制到剪贴板，可以直接 Ctrl+V 粘贴。")
        except Exception as exc:
            self.status.set(f"复制失败：{exc}")

    def save(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.window,
            defaultextension=".png",
            filetypes=[("PNG 图片", "*.png"), ("所有文件", "*.*")],
        )
        if path:
            self.current_image().save(path)
            self.status.set(f"已保存：{path}")

    def ocr(self) -> None:
        self.status.set("正在 OCR 识别...")
        threading.Thread(target=self._ocr_worker, daemon=True).start()

    def ocr_translate(self) -> None:
        self.status.set("正在 OCR 并翻译...")
        threading.Thread(target=self._ocr_worker, kwargs={"do_translate": True}, daemon=True).start()

    def _ocr_worker(self, do_translate: bool = False) -> None:
        try:
            text = recognize_text(self.current_image())
            result = translate_text(text) if do_translate and text else text
            self.window.after(0, lambda: self.show_text_result(result or "未识别到文字"))
        except Exception as exc:
            self.window.after(0, lambda: messagebox.showerror(APP_NAME, str(exc), parent=self.window))
            self.window.after(0, lambda: self.status.set("OCR 失败"))

    def show_text_result(self, text: str) -> None:
        win = tk.Toplevel(self.window)
        win.title("识别结果")
        win.geometry("620x420")
        box = tk.Text(win, wrap="word", font=("Microsoft YaHei UI", 11))
        box.pack(fill="both", expand=True, padx=10, pady=10)
        box.insert("1.0", text)
        self.status.set("OCR 完成")

    def close(self) -> None:
        self.window.destroy()


class ScreenshotMaskerApp:
    def __init__(self) -> None:
        set_dpi_awareness()
        self.root = tk.Tk()
        self.root.withdraw()
        self.capture_lock = threading.Lock()
        self.hotkey = HotkeyThread(self.on_hotkey)
        self.tray_icon = None

    def start(self) -> None:
        self.hotkey.start()
        self.tray_icon = pystray.Icon(
            "ScreenshotMasker",
            create_tray_image(),
            APP_NAME,
            menu=pystray.Menu(
                pystray.MenuItem("截图 Ctrl + Shift + A", lambda _i, _m: self.root.after(0, self.capture), default=True),
                pystray.MenuItem("退出", lambda _i, _m: self.root.after(0, self.quit)),
            ),
        )
        threading.Thread(target=self.tray_icon.run, daemon=True).start()
        self.root.mainloop()

    def on_hotkey(self, error: str | None = None) -> None:
        if error:
            self.root.after(0, lambda: messagebox.showwarning(APP_NAME, error))
        else:
            self.root.after(0, self.capture)

    def capture(self) -> None:
        if not self.capture_lock.acquire(blocking=False):
            return
        try:
            screenshot = ImageGrab.grab(all_screens=True)
            CaptureOverlay(self, screenshot)
        except Exception as exc:
            self.release_capture_lock()
            messagebox.showerror(APP_NAME, f"截图失败：{exc}")

    def open_editor(self, image: Image.Image) -> None:
        EditorWindow(self, image)

    def release_capture_lock(self) -> None:
        if self.capture_lock.locked():
            self.capture_lock.release()

    def quit(self) -> None:
        self.hotkey.stop()
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.quit()
        self.root.destroy()


def main() -> int:
    if sys.platform != "win32":
        print("This tool is intended for Windows.")
        return 1
    ScreenshotMaskerApp().start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
