"""Диктовка: держишь клавишу, говоришь, отпускаешь — текст вставляется в активное поле.

Точка входа. Трей, горячая клавиша, очередь распознавания. Окна (настройки, установка) — в webui.py,
в отдельном процессе этого же exe, чтобы в фоне программа оставалась лёгкой.
"""
import ctypes
import json
import logging
import os
import queue
import sys
import threading
import subprocess
import time

# Потоки OpenMP после распознавания не крутятся вхолостую, а сразу засыпают — в простое 0% CPU
os.environ.setdefault("KMP_BLOCKTIME", "0")
os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

import engine  # noqa: E402
from engine import config, stats, log  # noqa: E402

FROZEN = getattr(sys, "frozen", False)
key_label = engine.key_label
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


def single_instance():
    """Вторая копия не запускается, а просит первую открыть настройки."""
    import webui
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW(None, False, "Local\\DictateApp")
    if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        webui.signal(webui.SHOW_EVENT)
        sys.exit(0)


def on_event(name, callback):
    """Ждём именованное событие Windows в фоне (ноль нагрузки, пока его нет)."""
    k32 = ctypes.windll.kernel32
    ev = k32.CreateEventW(None, False, False, name)

    def loop():
        while True:
            k32.WaitForSingleObject(ev, 0xFFFFFFFF)
            try:
                callback()
            except Exception:
                log.exception("Событие %s", name)

    threading.Thread(target=loop, daemon=True).start()


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
        self.tray = None
        self.update = None  # (версия, ссылка), если вышла новая

        threading.Thread(target=self._worker, daemon=True).start()
        self.hotkey = Hotkey(self)
        self._start_tray()
        import webui
        on_event(webui.SHOW_EVENT, self.open_settings)
        on_event(webui.CONFIG_EVENT, self.on_config_changed)
        on_event(webui.QUIT_EVENT, self.quit)
        first_run = not (engine.CONF_DIR / "config.json").exists()
        if first_run:
            config.update()  # фиксируем настройки по умолчанию
            self.open_settings()
        self.reload_engine(notify_ready=first_run)
        threading.Thread(target=self._update_loop, daemon=True).start()

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

    def on_config_changed(self):
        """Окно настроек что-то поменяло: перечитываем и, если нужно, перезагружаем модель."""
        config.reload()
        t = self.transcriber
        if t is None or engine.pick_engine() != (t.device, t.name):
            self.reload_engine()
        if self.tray:
            self.tray.update_menu()

    def _update_loop(self):
        time.sleep(20)  # не мешаем старту
        while True:
            if config["check_updates"]:
                try:
                    upd = engine.check_update()
                    if upd and upd != self.update:
                        self.update = upd
                        self.notify(f"Вышла новая версия {upd[0]}. Скачать — в меню значка в трее.")
                        self.tray.update_menu()
                        self._write_status()
                except Exception as e:
                    log.info("Не удалось проверить обновления: %s", e)
            time.sleep(24 * 3600)

    def open_update(self):
        import webbrowser
        webbrowser.open(self.update[1] if self.update else f"https://github.com/{engine.REPO}/releases/latest")

    def set_status(self, text):
        self.status = text
        if self.tray:
            self.tray.update_menu()
        self._write_status()

    def _write_status(self):
        # окно настроек читает это, чтобы показать, на чём сейчас работаем
        t = self.transcriber
        data = {"status": self.status, "engine": [t.device, t.name] if t else None, "update": self.update}
        try:
            engine.STATUS_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

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

        self.tray = pystray.Icon("Dictate", tray_image(), f"Диктовка {engine.VERSION}", Menu(
            Item(lambda _: f"Вышла версия {self.update[0]} — скачать", self.open_update,
                 visible=lambda _: bool(self.update)),
            Item(lambda _: self.status, None, enabled=False),
            Item(spent, None, enabled=False,
                 visible=lambda _: config["cleanup_openrouter"] or stats["openrouter_usd"] > 0),
            Menu.SEPARATOR,
            Item("Настройки…", self.open_settings, default=True),
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
        # отдельный процесс того же exe: окно живёт, только пока открыто
        cmd = [sys.executable, "--settings"] if FROZEN else [sys.executable, os.path.abspath(__file__), "--settings"]
        subprocess.Popen(cmd, cwd=str(engine.DATA_DIR))

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
    args = sys.argv[1:]
    import webui

    if "--settings" in args:
        return webui.run_settings()
    if "--uninstall" in args:
        return webui.uninstall()
    if FROZEN:
        installed = engine.install_dir()
        here = os.path.dirname(os.path.abspath(sys.executable))
        if installed is None:
            return webui.run_setup()  # первый запуск: спрашиваем, куда ставить
        if os.path.normcase(str(installed)) != os.path.normcase(here):
            return webui.update_installed(installed)  # запустили скачанную версию — обновляем установленную

    setup_logging()
    single_instance()
    log.info("Старт v%s, папка: %s", engine.VERSION, engine.DATA_DIR)
    app = App()
    if "--updated" in args:
        app.notify(f"Диктовка обновлена до версии {engine.VERSION}.")
    app.run()


if __name__ == "__main__":
    main()
