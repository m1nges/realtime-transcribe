"""Стеклянная капсула внизу экрана.

Стекло рисуем сами: берём картинку экрана под капсулой, размываем и кладём под полупрозрачный тинт.
Окно layered с попиксельной альфой (UpdateLayeredWindow), поэтому края сглажены и есть мягкая тень.
Цвет стекла следует теме Windows (светлая/тёмная).
"""
import ctypes
import logging
import math
import time
import winreg
from ctypes import wintypes

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

W, H = 112, 28   # капсула, логические px
PAD = 12         # поле под тень вокруг капсулы
BARS = 22
SS = 3           # суперсэмплинг для гладких краёв

GRAD = [(120, 110, 255), (190, 100, 255), (255, 95, 170)]

THEMES = {
    "dark": dict(tint=(16, 12, 26, 150), border=(190, 160, 255, 70), shadow=110, sheen=22,
                 grad=GRAD, glow=0.55, text=(235, 225, 255, 255)),
    "light": dict(tint=(250, 248, 255, 165), border=(255, 255, 255, 190), shadow=55, sheen=60,
                  grad=[(98, 80, 240), (160, 70, 235), (235, 60, 145)], glow=0.3, text=(60, 40, 110, 255)),
}


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                       ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                       wintypes.DWORD, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wintypes.UINT]
HWND_TOPMOST = wintypes.HWND(-1)
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.MonitorFromPoint.restype = wintypes.HMONITOR
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]


def system_theme():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
    except OSError:
        return "dark"


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _grad(colors, t):
    t = min(max(t, 0.0), 1.0) * (len(colors) - 1)
    i = min(int(t), len(colors) - 2)
    return _lerp(colors[i], colors[i + 1], t - i)


def _monitor_under_cursor():
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    hmon = user32.MonitorFromPoint(pt, 2)
    mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
    dx, dy = ctypes.c_uint(), ctypes.c_uint()
    try:
        ctypes.windll.shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy))
        scale = dx.value / 96
    except Exception:
        scale = 1.0
    return mi.rcWork, scale


def _grab(x, y, w, h):
    """Снимок экрана под окном. Без CAPTUREBLT layered-окна (в т.ч. мы сами) в снимок не попадают."""
    sdc = user32.GetDC(0)
    mdc = gdi32.CreateCompatibleDC(sdc)
    bmp = gdi32.CreateCompatibleBitmap(sdc, w, h)
    old = gdi32.SelectObject(mdc, bmp)
    gdi32.BitBlt(mdc, 0, 0, w, h, sdc, x, y, 0x00CC0020)
    bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    gdi32.SelectObject(mdc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(0, sdc)
    return Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")


class Overlay:
    """Окно создаёт Tk, а содержимое целиком рисуем сами через UpdateLayeredWindow."""

    def __init__(self, root, level_source):
        import tkinter as tk

        self.root = root
        self.levels = level_source  # функция → последние уровни громкости
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.withdraw()
        self.win.update_idletasks()
        self.hwnd = user32.GetParent(self.win.winfo_id()) or self.win.winfo_id()
        ex = user32.GetWindowLongW(self.hwnd, -20)
        # LAYERED | TRANSPARENT (клики насквозь) | TOOLWINDOW (нет в таскбаре) | TOPMOST | NOACTIVATE
        user32.SetWindowLongW(self.hwnd, -20, ex | 0x80000 | 0x20 | 0x80 | 0x8 | 0x08000000)

        self.state = "hidden"
        self.text = ""
        self.alpha = 0.0
        self.t0 = time.time()
        self.smooth = np.zeros(BARS)
        self.bg = None
        self.bg_at = 0.0
        self._geom(1.0)
        self.ticking = False

    def _geom(self, scale):
        self.scale = scale
        self.fw, self.fh = int((W + 2 * PAD) * scale), int((H + 2 * PAD) * scale)

    def _place(self):
        work, scale = _monitor_under_cursor()
        self._geom(scale)
        self.theme = THEMES[system_theme()]
        x = (work.left + work.right - self.fw) // 2
        y = work.bottom - self.fh - int(14 * scale)
        self.pos = (x, y)
        self.bg = None

    # --- публичное
    def set(self, state, text=""):
        self.root.after(0, self._set, state, text)

    def _set(self, state, text):
        was_hidden = self.state == "hidden" and self.alpha < 0.05
        self.state, self.text = state, text
        if state != "hidden" and was_hidden:
            self._place()
            self.smooth[:] = 0
            self.t0 = time.time()
            self.win.deiconify()
            # HWND_TOPMOST: поверх всех окон, без активации (SWP_NOMOVE|NOSIZE|NOACTIVATE|SHOWWINDOW)
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0040)
        if state != "hidden" and not self.ticking:
            self.ticking = True
            self._tick()

    # --- рисование
    def _background(self, now):
        # Фон обновляем пару раз в секунду: этого хватает, а снимок экрана не бесплатный
        if self.bg is None or now - self.bg_at > 0.5:
            s = self.scale
            x, y = self.pos
            px, py, pw, ph = x + int(PAD * s), y + int(PAD * s), int(W * s), int(H * s)
            shot = _grab(px, py, pw, ph).filter(ImageFilter.GaussianBlur(6 * s))
            self.bg = shot.resize((pw * SS, ph * SS), Image.BILINEAR)
            self.bg_at = now
        return self.bg

    def _render(self, now):
        th = self.theme
        s = self.scale * SS
        fw, fh = self.fw * SS, self.fh * SS
        pad = int(PAD * s)
        pw, ph = self.bg.size if self.bg else (int(W * s), int(H * s))
        box = (pad, pad, pad + pw - 1, pad + ph - 1)
        r = ph / 2

        img = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
        # Тень
        sh = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle((box[0], box[1] + 3 * s, box[2], box[3] + 3 * s), radius=r,
                                             fill=(0, 0, 0, th["shadow"]))
        img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(5 * s)))

        # Стекло: размытый фон + тинт + блик, вырезанные по капсуле со сглаживанием
        body = Image.new("RGBA", (pw, ph), (0, 0, 0, 255))
        body.paste(self._background(now), (0, 0))
        body.alpha_composite(Image.new("RGBA", (pw, ph), th["tint"]))
        sheen = np.zeros((ph, pw), np.uint8)
        hh = int(ph * 0.55)
        sheen[:hh] = (th["sheen"] * (1 - np.arange(hh) / hh) ** 2)[:, None].astype(np.uint8)
        body.alpha_composite(Image.merge("RGBA", (*[Image.new("L", (pw, ph), 255)] * 3, Image.fromarray(sheen))))
        mask = Image.new("L", (pw, ph), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, pw - 1, ph - 1), radius=r, fill=255)
        body.putalpha(mask)
        img.alpha_composite(body, (pad, pad))
        ImageDraw.Draw(img).rounded_rectangle(box, radius=r, outline=th["border"], width=max(1, int(s * 0.8)))

        t = now - self.t0
        cx, cy = fw / 2, fh / 2
        n = BARS
        env = np.sin(np.pi * (np.arange(n) + 0.5) / n) ** 0.8
        if self.state == "recording":
            # Новейший уровень в центре, старые расходятся к краям
            hist = list(self.levels())[-(n // 2):][::-1]
            hist = np.pad(np.asarray(hist, float), (0, n // 2 - len(hist)))
            mirrored = np.concatenate([hist[::-1], hist]) if n % 2 == 0 else np.concatenate([hist[::-1], hist[:1], hist])
            target = np.clip(np.sqrt(mirrored) * 4.0, 0.0, 1.0)
            self.smooth = self.smooth * 0.5 + target * 0.5
            self._bars(img, s, cx, cy, np.maximum(self.smooth, 0.08) * env)
        elif self.state == "busy":
            wave = 0.14 + 0.3 * (0.5 + 0.5 * np.sin(t * 8 - np.arange(n) * 0.5))
            self._bars(img, s, cx, cy, wave * (0.5 + 0.5 * env))
        elif self.state == "error":
            try:
                font = ImageFont.truetype("C:/Windows/Fonts/seguisb.ttf", int(10 * s))
            except Exception:
                font = ImageFont.load_default()
            ImageDraw.Draw(img).text((cx, cy), self.text, fill=th["text"], font=font, anchor="mm")

        return img.resize((self.fw, self.fh), Image.LANCZOS)

    def _bars(self, img, s, cx, cy, heights):
        th = self.theme
        n = len(heights)
        bw, gap = 2.2 * s, 2.0 * s
        maxh = (H - 10) * s
        total = n * bw + (n - 1) * gap
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        x = cx - total / 2
        for i, v in enumerate(heights):
            bh = max(bw, float(v) * maxh)
            d.rounded_rectangle((x, cy - bh / 2, x + bw, cy + bh / 2), radius=bw / 2,
                                fill=(*_grad(th["grad"], i / (n - 1)), 255))
            x += bw + gap
        glow = layer.filter(ImageFilter.GaussianBlur(2.5 * s))
        glow.putalpha(glow.getchannel("A").point(lambda a: int(a * th["glow"])))
        img.alpha_composite(glow)
        img.alpha_composite(layer)

    def _blit(self, img, opacity):
        a = np.asarray(img, dtype=np.uint16)
        rgb = a[..., :3] * a[..., 3:4] // 255  # premultiplied alpha
        bgra = np.dstack([rgb[..., 2], rgb[..., 1], rgb[..., 0], a[..., 3]]).astype(np.uint8)
        w, h = img.size
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        sdc = user32.GetDC(0)
        mdc = gdi32.CreateCompatibleDC(sdc)
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(mdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        ctypes.memmove(bits, bgra.tobytes(), w * h * 4)
        old = gdi32.SelectObject(mdc, hbmp)
        blend = BLENDFUNCTION(0, 0, int(255 * opacity), 1)
        user32.UpdateLayeredWindow(self.hwnd, sdc, ctypes.byref(wintypes.POINT(*self.pos)),
                                   ctypes.byref(wintypes.SIZE(w, h)), mdc, ctypes.byref(wintypes.POINT(0, 0)),
                                   0, ctypes.byref(blend), 2)
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(0, sdc)

    def _tick(self):
        now = time.time()
        visible = self.state != "hidden"
        self.alpha += ((1.0 if visible else 0.0) - self.alpha) * 0.3
        if visible or self.alpha > 0.02:
            try:
                self._blit(self._render(now), min(1.0, self.alpha))
            except Exception:
                if not getattr(self, "_err_logged", False):
                    self._err_logged = True
                    logging.getLogger("dictate").exception("Плашка не отрисовалась")
            self.root.after(16, self._tick)
        else:
            # Спрятались — анимацию останавливаем полностью, в простое окно ничего не делает
            self.win.withdraw()
            self.ticking = False
