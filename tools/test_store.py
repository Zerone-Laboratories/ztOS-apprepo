# SPDX-License-Identifier: GPL-2.0-or-later
"""The store pipeline without GitHub or Discord: ledger rules, publishing only
what is approved and built from the pinned commit, revocation, and a client
that refuses anything not signed by the store key."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SDK = Path(os.environ.get("ZRN_SDK") or HERE.parents[1] / "zrn-sdk")
sys.path.insert(0, str(SDK / "tools"))
from zrnlib import sign, zexec  # noqa: E402

SHA = "1" * 40
MOD = "discord:123456789012345678"
REPO = "https://github.com/alice/notes"


def ledger(app_id="io.github.alice.notes", version="1.0.0", commit=SHA, status="approved", extra=""):
    return f'''id = "{app_id}"
source = "{REPO}"
subdir = ""
[[release]]
version = "{version}"
commit = "{commit}"
status = "{status}"
approved_by = "{MOD}"
approved_by_name = "mod"
approved_at = "2026-10-30T01:02:03Z"
{extra}
'''


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        (self.d / "ledger").mkdir()
        self.env = dict(os.environ, STORE_LEDGER=str(self.d / "ledger"), ZRN_SDK=str(SDK))
        self.key = sign.SecretKey.generate()
        (self.d / "store.key").write_text(self.key.dumps())
        (self.d / "store.pub").write_text(self.key.public.dumps())

    def tearDown(self):
        self.tmp.cleanup()

    def store(self, *args):
        p = subprocess.run([sys.executable, str(HERE / "store.py"), *args], capture_output=True, text=True,
                           env=self.env)
        return p.returncode, p.stdout, p.stderr

    def artifact(self, commit=SHA, version="1.0.0", name="a"):
        m = {"zexec": 1, "id": "io.github.alice.notes", "name": "Notes", "version": version, "arch": "any",
             "runtime": "python3", "entry": "app/main.py", "zrnui": "0.1",
             "source": {"repo": REPO, "commit": commit}}
        out = self.d / "artifacts" / name
        out.mkdir(parents=True, exist_ok=True)
        zexec.pack(str(out / f"notes-{version}.zexec"), m, {"app/main.py": (b"print('hi')\n", 0o644)})

    def publish_and_index(self):
        code, out, perr = self.store("publish", "--artifacts", str(self.d / "artifacts"), "--index-tree",
                                    str(self.d / "tree"), "--assets-out", str(self.d / "assets"),
                                    "--key", str(self.d / "store.key"))
        self.assertEqual(code, 0, perr)
        code, _, err = self.store("index", "--index-tree", str(self.d / "tree"), "--key", str(self.d / "store.key"),
                                  "--base-url", f"file://{self.d / 'assets'}")
        self.assertEqual(code, 0, err)
        return json.loads((self.d / "tree" / "index.json").read_text()), perr

    def test_ledger_rules(self):
        (self.d / "ledger" / "io.github.alice.notes.toml").write_text(
            ledger(commit="abc123").replace(MOD, "someone"))
        (self.d / "ledger" / "io.github.bob.other.toml").write_text(ledger())  # id differs from file name
        code, out, _ = self.store("validate")
        self.assertEqual(code, 1)
        self.assertIn("full 40-hex SHA", out)
        self.assertIn("discord:<moderator", out)
        self.assertIn("must equal the file name", out)

    def test_only_approved_pinned_signed(self):
        (self.d / "ledger" / "io.github.alice.notes.toml").write_text(ledger())
        self.artifact()                                   # the pinned commit
        self.artifact(commit="2" * 40, name="b")          # built from another commit
        self.artifact(version="9.9.9", name="c")          # never approved
        index, err = self.publish_and_index()
        self.assertIn("the ledger pins", err)
        self.assertIn("is not approved", err)
        rel = index["apps"][0]["releases"]
        self.assertEqual([(r["version"], r["commit"]) for r in rel], [("1.0.0", SHA)])
        code, out, _ = self.store("check-index", "--index", str(self.d / "tree" / "index.json"),
                                  "--pubkey", str(self.d / "store.pub"))
        self.assertEqual(code, 0, out)
        # a client with another key refuses the whole index
        other = sign.SecretKey.generate().public
        (self.d / "other.pub").write_text(other.dumps())
        code, _, err = self.store("check-index", "--index", str(self.d / "tree" / "index.json"),
                                  "--pubkey", str(self.d / "other.pub"))
        self.assertEqual(code, 1)
        self.assertIn("not trusted", err)
        # a package swapped on the mirror is refused
        pkg = next((self.d / "assets").rglob("*.zexec"))
        data = bytearray(pkg.read_bytes())
        data[30] ^= 1
        pkg.write_bytes(bytes(data))
        code, out, _ = self.store("check-index", "--index", str(self.d / "tree" / "index.json"),
                                  "--pubkey", str(self.d / "store.pub"))
        self.assertEqual(code, 1)
        self.assertIn("REFUSE", out)

    def test_revocation(self):
        path = self.d / "ledger" / "io.github.alice.notes.toml"
        path.write_text(ledger())
        self.artifact()
        self.publish_and_index()
        path.write_text(ledger(status="revoked", extra='revoked_by = "discord:123456789012345679"\n'
                               'revoked_at = "2026-10-31T00:00:00Z"\nrevoke_reason = "sends contacts away"'))
        code, _, err = self.store("index", "--index-tree", str(self.d / "tree"), "--key", str(self.d / "store.key"),
                                  "--base-url", f"file://{self.d / 'assets'}")
        self.assertEqual(code, 0, err)
        index = json.loads((self.d / "tree" / "index.json").read_text())
        self.assertEqual(index["apps"], [])
        self.assertEqual(index["revoked"][0]["reason"], "sends contacts away")

    def test_plan_skips_published_and_staging(self):
        (self.d / "ledger" / "io.github.alice.notes.toml").write_text(ledger().replace(MOD, "discord:0"))
        code, out, _ = self.store("plan", "--index-tree", str(self.d / "tree"))
        self.assertEqual(json.loads(out), [])
        code, out, _ = self.store("--staging", "plan", "--index-tree", str(self.d / "tree"))
        self.assertEqual(len(json.loads(out)), 1)


if __name__ == "__main__":
    unittest.main()
