"""Measured transfer progress; never manufacture speed or ETA for opaque subprocesses."""
import json
import threading
import time

_lock = threading.RLock()


def progress(value, message, **metrics):
    with _lock:
        print("ALPINE_PROGRESS=" + json.dumps({"progress": max(0, min(99, float(value))), "message": str(message), **metrics}, ensure_ascii=False), flush=True)


class TransferProgress:
    def __init__(self, message, initial=0, start=5, span=78):
        self.message, self.initial, self.start, self.span = message, initial, start, span
        self.started = time.monotonic()
        self.last = 0

    def update(self, done, total=0, force=False):
        now = time.monotonic()
        if not force and now - self.last < 1:
            return
        self.last = now
        elapsed = now - self.started
        speed = max(0, done - self.initial) / elapsed if elapsed > 0 else 0
        metrics = {"phase": "download", "bytes_done": done, "speed_bps": speed}
        if total > 0:
            metrics["bytes_total"] = total
            if speed > 0:
                metrics["eta_seconds"] = max(0, total - done) / speed
        progress(self.start + self.span * min(1, done / total) if total else self.start, self.message, **metrics)


def huggingface_progress_class():
    # Current Hub supports aggregate byte bars; older Hub versions emit file counts.
    # Keep both compatible, without treating a file count as downloaded bytes.
    from tqdm.auto import tqdm

    class HubProgress(tqdm):
        def __init__(self, *args, **kwargs):
            kwargs.pop("name", None)
            self.transfer = TransferProgress(str(kwargs.get("desc") or "Téléchargement Hugging Face"), initial=kwargs.get("initial", 0), start=15, span=75)
            kwargs["disable"] = False
            super().__init__(*args, **kwargs)

        def display(self, *args, **kwargs):
            if not hasattr(self, "n"):
                return
            if self.unit == "B":
                self.transfer.update(self.n, self.total or 0)
            else:
                now = time.monotonic()
                if now - self.transfer.last >= 1:
                    self.transfer.last = now
                    progress(15 + 75 * self.n / self.total if self.total else 15,
                             f"Hugging Face : {self.n}/{self.total or '?'} fichiers ; débit non fourni par cette version.", phase="download")

    return HubProgress
