# SPDX-License-Identifier: GPL-2.0-or-later
"""Login services of a format 2 app: systemd user units that zrn writes.

A package declares what to run ("services" in zexec.json: a name, arguments
and a few settings), never unit text. zrn writes each one as
~/.config/systemd/user/<name>.service running the installed package through
zrn-exec, enables it for default.target and (re)starts it. The package path
never changes across updates, so neither does the unit: an update only
restarts it. Every unit carries a marker line naming the app, so uninstall
and revocation find the units even when the package is already gone.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

MARK = "# zrn-app: "


def unit_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def _systemctl(*args):
    """systemctl --user; ZRN_SYSTEMCTL replaces the program (tests). Never fatal:
    without a user manager the units still start at the next login."""
    prog = os.environ.get("ZRN_SYSTEMCTL") or shutil.which("systemctl")
    if not prog:
        return False
    try:
        return subprocess.run([prog, "--user", *args], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _quote(word):
    """One word of an ExecStart= line: quoted, with $ kept literal."""
    return '"' + word.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$") + '"'


def unit_text(m, svc, package, zrn_exec):
    names = {s["name"] for s in m.get("services", [])}
    lines = ["[Unit]", f"Description={svc.get('description') or m['name']}",
             f"{MARK}{m['id']}", f"# Written by zrn for {m['id']} {m['version']}; zrn uninstall removes it.",
             f"ConditionPathExists={package}"]
    after = [a for a in svc.get("after", []) if a in names]
    if after:
        lines.append("After=" + " ".join(f"{a}.service" for a in after))
    lines += ["", "[Service]", "Environment=ZRN_EXEC_NO_LOG=1"]
    for k, v in sorted((svc.get("env") or {}).items()):
        lines.append(f"Environment={_quote(k + '=' + v.replace('{exec}', str(package)))}")
    lines.append("ExecStart=" + " ".join(_quote(w) for w in [zrn_exec, str(package), *svc.get("args", [])]))
    lines.append(f"Restart={svc.get('restart', 'on-failure')}")
    lines.append(f"RestartSec={svc.get('restart_sec', 5)}")
    if svc.get("restart_prevent_exit_status"):
        lines.append("RestartPreventExitStatus=" + " ".join(map(str, svc["restart_prevent_exit_status"])))
    if svc.get("umask"):
        lines.append(f"UMask={svc['umask']}")
    if "nice" in svc:
        lines.append(f"Nice={svc['nice']}")
    if svc.get("memory_high"):
        lines.append(f"MemoryHigh={svc['memory_high']}")
    lines += ["NoNewPrivileges=yes", "", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def units_of(app_id):
    """Unit names (without .service) zrn wrote for this app, found by their marker."""
    found = []
    d = unit_dir()
    if not d.is_dir():
        return found
    for f in sorted(d.glob("*.service")):
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        if re.search(rf"^{re.escape(MARK + app_id)}$", text, re.M):
            found.append(f.name[: -len(".service")])
    return found


def stop(names):
    if names:
        _systemctl("stop", *[f"{n}.service" for n in names])


def _remove_files(names):
    d = unit_dir()
    for n in names:
        (d / "default.target.wants" / f"{n}.service").unlink(missing_ok=True)
        (d / f"{n}.service").unlink(missing_ok=True)


def install(m, package, zrn_exec):
    """Write, enable and (re)start the services of manifest m; drop the ones an
    earlier version had and this one does not. Unit files of the same names that
    zrn did not write (an app installed by hand) are taken over."""
    want = [s for s in m.get("services", []) if isinstance(s, dict)]
    names = [s["name"] for s in want]
    gone = [n for n in units_of(m["id"]) if n not in names]
    stop(gone)
    _remove_files(gone)
    d = unit_dir()
    (d / "default.target.wants").mkdir(parents=True, exist_ok=True)
    for s in want:
        f = d / f"{s['name']}.service"
        tmp = f.with_suffix(".service.tmp")
        tmp.write_text(unit_text(m, s, package, zrn_exec))
        os.replace(tmp, f)
        link = d / "default.target.wants" / f.name
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(Path("..") / f.name)
    if want or gone:
        _systemctl("daemon-reload")
    if want:
        _systemctl("--no-block", "restart", *[f"{n}.service" for n in names])
    return {"services": names, "removed_services": gone}


def remove(app_id, declared=()):
    """Stop, disable and delete every unit of this app. Returns their names."""
    names = sorted(set(units_of(app_id)) | {n for n in declared if n})
    names = [n for n in names if (unit_dir() / f"{n}.service").exists()]
    if not names:
        return []
    stop(names)
    _remove_files(names)
    _systemctl("daemon-reload")
    return names
