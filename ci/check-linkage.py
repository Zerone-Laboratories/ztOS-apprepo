#!/usr/bin/env python3
"""Fails when a built package needs a library the ztOS image does not provide:
ldd in this rootfs, which holds only the image's packages. Prints which package
gives each library, for the log."""
import json, os, subprocess, sys, tempfile, zipfile

bad = 0
for path in sys.argv[1:]:
    with tempfile.TemporaryDirectory() as d, zipfile.ZipFile(path) as z:
        z.extractall(d)
        m = json.load(open(os.path.join(d, "zexec.json")))
        if m["runtime"] != "native":
            print(f"{path}: {m['runtime']}, nothing to link")
            continue
        entry = os.path.join(d, m["entry"])
        env = dict(os.environ, LD_LIBRARY_PATH=os.path.join(d, "lib"))
        out = subprocess.run(["ldd", entry], capture_output=True, text=True, env=env).stdout
        for line in out.splitlines():
            if "not found" in line:
                print(f"NOT IN THE IMAGE: {line.strip()}")
                bad += 1
            elif "=> /" in line:
                lib = line.split("=> ")[1].split()[0]
                own = subprocess.run(["pacman", "-Qqo", lib], capture_output=True, text=True).stdout.strip()
                print(f"{line.split()[0]}: {own or 'in the package'}")
sys.exit(1 if bad else 0)
