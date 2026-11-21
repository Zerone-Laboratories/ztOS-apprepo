# SPDX-License-Identifier: GPL-2.0-or-later
"""zrn on the phone: install, update, run and remove ztOS apps (.zexec).

The developer commands of SDK v1 (new, build, pack, render, run) are gone:
SDK v2 apps are CMake projects (zrn-sdk), and the store packs them. This is
the device side, kept with the store (zrn-apprepo/client) so the packer and
the client read one definition of the format.

Every command takes --json: one JSON object on stdout, {"ok": true, ...} or
{"ok": false, "error": {"code", "message", "hint"}}. Exit codes: 0 done,
1 failed, 2 a package is not trusted (verify, install), 3 usage.
"""
import argparse
import json
import os
import platform
import re
import shutil
import sys
from pathlib import Path

from . import services, sign, zexec
from .errors import ZrnError

VERSION = "0.3.0"
HERE = Path(__file__).resolve().parents[1]   # the folder with zrn and zrn-exec


# ---------------------------------------------------------------- output ---
class Out:
    def __init__(self, as_json):
        self.json = as_json

    def say(self, *parts):
        if not self.json:
            print(*parts)

    def note(self, *parts):
        print(*parts, file=sys.stderr)


def result(out, data, text=None):
    if out.json:
        print(json.dumps({"ok": True, **data}, indent=1, sort_keys=True))
    elif text:
        print(text)


# ----------------------------------------------------------------- paths ---
def config_home():
    env = os.environ.get("ZRN_CONFIG_HOME")
    if env:
        return Path(env)
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "zrn"


def data_home():
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def cache_home():
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "zrn"


def state_home():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "zrn"


def trusted_key_files(extra=()):
    dirs = [Path("/etc/zrn/trusted-keys"), Path("/usr/share/zrn/trusted-keys"), config_home() / "trusted-keys"]
    files = []
    for d in dirs:
        if d.is_dir():
            files += sorted(d.glob("*.pub"))
    for f in os.environ.get("ZRN_TRUSTED_KEYS", "").split(":"):
        if f:
            files.append(Path(f))
    files += [Path(f) for f in extra]
    return files


def load_keys(files):
    keys = []
    for f in files:
        try:
            keys.append(sign.PublicKey.loads(Path(f).read_text(), str(f)))
        except (OSError, sign.SignError) as e:
            raise ZrnError("bad-key", f"cannot read public key {f}: {e}",
                           "remove it, or replace it with a minisign public key")
    return keys


def host_arch():
    m = platform.machine()
    return {"arm64": "aarch64", "amd64": "x86_64"}.get(m, m)

def verify_package(path, sig_path=None, extra_keys=(), require_signature=True):
    """(manifest, problems, signer or None, signature error or None, sha256)"""
    path = str(path)
    if not os.path.isfile(path):
        raise ZrnError("no-file", f"{path} does not exist", "give the path of a .zexec (zrn pack writes dist/*.zexec)")
    try:
        m, problems = zexec.check(path)
    except zexec.ZexecError as e:
        raise ZrnError(e.code, e.message, e.hint)
    sha = zexec.sha256_file(path)
    sig_path = Path(sig_path or path + ".minisig")
    signer, sig_error = None, None
    if not sig_path.exists():
        sig_error = f"no signature ({sig_path} is missing)"
    else:
        try:
            keys = load_keys(trusted_key_files(extra_keys))
            key, s = sign.verify_file(path, sig_path.read_text(), keys, str(sig_path))
            tc = zexec.parse_trusted_comment(s.trusted_comment)
            for field in ("id", "version", "arch"):
                if tc.get(field) != str(m.get(field)):
                    raise sign.SignError(f"the signature is for {field}={tc.get(field)!r}, "
                                         f"the package says {m.get(field)!r}")
            if tc.get("sha256") and tc["sha256"] != sha:
                raise sign.SignError("the signature names another file (sha256 differs)")
            signer = key.id_hex
        except sign.SignError as e:
            sig_error = str(e)
    return m, problems, signer, sig_error, sha


def cmd_verify(args, out):
    m, problems, signer, sig_error, sha = verify_package(args.file, args.sig, args.key or ())
    ok = not problems and not sig_error
    data = {"command": "verify", "path": str(Path(args.file).resolve()), "sha256": sha,
            "id": m.get("id") if isinstance(m, dict) else None, "version": m.get("version") if isinstance(m, dict) else None,
            "arch": m.get("arch") if isinstance(m, dict) else None,
            "valid": not problems, "problems": problems, "signed_by": signer, "signature_error": sig_error}
    if out.json:
        print(json.dumps({"ok": ok, **data}, indent=1, sort_keys=True))
    else:
        for pr in problems:
            print(f"problem: {pr}")
        if sig_error:
            print(f"signature: {sig_error}")
        if ok:
            print(f"good: {m['id']} {m['version']} ({m['arch']}), signed by {signer}")
    if problems:
        return 1
    if sig_error:
        return 2
    return 0


def tell_shell(line):
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    try:
        fd = os.open(os.path.join(runtime, "ztmetro.ctl"), os.O_WRONLY | os.O_NONBLOCK)
        os.write(fd, (line + "\n").encode())
        os.close(fd)
        return True
    except OSError:
        return False


def zrn_exec_path():
    found = shutil.which("zrn-exec")
    return found or str(HERE / "zrn-exec")

def stop_app(app_id):
    """Everything running from this app's unpacked package. Returns how many."""
    import signal, time
    root = str(cache_home() / "apps" / app_id) + "/"
    pids = []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            if os.readlink(f"/proc/{name}/exe").startswith(root):
                pids.append(int(name))
        except OSError:
            pass
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    for _ in range(20):
        if not any(Path(f"/proc/{p}").exists() for p in pids):
            break
        time.sleep(0.1)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return len(pids)


# Folders shared by the system or by every app. Removal never touches these, whatever
# a package's manifest says.
RESERVED_DATA_NAMES = {"zrn", "applications", "icons", "mime", "fonts", "systemd", "ztmetro", "autostart",
                       "pulse", "pipewire", "dconf", "ssh", "gnupg", "keyrings", "waydroid", "Trash"}


def app_data_paths(app_id, extra=()):
    """What an app keeps: by its id, and the folder names its manifest declares."""
    names = [app_id]
    for n in extra:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", str(n)) and n not in RESERVED_DATA_NAMES:
            names.append(n)
    home = Path.home()
    roots = [data_home(),
             Path(os.environ.get("XDG_STATE_HOME") or home / ".local" / "state"),
             Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config"),
             Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")]
    paths = [root / n for n in names for root in roots]
    paths += [cache_home() / "apps" / app_id, state_home() / f"{app_id}.log", state_home() / f"{app_id}.log.1"]
    return paths

def cmd_info(args, out):
    if not os.path.isfile(args.file):
        raise ZrnError("no-file", f"{args.file} does not exist", "give the path of a .zexec")
    try:
        with zexec.open_zexec(args.file) as z:
            m = zexec.read_manifest(z)
            files = [(i.filename, i.file_size) for i in z.infolist()]
    except zexec.ZexecError as e:
        raise ZrnError(e.code, e.message, e.hint)
    if out.json:
        print(json.dumps({"ok": True, "command": "info", "manifest": m,
                          "files": [{"path": f, "size": s} for f, s in files]}, indent=1, sort_keys=True))
    else:
        print(json.dumps(m, indent=1, sort_keys=True))
        for f, s in files:
            print(f"  {s:>9}  {f}")

def cmd_store(args, out):
    """The ztOS store on this phone: only the store's key counts here."""
    from . import store
    if args.action == "refresh":
        r = store.refresh()
        result(out, {"command": "store", **r}, f"index: {r['index']}; disabled: {', '.join(r['disabled']) or 'nothing'}")
    elif args.action == "list":
        r = store.listing(host_arch())
        result(out, {"command": "store", **r}, "\n".join(f"{a['id']} {a['version']} {a['installed'] or '-'}" for a in r["apps"]) or "nothing listed")
    else:
        if not args.id:
            raise ZrnError("usage", "zrn store install needs an app id", "`zrn store list` shows them", exit_code=3)
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = store.fetch_package(args.id, host_arch(), d)
            args.file, args.key = str(path), [os.environ.get("ZRN_STORE_KEY") or store.SYSTEM_KEY]
            return cmd_install(args, out)


def cmd_exec(args, out):
    from .launcher import launch
    return launch(args.file, args.args)


# ------------------------------------------------------------ install ---
def package_path(app_id):
    """Where an app's package lives. It never moves: the app list's Exec=, the login
    units and anything else that cached it keep working across updates."""
    return data_home() / "zrn" / "apps" / app_id / f"{app_id}.zexec"


def installed_manifest(app_id):
    pkg = package_path(app_id)
    if not pkg.exists():
        return None
    try:
        with zexec.open_zexec(str(pkg)) as z:
            return zexec.read_manifest(z)
    except (OSError, zexec.ZexecError, ValueError, KeyError):
        return None


def _put(src, dest):
    """Copy, then rename over: a zrn-exec starting meanwhile sees the old file or the new one."""
    tmp = dest.with_name(dest.name + ".new")
    shutil.copyfile(src, tmp)
    os.chmod(tmp, 0o755 if dest.suffix == ".zexec" else 0o644)
    os.replace(tmp, dest)


def cmd_install(args, out):
    """Install or update. An update replaces the package, the app-list entry, the
    icon and the login units; it never touches what the app stored."""
    src = Path(args.file).resolve()
    m, problems, signer, sig_error, sha = verify_package(src, None, args.key or ())
    if problems:
        raise ZrnError("invalid-package", f"{src} is broken: " + "; ".join(problems), "rebuild it")
    if sig_error:
        raise ZrnError("untrusted", f"{src}: {sig_error}",
                       "install packages signed by a key this user trusts (~/.config/zrn/trusted-keys)", exit_code=2)
    if m["arch"] not in ("any", host_arch()):
        raise ZrnError("wrong-arch", f"{m['id']} is built for {m['arch']}, this is {host_arch()}",
                       "install the package built for this machine")
    from .store import too_old
    old_ztos = too_old(m.get("min_ztos"))
    if old_ztos:
        raise ZrnError("ztos-too-old", f"{m['id']}: {old_ztos}", "update ztOS first")
    dest = package_path(m["id"])
    before = installed_manifest(m["id"])
    v2 = m.get("zexec") == 2 or (before or {}).get("zexec") == 2
    stopped = 0
    if v2:
        # the old version's engine and windows go first, so the new one does not meet their locks
        services.stop([s.get("name") for s in m.get("services", []) + (before or {}).get("services", [])
                       if isinstance(s, dict) and s.get("name")] + services.units_of(m["id"]))
        stopped = stop_app(m["id"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    _put(str(src) + ".minisig", Path(str(dest) + ".minisig"))
    _put(src, dest)
    if v2:
        # only zrn-exec's unpacked copies; the app's own data is never touched here
        shutil.rmtree(cache_home() / "apps" / m["id"], ignore_errors=True)
    icon_name = None
    if m.get("icon"):
        ext = Path(m["icon"]).suffix
        icon_dir = data_home() / "icons" / "hicolor" / ("scalable" if ext == ".svg" else "256x256") / "apps"
        icon_dir.mkdir(parents=True, exist_ok=True)
        with zexec.open_zexec(str(dest)) as z:
            (icon_dir / f"{m['id']}{ext}").write_bytes(z.read(m["icon"]))
        icon_name = m["id"]
    apps = data_home() / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    desktop = apps / f"{m['id']}.desktop"
    cats = ";".join(m.get("categories") or ["Utility"]) + ";"
    tmp = desktop.with_name(desktop.name + ".new")
    tmp.write_text("\n".join([
        "[Desktop Entry]", "Type=Application", f"Name={m['name']}", f"Comment={m.get('summary') or m['name']}",
        f"Exec={zrn_exec_path()} {dest}", f"Icon={icon_name or 'application-x-executable'}", "Terminal=false",
        f"StartupWMClass={m['id']}", f"Categories={cats}", f"X-Zrn-Id={m['id']}",
        f"X-Zrn-Version={m['version']}", f"X-Zrn-Sha256={sha}", f"X-Zrn-Format={m.get('zexec', 1)}", ""]))
    os.replace(tmp, desktop)
    svc = {"services": [], "removed_services": []}
    if v2:
        svc = services.install(m, dest, zrn_exec_path())
    told = tell_shell("apps")
    result(out, {"command": "install", "id": m["id"], "version": m["version"], "path": str(dest),
                 "updated_from": (before or {}).get("version"), "desktop": str(desktop), "signed_by": signer,
                 "shell_told": told, "stopped": stopped, **svc},
           f"installed {m['id']} {m['version']} -> {dest}\n  app list: {desktop}"
           + (f"\n  login services: {', '.join(svc['services'])}" if svc["services"] else ""))


def _ask(question):
    try:
        with open("/dev/tty") as tty:
            sys.stderr.write(question + " [y/N] ")
            sys.stderr.flush()
            return tty.readline().strip().lower() in ("y", "yes")
    except OSError:
        return False


def cmd_uninstall(args, out):
    """Remove an app and, unless --keep-data, everything it stored. It asks first; with
    --yes, or --json (the store app, which asks on screen), it does not. Where nobody can
    answer, the answer is no."""
    app_id = args.id
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", app_id) or app_id in RESERVED_DATA_NAMES:
        raise ZrnError("bad-id", f"{app_id} is not an app id", "see ~/.local/share/zrn/apps")
    m = installed_manifest(app_id) or {}
    declared = m.get("data_dirs") or []
    keep = bool(getattr(args, "keep_data", False))
    if not (args.yes or out.json):
        what = "and keeps its data" if keep else "and DELETES everything it stored on this phone"
        if not (sys.stdin.isatty() and _ask(f"zrn: this removes {app_id} {what}. Go on?")):
            raise ZrnError("cancelled", "nothing was removed",
                           "answer y, or pass --yes where nobody can answer", exit_code=1)
    units = services.remove(app_id, [s.get("name") for s in m.get("services", []) if isinstance(s, dict)])
    stopped = stop_app(app_id)
    removed = []
    targets = [data_home() / "zrn" / "apps" / app_id, data_home() / "applications" / f"{app_id}.desktop",
               *(data_home() / "icons" / "hicolor").glob(f"*/apps/{app_id}.*")]
    if not keep:
        targets += app_data_paths(app_id, declared)
    else:
        targets += [cache_home() / "apps" / app_id]
    for p in targets:
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
            removed.append(str(p))
        elif p.exists() or p.is_symlink():
            p.unlink()
            removed.append(str(p))
    if not removed and not units:
        raise ZrnError("not-installed", f"{app_id} is not installed for this user", "see ~/.local/share/zrn/apps")
    tell_shell("apps")
    result(out, {"command": "uninstall", "id": app_id, "removed": removed, "stopped": stopped,
                 "services_removed": units, "data_kept": keep}, f"removed {app_id}")

# ------------------------------------------------------------------ main ---
def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output on stdout")
    ap = argparse.ArgumentParser(prog="zrn", description="Install, update, run and remove ztOS apps.",
                                 parents=[common])
    ap.add_argument("--version", action="version", version=f"zrn {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    p = sub.add_parser("verify", parents=[common], help="check a .zexec and its signature")
    p.add_argument("file")
    p.add_argument("--sig", help="signature file (default FILE.minisig)")
    p.add_argument("--key", action="append", help="also trust this public key (repeatable)")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("install", parents=[common], help="install or update a .zexec for this user")
    p.add_argument("file")
    p.add_argument("--key", action="append", help="also trust this public key")
    p.set_defaults(fn=cmd_install)

    p = sub.add_parser("store", parents=[common], help="the ztOS store: refresh, list, install ID")
    p.add_argument("action", choices=("refresh", "list", "install"))
    p.add_argument("id", nargs="?")
    p.set_defaults(fn=cmd_store)

    p = sub.add_parser("uninstall", parents=[common], help="remove an installed app and its data")
    p.add_argument("id")
    p.add_argument("--keep-data", action="store_true", help="leave what the app stored")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask")
    p.set_defaults(fn=cmd_uninstall)

    p = sub.add_parser("info", parents=[common], help="show a .zexec's manifest and files")
    p.add_argument("file")
    p.set_defaults(fn=cmd_info)

    p = sub.add_parser("exec", parents=[common], help="run a .zexec (what zrn-exec does)")
    p.add_argument("file")
    p.add_argument("args", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_exec)
    return ap


def main(argv=None):
    ap = parser()
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return 3 if e.code not in (0, None) else 0
    args.json = getattr(args, "json", False)
    out = Out(args.json)
    try:
        return args.fn(args, out) or 0
    except ZrnError as e:
        if out.json:
            print(json.dumps({"ok": False, "error": {"code": e.code, "message": e.message, "hint": e.hint}},
                             indent=1))
        else:
            print(f"zrn: error: {e.message}", file=sys.stderr)
            if e.hint:
                print(f"zrn: fix: {e.hint}", file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        return 130
    except OSError as e:
        msg = f"{e.filename}: {e.strerror}" if e.filename else str(e)
        if out.json:
            print(json.dumps({"ok": False, "error": {"code": "os-error", "message": msg,
                                                     "hint": "check the path and its permissions"}}, indent=1))
        else:
            print(f"zrn: error: {msg}", file=sys.stderr)
        return 1
