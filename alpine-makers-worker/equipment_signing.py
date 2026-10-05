"""Existing Bambu signing wire format, with trust state isolated per device.

Application identities stay on the Worker. Never import another user's key
from the dashboard or silently invent a manufacturer certificate.
"""
import base64
import json
import os
import threading
import time
from pathlib import Path


class DeviceSigner:
    def __init__(self, root):
        self.root = Path(root)
        self.material = None
        self.device_key = None
        self.ready = threading.Event()
        self.sequence = None

    def load(self):
        # Retain the historical PC-local identity if it already exists. All
        # other PCs can provide their own identity in their managed data root.
        candidates = [self.root / "data" / "bambu_security"]
        configured = os.getenv("BAMBU_SECURITY_DIR", "").strip()
        if configured:
            candidates.append(Path(configured))
        if os.getenv("LOCALAPPDATA"):
            candidates.append(Path(os.environ["LOCALAPPDATA"]) / "AlpineMakersDashboard" / "bambu_security")
        folder = next((p for p in candidates if all((p/name).is_file() for name in ("slicer_cert.pem", "slicer_key.pem", "slicer_crl.pem"))), None)
        if folder is None:
            return
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        cert = (folder / "slicer_cert.pem").read_text(encoding="ascii")
        leaf = x509.load_pem_x509_certificate(cert.encode("ascii"))
        self.material = {"cert_pem": cert, "crl_pem": (folder / "slicer_crl.pem").read_text(encoding="ascii"),
                         "private_key": serialization.load_pem_private_key((folder / "slicer_key.pem").read_bytes(), password=None),
                         "cert_id": f"{leaf.serial_number:x}{leaf.issuer.rfc4514_string()}"}

    def reset(self):
        self.ready.clear()
        self.device_key = None

    def accept(self, payload):
        value = payload.get("security", {})
        if not isinstance(value, dict) or value.get("command") != "app_cert_install" or value.get("sequence_id") != self.sequence:
            return
        if str(value.get("result", "")).upper() != "SUCCESS":
            self.reset()
            return
        from cryptography import x509
        cert = str(value.get("printer_cert") or "")
        if cert:
            self.device_key = x509.load_pem_x509_certificate(cert.encode("ascii")).public_key()
        self.ready.set()

    def prepare(self, client, serial):
        if not self.material or self.ready.is_set():
            return
        self.sequence = str(time.time_ns() % 2147483647)
        payload = {"security": {"sequence_id": self.sequence, "command": "app_cert_install", "app_cert": self.material["cert_pem"], "crl": self.material["crl_pem"]}}
        result = client.publish(f"device/{serial}/request", json.dumps(payload, ensure_ascii=False, separators=(",", ":")), qos=0)
        if result.rc != 0 or not self.ready.wait(4):
            raise ValueError("L’imprimante n’a pas confirmé l’autorisation de l’application locale.")

    def encode(self, wire):
        if not self.material or not isinstance(wire.get("print"), dict):
            return json.dumps(wire, ensure_ascii=False, separators=(",", ":"))
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        payload = dict(wire["print"])
        if self.device_key:
            for field in ("url", "param"):
                value = payload.get(field)
                if not isinstance(value, str) or field+"_enc" in payload:
                    continue
                raw = value.encode("utf-8")
                length = (self.device_key.key_size+7)//8-11
                encoded = b"".join(self.device_key.encrypt(raw[index:index+length], padding.PKCS1v15()) for index in range(0, len(raw), length))
                payload[field+"_enc"] = base64.b64encode(encoded).decode("ascii")
                payload.pop(field)
        print_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        signed = ('{"print":'+print_json+'}').encode("utf-8")
        signature = self.material["private_key"].sign(signed, padding.PKCS1v15(), hashes.SHA256())
        header = {"cert_id": self.material["cert_id"], "payload_len": len(signed), "sign_alg": "RSA_SHA256", "sign_string": base64.b64encode(signature).decode("ascii"), "sign_ver": "v1.0"}
        return '{"header":'+json.dumps(header, ensure_ascii=True, separators=(",", ":"))+',"print":'+print_json+'}'
