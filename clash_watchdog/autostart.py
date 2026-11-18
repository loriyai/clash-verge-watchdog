"""Manage the current user's XDG desktop autostart entry."""
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile


def entry_path():
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "autostart" / "clash-verge-watchdog.desktop"


def _argument(value):
    value = str(value).replace("%", "%%")
    for char in ('\\', '"', '`', '$'):
        value = value.replace(char, '\\' + char)
    return '"' + value.replace('\\', '\\\\') + '"'


def set_enabled(enabled, directory):
    path = entry_path()
    if not enabled:
        path.unlink(missing_ok=True)
        return
    project = Path(__file__).resolve().parents[1]
    if (project / "start").is_file():
        command = [project / "start"]
    elif project == Path("/usr/share/clash-verge-watchdog"):
        command = ["/usr/bin/clash-verge-watchdog"]
    else:
        command = [sys.executable, "-m", "clash_watchdog"]
    command += ["--login", "--data-dir", Path(directory).resolve()]
    workdir = str(project).replace("\\", "\\\\")
    content = ("[Desktop Entry]\nType=Application\nName=Clash Verge 监控器\n"
               + "Exec=" + " ".join(map(_argument, command))
               + "\nPath=" + workdir + "\nTerminal=false\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False, mode="w", encoding="utf-8") as output:
        temp = Path(output.name)
        try:
            output.write(content)
            output.flush()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
