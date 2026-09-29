"""Окно настроек. Всё применяется сразу, без кнопки «Сохранить»."""
import ctypes
import threading
import tkinter as tk
from tkinter import ttk

import engine
from engine import config, stats
from overlay import system_theme

PALETTES = {
    "dark": dict(bg="#18171d", card="#221f2b", fg="#ecebf2", dim="#9a96a8", accent="#8b6cff",
                 field="#2c2837", border="#3a3547", warn="#ff8aa8"),
    "light": dict(bg="#f4f3f8", card="#ffffff", fg="#1d1b24", dim="#6d6880", accent="#6a4cf0",
                  field="#f1eff7", border="#dcd8e6", warn="#c0305a"),
}
LANGS = {"ru": "Русский", "en": "English", "uk": "Українська", "auto": "Определять сам"}
DEVICES = {"auto": "Авто", "cuda": "Видеокарта NVIDIA", "cpu": "Процессор"}
MODEL_CHOICES = {"auto": "Авто (подберу сам)", **engine.MODELS}


class SettingsWindow:
    def __init__(self, app):
        self.app = app
        p = self.p = PALETTES[system_theme()]
        w = self.w = tk.Toplevel(app.root)
        w.title("Диктовка — настройки")
        w.configure(bg=p["bg"])
        w.resizable(False, False)
        w.protocol("WM_DELETE_WINDOW", self.close)
        try:
            from PIL import ImageTk
            from app import tray_image
            self._icon = ImageTk.PhotoImage(tray_image())
            w.iconphoto(False, self._icon)
        except Exception:
            pass
        self._style()
        self._switch_imgs = self._make_switches()

        body = tk.Frame(w, bg=p["bg"], padx=18, pady=16)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="Диктовка", bg=p["bg"], fg=p["fg"], font=("Segoe UI Semibold", 16)).pack(anchor="w")
        tk.Label(body, text="Держи клавишу и говори — текст вставится туда, где стоит курсор.",
                 bg=p["bg"], fg=p["dim"], font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 10))

        # --- клавиша
        c = self._card(body, "Клавиша записи")
        row = tk.Frame(c, bg=p["card"])
        row.pack(fill="x")
        self.key_lbl = tk.Label(row, bg=p["field"], fg=p["fg"], font=("Segoe UI Semibold", 11),
                                padx=12, pady=4, highlightthickness=1, highlightbackground=p["border"])
        self.key_lbl.pack(side="left")
        self.key_btn = ttk.Button(row, text="Назначить", style="Accent.TButton", command=self.capture_key)
        self.key_btn.pack(side="left", padx=10)
        self._hint(c, "Зажми и держи, пока говоришь. Отпустил — текст вставился.\n"
                      "Сочетания с этой клавишей (Ctrl+C, Ctrl+V…) работают как обычно, запись от них не включается.")

        # --- распознавание
        c = self._card(body, "Распознавание")
        self.lang = self._combo(c, "Язык", LANGS, config["language"], lambda v: config.update(language=v))
        self.device = self._combo(c, "Где считать", DEVICES, config["device"], self.on_device)
        self.model = self._combo(c, "Модель", MODEL_CHOICES, config["model"], self.on_model)
        self.gpu_row = tk.Frame(c, bg=p["card"])
        self.gpu_row.pack(fill="x", pady=(6, 0))
        self.gpu_lbl = tk.Label(self.gpu_row, bg=p["card"], fg=p["dim"], font=("Segoe UI", 9),
                                justify="left", wraplength=330)
        self.gpu_lbl.pack(side="left")
        self.gpu_btn = ttk.Button(self.gpu_row, text="Скачать", style="Accent.TButton", command=self.download_cuda)
        self.status_lbl = tk.Label(c, bg=p["card"], fg=p["accent"], font=("Segoe UI Semibold", 9))
        self.status_lbl.pack(anchor="w", pady=(8, 0))

        # совет под это железо + раскрывающаяся шпаргалка
        tip = tk.Frame(c, bg=p["field"], padx=10, pady=8)
        tip.pack(fill="x", pady=(8, 0))
        top = tk.Frame(tip, bg=p["field"])
        top.pack(fill="x")
        self.rec_lbl = tk.Label(top, text="Смотрю, что за компьютер…", bg=p["field"], fg=p["fg"],
                                font=("Segoe UI", 9), justify="left", wraplength=320, anchor="w")
        self.rec_lbl.pack(side="left", fill="x", expand=True)
        self.rec_btn = ttk.Button(top, text="Применить", style="Accent.TButton", command=self.apply_recommended)
        self.guide_btn = tk.Label(tip, text="Как выбрать? ▾", bg=p["field"], fg=p["accent"], cursor="hand2",
                                  font=("Segoe UI Semibold", 9))
        self.guide_btn.pack(anchor="w", pady=(6, 0))
        self.guide_btn.bind("<Button-1>", self.toggle_guide)
        self.guide_lbl = tk.Label(tip, text=engine.GUIDE, bg=p["field"], fg=p["dim"], font=("Segoe UI", 9),
                                  justify="left", wraplength=400, anchor="w")
        self.recommended = None
        threading.Thread(target=self._detect, daemon=True).start()

        # --- чистка
        c = self._card(body, "Чистка текста")
        self.local_var = tk.BooleanVar(value=config["cleanup_local"])
        self._check(c, "Убирать слова-паразиты («ну», «типа», «короче», «э-э»)",
                    self.local_var, lambda: config.update(cleanup_local=self.local_var.get()))
        self._hint(c, "Бесплатно, прямо на компьютере, текст никуда не уходит.")
        self.or_var = tk.BooleanVar(value=config["cleanup_openrouter"])
        self._check(c, "Умная чистка через OpenRouter", self.or_var, self.on_openrouter)
        self._hint(c, "Нейросеть лучше убирает оговорки и расставляет знаки. Нужен свой ключ "
                      "с openrouter.ai/keys, стоит доли цента за фразу. Текст уходит на сервер OpenRouter. "
                      "Если он не ответит за 3 секунды, текст почистится обычным способом.")
        self.or_box = tk.Frame(c, bg=p["card"])
        self.or_box.pack(fill="x")
        self.key_var = tk.StringVar(value=engine.get_api_key())
        self._entry(self.or_box, "Твой ключ", self.key_var, show="•")
        self.key_var.trace_add("write", lambda *_: engine.set_api_key(self.key_var.get()))
        self.cm_var = tk.StringVar(value=config["cleanup_model"])
        self._entry(self.or_box, "Модель", self.cm_var)
        self.cm_var.trace_add("write", lambda *_: config.update(cleanup_model=self.cm_var.get().strip()))
        self.spent_lbl = tk.Label(self.or_box, bg=p["card"], fg=p["fg"], font=("Segoe UI Semibold", 10))
        self.spent_lbl.pack(anchor="w", pady=(6, 0))

        # --- прочее
        c = self._card(body, "Прочее")
        self.auto_var = tk.BooleanVar(value=config["autostart"])
        self._check(c, "Запускать вместе с Windows", self.auto_var, self.on_autostart)
        self.stats_lbl = tk.Label(c, bg=p["card"], fg=p["dim"], font=("Segoe UI", 9))
        self.stats_lbl.pack(anchor="w", pady=(6, 0))

        foot = tk.Frame(body, bg=p["bg"])
        foot.pack(fill="x", pady=(12, 0))
        tk.Label(foot, text="Закрыть окно — программа продолжит работать в трее.",
                 bg=p["bg"], fg=p["dim"], font=("Segoe UI", 9)).pack(side="left")
        ttk.Button(foot, text="Готово", style="Accent.TButton", command=self.close).pack(side="right")

        self.refresh()
        w.update_idletasks()
        self._dark_titlebar()
        sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
        w.geometry(f"+{(sw - w.winfo_width()) // 2}+{max(20, (sh - w.winfo_height()) // 2)}")
        self.focus()

    # --- виджеты
    def _style(self):
        p = self.p
        s = ttk.Style(self.w)
        s.theme_use("clam")
        s.configure("TCombobox", fieldbackground=p["field"], background=p["field"], foreground=p["fg"],
                    arrowcolor=p["fg"], bordercolor=p["border"], lightcolor=p["field"], darkcolor=p["field"],
                    padding=4)
        s.map("TCombobox", fieldbackground=[("readonly", p["field"])], foreground=[("readonly", p["fg"])],
              selectbackground=[("readonly", p["field"])], selectforeground=[("readonly", p["fg"])])
        self.w.option_add("*TCombobox*Listbox.background", p["field"])
        self.w.option_add("*TCombobox*Listbox.foreground", p["fg"])
        self.w.option_add("*TCombobox*Listbox.selectBackground", p["accent"])
        self.w.option_add("*TCombobox*Listbox.font", ("Segoe UI", 10))
        s.configure("Accent.TButton", background=p["accent"], foreground="#ffffff", bordercolor=p["accent"],
                    lightcolor=p["accent"], darkcolor=p["accent"], focuscolor=p["accent"],
                    font=("Segoe UI Semibold", 9), padding=(12, 4))
        s.map("Accent.TButton", background=[("active", p["fg"]), ("disabled", p["border"])],
              foreground=[("active", p["bg"])])
        s.configure("TCheckbutton", background=p["card"], foreground=p["fg"], font=("Segoe UI", 10),
                    indicatorbackground=p["field"], indicatorforeground=p["accent"], focuscolor=p["card"])
        s.map("TCheckbutton", background=[("active", p["card"])],
              indicatorbackground=[("selected", p["accent"])], indicatorforeground=[("selected", "#ffffff")])
        s.configure("TEntry", fieldbackground=p["field"], foreground=p["fg"], bordercolor=p["border"],
                    lightcolor=p["field"], darkcolor=p["field"], insertcolor=p["fg"], padding=4)

    def _card(self, parent, title):
        p = self.p
        outer = tk.Frame(parent, bg=p["card"], highlightthickness=1, highlightbackground=p["border"])
        outer.pack(fill="x", pady=5)
        inner = tk.Frame(outer, bg=p["card"], padx=14, pady=10)
        inner.pack(fill="x")
        tk.Label(inner, text=title.upper(), bg=p["card"], fg=p["dim"],
                 font=("Segoe UI Semibold", 8)).pack(anchor="w", pady=(0, 6))
        return inner

    def _hint(self, parent, text):
        tk.Label(parent, text=text, bg=self.p["card"], fg=self.p["dim"], font=("Segoe UI", 9),
                 justify="left", wraplength=400).pack(anchor="w", pady=(4, 2))

    def _combo(self, parent, label, options, value, on_change):
        p = self.p
        row = tk.Frame(parent, bg=p["card"])
        row.pack(fill="x", pady=2)
        tk.Label(row, text=label, bg=p["card"], fg=p["fg"], font=("Segoe UI", 10), width=12,
                 anchor="w").pack(side="left")
        keys = list(options)
        cb = ttk.Combobox(row, values=[options[k] for k in keys], state="readonly", width=34,
                          font=("Segoe UI", 10))
        cb.current(keys.index(value) if value in keys else 0)
        cb.bind("<<ComboboxSelected>>", lambda e: (cb.selection_clear(), on_change(keys[cb.current()])))
        cb.pack(side="left")
        cb.keys = keys
        return cb

    def _make_switches(self):
        """Переключатели в стиле Windows 11, рисуем сами со сглаживанием."""
        from PIL import Image, ImageDraw, ImageTk

        p = self.p
        scale = self.w.winfo_fpixels("1i") / 96
        w, h, ss = int(36 * scale), int(20 * scale), 4
        out = {}
        for on in (False, True):
            img = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            r = h * ss / 2
            if on:
                d.rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), radius=r, fill=p["accent"])
                kx = w * ss - r
                d.ellipse((kx - r * 0.55, r - r * 0.55, kx + r * 0.55, r + r * 0.55), fill="#ffffff")
            else:
                d.rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), radius=r, fill=p["field"],
                                    outline=p["dim"], width=int(1.2 * scale * ss))
                d.ellipse((r - r * 0.45, r - r * 0.45, r + r * 0.45, r + r * 0.45), fill=p["dim"])
            out[on] = ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS))
        return out

    def _check(self, parent, text, var, cmd):
        p = self.p
        row = tk.Frame(parent, bg=p["card"], cursor="hand2")
        row.pack(fill="x", pady=(6, 0))
        sw = tk.Label(row, image=self._switch_imgs[var.get()], bg=p["card"])
        sw.pack(side="left")
        lbl = tk.Label(row, text=text, bg=p["card"], fg=p["fg"], font=("Segoe UI", 10))
        lbl.pack(side="left", padx=(10, 0))

        def toggle(_=None):
            var.set(not var.get())
            cmd()

        var.trace_add("write", lambda *_: sw.configure(image=self._switch_imgs[bool(var.get())]))
        for wdg in (row, sw, lbl):
            wdg.bind("<Button-1>", toggle)

    def _dark_titlebar(self):
        if system_theme() != "dark":
            return
        try:
            hwnd = ctypes.windll.user32.GetParent(self.w.winfo_id())
            val = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))
            # перерисовать рамку
            self.w.withdraw()
            self.w.deiconify()
        except Exception:
            pass

    def _entry(self, parent, label, var, show=None):
        p = self.p
        row = tk.Frame(parent, bg=p["card"])
        row.pack(fill="x", pady=2)
        tk.Label(row, text=label, bg=p["card"], fg=p["fg"], font=("Segoe UI", 10), width=12,
                 anchor="w").pack(side="left")
        ttk.Entry(row, textvariable=var, show=show, width=36, font=("Segoe UI", 10)).pack(side="left")

    # --- совет по железу
    def _detect(self):
        hw = engine.hardware()
        rec = engine.recommend(hw)
        self.w.after(0, self._show_rec, hw, rec)

    def _show_rec(self, hw, rec):
        if not self.w.winfo_exists():
            return
        self.recommended = rec
        dev, model, why = rec
        parts = [hw["cpu"] or "процессор", f"{hw['ram_gb']:.0f} ГБ ОЗУ"]
        if hw["gpu"]:
            parts.insert(0, f"{hw['gpu']} {hw['vram_gb']:.0f} ГБ")
        self.rec_lbl.configure(text=f"Совет: {DEVICES[dev]} + {engine.MODELS[model].split(' (')[0]} — {why}\n"
                                    f"Твой компьютер: {', '.join(parts)}")
        self._update_rec_btn()

    def _update_rec_btn(self):
        if not self.recommended:
            return
        dev, model, _ = self.recommended
        # кнопка не нужна, если сейчас и так работает рекомендованная связка
        t = self.app.transcriber
        if (config["device"], config["model"]) == (dev, model) or (t and (t.device, t.name) == (dev, model)):
            self.rec_btn.pack_forget()
        else:
            self.rec_btn.pack(side="right", padx=(8, 0))

    def apply_recommended(self):
        dev, model, _ = self.recommended
        config.update(device=dev, model=model)
        self.device.current(self.device.keys.index(dev))
        self.model.current(self.model.keys.index(model))
        if dev == "cuda" and not engine.cuda_ready():
            self.refresh()
        else:
            self.app.reload_engine()
        self._update_rec_btn()

    def toggle_guide(self, _=None):
        if self.guide_lbl.winfo_ismapped():
            self.guide_lbl.pack_forget()
            self.guide_btn.configure(text="Как выбрать? ▾")
        else:
            self.guide_lbl.pack(anchor="w", pady=(4, 0))
            self.guide_btn.configure(text="Как выбрать? ▴")

    # --- действия
    def capture_key(self):
        self.key_lbl.configure(text="Нажми клавишу…")
        self.key_btn.state(["disabled"])

        def got(name):
            if name and name != "esc":
                config.update(hotkey=name)
            self.w.after(0, lambda: (self.key_btn.state(["!disabled"]), self.refresh()))

        self.app.hotkey.capture = got

    def on_device(self, v):
        config.update(device=v)
        self._update_rec_btn()
        if v == "cuda" and not engine.cuda_ready():
            self.refresh()  # предложим скачать ускорение
            return
        self.app.reload_engine()

    def on_model(self, v):
        config.update(model=v)
        self._update_rec_btn()
        self.app.reload_engine()

    def on_openrouter(self):
        config.update(cleanup_openrouter=self.or_var.get())
        self.refresh()
        self.app.tray.update_menu()

    def on_autostart(self):
        from app import set_autostart
        try:
            set_autostart(self.auto_var.get())
            config.update(autostart=self.auto_var.get())
        except OSError as e:
            self.auto_var.set(not self.auto_var.get())
            self.status_lbl.configure(text=f"Не получилось изменить автозапуск: {e}")

    def download_cuda(self):
        self.gpu_btn.state(["disabled"])

        def prog(done, total):
            self.app.cuda_progress = (done, total)
            self.w.after(0, self.refresh)

        def run():
            try:
                ok = engine.download_cuda(prog)
            except Exception as e:
                ok = False
                engine.log.exception("Не скачалось ускорение")
                self.app.cuda_progress = str(e)
            else:
                self.app.cuda_progress = None
            if ok:
                self.app.reload_engine()
            self.w.after(0, self.refresh)

        threading.Thread(target=run, daemon=True).start()

    def refresh(self):
        if not self.w.winfo_exists():
            return
        p = self.p
        from app import key_label
        if self.app.hotkey.capture is None:
            self.key_lbl.configure(text=key_label(config["hotkey"]))
        self.status_lbl.configure(text=self.app.status)

        gpu = engine.has_nvidia()
        prog = self.app.cuda_progress
        self.gpu_btn.pack_forget()
        if not gpu:
            self.gpu_lbl.configure(text="Видеокарта NVIDIA не найдена — работаем на процессоре.", fg=p["dim"])
        elif isinstance(prog, tuple):
            done, total = prog
            pct = f"{done * 100 // total}%" if total else f"{done >> 20} МБ"
            self.gpu_lbl.configure(text=f"Качаю ускорение для видеокарты… {pct}", fg=p["accent"])
        elif isinstance(prog, str):
            self.gpu_lbl.configure(text=f"Не получилось скачать: {prog}", fg=p["warn"])
            self.gpu_btn.configure(text="Ещё раз")
            self.gpu_btn.state(["!disabled"])
            self.gpu_btn.pack(side="right")
        elif not engine.cuda_ready():
            self.gpu_lbl.configure(text="Есть видеокарта NVIDIA. Для работы на ней нужно один раз "
                                        "скачать ускорение (553 МБ) — будет в 5–10 раз быстрее.", fg=p["fg"])
            self.gpu_btn.configure(text="Скачать")
            self.gpu_btn.state(["!disabled"])
            self.gpu_btn.pack(side="right")
        else:
            self.gpu_lbl.configure(text="Ускорение для видеокарты установлено.", fg=p["dim"])

        if self.or_var.get():
            self.or_box.pack(fill="x")
        else:
            self.or_box.pack_forget()
        self.spent_lbl.configure(text=f"Потрачено: ${stats['openrouter_usd']:.4f} · "
                                      f"{stats['openrouter_requests']} запросов")
        mins = stats["audio_seconds"] / 60
        self.stats_lbl.configure(text=f"Надиктовано: {stats['dictations']} фраз, {mins:.1f} мин речи")

    def focus(self):
        self.w.deiconify()
        self.w.lift()
        self.w.focus_force()

    def close(self):
        self.app.hotkey.capture = None
        self.w.destroy()
        self.app.settings = None
