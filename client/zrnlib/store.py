# SPDX-License-Identifier: GPL-2.0-or-later
"""The phone's side of the ztOS store: `zrn store refresh|list|install ID`.

Only the store's key is trusted here, not a user's dev keys. The index must
verify before anything in it is read, may not be older than the one already
held, and every package must match its sha256 in the index and verify with that
key. A revoked release is disabled on this phone: zrn-exec refuses it by sha256.
"""
import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from . import services, sign, zexec
from .errors import ZrnError

DEFAULT_INDEX = "https://raw.githubusercontent.com/Zerone-Laboratories/ztOS-apprepo/store-index"
SYSTEM_KEY = "/usr/share/zrn/trusted-keys/store.pub"


def ztos_version():
    """This system's ztOS version as numbers, or None when it is not ztOS."""
    try:
        text = Path(os.environ.get("ZRN_OS_RELEASE") or "/etc/os-release").read_text()
    except OSError:
        return None
    if not re.search(r'^ID="?ztos"?$', text, re.M):
        return None
    m = re.search(r'^VERSION_ID="?(\d+(?:\.\d+)*)', text, re.M)
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def too_old(min_ztos):
    """Why this system cannot run an app needing min_ztos, or None."""
    have = ztos_version()
    if have is None or not min_ztos or os.environ.get("ZRN_IGNORE_MIN_ZTOS"):
        return None
    want = tuple(int(x) for x in re.findall(r"\d+", str(min_ztos).split("-")[0]))
    if have >= want:
        return None
    return f"it needs ztOS {min_ztos} or later, this is {'.'.join(map(str, have))}"


def _home(var, rel):
    return Path(os.environ.get(var) or Path.home() / rel)


def cache_file():
    return _home("XDG_CACHE_HOME", ".cache") / "zrn" / "store-index.json"


def revoked_file():
    """Read by zrn-exec: sha256 -> {id, reason}."""
    return _home("XDG_STATE_HOME", ".local/state") / "zrn" / "revoked.json"


def read_revoked():
    try:
        return json.loads(revoked_file().read_text())
    except (OSError, ValueError):
        return {}


def store_key():
    path = os.environ.get("ZRN_STORE_KEY") or SYSTEM_KEY
    try:
        return sign.PublicKey.loads(Path(path).read_text(), path)
    except OSError as e:
        raise ZrnError("no-store-key", f"cannot read the store's key {path}: {e.strerror}",
                       "it comes with ztOS BETA-1.1 and later")
    except sign.SignError as e:
        raise ZrnError("bad-key", f"{path}: {e}", "reinstall the store's key")


def _fetch(url, limit=256 * 1024 * 1024):
    if "://" not in url:
        return Path(url).read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": "zrn-store"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ZrnError("too-big", f"{url} is larger than expected", "try again later")
    return data


def _verified_index(raw, sig_text):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "index.json"
        p.write_bytes(raw)
        try:
            _, s = sign.verify_file(str(p), sig_text, [store_key()], "index.json.minisig")
        except sign.SignError as e:
            raise ZrnError("bad-index", f"the store's index did not verify: {e}", "nothing was changed")
    sha = hashlib.sha256(raw).hexdigest()
    if f"sha256={sha}" not in s.trusted_comment or not s.trusted_comment.startswith("zrn-index "):
        raise ZrnError("bad-index", "the index's signature is for another file", "nothing was changed")
    try:
        index = json.loads(raw)
    except ValueError:
        raise ZrnError("bad-index", "the index is not JSON", "nothing was changed")
    if index.get("format") != 1:
        raise ZrnError("bad-index", f"index format {index.get('format')!r} is not known here", "update zrn")
    return index


def _desktop(app_id):
    return _home("XDG_DATA_HOME", ".local/share") / "applications" / f"{app_id}.desktop"


def _installed(app_id):
    """(version, sha256) from the app-list entry `zrn install` wrote, or (None, None)."""
    try:
        t = _desktop(app_id).read_text()
    except OSError:
        return None, None
    v = re.search(r"^X-Zrn-Version=(.*)$", t, re.M)
    s = re.search(r"^X-Zrn-Sha256=(.*)$", t, re.M)
    return (v.group(1) if v else None), (s.group(1) if s else None)


def _apply_revocations(index):
    known = read_revoked()
    disabled = []
    for r in index.get("revoked", []):
        sha = r.get("sha256")
        if not sha:
            continue
        known[sha] = {"id": r.get("id"), "version": r.get("version"), "reason": r.get("reason", "")}
        _, have = _installed(r.get("id", ""))
        if have == sha:
            _desktop(r["id"]).unlink(missing_ok=True)
            services.remove(r["id"])     # its login services stop now, not at the next login
            disabled.append(r["id"])
    revoked_file().parent.mkdir(parents=True, exist_ok=True)
    revoked_file().write_text(json.dumps(known, indent=1, sort_keys=True))
    return disabled


def refresh():
    base = (os.environ.get("ZRN_STORE_INDEX") or DEFAULT_INDEX).rstrip("/")
    try:
        raw = _fetch(base + "/index.json", 16 * 1024 * 1024)
        sig = _fetch(base + "/index.json.minisig", 4096).decode()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"index": "none", "disabled": [], "note": "the store has published nothing yet"}
        raise ZrnError("no-index", f"the store answered {e.code}", "try again later")
    except (OSError, urllib.error.URLError):
        raise ZrnError("no-internet", "No internet connection.", "connect, then check the store again")
    index = _verified_index(raw, sig)
    held = _read_cache()
    if held and str(index.get("generated", "")) < str(held.get("generated", "")):
        raise ZrnError("old-index", "the store offered an older index than this phone already has",
                       "nothing was changed; try again later")
    disabled = _apply_revocations(index)
    p = cache_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.with_suffix(".sig").write_text(sig)
    p.write_bytes(raw)
    return {"index": "ok", "disabled": disabled, "apps": len(index.get("apps", []))}


def _read_cache():
    """The held index, verified again: the cache is only a file."""
    p = cache_file()
    try:
        return _verified_index(p.read_bytes(), p.with_suffix(".sig").read_text())
    except (OSError, ZrnError):
        return None


def _vkey(v):
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.-]", str(v)))


def _latest(app, arch, revoked):
    best = None
    for r in app.get("releases", []):
        if r.get("arch") not in (arch, "any") or r.get("sha256") in revoked:
            continue
        if best is None or _vkey(r["version"]) > _vkey(best["version"]):
            best = r
    return best


def listing(arch):
    index = _read_cache()
    revoked = read_revoked()
    out = []
    for app in (index or {}).get("apps", []):
        rel = _latest(app, arch, revoked)
        have, _ = _installed(app.get("id", ""))
        if not rel and not have:
            continue
        out.append({"id": app.get("id"), "name": app.get("name"), "summary": app.get("summary"),
                    "author": app.get("author"), "licence": app.get("licence"), "source": app.get("source"), "icon_b64": app.get("icon_b64", ""),
                    "version": rel["version"] if rel else "", "size": rel.get("size", 0) if rel else 0,
                    "permissions": rel.get("permissions", []) if rel else [],
                    "format": rel.get("zexec", 1) if rel else 1,
                    "installed": have or "", "update": bool(rel and have and _vkey(rel["version"]) > _vkey(have))})
    disabled = [{"id": v.get("id"), "version": v.get("version"), "reason": v.get("reason", "")} for v in revoked.values()]
    return {"index": "ok" if index else "none", "apps": out, "revoked": disabled}


def fetch_package(app_id, arch, dest_dir):
    """The release's file and signature in dest_dir, checked against the index and the store's key."""
    index = _read_cache()
    if not index:
        raise ZrnError("no-index", "no verified index on this phone", "run `zrn store refresh` first")
    app = next((a for a in index.get("apps", []) if a.get("id") == app_id), None)
    if not app:
        raise ZrnError("not-listed", f"{app_id} is not in the store", "`zrn store list` shows what is")
    revoked = read_revoked()
    rel = _latest(app, arch, revoked)
    if not rel:
        raise ZrnError("no-release", f"{app_id} has no usable release for {arch}", "")
    old = too_old(rel.get("min_ztos"))
    if old:
        raise ZrnError("ztos-too-old", f"{app_id}: {old}", "update ztOS first")
    try:
        data = _fetch(rel["url"])
        sig = _fetch(rel["sig_url"], 4096).decode()
    except (OSError, urllib.error.URLError):
        raise ZrnError("no-internet", "No internet connection.", "connect, then try again")
    sha = hashlib.sha256(data).hexdigest()
    if sha != rel["sha256"]:
        raise ZrnError("bad-package", f"{app_id}: the download differs from the store's index", "it was not installed")
    path = Path(dest_dir) / f"{app_id}.zexec"
    path.write_bytes(data)
    Path(str(path) + ".minisig").write_text(sig)
    try:
        _, s = sign.verify_file(str(path), sig, [store_key()], "the package's signature")
    except sign.SignError as e:
        raise ZrnError("bad-package", f"{app_id}: {e}", "it was not installed")
    tc = zexec.parse_trusted_comment(s.trusted_comment)
    if tc.get("id") != app_id or tc.get("version") != rel["version"] or tc.get("sha256") != sha:
        raise ZrnError("bad-package", f"{app_id}: its signature names something else", "it was not installed")
    return path
