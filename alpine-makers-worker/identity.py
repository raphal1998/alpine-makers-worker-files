"""Worker-owned Ed25519 identity; no IP binding and no hardware attestation claim."""
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import time
import urllib.parse


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def crypto():
    vendor = Path(__file__).resolve().parent / "runtime" / "identity-deps"
    if vendor.is_dir() and str(vendor) not in sys.path:
        sys.path.insert(0, str(vendor))
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
    from cryptography.hazmat.primitives import serialization
    return Ed25519PrivateKey, Ed25519PublicKey, serialization


def verify(public, message, signature):
    _, Public, _ = crypto()
    key = base64.b64decode(public, validate=True)
    sig = base64.b64decode(signature, validate=True)
    if len(key) != 32 or len(sig) != 64:
        raise ValueError("Invalid identity proof")
    Public.from_public_bytes(key).verify(sig, canonical(message))


def fingerprint(public):
    data = base64.b64decode(public, validate=True)
    if len(data) != 32:
        raise ValueError("Invalid public key")
    return hashlib.sha256(data).hexdigest()


def dpapi(data, decrypt=False):
    import ctypes
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise OSError(ctypes.get_last_error(), "Worker identity unavailable for this Windows account")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


class Identity:
    def __init__(self, root, create=False):
        self.root = Path(root).resolve()
        directory = self.root / "config" / "identity"
        path = directory / "private.key"
        # Do not follow a substituted identity directory/file.
        for entry in (self.root / "config", directory, path):
            if entry.is_symlink() or (entry.exists() and getattr(entry.lstat(), "st_file_attributes", 0) & 0x400):
                raise ValueError("Identity paths must not be links")
        Private, _, serialization = crypto()
        if not path.exists():
            if not create:
                raise FileNotFoundError("Worker identity not initialized")
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            private = Private.generate().private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
            protected = b"DPAPI1:" + dpapi(private) if os.name == "nt" else b"POSIX1:" + private
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(protected)
                output.flush()
                os.fsync(output.fileno())
        protected = path.read_bytes()
        if os.name == "nt":
            if not protected.startswith(b"DPAPI1:"):
                raise ValueError("Windows identity must use DPAPI")
            private = dpapi(protected[7:], decrypt=True)
        else:
            if not protected.startswith(b"POSIX1:") or path.stat().st_mode & 0o077:
                raise ValueError("Identity requires service-account-only permissions")
            private = protected[7:]
        self.key = Private.from_private_bytes(private)
        self.public = base64.b64encode(self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode("ascii")
        self.fingerprint = fingerprint(self.public)

    def sign(self, value):
        return base64.b64encode(self.key.sign(canonical(value))).decode("ascii")

    def request_headers(self, request, worker_id):
        data = request.data
        digest = hashlib.sha256()
        if data is not None:
            if isinstance(data, bytes):
                digest.update(data)
            else:
                position = data.tell()
                try:
                    for chunk in iter(lambda: data.read(1024 * 1024), b""):
                        digest.update(chunk)
                finally:
                    data.seek(position)
        parsed = urllib.parse.urlsplit(request.full_url)
        proof = {"v": 1, "worker_id": worker_id, "method": request.get_method(),
                 "audience": getattr(self, "audience", ""),
                 "epoch": getattr(self, "epoch", ""),
                 "path": urllib.parse.unquote(parsed.path) + ("?" + parsed.query if parsed.query else ""),
                 "time": int(time.time()), "nonce": secrets.token_hex(24), "sha256": digest.hexdigest()}
        return {"X-Worker-Proof": base64.b64encode(canonical(proof)).decode("ascii"),
                "X-Worker-Signature": self.sign(proof)}


_identities = {}


def configure(root, config):
    if config.get("identity_enabled"):
        identity = Identity(root)
        identity.audience = config.get("identity_server_id", "")
        identity.epoch = config.get("identity_epoch", "")
        _identities[(str(config.get("server_url", "")).rstrip("/"), str(config.get("worker_id", "")))] = identity


def sign_request(request):
    worker_id = request.get_header("X-worker-id", "")
    for (server, registered_id), identity in list(_identities.items()):
        if worker_id == registered_id and request.full_url.startswith(server + "/api/worker-protocol/"):
            for name, value in identity.request_headers(request, worker_id).items():
                request.add_unredirected_header(name, value)
            break
    return request
