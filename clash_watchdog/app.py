"""Ubuntu desktop window. Tk is imported lazily so --self-check needs no display."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import platform as host_platform
import queue
import subprocess
import sys
import threading
import time

from . import autostart, __version__
from .store import Store, InstanceLock
from .platform_linux import LinuxPlatform, read_connection, default_data_directory


def self_check():
    result = {"version": __version__, "system": host_platform.system(),
              "architecture": host_platform.machine(), "python": host_platform.python_version(),
              "desktop_session": os.environ.get("XDG_SESSION_TYPE", "unknown"),
              "display_present": bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))}
    try:
        import tkinter
        result["tk"] = tkinter.TkVersion
    except ImportError:
        result["tk"] = "缺少 python3-tk"
    runtime = LinuxPlatform()
    try:
        inspection = runtime.inspect_app()
        result["clash_process"] = inspection.state
        result["clash_process_message"] = inspection.message
        ready = runtime.probe_restart()
        result["restart_available"] = ready.available
        result["restart_message"] = ready.message
    except Exception as exc:
        result["restart_available"] = False
        result["restart_message"] = str(exc)
    result["read_only"] = True
    return result


class Window:
    NAVY, GREEN, AMBER, RED = "#182c49", "#1f5b45", "#6f5024", "#74312e"

    def __init__(self, root, store, state):
        import tkinter as tk
        from tkinter import ttk, messagebox, filedialog
        from .controller import Monitor
        self.tk, self.ttk, self.messagebox, self.filedialog = tk, ttk, messagebox, filedialog
        self.root, self.store, self.state = root, store, state
        self.events = queue.Queue()
        self.lifetime_cancel = threading.Event()
        self.monitor = Monitor(store, state, lambda event, data: self.events.put((event, data)))
        self.busy, self.connected, self.closing = False, False, False
        self.ready_at = 0.0
        self.recovery_status = ""
        self.health = None
        root.title(f"Clash Verge 监控器 · Ubuntu {__version__}")
        root.geometry("1040x850")
        root.minsize(940, 760)
        root.configure(bg="#f5f7fb")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f5f7fb")
        style.configure("TLabel", background="#f5f7fb", foreground="#203047", padding=2)
        style.configure("TButton", padding=(12, 7))
        style.configure("TCheckbutton", background="#f5f7fb", padding=4)
        style.configure("TNotebook.Tab", padding=(18, 8))
        root.option_add("*Font", ("Noto Sans CJK SC", 10))

        self.header = tk.Frame(root, bg=self.NAVY, padx=22, pady=14)
        self.header.pack(fill="x")
        self.title = tk.Label(self.header, text=f"Clash Verge 监控器  v{__version__}", bg=self.NAVY,
                              fg="white", font=("Noto Sans CJK SC", 19, "bold"), anchor="w")
        self.title.pack(fill="x")
        self.subtitle = tk.Label(self.header, text="Ubuntu 版 · 跟随当前节点 · 双地址复核 · 有限自动恢复",
                                bg=self.NAVY, fg="#d2e1e7", anchor="w")
        self.subtitle.pack(fill="x", pady=(5, 0))
        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True, padx=12, pady=10)
        main = ttk.Frame(notebook, padding=16)
        setup = ttk.Frame(notebook, padding=20)
        notebook.add(main, text="监控")
        notebook.add(setup, text="连接设置 / 使用说明")
        main.columnconfigure(1, weight=1)
        main.columnconfigure(3, weight=1)
        main.rowconfigure(10, weight=1)
        self.vars = {key: tk.StringVar(value=str(state[key])) for key in
                     ("controller", "secret", "group", "test_url", "executable", "config_directory")}
        if not self.vars["config_directory"].get():
            self.vars["config_directory"].set(default_data_directory())
        if not self.vars["executable"].get():
            self.vars["executable"].set(self.monitor.platform.find_executable())
        self.inputs = []
        self._entry(main, "控制地址", "controller", 0, 0)
        self._entry(main, "密钥", "secret", 0, 2, show="•")
        self._entry(main, "测速地址", "test_url", 1, 0, span=3)
        ttk.Label(main, text="跟随代理组").grid(row=2, column=0, sticky="w", pady=5)
        self.groups = ttk.Combobox(main, textvariable=self.vars["group"])
        self.groups.grid(row=2, column=1, columnspan=3, sticky="ew", padx=(10, 0), pady=5)
        self.inputs.append(self.groups)
        actions = ttk.Frame(main)
        actions.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(10, 12))
        self.auto_connect_button = ttk.Button(actions, text="一键配置连接", command=self.read_config)
        self.connect_button = ttk.Button(actions, text="连接并加载", command=self.connect)
        self.start_button = ttk.Button(actions, text="开始监控", command=self.start)
        self.stop_button = ttk.Button(actions, text="停止", command=self.stop)
        self.check_button = ttk.Button(actions, text="检查重启入口", command=lambda: self.worker(self.monitor.check_restart))
        self.resume_button = ttk.Button(actions, text="恢复自动重启", command=self.resume)
        for button in (self.auto_connect_button, self.connect_button, self.start_button, self.stop_button, self.check_button, self.resume_button):
            button.pack(side="left", padx=(0, 8))

        cards = tk.Frame(main, bg="white", padx=14, pady=10)
        cards.grid(row=4, column=0, columnspan=4, sticky="ew")
        self.labels = {}
        defaults = {
            "status": "等待连接", "selection": "当前节点：—", "probe": "主动测速：尚无结果",
            "failures": "连续失败：0 / 5 轮", "readiness": "自动重启：尚未检查 D-Bus 菜单入口",
            "history": "内核历史参考：尚无记录（不等同于 Clash 界面当前读数）",
            "routes": "实际连接：监控时每 60 秒只读观察相关连接",
            "summary": "近 15 分钟：尚无诊断轮次", "app": "主程序退出后自动启动：" + ("已开启" if state["automatic_launch"] else "已关闭"),
        }
        for key, text in defaults.items():
            label = tk.Label(cards, text=text, bg="white", fg="#24364a", anchor="w", justify="left",
                             wraplength=910, font=("Noto Sans CJK SC", 11 if key == "status" else 10,
                                                   "bold" if key == "status" else "normal"))
            label.pack(fill="x", pady=2)
            self.labels[key] = label
        cards.bind("<Configure>", lambda e: [label.configure(wraplength=max(200,e.width-30)) for label in self.labels.values()])

        option_frame = ttk.Frame(main)
        option_frame.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(10, 4))
        self.option_vars = {}
        specs = [("正常测速 (秒)", "normal_seconds", 15, 3600), ("失败复测 (秒)", "retry_seconds", 1, 60),
                 ("超时 (秒)", "timeout_seconds", 1, 30), ("连续失败轮数", "failure_threshold", 1, 20),
                 ("宽限期 (秒)", "grace_seconds", 0, 600), ("重启冷却 (秒)", "cooldown_seconds", 120, 3600),
                 ("小时重启上限", "hourly_limit", 1, 10)]
        for i, (title, key, minimum, maximum) in enumerate(specs):
            box = ttk.Frame(option_frame)
            box.grid(row=i//4, column=i%4, sticky="w", padx=(0, 14), pady=4)
            ttk.Label(box, text=title).pack(side="left")
            variable = tk.StringVar(value=str(state["options"][key]))
            self.option_vars[key] = variable
            spin = ttk.Spinbox(box, textvariable=variable, from_=minimum, to=maximum, width=6)
            spin.pack(side="left", padx=4)
            self.inputs.append(spin)
        toggles = ttk.Frame(main)
        toggles.grid(row=6, column=0, columnspan=4, sticky="ew", pady=5)
        self.auto_restart = tk.BooleanVar(value=state["automatic_restart"])
        self.auto_launch = tk.BooleanVar(value=state["automatic_launch"])
        restart_check = ttk.Checkbutton(toggles, text="启用自动重启内核", variable=self.auto_restart)
        restart_check.pack(side="left")
        self.inputs.append(restart_check)
        ttk.Checkbutton(toggles, text="Clash 退出后自动启动", variable=self.auto_launch,
                        command=self.change_launch).pack(side="left", padx=12)
        self.login_enabled = tk.BooleanVar(value=autostart.entry_path().is_file())
        self.login_check = ttk.Checkbutton(toggles, text="登录时自动监控（收进托盘）",
                                          variable=self.login_enabled, command=self.change_login)
        self.login_check.pack(side="left")
        ttk.Button(toggles, text="打开日志目录", command=self.open_logs).pack(side="right")
        ttk.Label(main, text="自动启动默认开启；主动退出 Clash 前先取消开关。最小化可继续监控，托盘可用时关闭窗口可后台监控。",
                  wraplength=930).grid(row=7, column=0, columnspan=4, sticky="w", pady=3)
        ttk.Label(main, text="Ubuntu 通过 Clash 自身的 D-Bus 菜单重启内核；界面缓存与内核历史仅作参考。",
                  wraplength=930).grid(row=8, column=0, columnspan=4, sticky="w", pady=(0, 5))
        log_frame = ttk.Frame(main)
        log_frame.grid(row=10, column=0, columnspan=4, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=5, wrap="word", bg="white", fg="#26374a", relief="flat", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

        setup.columnconfigure(1, weight=1)
        ttk.Label(setup, text="首次连接", font=("Noto Sans CJK SC", 15, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 14))
        self._entry(setup, "Clash 配置目录", "config_directory", 1, 0)
        ttk.Button(setup, text="选择目录…", command=self.choose_directory).grid(row=1, column=2, padx=8)
        self._entry(setup, "Clash 程序路径", "executable", 2, 0)
        ttk.Button(setup, text="选择程序…", command=self.choose_executable).grid(row=2, column=2, padx=8)
        setup_buttons = ttk.Frame(setup)
        setup_buttons.grid(row=3, column=0, columnspan=3, sticky="w", pady=14)
        self.read_button = ttk.Button(setup_buttons, text="一键配置连接", command=self.read_config)
        self.read_button.pack(side="left", padx=(0, 10))
        self.launch_button = ttk.Button(setup_buttons, text="打开 Clash Verge", command=self.launch_clash)
        self.launch_button.pack(side="left")
        instructions = (
            "1. 安装并启动 Clash Verge Rev，保持在同一个 Ubuntu 用户的桌面会话。\n"
            "2. 点击“一键配置连接”，自动发现本机接口并加载代理组。\n"
            "3. 选择当前使用的代理组，检查重启入口，然后开始监控。\n\n"
            "如果接口未开启：在 Clash Verge 的 Clash 设置中启用外部控制器，\n"
            "监听地址设为 127.0.0.1:9097，并设置密钥；两边填写相同地址和密钥。\n"
            "本程序读取现有配置，不会自动改写 Clash 的订阅或配置文件。\n\n"
            "正常每 30 秒测速；两个地址都失败才计 1 轮；5 秒后复测，连续 5 轮才请求重启。\n"
            "主地址失败但备用成功显示“部分连接异常”，不会触发重启。\n"
            "内核重启每滚动小时最多 5 次；未恢复时按 2 / 5 / 10 分钟退避，额度到期自动恢复。\n\n"
            "Clash 主程序自动启动是独立开关，默认开启。确认退出 10 秒后尝试启动，\n"
            "至少间隔 2 分钟、每小时最多 3 次。无法区分崩溃与主动退出。\n\n"
            "Ubuntu 版通过当前用户的 D-Bus 菜单定位 Clash，支持 Wayland / X11 的同一接口。\n"
            "若系统没有提供此接口，界面会说明原因；不能仅凭网络失败猜测或强杀进程。\n"
            "Ubuntu 版显示内核历史参考；不会读取或覆盖 Clash 窗口的延迟缓存。\n\n"
            "连接密钥保存在当前用户的私有配置目录中（目录 700、文件 600），不会写入日志。\n"
            "请使用普通用户启动监控器；不需要用 sudo 运行桌面程序。"
        )
        ttk.Label(setup, text=instructions, justify="left", wraplength=890).grid(row=4, column=0, columnspan=3, sticky="nw")
        self.tray = None
        root.protocol("WM_DELETE_WINDOW", self.hide_or_close)
        self._buttons()
        self.monitor.log(f"session_start version={__version__} platform={host_platform.system()} arch={host_platform.machine()}")
        root.after(100, self.drain)

    def _entry(self, frame, title, key, row, col, span=1, show=None):
        self.ttk.Label(frame, text=title).grid(row=row, column=col, sticky="w", pady=5)
        entry = self.ttk.Entry(frame, textvariable=self.vars[key], show=show or "")
        entry.grid(row=row, column=col+1, columnspan=span, sticky="ew", padx=(10, 0), pady=5)
        self.inputs.append(entry)

    def values(self):
        state = copy.deepcopy(self.monitor.state)
        for key, variable in self.vars.items():
            state[key] = variable.get().strip() if key != "secret" else variable.get()
        for key, variable in self.option_vars.items():
            state["options"][key] = int(variable.get())
        state["automatic_restart"] = self.auto_restart.get()
        state["automatic_launch"] = self.auto_launch.get()
        if not state["test_url"].startswith("https://"):
            raise ValueError("测速地址需要使用 HTTPS")
        return state

    def _buttons(self):
        running = self.monitor.running
        state = "disabled" if running or self.busy else "normal"
        self.login_check.configure(state="disabled" if self.busy else "normal")
        for widget in self.inputs:
            widget.configure(state=state)
        for widget in (self.auto_connect_button, self.connect_button, self.check_button, self.read_button, self.launch_button):
            widget.configure(state=state)
        self.start_button.configure(state="normal" if self.connected and not running and not self.busy else "disabled")
        self.stop_button.configure(state="normal" if running else "disabled")
        self.resume_button.configure(state="normal" if running else "disabled")

    def worker(self, action):
        if self.busy or self.closing:
            return
        self.busy = True
        self._buttons()
        def run():
            try:
                if not self.lifetime_cancel.is_set():
                    action()
            except Exception as exc:
                self.events.put(("error", str(exc)))
            finally:
                self.events.put(("idle", None))
        threading.Thread(target=run, name="watchdog-setup", daemon=True).start()

    def connect(self):
        try:
            self.monitor.configure(self.values())
            self.connected = False
            self.worker(self.monitor.connect)
        except Exception as exc:
            self.messagebox.showerror("连接设置", str(exc), parent=self.root)

    def start(self):
        try:
            self.monitor.configure(self.values())
            self.monitor.start()
            self.labels["status"].configure(text="监控已启动 · 等待新诊断")
            self._buttons()
        except Exception as exc:
            self.messagebox.showerror("开始监控", str(exc), parent=self.root)

    def stop(self):
        self.monitor.stop()
        self._buttons()

    def resume(self):
        try:
            self.monitor.resume_recovery()
        except Exception as exc:
            self.messagebox.showerror("恢复自动重启", str(exc), parent=self.root)

    def change_launch(self):
        try:
            self.monitor.set_automatic_launch(self.auto_launch.get())
        except Exception as exc:
            self.auto_launch.set(False)
            self.messagebox.showerror("自动启动设置", str(exc), parent=self.root)

    def choose_directory(self):
        if self.monitor.running or self.busy:
            return
        path = self.filedialog.askdirectory(parent=self.root)
        if path:
            self.vars["config_directory"].set(path)

    def choose_executable(self):
        if self.monitor.running or self.busy:
            return
        path = self.filedialog.askopenfilename(parent=self.root, title="选择 Clash Verge 程序")
        if path:
            self.vars["executable"].set(path)

    def change_login(self):
        try:
            if self.login_enabled.get() and not self.monitor.running:
                state = self.values()
                if not self.connected and not self.store.path.exists():
                    config = read_connection(state["config_directory"])
                    state.update(controller=config["controller_url"], secret=config["secret"])
                    self.vars["controller"].set(state["controller"])
                    self.vars["secret"].set(state["secret"])
                self.monitor.configure(state)
            autostart.set_enabled(self.login_enabled.get(), self.store.directory)
        except Exception as exc:
            self.login_enabled.set(autostart.entry_path().is_file())
            self.messagebox.showerror("登录自启动", str(exc), parent=self.root)

    def read_config(self):
        path = self.vars["config_directory"].get()
        self.connected = False
        try:
            state = self.values()
        except ValueError as exc:
            self.messagebox.showerror("连接设置", str(exc), parent=self.root)
            return
        def discover():
            config = read_connection(path)
            state.update(controller=config["controller_url"], secret=config["secret"],
                         config_directory=config["data_directory"])
            self.monitor.configure(state)
            self.events.put(("read_config", config))
            self.monitor.connect()
        self.worker(discover)

    def launch_clash(self):
        path = self.vars["executable"].get()
        self.worker(lambda: self.events.put(("notice", self.monitor.platform.launch_app(path, self.lifetime_cancel).message)))

    def open_logs(self):
        if sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", str(self.store.directory)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    def _header(self):
        color, note = self.NAVY, "Ubuntu 版 · 跟随当前节点 · 双地址复核 · 有限自动恢复"
        if self.monitor.running:
            health = self.health or {}
            state = health.get("state", "waiting")
            ready = self.monitor.last_readiness
            warning = (self.auto_restart.get() and (not ready or not ready.available or time.time()-self.ready_at>90))
            warning = warning or (self.auto_launch.get() and self.monitor.last_app_state != "running")
            color = self.RED if state == "failed" else self.GREEN if (
                state == "healthy" and not warning and not health.get("paused") and time.time() >= health.get("grace", 0)
            ) else self.AMBER
            note = "正在监控 · 分项状态见下方；测速正常不等于所有应用连接正常"
        if self.recovery_status:
            color, note = self.AMBER, self.recovery_status
        if self.tray:
            self.tray.update({self.NAVY: "idle", self.GREEN: "healthy", self.AMBER: "warning",
                              self.RED: "failed"}[color],
                             (self.health or {}).get("delay_ms") if self.monitor.running and not self.recovery_status else None, self.recovery_status)
        self.header.configure(bg=color)
        self.title.configure(bg=color)
        self.subtitle.configure(bg=color, text=note)

    def drain(self):
        if self.closing:
            return
        for _ in range(200):
            try:
                event, data = self.events.get_nowait()
            except queue.Empty:
                break
            if event == "log":
                self.log_text.configure(state="normal")
                self.log_text.insert("end", time.strftime("%H:%M:%S ")+data+"\n")
                if int(self.log_text.index("end-1c").split(".")[0]) > 600:
                    self.log_text.delete("1.0", "100.0")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
            elif event == "connected":
                self.connected = True
                self.groups.configure(values=data["groups"])
                if self.vars["group"].get() not in data["groups"] and data["groups"]:
                    self.vars["group"].set(data["groups"][0])
                self.labels["status"].configure(text=f"已连接 Mihomo {data['version']} · 可以开始监控")
            elif event == "read_config":
                self.vars["controller"].set(data["controller_url"])
                self.vars["secret"].set(data["secret"])
                self.vars["config_directory"].set(data["data_directory"])
                self.connected = False
                self.labels["status"].configure(text="已读取配置，正在连接")
            elif event == "readiness":
                self.ready_at = time.time()
                self.labels["readiness"].configure(text="自动重启："+str(data))
            elif event == "selection":
                self.connected = True
                self.labels["selection"].configure(text="当前选择："+" → ".join(data.chain))
            elif event == "probe":
                result = f"{data['delay']} ms" if data["delay"] else "双地址测试均失败"
                self.labels["probe"].configure(text="主动测速："+result+" · "+time.strftime("%H:%M:%S", time.localtime(data["time"])))
            elif event == "history":
                self.labels["history"].configure(text="内核历史参考："+data+"（非界面读数，不参与故障计数）")
            elif event == "routes":
                self.labels["routes"].configure(text="实际连接："+data.summary)
            elif event == "app_status":
                self.labels["app"].configure(text="主程序退出后自动启动："+str(data))
            elif event == "recovery":
                self.recovery_status = str(data)
            elif event == "health":
                self.health = data
                if self.monitor.running:
                    status = {"waiting":"等待完整诊断", "healthy":"连接正常", "partial":"部分连接异常 · 备用测速成功",
                              "failed":"双地址测试失败", "unknown":"健康状态未知"}.get(data["state"], data["state"])
                    due = data["next_probe"]
                    countdown = "" if due is None else f" · {max(0, int(due-time.time()))} 秒后测速"
                    self.labels["status"].configure(text=status+countdown)
                    self.labels["failures"].configure(text=f"连续失败：{data['failures']} / {data['threshold']} 轮")
                    self.labels["summary"].configure(text=f"近 15 分钟：{data['rounds']} 轮，部分异常 {data['partial']} 轮，双地址失败 {data['failed']} 轮")
                    if data["paused"]:
                        self.labels["readiness"].configure(text="自动重启：上次结果不确定，确认 Clash 状态后点击恢复自动重启")
            elif event == "stopped":
                self.labels["status"].configure(text="已停止")
                self.health = None
            elif event == "notice":
                self.labels["status"].configure(text=str(data))
            elif event == "error":
                self.messagebox.showerror("Clash Verge 监控器", str(data), parent=self.root)
            elif event == "idle":
                self.busy = False
            self._buttons()
        self._header()
        self.root.after(200, self.drain)

    def show(self):
        if self.tray:
            self.tray.hidden = False
        self.root.deiconify()
        self.root.lift()

    def hide_or_close(self):
        if self.tray and self.tray.connected:
            self.tray.hidden = True
            self.root.withdraw()
        else:
            self.close()

    def close(self):
        self.closing = True
        if self.tray:
            self.tray.close()
        self.lifetime_cancel.set()
        self.monitor.stop()
        self.store.log("session_end reason=window_closed")
        def finish():
            if self.monitor.core_busy:
                self.root.after(100, finish)
            else:
                self.root.destroy()
        finish()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Clash Verge 监控器 Ubuntu 版")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--self-check", action="store_true", help="只读检查平台与重启入口，不启动或重启程序")
    parser.add_argument("--login", action="store_true", help="登录时在托盘启动监控")
    parser.add_argument("--data-dir", help="使用独立配置目录")
    parser.add_argument("--ui-smoke", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.self_check:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(self_check(), ensure_ascii=False, indent=2))
        return 0
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk(className="Clash-verge-watchdog")
    root.withdraw()
    store = lock = None
    try:
        if os.name == "posix":
            os.umask(0o077)
        store = Store(args.data_dir)
        if not args.ui_smoke:
            lock = InstanceLock(store.directory)
        state = store.load()
        app = Window(root, store, state)
        if args.ui_smoke:
            root.update_idletasks()
            assert app.auto_launch.get() is state["automatic_launch"]
            assert app.header.cget("background") == app.NAVY
            app.close()
            print("UI smoke passed: saved/default switch, initial header, widget construction; no network/actions")
        else:
            try:
                from .tray import Tray
                app.tray = Tray(root, app.show, app.close)
            except (ImportError, ValueError, RuntimeError) as exc:
                store.log(f"系统托盘不可用：{exc}")
            if args.login:
                app.monitor.start()
                if app.tray:
                    app.tray.hidden = True
                    root.after(10000, lambda: app.show() if not app.tray.connected else None)
                else:
                    app.show()
            else:
                app.show()
            root.mainloop()
    except Exception as exc:
        if args.ui_smoke:
            raise
        messagebox.showerror("Clash Verge 监控器", str(exc), parent=root)
    finally:
        if lock:
            lock.close()
        # Daemon workers may finish a cancelled I/O; handlers close on process exit.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
