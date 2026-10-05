"""Durable execution receipts. Never infer that a lost response means no work."""
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


TERMINAL = {"completed", "failed", "cancelled"}
# Codes « connexion refusée » : Windows (10061) et POSIX (111).
_REFUSED_ERRNOS = {10061, 111}


def engine_unreachable(error):
    """Vrai quand le moteur local refuse la connexion : aucun processus n'écoute.

    Sur 127.0.0.1, un refus signifie que le moteur n'est pas lancé, donc que rien
    ne calcule pour ce job. C'est la seule erreur réseau qui autorise un état
    terminal sûr ; un délai dépassé, lui, laisse le doute et reste « incertain ».
    """
    seen = set()
    while isinstance(error, BaseException) and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, ConnectionRefusedError) or getattr(error, "errno", None) in _REFUSED_ERRNOS \
                or getattr(error, "winerror", None) in _REFUSED_ERRNOS:
            return True
        # URLError enveloppe l'erreur socket dans .reason ; HTTPError y met un texte.
        reason = getattr(error, "reason", None)
        error = reason if isinstance(reason, BaseException) else (error.__cause__ or error.__context__)
    return False


def engine_unreachable_message(engine, port):
    return (f"Moteur {engine} injoignable sur ce Worker (connexion refusée sur 127.0.0.1:{port}) : il ne tourne pas, "
            "aucun calcul n’est en cours pour ce job. Démarre-le dans Mes Workers, puis relance.")


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path, default=None):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    # A corrupt receipt must stop recovery, never trigger a new submission.
    if not isinstance(value, dict):
        raise ValueError("Journal d’exécution Worker invalide ; aucune relance automatique.")
    return value


class DurableJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS records(kind TEXT NOT NULL,id TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(kind,id))")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def get(self, kind, key):
        with self.lock, self.connect() as db:
            row = db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
            return json.loads(row[0]) if row else None

    def records(self, kind):
        with self.lock, self.connect() as db:
            return {key: json.loads(value) for key, value in db.execute("SELECT id,data FROM records WHERE kind=?", (kind,))}

    def update(self, kind, key, **fields):
        with self.lock, self.connect() as db:
            row = db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
            value = json.loads(row[0]) if row else {}
            value.update(fields, updated_at=time.time())
            db.execute("INSERT OR REPLACE INTO records(kind,id,data) VALUES(?,?,?)", (kind, key, json.dumps(value, ensure_ascii=False)))
            return value


class RunnerState:
    """One runner writes its state; the agent writes only the cancel marker."""
    def __init__(self, workdir, job):
        self.workdir = Path(workdir).resolve()
        self.path = self.workdir / ".runner-state.json"
        self.cancel_path = self.workdir / ".cancel.json"
        self.value = read_json(self.path)
        identity = str(job.get("execution_id") or "")
        if identity and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
            raise ValueError("Identité d’exécution Worker invalide.")
        if self.value is None:
            self.value = {"execution_id": identity or uuid.uuid4().hex, "job_id": str(job.get("job_id") or ""),
                          "state": "preparing", "engine_stop_confirmed": True, "attempts": 0}
            self.update()
        elif identity and self.value.get("execution_id") != identity:
            raise ValueError("Le journal appartient à une autre exécution.")

    def update(self, **fields):
        self.value.update(fields, updated_at=time.time())
        atomic_json(self.path, self.value)
        return self.value

    def cancellation_requested(self):
        marker = read_json(self.cancel_path, {})
        return marker.get("execution_id") == self.value["execution_id"]

    def submit_intent(self, **fields):
        if self.value["state"] != "preparing":
            raise ValueError("Une exécution déjà soumise ou incertaine ne peut pas être relancée.")
        return self.update(state="submitting", engine_stop_confirmed=False,
                           attempts=self.value.get("attempts", 0) + 1, **fields)

    def finish(self, state, result=None, error=None):
        if state not in TERMINAL:
            raise ValueError("État terminal invalide.")
        return self.update(state=state, engine_stop_confirmed=True, result=result or {}, error=error or "")

    def uncertain(self, error):
        return self.update(state="cancel_requested" if self.cancellation_requested() else "uncertain",
                           engine_stop_confirmed=False, error=str(error))


class RunnerLease:
    """OS-owned lock survives an agent crash, and vanishes with the runner."""
    def __init__(self, workdir):
        self.path = Path(workdir) / ".runner.lock"
        self.handle = None

    def acquire(self):
        try:
            self.handle = self.path.open("a+b")
            # Reading another runner's locked byte raises PermissionError on
            # Windows; use metadata rather than reading through its lock.
            if os.fstat(self.handle.fileno()).st_size == 0:
                self.handle.write(b"1")
                self.handle.flush()
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if self.handle is not None:
                self.handle.close()
            self.handle = None
            return False

    def close(self):
        if self.handle is not None:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("Un runner possède déjà cette exécution ; aucune répétition.")
        return self

    def __exit__(self, *_args):
        self.close()


def runner_active(workdir):
    lease = RunnerLease(workdir)
    acquired = lease.acquire()
    lease.close()
    return not acquired
