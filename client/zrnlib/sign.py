# SPDX-License-Identifier: GPL-2.0-or-later
"""Signatures in minisign's format (https://jedisct1.github.io/minisign/).

Keys and .minisig files made here are read by `minisign` itself and the other
way round: Ed25519 over the BLAKE2b-512 of the file ("ED", prehashed), plus a
trusted comment signed with it. Secret keys are written unencrypted (minisign
-W); keep them 0600 and out of git. In the store the key lives only in CI.
"""
import base64
import hashlib
import os
import secrets

from . import ed25519 as _pure

try:  # the fast, constant-time implementation when it is there
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    HAVE_CRYPTOGRAPHY = True
except ImportError:  # pragma: no cover - depends on the machine
    HAVE_CRYPTOGRAPHY = False


class SignError(Exception):
    pass


def _ed_public(seed):
    if HAVE_CRYPTOGRAPHY:
        from cryptography.hazmat.primitives import serialization
        k = Ed25519PrivateKey.from_private_bytes(seed)
        return k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return _pure.public_key(seed)


def _ed_sign(seed, msg):
    if HAVE_CRYPTOGRAPHY:
        return Ed25519PrivateKey.from_private_bytes(seed).sign(msg)
    return _pure.sign(seed, msg)


def _ed_verify(public, msg, sig):
    if HAVE_CRYPTOGRAPHY:
        try:
            Ed25519PublicKey.from_public_bytes(public).verify(sig, msg)
            return True
        except (InvalidSignature, ValueError):
            return False
    return _pure.verify(public, msg, sig)


class PublicKey:
    def __init__(self, keyid, pk, comment=""):
        self.keyid = keyid      # 8 bytes
        self.pk = pk            # 32 bytes
        self.comment = comment

    @property
    def id_hex(self):
        return "%016X" % int.from_bytes(self.keyid, "little")

    def dumps(self):
        b = base64.b64encode(b"Ed" + self.keyid + self.pk).decode()
        return f"untrusted comment: minisign public key {self.id_hex}\n{b}\n"

    @classmethod
    def loads(cls, text, origin="public key"):
        lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
        if not lines:
            raise SignError(f"{origin}: empty")
        line = lines[-1] if not lines[-1].startswith("untrusted comment:") else ""
        try:
            raw = base64.b64decode(line, validate=True)
        except Exception:
            raise SignError(f"{origin}: not a minisign public key (bad base64)")
        if len(raw) != 42 or raw[:2] != b"Ed":
            raise SignError(f"{origin}: not a minisign Ed25519 public key")
        comment = lines[0][len("untrusted comment: "):] if lines[0].startswith("untrusted comment:") else ""
        return cls(raw[2:10], raw[10:], comment)


class SecretKey:
    def __init__(self, keyid, seed):
        self.keyid = keyid
        self.seed = seed        # 32 bytes; minisign stores seed || pk as its 64-byte sk

    @property
    def public(self):
        return PublicKey(self.keyid, _ed_public(self.seed))

    def dumps(self):
        sk = self.seed + self.public.pk
        chk = hashlib.blake2b(b"Ed" + self.keyid + sk, digest_size=32).digest()
        raw = (b"Ed" + b"\0\0" + b"B2" + b"\0" * 32 + (0).to_bytes(8, "little") + (0).to_bytes(8, "little")
               + self.keyid + sk + chk)
        return "untrusted comment: minisign secret key (unencrypted)\n" + base64.b64encode(raw).decode() + "\n"

    @classmethod
    def loads(cls, text, origin="secret key"):
        lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
        try:
            raw = base64.b64decode(lines[-1], validate=True)
        except Exception:
            raise SignError(f"{origin}: not a minisign secret key")
        if len(raw) != 158 or raw[:2] != b"Ed":
            raise SignError(f"{origin}: not a minisign Ed25519 secret key")
        if raw[2:4] != b"\0\0":
            raise SignError(f"{origin}: the key is password-protected (scrypt); "
                            "zrn reads unencrypted keys only: make one with `zrn keygen` or `minisign -G -W`")
        keyid, sk, chk = raw[54:62], raw[62:126], raw[126:158]
        if hashlib.blake2b(b"Ed" + keyid + sk, digest_size=32).digest() != chk:
            raise SignError(f"{origin}: checksum does not match (damaged key)")
        return cls(keyid, sk[:32])

    @classmethod
    def generate(cls):
        return cls(secrets.token_bytes(8), secrets.token_bytes(32))


def blake2b_file(path):
    h = hashlib.blake2b(digest_size=64)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.digest()


def sign_file(key, path, trusted_comment, untrusted_comment="signature from zrn"):
    if "\n" in trusted_comment or "\r" in trusted_comment:
        raise SignError("the trusted comment must be one line")
    sig = _ed_sign(key.seed, blake2b_file(path))
    global_sig = _ed_sign(key.seed, sig + trusted_comment.encode())
    return (f"untrusted comment: {untrusted_comment}\n"
            + base64.b64encode(b"ED" + key.keyid + sig).decode() + "\n"
            + f"trusted comment: {trusted_comment}\n"
            + base64.b64encode(global_sig).decode() + "\n")


class Signature:
    def __init__(self, alg, keyid, sig, trusted_comment, global_sig):
        self.alg, self.keyid, self.sig = alg, keyid, sig
        self.trusted_comment, self.global_sig = trusted_comment, global_sig

    @property
    def key_hex(self):
        return "%016X" % int.from_bytes(self.keyid, "little")

    @classmethod
    def loads(cls, text, origin="signature"):
        lines = text.splitlines()
        if len(lines) < 4 or not lines[0].startswith("untrusted comment:") or \
                not lines[2].startswith("trusted comment: "):
            raise SignError(f"{origin}: not a minisign signature (4 lines expected)")
        try:
            raw = base64.b64decode(lines[1].strip(), validate=True)
            gsig = base64.b64decode(lines[3].strip(), validate=True)
        except Exception:
            raise SignError(f"{origin}: bad base64")
        if len(raw) != 74 or raw[:2] not in (b"ED", b"Ed") or len(gsig) != 64:
            raise SignError(f"{origin}: not an Ed25519 minisign signature")
        return cls(raw[:2], raw[2:10], raw[10:], lines[2][len("trusted comment: "):], gsig)


def verify_file(path, sig_text, keys, origin="signature"):
    """The key that signed it, or SignError saying exactly why not."""
    sig = Signature.loads(sig_text, origin)
    key = next((k for k in keys if k.keyid == sig.keyid), None)
    if key is None:
        raise SignError(f"{origin}: signed by key {sig.key_hex}, which is not trusted here "
                        f"(trusted: {', '.join(k.id_hex for k in keys) or 'none'})")
    if sig.alg == b"ED":
        msg = blake2b_file(path)
    else:
        with open(path, "rb") as f:
            msg = f.read()
    if not _ed_verify(key.pk, msg, sig.sig):
        raise SignError(f"{origin}: the file does not match its signature (changed after signing?)")
    if not _ed_verify(key.pk, sig.sig + sig.trusted_comment.encode(), sig.global_sig):
        raise SignError(f"{origin}: the trusted comment was altered")
    return key, sig


def write_secret(path, key):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key.dumps())
