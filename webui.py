"""Окна на WebView2: настройки и установщик. Живут в отдельном процессе, только пока открыты."""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
import winreg
from pathlib import Path

import engine
from engine import config, stats

k32 = ctypes.windll.kernel32
FROZEN = getattr(sys, "frozen", False)

CONFIG_EVENT = "Local\\DictateConfigChanged"   # настройки → основной процесс: «перечитай конфиг»
SHOW_EVENT = "Local\\DictateShowSettings"      # кто угодно → основной процесс: «открой настройки»
QUIT_EVENT = "Local\\DictateQuit"              # установщик/обновление → основной процесс: «закройся»
FOCUS_EVENT = "Local\\DictateSettingsFocus"    # → окну настроек: «выйди на передний план»

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Dictate"
SAFE_URLS = ("https://openrouter.ai/", "https://github.com/m1nges/", "https://yoomoney.ru/fundraise/1KJUSBQONDU.260930")


def signal(name):
    ev = k32.OpenEventW(0x0002, False, name)
    if ev:
        k32.SetEvent(ev)
        k32.CloseHandle(ev)
        return True
    return False


def resource(*parts):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def exe_command(exe=None):
    if FROZEN:
        return f'"{exe or sys.executable}"'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pyw}" "{resource("app.py")}"'


def set_autostart(on, exe=None):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, "Dictate", 0, winreg.REG_SZ, exe_command(exe))
        else:
            try:
                winreg.DeleteValue(k, "Dictate")
            except FileNotFoundError:
                pass


def read_status():
    try:
        return json.loads(engine.STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _window_chrome(win):
    """Иконка и тёмная рамка окна под тему Windows."""
    try:
        import clr  # noqa: F401
        from System.Drawing import Icon
        win.native.Icon = Icon(resource("dictate.ico"))
    except Exception:
        pass
    try:
        from overlay import system_theme
        if system_theme() == "dark":
            hwnd = win.native.Handle.ToInt64()
            val = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 20, ctypes.byref(val), 4)
    except Exception:
        pass


def _make_window(title, page, api, width, height):
    import webview
    from overlay import system_theme

    dark = system_theme() == "dark"
    win = webview.create_window(title, url=resource("ui", page), js_api=api, width=width, height=height,
                                resizable=False, hidden=True, background_color="#131218" if dark else "#f5f4f9")
    api._window = win
    win.events.before_show += lambda: _window_chrome(win)
    # страховка: если страница не позвала shown(), показываем окно, как только она загрузилась
    win.events.loaded += lambda: threading.Timer(0.4, win.show).start()
    return win


def _log_to_file():
    import logging
    engine.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:
        sink = open(engine.DATA_DIR / "ui.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or sink
        sys.stderr = sys.stderr or sink
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)


def _start():
    import webview
    # своя временная папка движка на каждое окно: никаких «папка занята» и хвостов на диске
    import tempfile
    webview.start(private_mode=True, storage_path=tempfile.mkdtemp(prefix="dictate-ui-"))


# ---------------------------------------------------------------- настройки

class SettingsApi:
    def __init__(self):
        self._window = None
        self.rec = None
        self.has_nvidia = False
        self.cuda_progress = None
        threading.Thread(target=self._detect, daemon=True).start()

    def _detect(self):
        hw = engine.hardware()
        self.has_nvidia = bool(hw["gpu"]) or engine.has_nvidia()
        dev, model, why = engine.recommend(hw)
        parts = [hw["cpu"] or "процессор", f"{hw['ram_gb']:.0f} ГБ ОЗУ"]
        if hw["gpu"]:
            parts.insert(0, f"{hw['gpu']} {hw['vram_gb']:.0f} ГБ")
        self.rec = {"device": dev, "model": model, "why": why[:1].upper() + why[1:], "hw": ", ".join(parts)}

    def get_state(self):
        config.reload()
        stats.reload()
        st = read_status()
        cfg = {k: v for k, v in config.data.items() if k != "openrouter_key_enc"}
        return {
            "version": engine.VERSION, "config": cfg, "stats": stats.data, "api_key": engine.get_api_key(),
            "status": st.get("status", "Программа не запущена"), "engine": st.get("engine"), "update": st.get("update"),
            "key_label": engine.key_label(config["hotkey"]), "has_nvidia": self.has_nvidia,
            "cuda_ready": engine.cuda_ready(), "cuda_progress": self.cuda_progress, "rec": self.rec,
        }

    def set(self, key, value):
        if key not in engine.DEFAULTS:
            return self.get_state()
        if key == "autostart":
            set_autostart(bool(value))
        config.update(**{key: value})
        signal(CONFIG_EVENT)
        return self.get_state()

    def set_api_key(self, value):
        engine.set_api_key(value)
        signal(CONFIG_EVENT)

    def apply_recommended(self):
        if self.rec:
            config.update(device=self.rec["device"], model=self.rec["model"])
            signal(CONFIG_EVENT)
        return self.get_state()

    def download_cuda(self):
        if isinstance(self.cuda_progress, dict) and "error" not in self.cuda_progress:
            return self.get_state()
        self.cuda_progress = {"done": 0, "total": 0}

        def prog(done, total):
            self.cuda_progress = {"done": done, "total": total}

        def run():
            try:
                engine.download_cuda(prog)
                self.cuda_progress = None
                signal(CONFIG_EVENT)
            except Exception as e:
                self.cuda_progress = {"error": str(e)}

        threading.Thread(target=run, daemon=True).start()
        return self.get_state()

    def open_update(self):
        upd = read_status().get("update")
        webbrowser.open(upd[1] if upd else f"https://github.com/{engine.REPO}/releases/latest")

    def open_url(self, url):
        if url.startswith(SAFE_URLS):
            webbrowser.open(url)

    def shown(self):
        self._window.show()


def run_settings():
    _log_to_file()
    k32.CreateMutexW(None, False, "Local\\DictateSettingsUI")
    if k32.GetLastError() == 183:  # окно уже открыто — просто поднимем его
        signal(FOCUS_EVENT)
        return
    api = SettingsApi()
    win = _make_window("Диктовка — настройки", "settings.html", api, 560, 800)

    def focus_loop():
        ev = k32.CreateEventW(None, False, False, FOCUS_EVENT)
        while True:
            k32.WaitForSingleObject(ev, 0xFFFFFFFF)
            win.restore()
            win.show()

    threading.Thread(target=focus_loop, daemon=True).start()
    _start()


# ---------------------------------------------------------------- установка

def shortcut(lnk, target, workdir):
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
          f"$s.TargetPath='{target}';$s.WorkingDirectory='{workdir}';$s.IconLocation='{target},0';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   creationflags=0x08000000, timeout=30, check=False)


def start_menu_dir():
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def desktop_dir():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as k:
            return Path(winreg.QueryValueEx(k, "Desktop")[0])
    except OSError:
        return Path.home() / "Desktop"


def migrate_data(dst):
    """Переносит модели, ускорение и настройки из старых мест в папку установки."""
    old = [engine.DEFAULT_DIR, Path(os.environ.get("APPDATA", "")) / engine.APP]
    for src in old:
        if not src.exists() or src.resolve() == dst.resolve():
            continue
        for item in src.iterdir():
            target = dst / item.name
            if item.name in ("webview",) or target.exists():
                continue
            try:
                shutil.move(str(item), str(target))
            except OSError:
                pass
        try:
            src.rmdir()  # только если опустела
        except OSError:
            pass


def register(dst, exe, autostart):
    engine.set_install_dir(dst)
    shortcut(str(start_menu_dir() / "Диктовка.lnk"), str(exe), str(dst))
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
        for name, val in {"DisplayName": "Диктовка", "DisplayVersion": engine.VERSION, "Publisher": "m1nges",
                          "DisplayIcon": f"{exe},0", "InstallLocation": str(dst),
                          "UninstallString": f'"{exe}" --uninstall',
                          "URLInfoAbout": f"https://github.com/{engine.REPO}"}.items():
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
    set_autostart(autostart, exe)


class SetupApi:
    def __init__(self):
        self._window = None

    def default_dir(self):
        return str(engine.DEFAULT_DIR)

    def final_dir(self, path):
        p = Path(path.strip().strip('"'))
        return str(p if p.name.lower() == "dictate" else p / "Dictate")

    def pick_dir(self, current):
        import webview
        res = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=str(Path(current).parent))
        return res[0] if res else None

    def install(self, path, desktop, autostart):
        dst = Path(self.final_dir(path))
        try:
            dst.mkdir(parents=True, exist_ok=True)
            test = dst / ".write_test"
            test.write_text("ok")
            test.unlink()
        except OSError as e:
            return {"ok": False, "error": f"В эту папку нельзя записать: {e}"}
        exe = dst / "Dictate.exe"
        try:
            if Path(sys.executable).resolve() != exe.resolve():
                shutil.copy2(sys.executable, exe)
            migrate_data(dst)
            register(dst, exe, autostart)
            if desktop:
                shortcut(str(desktop_dir() / "Диктовка.lnk"), str(exe), str(dst))
        except Exception as e:
            return {"ok": False, "error": str(e)}
        subprocess.Popen([str(exe)], cwd=str(dst), creationflags=0x00000008)  # DETACHED_PROCESS
        threading.Timer(0.6, self._window.destroy).start()
        return {"ok": True, "path": str(dst)}

    def shown(self):
        self._window.show()

    def close(self):
        self._window.destroy()


def run_setup():
    _log_to_file()
    api = SetupApi()
    _make_window("Установка Диктовки", "setup.html", api, 560, 560)
    _start()


# ---------------------------------------------------------------- обновление и удаление

def wait_main_closed(timeout=8.0):
    """Просим работающую копию закрыться и ждём, пока освободится её мьютекс."""
    if not signal(QUIT_EVENT):
        return True
    end = time.time() + timeout
    while time.time() < end:
        h = k32.OpenMutexW(0x00100000, False, "Local\\DictateApp")  # SYNCHRONIZE
        if not h:
            return True
        k32.CloseHandle(h)
        time.sleep(0.2)
    return False


def close_other_copies():
    """Окно настроек и прочие копии держат exe открытым — закрываем всё, кроме себя (и своего загрузчика)."""
    subprocess.run(["taskkill", "/F", "/IM", "Dictate.exe", "/FI", f"PID ne {os.getpid()}",
                    "/FI", f"PID ne {os.getppid()}"], creationflags=0x08000000, capture_output=True, check=False)
    time.sleep(0.5)


def update_installed(dst):
    """Запустили скачанный exe, а программа уже установлена: встаём на место старой версии."""
    exe = dst / "Dictate.exe"
    wait_main_closed()
    close_other_copies()
    for _ in range(25):
        try:
            shutil.copy2(sys.executable, exe)
            break
        except OSError:
            time.sleep(0.2)
    else:
        ctypes.windll.user32.MessageBoxW(None, f"Не получилось обновить {exe}. Закрой Диктовку и попробуй ещё раз.",
                                         "Диктовка", 0x10)
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, engine.VERSION)
    except OSError:
        pass
    subprocess.Popen([str(exe), "--updated"], cwd=str(dst), creationflags=0x00000008)


def uninstall():
    dst = engine.install_dir()
    if not dst:
        return
    msg = (f"Удалить Диктовку?\n\nБудет удалена папка {dst} вместе с моделями и настройками, "
           "ярлыки и автозапуск.")
    if ctypes.windll.user32.MessageBoxW(None, msg, "Удаление Диктовки", 0x04 | 0x30) != 6:  # IDYES
        return
    wait_main_closed()
    close_other_copies()
    set_autostart(False)
    for lnk in (start_menu_dir() / "Диктовка.lnk", desktop_dir() / "Диктовка.lnk"):
        try:
            lnk.unlink()
        except OSError:
            pass
    for key in (UNINSTALL_KEY, engine.REG_KEY):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except OSError:
            pass
    # exe не может удалить сам себя — поручаем это cmd после выхода
    subprocess.Popen(f'cmd /c ping 127.0.0.1 -n 3 >nul & rmdir /s /q "{dst}"',
                     creationflags=0x08000000, cwd=os.environ.get("TEMP", "C:\\"))
