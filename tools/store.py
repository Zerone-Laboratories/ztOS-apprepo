#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""The ztOS store's own tools: the ledger, what to build, building one app at
its pinned commit, signing, the index, and checking it all as a client would.

    store.py validate                     every ledger file is well formed
    store.py plan  --index-tree DIR       approved releases with nothing published yet (JSON)
    store.py build --id ID --version V --out DIR     clone at the pinned SHA, zrn pack --no-sign
    store.py publish --artifacts DIR --index-tree DIR --assets-out DIR --key K --base-url URL
    store.py index --index-tree DIR --key K --base-url URL
    store.py check-index --index URL|FILE --pubkey store.pub     what a client must do

Needs the zrn tools: ZRN_SDK names a zrn-sdk checkout (sdk.lock says which
commit CI uses). Only `build` ever runs code from an app, and it runs with no
secrets; `publish` reads packages and never executes them.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = Path(os.environ.get("STORE_LEDGER") or ROOT / "ledger")
SDK = Path(os.environ.get("ZRN_SDK") or ROOT.parent / "zrn-sdk")
sys.path.insert(0, str(SDK / "tools"))
try:
    from zrnlib import sign, zexec  # noqa: E402
except ImportError:
    sys.exit(f"store.py: the zrn tools are not at {SDK}/tools; set ZRN_SDK to a zrn-sdk checkout (see sdk.lock)")

REPO_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MOD_RE = re.compile(r"^discord:\d{17,20}$")         # a Discord user id (snowflake)
STAGING_MOD = "discord:0"                             # the staged sample, never published for real
TIME_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
STATUSES = ("approved", "revoked")


class StoreError(Exception):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ ledger ---
def load_ledger(path):
    try:
        data = tomllib.loads(Path(path).read_text())
    except tomllib.TOMLDecodeError as e:
        raise StoreError(f"{path}: {e}")
    return data


def check_ledger(path, data, staging=False):
    """(errors, warnings) for one ledger file."""
    errors, warnings = [], []
    name = Path(path).stem
    i = data.get("id")
    if i != name:
        errors.append(f"id {i!r} must equal the file name ({name})")
    if not (isinstance(i, str) and zexec.ID_RE.match(i)):
        errors.append(f"id {i!r} is not a valid app id")
    if isinstance(i, str) and i.startswith("zrn-") and \
            not str(data.get("source", "")).startswith("https://github.com/Zerone-Laboratories/"):
        errors.append("zrn- ids are for Zerone Laboratories' own repositories")
    if not REPO_RE.match(str(data.get("source", ""))):
        errors.append(f"source {data.get('source')!r} must be https://github.com/OWNER/REPO")
    sub = data.get("subdir", "")
    if sub and (sub.startswith("/") or ".." in sub.split("/")):
        errors.append(f"subdir {sub!r} must be a relative path inside the repository")
    releases = data.get("release", [])
    if not isinstance(releases, list) or not releases:
        errors.append("there is no [[release]]")
        releases = []
    seen = set()
    for n, r in enumerate(releases, 1):
        where = f"release {n} ({r.get('version')})"
        if not zexec.VERSION_RE.match(str(r.get("version", ""))):
            errors.append(f"{where}: version must be semantic (1.2.0)")
        if r.get("version") in seen:
            errors.append(f"{where}: this version is in the ledger twice (an update needs a new version)")
        seen.add(r.get("version"))
        if not SHA_RE.match(str(r.get("commit", ""))):
            errors.append(f"{where}: commit must be a full 40-hex SHA (never a branch or a short SHA)")
        status = r.get("status")
        if status not in STATUSES:
            errors.append(f"{where}: status {status!r} must be one of {', '.join(STATUSES)}")
        who = str(r.get("approved_by", ""))
        if who == STAGING_MOD:
            warnings.append(f"{where}: a staging approval ({STAGING_MOD}): never published by CI; "
                            "submit it on Discord to approve it for real")
        elif not MOD_RE.match(who):
            errors.append(f"{where}: approved_by {who!r} must be discord:<moderator's user id>")
        if not TIME_RE.match(str(r.get("approved_at", ""))):
            errors.append(f"{where}: approved_at must be UTC like 2026-10-30T01:02:03Z")
        if status == "revoked":
            for k in ("revoked_by", "revoked_at", "revoke_reason"):
                if not r.get(k):
                    errors.append(f"{where}: a revoked release needs {k}")
    return errors, warnings


def ledgers():
    for path in sorted(LEDGER.glob("*.toml")):
        yield path, load_ledger(path)


def publishable(data, staging):
    for r in data.get("release", []):
        if r.get("status") != "approved":
            continue
        if r.get("approved_by") == STAGING_MOD and not staging:
            continue
        yield r


# --------------------------------------------------------------- the tree ---
# The index tree (the store-index branch): index.json, index.json.minisig and
# releases/<id>/<version>/<arch>.json, one per published package.
def published(tree, app_id, version):
    d = Path(tree) / "releases" / app_id / version
    return sorted(d.glob("*.json")) if d.is_dir() else []


def cmd_validate(args):
    bad = 0
    for path, data in ledgers():
        errors, warnings = check_ledger(path, data, args.staging)
        for w in warnings:
            print(f"warning: {path.name}: {w}")
        for e in errors:
            print(f"error: {path.name}: {e}")
        bad += bool(errors)
    print(f"{len(list(LEDGER.glob('*.toml')))} ledger files, {bad} with errors")
    return 1 if bad else 0


def cmd_plan(args):
    builds = []
    for path, data in ledgers():
        errors, _ = check_ledger(path, data, args.staging)
        if errors:
            print(f"plan: skipping {path.name}: {errors[0]}", file=sys.stderr)
            continue
        for r in publishable(data, args.staging):
            if published(args.index_tree, data["id"], r["version"]):
                continue
            builds.append({"id": data["id"], "version": r["version"], "commit": r["commit"],
                           "source": data["source"], "subdir": data.get("subdir", "")})
    out = json.dumps(builds)
    if args.github_output and os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"builds={out}\nany={'true' if builds else 'false'}\n")
    print(out)
    return 0


# ------------------------------------------------------------------ build ---
def git(*args, cwd=None):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if p.returncode:
        raise StoreError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout.strip()


def cmd_build(args):
    """Fetch the app at its pinned commit and package it, unsigned. Runs app
    code (its build): CI gives this step no secrets."""
    data = load_ledger(LEDGER / f"{args.id}.toml")
    rel = next((r for r in data.get("release", []) if r.get("version") == args.version), None)
    if not rel or rel.get("status") != "approved":
        raise StoreError(f"{args.id} {args.version} is not an approved release in the ledger")
    sha, source = rel["commit"], data["source"]
    fetch_from = source
    for o in args.source_override or []:  # tests: a local clone stands in for GitHub
        url, _, local = o.partition("=")
        if url == source:
            fetch_from = local
    work = Path(tempfile.mkdtemp(prefix="zrn-store-build-"))
    git("init", "-q", str(work))
    git("fetch", "-q", "--depth", "1", fetch_from, sha, cwd=work)
    git("checkout", "-q", "--detach", "FETCH_HEAD", cwd=work)
    head = git("rev-parse", "HEAD", cwd=work)
    if head != sha:
        raise StoreError(f"fetched {head}, the ledger pins {sha}")
    app = work / data.get("subdir", "")
    toml = app / "zrnapp.toml"
    if not toml.exists():
        raise StoreError(f"no zrnapp.toml at {data.get('subdir') or '/'} in {source} @ {sha}")
    proj = tomllib.loads(toml.read_text()).get("app", {})
    if proj.get("id") != args.id or proj.get("version") != args.version:
        raise StoreError(f"the commit's zrnapp.toml says {proj.get('id')} {proj.get('version')}, "
                         f"the ledger {args.id} {args.version}")
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, ZRN_SOURCE_REPO=source, ZRN_SOURCE_COMMIT=sha)
    p = subprocess.run([sys.executable, str(SDK / "tools" / "zrn"), "pack", "--no-sign", "--bundle-zrnui", "--json",
                        "-C", str(app), "--out", str(out)], capture_output=True, text=True, env=env)
    try:
        info = json.loads(p.stdout)
    except ValueError:
        raise StoreError(f"zrn pack failed: {p.stderr.strip()[-2000:]}")
    if not info.get("ok"):
        raise StoreError(f"zrn pack failed: {info.get('error')}")
    print(json.dumps({"id": args.id, "version": args.version, "commit": sha, "package": info["path"],
                      "sha256": info["sha256"], "arch": info["arch"]}))
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    return 0


# ---------------------------------------------------------------- publish ---
def bot_authored(app_id, email):
    """The last commit that changed this app's ledger file was the bot's."""
    log = git("log", "-1", "--format=%ae", "--", f"ledger/{app_id}.toml", cwd=ROOT)
    return log.strip() == email


def cmd_publish(args):
    """Sign packages built by CI and record them in the index tree. Never runs them."""
    key = sign.SecretKey.loads(Path(args.key).read_text(), args.key)
    approved = {}
    for path, data in ledgers():
        errors, _ = check_ledger(path, data, args.staging)
        if errors:
            continue
        for r in publishable(data, args.staging):
            approved[(data["id"], r["version"])] = (data, r)
    done = []
    for pkg in sorted(Path(args.artifacts).rglob("*.zexec")):
        m, problems = zexec.check(str(pkg))
        if problems:
            print(f"publish: refusing {pkg.name}: {'; '.join(problems)}", file=sys.stderr)
            continue
        entry = approved.get((m["id"], m["version"]))
        if not entry:
            print(f"publish: refusing {pkg.name}: {m['id']} {m['version']} is not approved", file=sys.stderr)
            continue
        data, rel = entry
        src = m.get("source") or {}
        if src.get("commit") != rel["commit"] or src.get("repo") != data["source"]:
            print(f"publish: refusing {pkg.name}: built from {src}, the ledger pins "
                  f"{data['source']} @ {rel['commit']}", file=sys.stderr)
            continue
        if m["arch"] not in ("aarch64", "any") and not args.allow_arch:
            print(f"publish: refusing {pkg.name}: arch {m['arch']} (the store ships aarch64 and any)",
                  file=sys.stderr)
            continue
        if args.require_bot_email and not bot_authored(m["id"], args.require_bot_email):
            print(f"publish: refusing {pkg.name}: ledger/{m['id']}.toml was last changed by someone "
                  f"other than {args.require_bot_email}", file=sys.stderr)
            continue
        meta_path = Path(args.index_tree) / "releases" / m["id"] / m["version"] / f"{m['arch']}.json"
        if meta_path.exists():
            print(f"publish: {m['id']} {m['version']} {m['arch']} is already published; packages never change",
                  file=sys.stderr)
            continue
        sha = zexec.sha256_file(str(pkg))
        tag = f"{m['id']}-{m['version']}"
        name = f"{m['id']}-{m['version']}-{m['arch']}.zexec"
        sig_text = sign.sign_file(key, str(pkg), zexec.trusted_comment(m, sha), "ztOS store signature")
        assets = Path(args.assets_out) / tag
        assets.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pkg, assets / name)
        (assets / (name + ".minisig")).write_text(sig_text)
        meta = {"id": m["id"], "version": m["version"], "arch": m["arch"], "sha256": sha,
                "size": pkg.stat().st_size, "tag": tag, "file": name, "minisig": sig_text,
                "manifest": m, "published_at": now()}
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")
        done.append(tag)
        print(f"publish: signed {name} ({sha[:16]}...)")
    print(json.dumps({"published": done}))
    return 0


def cmd_index(args):
    key = sign.SecretKey.loads(Path(args.key).read_text(), args.key)
    tree = Path(args.index_tree)
    apps, revoked = [], []
    for path, data in ledgers():
        errors, _ = check_ledger(path, data, args.staging)
        if errors:
            print(f"index: leaving out {path.name}: {errors[0]}", file=sys.stderr)
            continue
        releases = []
        for r in data.get("release", []):
            for meta_path in published(tree, data["id"], r["version"]):
                meta = json.loads(meta_path.read_text())
                if r.get("status") == "revoked":
                    revoked.append({"id": data["id"], "version": r["version"], "arch": meta["arch"],
                                    "sha256": meta["sha256"], "reason": r.get("revoke_reason", ""),
                                    "revoked_at": r.get("revoked_at", "")})
                    continue
                if r.get("status") != "approved" or (r.get("approved_by") == STAGING_MOD and not args.staging):
                    continue
                m = meta["manifest"]
                base = args.base_url.rstrip("/")
                releases.append({
                    "version": r["version"], "arch": meta["arch"], "sha256": meta["sha256"], "size": meta["size"],
                    "url": f"{base}/{meta['tag']}/{meta['file']}", "sig_url": f"{base}/{meta['tag']}/{meta['file']}.minisig",
                    "zrnui": m.get("zrnui"), "min_ztos": m.get("min_ztos"), "permissions": m.get("permissions", []),
                    "commit": r["commit"], "approved_by": r["approved_by"], "approved_at": r["approved_at"],
                })
        if not releases:
            continue
        latest = max(releases, key=lambda x: tuple(int(p) for p in x["version"].split("-")[0].split(".")))
        meta = json.loads(published(tree, data["id"], latest["version"])[0].read_text())["manifest"]
        apps.append({"id": data["id"], "name": meta.get("name", data.get("name", "")),
                     "summary": meta.get("summary", ""), "author": meta.get("author", data.get("author", "")),
                     "licence": meta.get("licence", ""), "categories": meta.get("categories", []),
                     "source": data["source"], "subdir": data.get("subdir", ""), "releases": releases})
    index = {"format": 1, "generated": now(), "store_key": key.public.id_hex, "apps": apps, "revoked": revoked}
    tree.mkdir(parents=True, exist_ok=True)
    path = tree / "index.json"
    path.write_text(json.dumps(index, indent=1, sort_keys=True) + "\n")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    (tree / "index.json.minisig").write_text(
        sign.sign_file(key, str(path), f"zrn-index format=1 generated={index['generated']} sha256={sha}",
                       "ztOS store index"))
    print(f"index: {len(apps)} apps, {len(revoked)} revoked -> {path}")
    return 0


# ------------------------------------------------------------ as a client ---
def fetch(url, dest):
    with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def cmd_check_index(args):
    """What ztstore must do: trust only the store key; refuse anything else."""
    pub = sign.PublicKey.loads(Path(args.pubkey).read_text(), args.pubkey)
    work = Path(tempfile.mkdtemp(prefix="zrn-store-check-"))
    src = args.index
    idx = work / "index.json"
    for name, url in ((idx, src), (work / "index.json.minisig", src + ".minisig")):
        if "://" in url:
            fetch(url, name)
        else:
            shutil.copy2(url, name)
    sign.verify_file(str(idx), (work / "index.json.minisig").read_text(), [pub], "index.json.minisig")
    index = json.loads(idx.read_text())
    bad = 0
    for app in index["apps"]:
        for r in app["releases"]:
            pkg = work / Path(r["url"]).name
            fetch(r["url"], pkg)
            fetch(r["sig_url"], str(pkg) + ".minisig")
            try:
                if zexec.sha256_file(str(pkg)) != r["sha256"]:
                    raise sign.SignError("sha256 differs from the index")
                _, s = sign.verify_file(str(pkg), Path(str(pkg) + ".minisig").read_text(), [pub], pkg.name)
                tc = zexec.parse_trusted_comment(s.trusted_comment)
                if (tc.get("id"), tc.get("version"), tc.get("arch")) != (app["id"], r["version"], r["arch"]):
                    raise sign.SignError(f"signed for {tc}, listed as {app['id']} {r['version']} {r['arch']}")
                _, problems = zexec.check(str(pkg))
                if problems:
                    raise sign.SignError("; ".join(problems))
                print(f"good: {app['id']} {r['version']} {r['arch']} ({r['sha256'][:16]}...)")
            except sign.SignError as e:
                bad += 1
                print(f"REFUSE: {app['id']} {r['version']}: {e}")
    for rv in index.get("revoked", []):
        print(f"revoked: {rv['id']} {rv['version']} ({rv['sha256'][:16]}...): {rv['reason']}")
    shutil.rmtree(work, ignore_errors=True)
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="store.py")
    ap.add_argument("--staging", action="store_true", help="accept the staged sample approval (local tests)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate").set_defaults(fn=cmd_validate)
    p = sub.add_parser("plan")
    p.add_argument("--index-tree", required=True)
    p.add_argument("--github-output", action="store_true")
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("build")
    p.add_argument("--id", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--source-override", action="append", help="URL=LOCALPATH (tests only)")
    p.add_argument("--keep", action="store_true")
    p.set_defaults(fn=cmd_build)
    p = sub.add_parser("publish")
    p.add_argument("--artifacts", required=True)
    p.add_argument("--index-tree", required=True)
    p.add_argument("--assets-out", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--require-bot-email")
    p.add_argument("--allow-arch", action="store_true", help="also x86_64 (tests on a laptop)")
    p.set_defaults(fn=cmd_publish)
    p = sub.add_parser("index")
    p.add_argument("--index-tree", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--base-url", required=True)
    p.set_defaults(fn=cmd_index)
    p = sub.add_parser("check-index")
    p.add_argument("--index", required=True, help="URL or path of index.json (its .minisig next to it)")
    p.add_argument("--pubkey", required=True)
    p.set_defaults(fn=cmd_check_index)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (StoreError, sign.SignError, zexec.ZexecError) as e:
        print(f"store.py: {getattr(e, 'message', e)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
