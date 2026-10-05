"""Install only the identity dependency in the Worker's own runtime, not globally."""
import subprocess
import sys
from pathlib import Path


def prepare():
    try:
        from .identity import crypto
    except ImportError:
        from identity import crypto
    try:
        crypto()
        return
    except ImportError:
        pass
    target = Path(__file__).resolve().parent / "runtime" / "identity-deps"
    for entry in (target.parent, target):
        if entry.is_symlink() or (entry.exists() and getattr(entry.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("Identity dependencies must remain inside the Worker")
    target.mkdir(parents=True, exist_ok=True)
    try:
        from .storage_paths import WorkerStorage
    except ImportError:
        from storage_paths import WorkerStorage
    environment = WorkerStorage(Path(__file__).resolve().parent).environment()
    result = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                             "--only-binary=:all:", "--target", str(target), "cryptography==50.0.1"],
                            timeout=180, env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError("Installation de la sécurité Worker impossible ; réessaie l'installation.")
    crypto()


if __name__ == "__main__":
    prepare()
