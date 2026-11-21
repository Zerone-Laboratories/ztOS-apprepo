#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""The phone's client in a throwaway home: a format 1 app keeps installing and
updating as before; a format 2 app takes over units an app installed by hand
wrote, keeps its data on every update, runs with its own fonts and theme, and
loses units and data only on uninstall (or units on revocation)."""
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from zrnlib import sign, zexec  # noqa: E402

ARCH = {"arm64": "aarch64", "amd64": "x86_64"}.get(platform.machine(), platform.machine())
SERVICES = [
    {"name": "zrn-ztwhatsappd", "description": "engine", "args": ["--daemon"], "restart_prevent_exit_status": [3],
     "umask": "0077"},
    {"name": "zrn-ztwhatsapp-notify", "args": ["--notifier"], "after": ["zrn-ztwhatsappd"],
     "env": {"ZTWHATSAPP_UI": "{exec}"}, "nice": 5, "memory_high": "24M"},
]
# what the program prints: where zrn-exec told zrnui its fonts and theme are
PROGRAM = r'''#include <stdio.h>
#include <stdlib.h>
int main(int c, char **v) {
    FILE *f = fopen(getenv("OUT"), "w");
    fprintf(f, "%s\n%s\n%s\n%s\n", getenv("ZRNUI_FONT_DIR"), getenv("ZRNUI_THEME"), getenv("ZRN_APP_PACKAGE"),
            c > 1 ? v[1] : "");
    return 0;
}
'''


class Client(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        t = Path(cls.tmp.name)
        cls.prog = t / "prog"
        cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
        if cc:
            (t / "prog.c").write_text(PROGRAM)
            subprocess.run([cc, "-O1", "-o", str(cls.prog), str(t / "prog.c")], check=True)
        else:
            shutil.copy("/bin/true", cls.prog)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        t = self.t = Path(self.td.name)
        self.home = t / "home"
        self.home.mkdir()
        self.key = sign.SecretKey.generate()
        (t / "k.pub").write_text(self.key.public.dumps())
        # systemctl is a stub that records its arguments
        self.calls = t / "systemctl.log"
        stub = t / "systemctl"
        stub.write_text(f"#!/bin/sh\necho \"$@\" >> {self.calls}\n")
        stub.chmod(0o755)
        # zrn-exec as the phone has it: on PATH, next to zrn
        bindir = t / "bin"
        bindir.mkdir()
        (bindir / "zrn-exec").symlink_to(HERE / "zrn-exec")
        osr = t / "os-release"
        osr.write_text('ID=ztos\nVERSION_ID="0.2.1-BETA-1.1"\n')
        self.env = dict(os.environ, HOME=str(self.home), ZRN_TRUSTED_KEYS=str(t / "k.pub"), ZRN_SYSTEMCTL=str(stub),
                        ZRN_OS_RELEASE=str(osr), PATH=f"{bindir}:{os.environ.get('PATH', '/usr/bin')}",
                        ZRN_STORE_KEY=str(t / "k.pub"), ZRN_STORE_INDEX=str(t / "idx"))
        for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
            self.env.pop(k, None)

    def tearDown(self):
        self.td.cleanup()

    def zrn(self, *args):
        p = subprocess.run([sys.executable, "-I", str(HERE / "zrn"), *args, "--json"], capture_output=True,
                           text=True, env=self.env, stdin=subprocess.DEVNULL)
        try:
            return p.returncode, json.loads(p.stdout)
        except ValueError:
            self.fail(f"zrn {args}: {p.stdout} {p.stderr}")

    def package(self, app_id, version, v2=False, services=SERVICES):
        if v2:
            m = {"zexec": 2, "id": app_id, "name": "WhatsApp", "version": version, "arch": ARCH, "runtime": "native",
                 "entry": f"bin/{app_id}", "zrnui": "2.0", "min_ztos": "0.2.1", "sdk": 2,
                 "permissions": ["network", "background"], "data_dirs": ["ztwhatsapp"], "services": services,
                 "icon": "icon.svg"}
            files = {m["entry"]: (str(self.prog), 0o755), "icon.svg": (b"<svg/>", 0o644),
                     "share/zrnui/fonts/selawk.ttf": (b"font", 0o644),
                     "share/zrnui/theme/theme.ini": (b"[dark]\n", 0o644)}
        else:
            m = {"zexec": 1, "id": app_id, "name": "Hello", "version": version, "arch": "any", "runtime": "python3",
                 "entry": "app/main.py", "zrnui": "0.1"}
            files = {"app/main.py": (b"print('hello')\n", 0o644)}
        path = self.t / f"{app_id}-{version}.zexec"
        sha = zexec.pack(str(path), m, files)
        Path(str(path) + ".minisig").write_text(sign.sign_file(self.key, str(path), zexec.trusted_comment(m, sha), "t"))
        return path

    def units(self):
        return self.home / ".config" / "systemd" / "user"

    def data(self):
        """The data a WhatsApp install keeps (the hand install's folders)."""
        h = self.home
        return [h / ".local/share/ztwhatsapp/db.sqlite", h / ".local/state/ztwhatsapp/s",
                h / ".config/zrn-whatsapp/prefs", h / ".local/share/zrn-whatsapp/media"]

    def test_format1_install_update_keeps_data(self):
        rc, o = self.zrn("install", str(self.package("io.github.a.hello", "1.0.0")))
        self.assertEqual(rc, 0, o)
        stored = self.home / ".local/share/io.github.a.hello/notes.txt"
        stored.parent.mkdir(parents=True)
        stored.write_text("mine")
        rc, o = self.zrn("install", str(self.package("io.github.a.hello", "1.1.0")))
        self.assertEqual((rc, o["updated_from"], o["services"]), (0, "1.0.0", []), o)
        self.assertEqual(stored.read_text(), "mine")
        desktop = (self.home / ".local/share/applications/io.github.a.hello.desktop").read_text()
        self.assertIn(f"{self.home}/.local/share/zrn/apps/io.github.a.hello/io.github.a.hello.zexec", desktop)
        self.assertIn("X-Zrn-Version=1.1.0", desktop)
        self.assertFalse(self.calls.exists(), "a format 1 app has no services: systemctl is never asked")

    def test_format2_takes_over_hand_install(self):
        # what the hand install left: its units (running /usr/local/bin) and its data
        u = self.units()
        (u / "default.target.wants").mkdir(parents=True)
        for n in ("zrn-ztwhatsappd", "zrn-ztwhatsapp-notify"):
            (u / f"{n}.service").write_text("[Service]\nExecStart=/usr/local/bin/zrn-whatsapp --daemon\n")
            (u / "default.target.wants" / f"{n}.service").symlink_to(f"../{n}.service")
        (u / "ztos-soundd.service").write_text("[Service]\nExecStart=/usr/bin/x\n")   # someone else's
        for f in self.data():
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("keep")
        pkg_path = self.home / ".local/share/zrn/apps/zrn-whatsapp/zrn-whatsapp.zexec"

        rc, o = self.zrn("install", str(self.package("zrn-whatsapp", "0.3.0", v2=True)))
        self.assertEqual(rc, 0, o)
        self.assertEqual(o["services"], ["zrn-ztwhatsappd", "zrn-ztwhatsapp-notify"])
        engine = (u / "zrn-ztwhatsappd.service").read_text()
        self.assertIn(f'ExecStart="{self.t}/bin/zrn-exec" "{pkg_path}" "--daemon"', engine)
        self.assertIn("# zrn-app: zrn-whatsapp", engine)
        self.assertIn("RestartPreventExitStatus=3", engine)
        self.assertIn("UMask=0077", engine)
        notify = (u / "zrn-ztwhatsapp-notify.service").read_text()
        self.assertIn(f'Environment="ZTWHATSAPP_UI={pkg_path}"', notify)
        self.assertIn("After=zrn-ztwhatsappd.service", notify)
        self.assertNotIn("/usr/local/bin", engine + notify)
        self.assertTrue((u / "default.target.wants" / "zrn-ztwhatsappd.service").is_symlink())
        calls = self.calls.read_text()
        self.assertIn("--user stop zrn-ztwhatsappd.service zrn-ztwhatsapp-notify.service", calls)
        self.assertIn("--user --no-block restart zrn-ztwhatsappd.service zrn-ztwhatsapp-notify.service", calls)
        self.assertTrue((u / "ztos-soundd.service").exists())
        for f in self.data():
            self.assertEqual(f.read_text(), "keep", f)

        # it runs, with the package's fonts and theme, and knows its stable path
        if shutil.which("cc") or shutil.which("gcc") or shutil.which("clang"):
            out = self.t / "ran"
            env = dict(self.env, OUT=str(out), ZRN_EXEC_NO_LOG="1")
            p = subprocess.run([str(pkg_path), "--daemon"], env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            fonts, theme, package, arg = out.read_text().splitlines()
            self.assertTrue(fonts.endswith("/share/zrnui/fonts") and Path(fonts, "selawk.ttf").exists(), fonts)
            self.assertTrue(theme.endswith("/share/zrnui/theme/theme.ini"), theme)
            self.assertEqual((package, arg), (str(pkg_path), "--daemon"))

        # an update: same path, same units (restarted), a dropped service goes, data stays
        desktop = self.home / ".local/share/applications/zrn-whatsapp.desktop"
        exec_line = [l for l in desktop.read_text().splitlines() if l.startswith("Exec=")]
        self.calls.unlink()
        rc, o = self.zrn("install", str(self.package("zrn-whatsapp", "0.3.1", v2=True, services=SERVICES[:1])))
        self.assertEqual((rc, o["updated_from"], o["removed_services"]), (0, "0.3.0", ["zrn-ztwhatsapp-notify"]), o)
        self.assertFalse((u / "zrn-ztwhatsapp-notify.service").exists())
        self.assertFalse((u / "default.target.wants" / "zrn-ztwhatsapp-notify.service").exists())
        self.assertEqual([l for l in desktop.read_text().splitlines() if l.startswith("Exec=")], exec_line)
        self.assertIn("--no-block restart zrn-ztwhatsappd.service", self.calls.read_text())
        for f in self.data():
            self.assertEqual(f.read_text(), "keep", f)

        # uninstall: units and data go (the store app asked on screen; --json does not ask)
        rc, o = self.zrn("uninstall", "zrn-whatsapp")
        self.assertEqual((rc, o["services_removed"]), (0, ["zrn-ztwhatsappd"]), o)
        self.assertFalse((u / "zrn-ztwhatsappd.service").exists())
        self.assertTrue((u / "ztos-soundd.service").exists())
        for f in self.data():
            self.assertFalse(f.exists(), f)

    def test_uninstall_asks_in_a_terminal(self):
        rc, _ = self.zrn("install", str(self.package("io.github.a.hello", "1.0.0")))
        self.assertEqual(rc, 0)
        # nobody to answer: the answer is no, nothing goes
        p = subprocess.run([sys.executable, "-I", str(HERE / "zrn"), "uninstall", "io.github.a.hello"],
                           capture_output=True, text=True, env=self.env, stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, 1)
        self.assertIn("nothing was removed", p.stderr)
        self.assertTrue((self.home / ".local/share/zrn/apps/io.github.a.hello").exists())
        rc, o = self.zrn("uninstall", "io.github.a.hello")
        self.assertEqual(rc, 0, o)

    def test_revocation_stops_services(self):
        pkg = self.package("zrn-whatsapp", "0.3.0", v2=True)
        rc, o = self.zrn("install", str(pkg))
        self.assertEqual(rc, 0, o)
        sha = zexec.sha256_file(str(pkg))
        idx = self.t / "idx"
        idx.mkdir()
        index = {"format": 1, "generated": "2026-11-19T00:00:00Z", "apps": [],
                 "revoked": [{"id": "zrn-whatsapp", "version": "0.3.0", "arch": ARCH, "sha256": sha, "reason": "x"}]}
        (idx / "index.json").write_text(json.dumps(index))
        h = hashlib.sha256((idx / "index.json").read_bytes()).hexdigest()
        (idx / "index.json.minisig").write_text(sign.sign_file(
            self.key, str(idx / "index.json"), f"zrn-index format=1 generated={index['generated']} sha256={h}", "i"))
        rc, o = self.zrn("store", "refresh")
        self.assertEqual((rc, o["disabled"]), (0, ["zrn-whatsapp"]), o)
        self.assertEqual(list(self.units().glob("zrn-ztwhatsapp*.service")), [])


if __name__ == "__main__":
    unittest.main()
