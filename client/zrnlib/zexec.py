# SPDX-License-Identifier: GPL-2.0-or-later
"""The .zexec package: one file, an app.

    #!/usr/bin/env zrn-exec\\n      24 bytes: the kernel runs it with zrn-exec
    <zip archive>                   zexec.json first, then icon, bin/ lib/ share/ or app/

and next to it, detached, <file>.minisig (see sign.py). The signature covers
every byte, the header line included. Packing is reproducible: the same
inputs give the same bytes (sorted names, fixed times and modes).

Format 1 (SDK v1): libzrnui shared, from the system or bundled in lib/.
Format 2 (SDK v2): one native program with zrnui and zrnapp linked in
statically, the zrnui fonts and theme under share/zrnui/, and optional login
services the client turns into systemd user units. See the store's README.
"""
import hashlib
import io
import json
import os
import re
import stat
import zipfile

HEADER = b"#!/usr/bin/env zrn-exec\n"
FORMAT = 1          # what SDK v1 packs
FORMATS = (1, 2)    # what this client reads
SERVICE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,63}$")
RESTARTS = ("no", "on-failure", "always")
ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MANIFEST = "zexec.json"
ZIP_TIME = (1980, 1, 1, 0, 0, 0)

ID_RE = re.compile(r"^(zrn-[a-z0-9]+(-[a-z0-9]+)*|[a-z][a-z0-9]*(\.[a-z0-9][a-z0-9-]*){2,})$")
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?$")
ARCHES = ("aarch64", "x86_64", "any")
RUNTIMES = ("native", "python3")
PERMISSIONS = {
    "network": "the internet and the local network",
    "audio": "play and record sound",
    "camera": "the cameras",
    "location": "where the phone is (GNSS)",
    "notifications": "show notifications",
    "bluetooth": "Bluetooth devices",
    "phone": "calls and the modem",
    "contacts": "the address book",
    "messages": "SMS",
    "background": "keep running when not in front",
    "files.music": "~/Music",
    "files.pictures": "~/Pictures",
    "files.documents": "~/Documents",
    "files.downloads": "~/Downloads",
}
REQUIRED = ("zexec", "id", "name", "version", "arch", "runtime", "entry", "zrnui")


class ZexecError(Exception):
    def __init__(self, code, message, hint=None):
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint


def validate_manifest(m):
    """A list of exact problems; empty when the manifest is good."""
    problems = []
    if not isinstance(m, dict):
        return ["the manifest is not a JSON object"]
    for k in REQUIRED:
        if k not in m:
            problems.append(f"'{k}' is missing")
    if m.get("zexec") not in (None, *FORMATS):
        problems.append(f"'zexec' is {m.get('zexec')!r}; this zrn reads formats {', '.join(map(str, FORMATS))}")
    if m.get("zexec") == 2:
        problems += _check_v2(m)
    i = m.get("id", "")
    if "id" in m and not (isinstance(i, str) and ID_RE.match(i) and len(i) <= 96):
        problems.append(f"'id' {i!r} must be reverse-DNS (io.github.you.app: at least three parts, "
                        "lower case) or zrn-name (Zerone Laboratories' own apps)")
    v = m.get("version", "")
    if "version" in m and not (isinstance(v, str) and VERSION_RE.match(v)):
        problems.append(f"'version' {v!r} must be semantic, like 1.2.0")
    if "arch" in m and m["arch"] not in ARCHES:
        problems.append(f"'arch' {m['arch']!r} must be one of {', '.join(ARCHES)}")
    if "runtime" in m and m["runtime"] not in RUNTIMES:
        problems.append(f"'runtime' {m['runtime']!r} must be one of {', '.join(RUNTIMES)}")
    if m.get("runtime") == "python3" and m.get("arch") not in (None, "any"):
        problems.append("a python3 app's 'arch' must be 'any' (native code goes in a native app)")
    if m.get("runtime") == "native" and m.get("arch") == "any":
        problems.append("a native app's 'arch' must name the CPU (aarch64 for the phone)")
    e = m.get("entry", "")
    if "entry" in m and not _safe_path(e):
        problems.append(f"'entry' {e!r} must be a relative path inside the package")
    if "zrnui" in m and not re.match(r"^\d+\.\d+$", str(m["zrnui"])):
        problems.append(f"'zrnui' {m['zrnui']!r} must be the ABI the app was built against, like 0.1")
    perms = m.get("permissions", [])
    if not isinstance(perms, list):
        problems.append("'permissions' must be a list")
    else:
        for p in perms:
            if p not in PERMISSIONS:
                problems.append(f"permission {p!r} is not known (known: {', '.join(sorted(PERMISSIONS))})")
    if "name" in m and not (isinstance(m["name"], str) and 0 < len(m["name"]) <= 40):
        problems.append("'name' must be 1 to 40 characters")
    icon = m.get("icon")
    if icon is not None and not (_safe_path(icon) and icon.endswith((".svg", ".png"))):
        problems.append(f"'icon' {icon!r} must be an .svg or .png inside the package")
    return problems


def service_name_ok(app_id, name):
    """A service is written to <name>.service. An app names units only inside its own
    id (io.github.a.b, io.github.a.b-x); Zerone Laboratories' zrn- apps (the ledger
    keeps zrn- ids to their repositories) may use any zrn- name."""
    if not (isinstance(name, str) and SERVICE_NAME_RE.match(name)):
        return False
    if name == app_id or name.startswith(app_id + "-") or name.startswith(app_id + "."):
        return True
    return app_id.startswith("zrn-") and name.startswith("zrn-")


def _check_v2(m):
    problems = []
    if m.get("runtime") != "native":
        problems.append("a format 2 package is a native program ('runtime' native)")
    if not re.match(r"^2\.\d+$", str(m.get("zrnui", ""))):
        problems.append(f"'zrnui' {m.get('zrnui')!r}: a format 2 package is built with zrnui 2.x")
    services = m.get("services", [])
    if not isinstance(services, list):
        return problems + ["'services' must be a list"]
    if services and "background" not in (m.get("permissions") or []):
        problems.append("an app with login services must ask for the 'background' permission")
    names = [s.get("name") for s in services if isinstance(s, dict)]
    for s in services:
        if not isinstance(s, dict):
            problems.append("each service must be an object")
            continue
        n = s.get("name")
        if not service_name_ok(str(m.get("id", "")), n):
            problems.append(f"service {n!r}: its name must be the app id or start with it "
                            "(zrn- apps: any zrn- name), lower case, at most 64 characters")
        if names.count(n) > 1:
            problems.append(f"service {n!r} is declared twice")
        args = s.get("args", [])
        if not (isinstance(args, list) and all(isinstance(a, str) and "\n" not in a and "%" not in a for a in args)):
            problems.append(f"service {n!r}: 'args' must be a list of strings (no newlines, no %)")
        if s.get("restart", "on-failure") not in RESTARTS:
            problems.append(f"service {n!r}: 'restart' must be one of {', '.join(RESTARTS)}")
        for k in ("restart_sec", "nice"):
            if k in s and not (isinstance(s[k], int) and -20 <= s[k] <= 3600):
                problems.append(f"service {n!r}: '{k}' must be a whole number")
        codes = s.get("restart_prevent_exit_status", [])
        if not (isinstance(codes, list) and all(isinstance(c, int) and 0 <= c <= 255 for c in codes)):
            problems.append(f"service {n!r}: 'restart_prevent_exit_status' must be exit codes")
        if "umask" in s and not re.match(r"^0[0-7]{3}$", str(s["umask"])):
            problems.append(f"service {n!r}: 'umask' must be like 0077")
        mh = s.get("memory_high")
        if mh is not None and not re.match(r"^\d+[KMG]?$", str(mh)):
            problems.append(f"service {n!r}: 'memory_high' must be like 24M")
        for a in s.get("after", []) if isinstance(s.get("after", []), list) else [None]:
            if a not in names or a == n:
                problems.append(f"service {n!r}: 'after' may name only this app's other services")
        env = s.get("env", {})
        if not isinstance(env, dict) or any(not ENV_RE.match(str(k)) or not isinstance(v, str) or "\n" in v
                                            or "%" in v for k, v in env.items()):
            problems.append(f"service {n!r}: 'env' must map NAMES to strings (no newlines, no %)")
        d = s.get("description", "")
        if not isinstance(d, str) or "\n" in d or len(d) > 120:
            problems.append(f"service {n!r}: 'description' must be one line")
    return problems


def _safe_path(p):
    return isinstance(p, str) and p and not p.startswith("/") and ".." not in p.split("/") and "\\" not in p


def trusted_comment(m, sha256):
    return f"zexec id={m['id']} version={m['version']} arch={m['arch']} sha256={sha256}"


def parse_trusted_comment(text):
    if not text.startswith("zexec "):
        return {}
    out = {}
    for part in text[6:].split():
        k, _, v = part.partition("=")
        out[k] = v
    return out


def pack(out_path, manifest, files):
    """files: {path in package: (source path or bytes, mode)}. Returns the sha256."""
    problems = validate_manifest(manifest)
    if problems:
        raise ZexecError("manifest-invalid", "the manifest is not valid: " + "; ".join(problems))
    if manifest["entry"] not in files:
        raise ZexecError("entry-missing", f"the entry {manifest['entry']!r} is not in the package",
                         "check [build] in zrnapp.toml, then run `zrn build`")
    if manifest.get("icon") and manifest["icon"] not in files:
        raise ZexecError("icon-missing", f"the icon {manifest['icon']!r} is not in the package")
    buf = io.BytesIO()
    buf.write(HEADER)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        def add(name, data, mode):
            info = zipfile.ZipInfo(name, ZIP_TIME)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_STORED if name == MANIFEST else zipfile.ZIP_DEFLATED
            z.writestr(info, data)
        add(MANIFEST, json.dumps(manifest, indent=1, sort_keys=True).encode() + b"\n", 0o644)
        for name in sorted(files):
            if not _safe_path(name) or name == MANIFEST:
                raise ZexecError("bad-path", f"{name!r} cannot go in a package")
            src, mode = files[name]
            if isinstance(src, (bytes, bytearray)):
                data = bytes(src)
            else:
                if os.path.islink(src):
                    raise ZexecError("symlink", f"{src} is a symbolic link; packages hold files only")
                with open(src, "rb") as f:
                    data = f.read()
            add(name, data, 0o755 if mode & 0o111 else 0o644)
    data = buf.getvalue()
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.chmod(tmp, 0o755)
    os.replace(tmp, out_path)
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def open_zexec(path):
    with open(path, "rb") as f:
        head = f.read(len(HEADER))
    if head != HEADER:
        raise ZexecError("not-zexec", f"{path} is not a .zexec (it does not start with {HEADER[:-1].decode()!r})")
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise ZexecError("bad-archive", f"{path}: the archive is damaged ({e})")


def read_manifest(z):
    try:
        raw = z.read(MANIFEST)
    except KeyError:
        raise ZexecError("no-manifest", f"no {MANIFEST} in the package")
    try:
        return json.loads(raw)
    except ValueError as e:
        raise ZexecError("manifest-json", f"{MANIFEST} is not JSON: {e}")


def check(path):
    """Structure checks (no signature). Returns (manifest, list of problems)."""
    z = open_zexec(path)
    with z:
        m = read_manifest(z)
        problems = validate_manifest(m)
        names = set()
        first = z.infolist()[0].filename if z.infolist() else None
        if first != MANIFEST:
            problems.append(f"{MANIFEST} must be the first entry (it is {first!r})")
        for info in z.infolist():
            n = info.filename
            if n in names:
                problems.append(f"{n!r} is in the package twice")
            names.add(n)
            if info.is_dir():
                continue
            if not _safe_path(n):
                problems.append(f"{n!r} would land outside the app's folder")
            mode = info.external_attr >> 16
            if mode and stat.S_ISLNK(mode):
                problems.append(f"{n!r} is a symbolic link")
            if mode & 0o6000:
                problems.append(f"{n!r} is setuid or setgid")
        bad = z.testzip()
        if bad:
            problems.append(f"{bad!r} is damaged (CRC)")
        if isinstance(m, dict):
            if m.get("entry") and m["entry"] not in names:
                problems.append(f"the entry {m['entry']!r} is not in the package")
            if m.get("icon") and m["icon"] not in names:
                problems.append(f"the icon {m['icon']!r} is not in the package")
            if m.get("runtime") == "native" and m.get("entry") in names:
                head = z.read(m["entry"])[:20]
                if head[:4] != b"\x7fELF":
                    problems.append(f"the entry {m['entry']!r} is not an ELF program")
                else:
                    machine = int.from_bytes(head[18:20], "little")
                    want = {"aarch64": 183, "x86_64": 62}.get(m.get("arch"))
                    if want and machine != want:
                        problems.append(f"the entry is built for ELF machine {machine}, not {m.get('arch')}")
    return m, problems


def extract(path, dest):
    """Unpack into dest (made fresh), keeping the executable bit."""
    z = open_zexec(path)
    with z:
        tmp = dest + ".part"
        if os.path.exists(tmp):
            import shutil
            shutil.rmtree(tmp)
        os.makedirs(tmp)
        for info in z.infolist():
            if info.is_dir() or not _safe_path(info.filename):
                continue
            target = os.path.join(tmp, info.filename)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                out.write(src.read())
            mode = (info.external_attr >> 16) & 0o777
            os.chmod(target, 0o755 if mode & 0o111 else 0o644)
        os.replace(tmp, dest)
