"""Parse complete artifacts without dpkg, installation or importing the real app."""
from __future__ import annotations

import configparser
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile


PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("watchdog_package_builder", PROJECT / "packaging" / "build_release.py")
builder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = builder
SPEC.loader.exec_module(builder)


def read_ar(data):
    if data[:8] != b"!<arch>\n":
        raise ValueError("Not ar")
    offset, entries = 8, []
    while offset < len(data):
        header = data[offset:offset + 60]
        if len(header) != 60 or header[58:60] != b"`\n":
            raise ValueError("Invalid ar header")
        name = header[:16].decode("ascii").rstrip().removesuffix("/")
        size = int(header[48:58])
        offset += 60
        body = data[offset:offset + size]
        if len(body) != size:
            raise ValueError("Truncated ar member")
        entries.append((name, body, {"mtime": int(header[16:28]), "uid": int(header[28:34]),
                                     "gid": int(header[34:40]), "mode": int(header[40:48], 8)}))
        offset += size
        if size % 2:
            if data[offset:offset + 1] != b"\n":
                raise ValueError("Missing ar padding")
            offset += 1
    if offset != len(data):
        raise ValueError("Trailing ar bytes")
    return entries


def read_tar(data):
    result = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for info in archive.getmembers():
            result[info.name.removeprefix("./")] = (info, archive.extractfile(info).read() if info.isfile() else None)
    return result


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="clash-watchdog-package-")
        self.root = Path(self.temp.name) / "project"
        for folder in ("clash_watchdog", "tests", "packaging"):
            (self.root / folder).mkdir(parents=True)
        for name in builder.MODULES:
            (self.root / "clash_watchdog" / name).write_text("# allowlisted Python source\n", encoding="utf-8")
        for name in builder.PACKAGING_FILES:
            (self.root / "packaging" / name).write_bytes((PROJECT / "packaging" / name).read_bytes())
        for name in builder.DOCUMENTS:
            (self.root / name).write_text("Fixture document\n", encoding="utf-8")
        (self.root / "tests" / "test_api.py").write_text("# fixture test\n", encoding="utf-8")
        self.artifacts = builder.make_artifacts(self.root)
        self.deb_name = f"{builder.PACKAGE}_{builder.VERSION}_all.deb"
        self.members = read_ar(self.artifacts[self.deb_name])
        self.control = read_tar(self.members[1][1])
        self.data = read_tar(self.members[2][1])

    def tearDown(self):
        self.temp.cleanup()

    def test_deb_has_exact_standard_ar_members_and_metadata(self):
        self.assertEqual([entry[0] for entry in self.members], ["debian-binary", "control.tar.gz", "data.tar.gz"])
        self.assertEqual(self.members[0][1], b"2.0\n")
        for _, _, metadata in self.members:
            self.assertEqual(metadata, {"mtime": builder.DEFAULT_EPOCH, "uid": 0, "gid": 0, "mode": 0o100644})

    def test_control_declares_all_architecture_and_ubuntu_runtime_dependencies(self):
        control = self.control["control"][1].decode("utf-8")
        self.assertIn("Package: clash-verge-watchdog\n", control)
        self.assertIn("Version: 1.3.3\n", control)
        self.assertIn("Architecture: all\n", control)
        self.assertIn("Depends: python3 (>= 3.10), python3-tk, python3-gi, gir1.2-glib-2.0, gir1.2-ayatanaappindicator3-0.1\n", control)
        self.assertIn("Recommends: xdg-utils, fonts-noto-cjk\n", control)
        self.assertEqual(set(self.control), {"control", "md5sums"})

    def test_deb_installs_exact_module_launcher_desktop_icon_and_doc_set(self):
        files = {name for name, (info, _) in self.data.items() if info.isfile()}
        expected = {f"usr/share/clash-verge-watchdog/clash_watchdog/{name}" for name in builder.MODULES}
        expected |= {"usr/bin/clash-verge-watchdog", "usr/share/applications/clash-verge-watchdog.desktop",
                     "usr/share/icons/hicolor/scalable/apps/clash-verge-watchdog.svg"}
        expected |= {f"usr/share/doc/clash-verge-watchdog/{name}" for name in builder.DOCUMENTS}
        self.assertEqual(files, expected)
        self.assertFalse(any("etc/" in path or "home/" in path for path in files))

    def test_unix_permissions_root_ownership_and_timestamps_do_not_follow_host(self):
        for name, (info, _) in self.data.items():
            self.assertEqual((info.uid, info.gid, info.uname, info.gname), (0, 0, "root", "root"))
            self.assertEqual(info.mtime, builder.DEFAULT_EPOCH)
            self.assertEqual(info.mode, 0o755 if info.isdir() or name == "usr/bin/clash-verge-watchdog" else 0o644)
            self.assertFalse(info.issym() or info.islnk())

    def test_installed_entrypoint_loads_packaged_module_and_forwards_exit(self):
        code = self.data["usr/bin/clash-verge-watchdog"][1].decode("utf-8")
        self.assertTrue(code.startswith("#!/usr/bin/python3\n"))
        fake = types.ModuleType("clash_watchdog.app")
        fake.main = lambda: 37
        old_path, old_bytecode = list(sys.path), sys.dont_write_bytecode
        try:
            with patch.dict(sys.modules, {"clash_watchdog.app": fake}):
                with self.assertRaises(SystemExit) as outcome:
                    exec(compile(code, "/usr/bin/clash-verge-watchdog", "exec"), {"__name__": "__main__"})
                self.assertEqual(outcome.exception.code, 37)
                self.assertEqual(sys.path[0], "/usr/share/clash-verge-watchdog")
                self.assertTrue(sys.dont_write_bytecode)
        finally:
            sys.path[:] = old_path
            sys.dont_write_bytecode = old_bytecode

    def test_desktop_matches_launcher_icon_and_tk_class(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read_string(self.data["usr/share/applications/clash-verge-watchdog.desktop"][1].decode("utf-8"))
        entry = parser["Desktop Entry"]
        self.assertEqual(entry["Type"], "Application")
        self.assertEqual(entry["Name"], "Clash Verge 监控器")
        self.assertEqual(entry["Exec"], "clash-verge-watchdog")
        self.assertEqual(entry["TryExec"], "/usr/bin/clash-verge-watchdog")
        self.assertEqual(entry["Icon"], "clash-verge-watchdog")
        self.assertEqual(entry["Terminal"], "false")
        self.assertEqual(entry["StartupWMClass"], "Clash-verge-watchdog")

    def test_md5sums_match_every_installed_regular_file(self):
        entries = {}
        for line in self.control["md5sums"][1].decode("ascii").splitlines():
            checksum, name = line.split("  ", 1)
            entries[name] = checksum
        regular = {name: data for name, (info, data) in self.data.items() if info.isfile()}
        self.assertEqual(set(entries), set(regular))
        for name, data in regular.items():
            self.assertEqual(entries[name], hashlib.md5(data, usedforsecurity=False).hexdigest())

    def test_portable_has_modules_docs_and_executable_safe_launcher(self):
        portable = read_tar(self.artifacts[f"{builder.PACKAGE}_{builder.VERSION}_linux-all.tar.gz"])
        root = f"{builder.PACKAGE}-{builder.VERSION}"
        launcher_info, launcher = portable[root + "/launcher.sh"]
        self.assertEqual(launcher_info.mode, 0o755)
        self.assertTrue(launcher.startswith(b"#!/bin/sh\n"))
        self.assertIn(b'exec python3 -m clash_watchdog "$@"', launcher)
        self.assertIn((root + "/clash_watchdog/__main__.py"), portable)
        self.assertFalse(any("/packaging/" in path or "/tests/" in path for path in portable))

    def test_source_zip_contains_build_inputs_and_unix_file_metadata(self):
        with zipfile.ZipFile(io.BytesIO(self.artifacts[f"{builder.PACKAGE}_{builder.VERSION}_source.zip"])) as archive:
            names = archive.namelist()
            prefix = f"{builder.PACKAGE}-{builder.VERSION}-source/"
            self.assertIn(prefix + "packaging/build_release.py", names)
            self.assertIn(prefix + "tests/test_api.py", names)
            self.assertIn(prefix + "clash_watchdog/__main__.py", names)
            for info in archive.infolist():
                self.assertEqual(info.create_system, 3)
                mode = info.external_attr >> 16
                self.assertTrue(stat.S_ISREG(mode))
                self.assertEqual(stat.S_IMODE(mode), 0o755 if info.filename.endswith("packaging/build_release.py") else 0o644)

    def test_allowlist_excludes_private_settings_logs_caches_and_extra_source(self):
        for path in ["settings.json", "watchdog.log", "clash_watchdog/secret.py", "tests/test_unrelated.py",
                     "clash_watchdog/__pycache__/app.pyc", "backups/config.yaml"]:
            destination = self.root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"PRIVATE-DATA-MUST-NOT-ENTER-RELEASE")
        new = builder.make_artifacts(self.root)
        self.assertEqual(new, self.artifacts)

    def test_same_inputs_reproduce_identical_bytes_despite_changed_mtime(self):
        for path in self.root.rglob("*"):
            if path.is_file():
                os.utime(path, (2000000000, 2000000000))
        self.assertEqual(builder.make_artifacts(self.root), self.artifacts)

    def test_missing_required_module_fails_before_creating_outputs(self):
        (self.root / "clash_watchdog/engine.py").unlink()
        output = Path(self.temp.name) / "new-output"
        with self.assertRaises(builder.PackageError):
            builder.build_release(self.root, output)
        self.assertFalse(output.exists())

    def test_new_relative_module_import_cannot_be_silently_omitted(self):
        (self.root / "clash_watchdog/app.py").write_text("from .extra import thing\n", encoding="utf-8")
        with self.assertRaises(builder.PackageError):
            builder.make_artifacts(self.root)

    def test_optional_docs_may_be_absent_without_collecting_other_files(self):
        for name in ("README.txt", "VALIDATION.txt"):
            (self.root / name).unlink()
        parsed = read_ar(builder.make_artifacts(self.root)[self.deb_name])
        contents = read_tar(parsed[2][1])
        self.assertNotIn("usr/share/doc/clash-verge-watchdog/README.txt", contents)
        self.assertIn("usr/share/doc/clash-verge-watchdog/QUICKSTART.txt", contents)

    def test_share_bundle_has_only_installer_offline_guides_and_verified_manifest(self):
        with zipfile.ZipFile(io.BytesIO(self.artifacts[builder.SHARE_ARCHIVE])) as archive:
            prefix = builder.SHARE_ROOT + "/"
            self.assertEqual(set(archive.namelist()), {
                prefix + self.deb_name, prefix + "安装说明.txt", prefix + "开始使用.html", prefix + "SHA256SUMS"})
            self.assertEqual(archive.read(prefix + self.deb_name), self.artifacts[self.deb_name])
            self.assertEqual(archive.read(prefix + "安装说明.txt"), (self.root / "QUICKSTART.txt").read_bytes())
            self.assertEqual(archive.read(prefix + "开始使用.html"), (self.root / "START-HERE.html").read_bytes())
            for line in archive.read(prefix + "SHA256SUMS").decode("utf-8").splitlines():
                digest, name = line.split("  ", 1)
                self.assertEqual(digest, hashlib.sha256(archive.read(prefix + name)).hexdigest())
            for info in archive.infolist():
                self.assertFalse(stat.S_IMODE(info.external_attr >> 16) & 0o111)

    def test_share_guides_are_required_before_any_artifact_is_written(self):
        for name in ("QUICKSTART.txt", "START-HERE.html"):
            path = self.root / name
            original = path.read_bytes()
            path.unlink()
            output = Path(self.temp.name) / (name + "-output")
            with self.assertRaises(builder.PackageError):
                builder.build_release(self.root, output)
            self.assertFalse(output.exists())
            path.write_bytes(original)

    def test_sha256_manifest_and_atomic_output_files(self):
        output = Path(self.temp.name) / "output"
        paths = builder.build_release(self.root, output)
        self.assertEqual({path.name for path in paths}, set(self.artifacts))
        self.assertFalse(list(output.glob("*.tmp")))
        for line in (output / "SHA256SUMS").read_text("ascii").splitlines():
            digest, name = line.split("  ", 1)
            self.assertEqual(digest, hashlib.sha256((output / name).read_bytes()).hexdigest())

    def test_bad_epochs_and_parent_paths_are_rejected(self):
        for stamp in [-1, 2**32, "123", True]:
            with self.subTest(stamp=stamp), self.assertRaises(builder.PackageError):
                builder.make_artifacts(self.root, epoch=stamp)
        for name in ["/absolute", "../escape", "dir/../../escape", "windows\\path"]:
            with self.subTest(name=name), self.assertRaises(builder.PackageError):
                builder._tar_gzip({name: builder.Member(b"x")}, builder.DEFAULT_EPOCH)


if __name__ == "__main__":
    unittest.main()
