#!/usr/bin/env python3
"""Reproducible, allowlisted Ubuntu packages built with Python's standard library.

deb format: https://manpages.debian.org/trixie/dpkg-dev/deb.5.en.html
No maintainer scripts, installation, downloads or application imports are performed.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile
import zipfile


PACKAGE = "clash-verge-watchdog"
VERSION = "1.3.4"
SHARE_ROOT = "ClashVergeWatchdog-Ubuntu-ARM64"
SHARE_ARCHIVE = "ClashVergeWatchdog-v1.3.4-Ubuntu-ARM64-Share.zip"
DEFAULT_EPOCH = int(datetime(2026, 11, 14, tzinfo=timezone.utc).timestamp())
MODULES = ("__init__.py", "__main__.py", "api.py", "app.py", "controller.py",
           "engine.py", "platform_linux.py", "store.py", "tray.py", "autostart.py", "recovery.py")
TESTS = ("test_api.py", "test_engine.py", "test_platform_linux.py", "test_store.py",
         "test_controller.py", "test_app.py", "test_package.py", "test_recovery.py")
DOCUMENTS = ("README.txt", "VALIDATION.txt", "QUICKSTART.txt", "START-HERE.html")
PACKAGING_FILES = ("build_release.py", "clash-verge-watchdog.desktop", "clash-verge-watchdog.svg")
DEPENDENCIES = "python3 (>= 3.10), python3-tk, python3-gi, gir1.2-glib-2.0, gir1.2-ayatanaappindicator3-0.1"
RECOMMENDS = "xdg-utils, fonts-noto-cjk"

LAUNCHER = b'''#!/usr/bin/python3
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, "/usr/share/clash-verge-watchdog")
from clash_watchdog.app import main
raise SystemExit(main())
'''
PORTABLE_LAUNCHER = b'''#!/bin/sh
set -eu
PACKAGE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$PACKAGE_DIR"
exec python3 -m clash_watchdog "$@"
'''


class PackageError(ValueError):
    pass


@dataclass(frozen=True)
class Member:
    data: bytes
    mode: int = 0o644


def _read(project: Path, relative: str, *, required: bool = True) -> bytes | None:
    path = project / relative
    if not path.exists():
        if required:
            raise PackageError(f"Missing required release input: {relative}")
        return None
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(project):
        raise PackageError(f"Release input must be an ordinary project file: {relative}")
    return path.read_bytes()


def _source_inputs(project: Path) -> dict[str, Member]:
    sources: dict[str, Member] = {}
    names = {Path(name).stem for name in MODULES}
    for name in MODULES:
        relative = "clash_watchdog/" + name
        data = _read(project, relative)
        assert data is not None
        try:
            tree = ast.parse(data, filename=relative, feature_version=(3, 10))
        except (SyntaxError, UnicodeError) as error:
            raise PackageError(f"Invalid Python 3.10-compatible source: {relative}") from error
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level and node.module:
                if node.level != 1 or node.module.split(".", 1)[0] not in names:
                    raise PackageError(f"Unpackaged relative import in {relative}: {node.module}")
        sources[relative] = Member(data)
    for name in DOCUMENTS:
        data = _read(project, name, required=name in ("QUICKSTART.txt", "START-HERE.html"))
        if data is not None:
            sources[name] = Member(data)
    for name in PACKAGING_FILES:
        relative = "packaging/" + name
        data = _read(project, relative)
        assert data is not None
        sources[relative] = Member(data, 0o755 if name == "build_release.py" else 0o644)
    for name in TESTS:
        relative = "tests/" + name
        data = _read(project, relative, required=False)
        if data is not None:
            sources[relative] = Member(data)
    return sources


def _directories(files: dict[str, Member]) -> list[str]:
    result: set[str] = set()
    for name in files:
        value = PurePosixPath(name)
        if value.is_absolute() or ".." in value.parts or "\\" in name:
            raise PackageError(f"Unsafe archive member: {name}")
        result.update(str(parent) for parent in value.parents if str(parent) != ".")
    return sorted(result)


def _tar_gzip(files: dict[str, Member], epoch: int) -> bytes:
    output = io.BytesIO()
    # GzipFile with an empty filename and explicit mtime writes stable gzip metadata.
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=epoch, compresslevel=9) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            directories = set(_directories(files))
            for name in sorted(directories | set(files)):
                info = tarfile.TarInfo("./" + name)
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                info.mtime = epoch
                if name in directories:
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    archive.addfile(info)
                else:
                    member = files[name]
                    info.mode = member.mode
                    info.size = len(member.data)
                    archive.addfile(info, io.BytesIO(member.data))
    return output.getvalue()


def _ar(members: list[tuple[str, bytes]], epoch: int) -> bytes:
    output = io.BytesIO()
    output.write(b"!<arch>\n")
    for name, data in members:
        if len(name) > 15 or not name.isascii() or len(data) >= 10**10:
            raise PackageError("Invalid Debian ar member")
        header = (f"{name + '/':<16}{epoch:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n").encode("ascii")
        if len(header) != 60:
            raise PackageError("Invalid Debian ar header size")
        output.write(header)
        output.write(data)
        if len(data) % 2:
            output.write(b"\n")
    return output.getvalue()


def _zip(files: dict[str, Member], epoch: int) -> bytes:
    output = io.BytesIO()
    stamp = datetime.fromtimestamp(max(epoch, 315532800), timezone.utc)
    if stamp.year > 2107:
        raise PackageError("SOURCE_DATE_EPOCH exceeds the ZIP timestamp range")
    date_time = (stamp.year, stamp.month, stamp.day, stamp.hour, stamp.minute, stamp.second)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, member in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=date_time)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | member.mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, member.data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return output.getvalue()


def make_artifacts(project: Path, *, epoch: int = DEFAULT_EPOCH) -> dict[str, bytes]:
    project = project.resolve()
    if type(epoch) is not int or not 0 <= epoch < 2**32:
        raise PackageError("SOURCE_DATE_EPOCH must fit an unsigned 32-bit gzip timestamp")
    sources = _source_inputs(project)
    installed: dict[str, Member] = {}
    for name in MODULES:
        installed[f"usr/share/{PACKAGE}/clash_watchdog/{name}"] = sources["clash_watchdog/" + name]
    installed[f"usr/bin/{PACKAGE}"] = Member(LAUNCHER, 0o755)
    installed[f"usr/share/applications/{PACKAGE}.desktop"] = sources[f"packaging/{PACKAGE}.desktop"]
    installed[f"usr/share/icons/hicolor/scalable/apps/{PACKAGE}.svg"] = sources[f"packaging/{PACKAGE}.svg"]
    for name in DOCUMENTS:
        if name in sources:
            installed[f"usr/share/doc/{PACKAGE}/{name}"] = sources[name]
    installed_size = (sum(len(member.data) for member in installed.values()) + 1023) // 1024
    control = (f"Package: {PACKAGE}\nVersion: {VERSION}\nSection: net\nPriority: optional\n"
               f"Architecture: all\nMaintainer: Local build <noreply@example.invalid>\n"
               f"Installed-Size: {installed_size}\nDepends: {DEPENDENCIES}\nRecommends: {RECOMMENDS}\n"
               "Description: Local Clash Verge connection watchdog\n"
               " Desktop monitor for the local Mihomo controller used by Clash Verge.\n"
               " Uses independent diagnostic rounds, persistent limits and bounded recovery.\n").encode("utf-8")
    checksums = "".join(f"{hashlib.md5(member.data, usedforsecurity=False).hexdigest()}  {name}\n"
                        for name, member in sorted(installed.items())).encode("ascii")
    deb = _ar([("debian-binary", b"2.0\n"),
               ("control.tar.gz", _tar_gzip({"control": Member(control), "md5sums": Member(checksums)}, epoch)),
               ("data.tar.gz", _tar_gzip(installed, epoch))], epoch)
    portable_root = f"{PACKAGE}-{VERSION}"
    portable = {f"{portable_root}/{name}": member for name, member in sources.items()
                if name.startswith("clash_watchdog/") or name in DOCUMENTS}
    portable[f"{portable_root}/launcher.sh"] = Member(PORTABLE_LAUNCHER, 0o755)
    source_root = f"{PACKAGE}-{VERSION}-source"
    source_zip = {f"{source_root}/{name}": member for name, member in sources.items()}
    artifacts = {
        f"{PACKAGE}_{VERSION}_all.deb": deb,
        f"{PACKAGE}_{VERSION}_linux-all.tar.gz": _tar_gzip(portable, epoch),
        f"{PACKAGE}_{VERSION}_source.zip": _zip(source_zip, epoch),
    }
    # The share bundle contains only the native graphical installer and offline
    # instructions. It needs no executable scripts, trust flags or custom sudo.
    share = {
        f"{PACKAGE}_{VERSION}_all.deb": Member(deb),
        "安装说明.txt": sources["QUICKSTART.txt"],
        "开始使用.html": sources["START-HERE.html"],
    }
    share["SHA256SUMS"] = Member("".join(
        f"{hashlib.sha256(member.data).hexdigest()}  {name}\n"
        for name, member in sorted(share.items())
    ).encode("utf-8"))
    artifacts[SHARE_ARCHIVE] = _zip(
        {f"{SHARE_ROOT}/{name}": member for name, member in share.items()}, epoch)
    artifacts["SHA256SUMS"] = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n"
                                         for name, data in sorted(artifacts.items())).encode("ascii")
    return artifacts


def build_release(project: Path, output: Path, *, epoch: int = DEFAULT_EPOCH) -> list[Path]:
    # Validate and fully construct all artifacts before creating any output file.
    artifacts = make_artifacts(project, epoch=epoch)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    written = []
    for name, data in artifacts.items():
        destination = output / name
        temporary = output / (name + ".tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        written.append(destination)
    return written


def main(argv=None) -> int:
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Build an architecture-independent Ubuntu package without installing it.")
    parser.add_argument("--output", type=Path, default=project / "packaging-smoke")
    args = parser.parse_args(argv)
    try:
        epoch = int(os.environ.get("SOURCE_DATE_EPOCH", str(DEFAULT_EPOCH)))
        paths = build_release(project, args.output, epoch=epoch)
    except (PackageError, OSError, ValueError) as error:
        parser.exit(1, f"Package build failed: {error}\n")
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
