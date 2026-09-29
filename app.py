"""Диктовка: держишь клавишу, говоришь, отпускаешь — текст вставляется в активное поле.

Точка входа. Трей, окно настроек, горячая клавиша, очередь распознавания.
"""
import ctypes
import logging
import os
import queue
import sys
import threading
import time
import winreg

# Потоки OpenMP после распознавания не крутятся вхолостую, а сразу засыпают — в простое 0% CPU
os.environ.setdefault("KMP_BLOCKTIME", "0")
os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

import engine  # noqa: E402
from engine import config, stats, log  # noqa: E402

FROZEN = getattr(sys, "frozen", False)
KEY_NAMES = {"right ctrl": "Правый Ctrl", "left ctrl": "Левый Ctrl", "ctrl": "Ctrl", "right alt": "Правый Alt",
             "alt gr": "Правый Alt", "left alt": "Левый Alt", "right shift": "Правый Shift", "caps lock": "Caps Lock",
             "left windows": "Win", "right windows": "Правый Win", "menu": "Menu", "space": "Пробел",
             "scroll lock": "Scroll Lock", "pause": "Pause", "insert": "Insert"}


def key_label(name):
    return KEY_NAMES.get(name, name.upper() if len(name) <= 3 else name.capitalize())


# ---------------------------------------------------------------- служебное

def setup_logging():
    engine.DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if engine.LOG_FILE.stat().st_size > 1_000_000:
            engine.LOG_FILE.unlink()
    except OSError:
        pass
    if sys.stdout is None or sys.stderr is None:  # exe без консоли: библиотекам некуда печатать
        sink = open(engine.DATA_DIR / "stdout.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or sink
        sys.stderr = sys.stderr or sink
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(engine.LOG_FILE, encoding="utf-8"),
                                  *([logging.StreamHandler()] if not FROZEN else [])])
    for noisy in ("httpx", "httpcore", "huggingface_hub", "urllib3", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


SHOW_EVENT = "Local\\DictateShowSettings"


def single_instance():
    """Вторая копия не запускается, а просит первую открыть настройки."""
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW(None, False, "Local\\DictateApp")
    if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        ev = k32.OpenEventW(0x0002, False, SHOW_EVENT)  # EVENT_MODIFY_STATE
        if ev:
            k32.SetEvent(ev)
        sys.exit(0)


def wait_show_requests(callback):
    k32 = ctypes.windll.kernel32
    ev = k32.CreateEventW(None, False, False, SHOW_EVENT)

    def loop():
        while True:
            k32.WaitForSingleObject(ev, 0xFFFFFFFF)
            callback()

    threading.Thread(target=loop, daemon=True).start()


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command():
    if FROZEN:
        return f'"{sys.executable}"'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pyw}" "{os.path.abspath(__file__)}"'


def set_autostart(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, "Dictate", 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(k, "Dictate")
            except FileNotFoundError:
                pass


def tray_image(active=True, size=64):
    from PIL import Image, ImageDraw

    S = 256
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    a, b = ((124, 104, 255), (255, 92, 168)) if active else ((120, 120, 128), (160, 160, 168))
    for i in range(S):
        t = i / (S - 1)
        gd.line((0, i, S, i), fill=tuple(int(a[k] + (b[k] - a[k]) * t) for k in range(3)) + (255,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).ellipse((8, 8, S - 8, S - 8), fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    bw, gap = 22, 14
    heights = [60, 118, 150, 118, 60]
    x = S / 2 - (len(heights) * bw + (len(heights) - 1) * gap) / 2
    for h in heights:
        d.rounded_rectangle((x, S / 2 - h / 2, x + bw, S / 2 + h / 2), radius=bw / 2, fill=(255, 255, 255, 255))
        x += bw + gap
    return img.resize((size, size), Image.LANCZOS)


# ---------------------------------------------------------------- горячая клавиша

class Hotkey:
    """Держишь клавишу — запись. Нажал с ней любую другую (Ctrl+C и т.п.) — запись отменяется.
    События уходят в отдельный поток по порядку, чтобы не тормозить системный хук клавиатуры."""

    def __init__(self, app):
        import keyboard

        self.app = app
        self.held = False
        self.cancelled = False
        self.capture = None  # колбэк «назначить клавишу»
        self.events = queue.Queue()
        threading.Thread(target=self._loop, daemon=True).start()
        keyboard.hook(self._event)

    def _event(self, e):
        name = (e.name or "").lower()
        if self.capture and e.event_type == "down":
            cb, self.capture = self.capture, None
            cb(name)
            return
        if self.app.paused or not name:
            return
        if name == config["hotkey"]:
            if e.event_type == "down" and not self.held:
                self.held, self.cancelled = True, False
                self.events.put("start")
            elif e.event_type == "up" and self.held:
                self.held = False
                if not self.cancelled:
                    self.events.put("stop")
        elif self.held and e.event_type == "down" and not self.cancelled:
            self.cancelled = True
            self.events.put("cancel")

    def _loop(self):
        while True:
            ev = self.events.get()
            try:
                getattr(self.app, "on_" + ev)()
            except Exception:
                log.exception("Ошибка обработки %s", ev)


# ---------------------------------------------------------------- приложение

class App:
    def __init__(self):
        import tkinter as tk

        from overlay import Overlay

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Диктовка")
        self.rec = engine.Recorder()
        self.overlay = Overlay(self.root, lambda: list(self.rec.levels))
        self.transcriber = None
        self.status = "Запускаюсь…"
        self.paused = False
        self.recording = False
        self.jobs = queue.Queue()
        self.settings = None
        self.cuda_progress = None
        self.tray = None

        threading.Thread(target=self._worker, daemon=True).start()
        self.hotkey = Hotkey(self)
        self._start_tray()
        wait_show_requests(lambda: self.root.after(0, self.open_settings))
        first_run = not (engine.CONF_DIR / "config.json").exists()
        if first_run:
            config.update()  # фиксируем настройки по умолчанию
            self.root.after(300, self.open_settings)
        self.reload_engine(notify_ready=first_run)

    # --- движок
    def reload_engine(self, notify_ready=False):
        def load():
            dev, model = engine.pick_engine()
            where = "видеокарте" if dev == "cuda" else "процессоре"
            cached = any(engine.MODELS_DIR.glob(f"models--*faster-whisper-{model}"))
            self.set_status(f"Загружаю модель {model}…" if cached else
                            f"Скачиваю модель {model} (один раз)…")
            if not cached:
                self.notify(f"Скачиваю модель распознавания {model}. Это один раз, пару минут.")
            try:
                self.transcriber = None
                self.transcriber = engine.Transcriber(dev, model)
            except Exception as e:
                log.exception("Модель не загрузилась")
                if dev == "cuda":
                    log.info("Откатываюсь на процессор")
                    self.transcriber = engine.Transcriber("cpu", "small" if config["model"] == "auto" else model)
                    where = "процессоре"
                else:
                    self.set_status(f"Ошибка загрузки модели: {e}")
                    return
            self.set_status(f"Работает на {where} · {self.transcriber.name}")
            log.info(self.status)
            if notify_ready or not cached:
                self.notify(f"Готово! Держи «{key_label(config['hotkey'])}» и говори.")

        threading.Thread(target=load, daemon=True).start()

    def set_status(self, text):
        self.status = text
        if self.tray:
            self.tray.update_menu()
        if self.settings:
            self.root.after(0, self.settings.refresh)

    def notify(self, text):
        try:
            self.tray.notify(text, "Диктовка")
        except Exception:
            pass

    # --- клавиша
    def on_start(self):
        if not self.transcriber:
            self.overlay.set("error", "Модель ещё грузится")
            return
        try:
            self.rec.start()
        except Exception as e:
            log.exception("Микрофон")
            self.overlay.set("error", "Нет микрофона")
            return
        self.recording = True
        self.overlay.set("recording")

    def on_cancel(self):
        if self.recording:
            self.recording = False
            self.rec.stop()
            self.overlay.set("hidden")

    def on_stop(self):
        if not self.recording:
            self.root.after(1200, lambda: self.overlay.state == "error" and self.overlay.set("hidden"))
            return
        self.recording = False
        audio = self.rec.stop()
        if len(audio) < config["min_seconds"] * engine.SAMPLE_RATE:
            self.overlay.set("hidden")
            return
        self.overlay.set("busy")
        self.jobs.put(audio)

    def _worker(self):
        while True:
            audio = self.jobs.get()
            try:
                t0 = time.time()
                raw = self.transcriber.transcribe(audio, config["language"])
                t1 = time.time()
                if not raw:
                    log.info("Тишина, ничего не вставляю")
                    continue
                text = engine.cleanup(raw)
                t2 = time.time()
                secs = len(audio) / engine.SAMPLE_RATE
                # Сам текст в лог не пишем: там может быть что угодно личное
                log.info("Фраза: %.1fс речи, распознавание %.2fс, чистка %.2fс, %d симв.", secs, t1 - t0, t2 - t1, len(text))
                engine.paste(text + " ")
                stats.update(dictations=stats["dictations"] + 1, audio_seconds=stats["audio_seconds"] + secs)
                if self.settings:
                    self.root.after(0, self.settings.refresh)
            except Exception:
                log.exception("Ошибка распознавания")
            finally:
                if self.jobs.empty() and self.overlay.state == "busy":
                    self.overlay.set("hidden")

    # --- трей
    def _start_tray(self):
        import pystray
        from pystray import Menu, MenuItem as Item

        def spent(_):
            return f"OpenRouter: потрачено ${stats['openrouter_usd']:.4f}"

        self.tray = pystray.Icon("Dictate", tray_image(), "Диктовка", Menu(
            Item(lambda _: self.status, None, enabled=False),
            Item(spent, None, enabled=False,
                 visible=lambda _: config["cleanup_openrouter"] or stats["openrouter_usd"] > 0),
            Menu.SEPARATOR,
            Item("Настройки…", lambda: self.root.after(0, self.open_settings), default=True),
            Item("Пауза", self.toggle_pause, checked=lambda _: self.paused),
            Item("Открыть лог", lambda: os.startfile(engine.LOG_FILE)),
            Menu.SEPARATOR,
            Item("Выход", self.quit),
        ))
        threading.Thread(target=self.tray.run, daemon=True).start()

    def toggle_pause(self):
        self.paused = not self.paused
        self.tray.icon = tray_image(not self.paused)
        self.tray.title = "Диктовка (пауза)" if self.paused else "Диктовка"

    def open_settings(self):
        from settings import SettingsWindow

        if self.settings:
            self.settings.focus()
        else:
            self.settings = SettingsWindow(self)

    def quit(self):
        try:
            self.tray.stop()
        except Exception:
            pass
        self.root.after(0, self.root.destroy)

    def run(self):
        self.root.mainloop()
        os._exit(0)


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    setup_logging()
    single_instance()
    log.info("Старт, настройки: %s", engine.CONF_DIR)
    App().run()


if __name__ == "__main__":
    main()
