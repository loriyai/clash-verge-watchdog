"""Ubuntu status indicator, driven by the Tk main thread."""
from pathlib import Path
from tempfile import TemporaryDirectory

STATES = {
    "idle": ("#84909c", "未监控", "M8 12h8"),
    "healthy": ("#179451", "监控正常", "m7 12 3 3 7-7"),
    "warning": ("#c57b05", "等待诊断或需要关注", "M12 6v7m0 3v1"),
    "failed": ("#d53c42", "双地址测速失败", "m8 8 8 8m0-8-8 8"),
}


class Tray:
    def __init__(self, root, show, quit_app):
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import Gtk, GLib, AyatanaAppIndicator3 as Indicator

        if not Gtk.init_check()[0]:
            raise RuntimeError("无法连接桌面托盘")
        self.root = root
        self.context = GLib.MainContext.default()
        self.icons = TemporaryDirectory(prefix="clash-watchdog-icons-")
        for key, (color, _, mark) in STATES.items():
            Path(self.icons.name, key + ".svg").write_text(
                f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">'
                f'<circle cx="12" cy="12" r="10" fill="{color}" stroke="white"/>'
                f'<path d="{mark}" fill="none" stroke="white" stroke-width="2.5" '
                'stroke-linecap="round" stroke-linejoin="round"/></svg>', encoding="utf-8")
        self.indicator = Indicator.Indicator.new("clash-verge-watchdog", self.icon("idle"),
                                                Indicator.IndicatorCategory.APPLICATION_STATUS)
        self.menu = Gtk.Menu()
        self.label = Gtk.MenuItem()
        self.label.set_sensitive(False)
        self.menu.append(self.label)
        for title, action in (("打开监控窗口", show), ("退出监控器", quit_app)):
            item = Gtk.MenuItem.new_with_label(title)
            item.connect("activate", lambda _, callback=action: root.after_idle(callback))
            self.menu.append(item)
        self.menu.show_all()
        self.indicator.set_menu(self.menu)
        self.connected = False
        self.hidden = False
        def connection_changed(_, connected):
            self.connected = connected
            if self.hidden and not connected:
                show()
        self.indicator.connect("connection-changed", connection_changed)
        self.delay_label = None
        self.state = None
        self.update("idle")
        self.passive = Indicator.IndicatorStatus.PASSIVE
        self.indicator.set_status(Indicator.IndicatorStatus.ACTIVE)
        self.timer = root.after(50, self.pump)

    def pump(self):
        for _ in range(20):
            if not self.context.iteration(False):
                break
        self.timer = self.root.after(50, self.pump)

    def icon(self, key):
        return str(Path(self.icons.name, key + ".svg"))

    def update(self, key, delay_ms=None, recovery_status=""):
        label = f"{delay_ms} ms" if delay_ms is not None else ("—" if key != "idle" else "")
        if recovery_status:
            label = recovery_status
        if label != self.delay_label:
            self.indicator.set_label(label, "")
            self.delay_label = label
        if key != self.state:
            self.state = key
            title = "Clash Verge 监控器 · " + STATES[key][1]
            self.label.set_label(title)
            self.indicator.set_icon_full(self.icon(key), title)

    def close(self):
        self.root.after_cancel(self.timer)
        self.indicator.set_status(self.passive)
        self.icons.cleanup()
