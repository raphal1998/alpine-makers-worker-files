"""Capture d'UNE caméra USB dans son propre processus (agent 1.50.3).

Lancé par l'agent pour chaque fiche « Caméra USB » sous Windows : deux captures DirectShow dans un même processus se
font tomber l'une l'autre avec certains pilotes (atelier, 11/10/2026 : deux caméras qui marchent chacune seule,
jamais ensemble), alors que deux processus coexistent sans problème. Ici, rien du réseau : les images JPEG partent
sur la sortie standard, l'agent les relaie au site comme avant.

Protocole (sortie standard, binaire) :
    STATUS <json>\\n            état : {"opened": bool, "backend": "...", "error": "..."}
    FRAME <octets> <qualite>\\n  puis les octets du JPEG
Le processus s'arrête quand l'agent le tue, ou de lui-même (code 2) quand la caméra ne livre plus d'images.
"""
import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

MISSES_MAX = 30


def watch_parent(pid):
    """Agent 1.50.9 : l'agent tué net (mise à jour, redémarrage) laissait ce processus orphelin, caméra en main, s'il
    n'avait aucune image à écrire (le tube fermé ne se remarque qu'à l'écriture). Un fil attend la fin du processus
    parent et sort aussitôt."""
    def wait_then_exit():
        try:
            if os.name == "nt":
                import ctypes
                handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, int(pid))   # SYNCHRONIZE
                if handle:
                    ctypes.windll.kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
            else:
                while os.getppid() == pid:
                    time.sleep(1)
        except Exception:
            return
        os._exit(0)
    threading.Thread(target=wait_then_exit, name="parent-watch", daemon=True).start()


def emit_status(**facts):
    sys.stdout.buffer.write(b"STATUS " + json.dumps(facts, ensure_ascii=True).encode("ascii") + b"\n")
    sys.stdout.buffer.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--options", required=True, help="JSON de usb_camera_settings")
    parser.add_argument("--parent", type=int, default=0, help="PID de l'agent : ce processus s'arrête avec lui")
    args = parser.parse_args()
    if args.parent:
        watch_parent(args.parent)
    root = Path(args.root)
    sys.path.insert(0, str(root))
    import equipment_runtime  # noqa: E402  (même logique d'ouverture et d'encodage que la capture en processus)
    target = equipment_runtime.runtime_dependencies(root)
    if str(target) not in sys.path:
        sys.path.insert(0, str(target))
    options = json.loads(args.options)
    backend = str(options.get("backend") or "auto")
    # Agent 1.50.4 : Media Foundation par Windows (usb_camera_mf.py) pour « msmf », et en premier en automatique ;
    # OpenCV (DirectShow) reste le repli de l'automatique et le choix « dshow ».
    mf_failure = ""
    if backend in ("auto", "msmf") and os.name == "nt":
        import usb_camera_mf
        usb_camera_mf.QUIET = backend == "auto"
        try:
            code = usb_camera_mf.run(options)
        except Exception as error:  # noqa: BLE001
            if backend == "msmf":
                emit_status(opened=False, error=f"Media Foundation : {type(error).__name__}: {error}"[:300])
            code = 2
        if backend == "msmf" or code == 0:
            return code
        # Automatique : Media Foundation n'a pas abouti, DirectShow prend le relais ; la raison est gardée pour la fiche.
        mf_failure = str(usb_camera_mf.LAST_FAILURE.get("error") or "")
    try:
        cv2 = equipment_runtime.usb_capture_module()
    except ImportError as error:
        emit_status(opened=False, error=equipment_runtime.usb_dependency_error(error), dependency=True)
        return 3
    capture = None
    try:
        capture = equipment_runtime.usb_open_capture(cv2, options)
        if not capture.isOpened():
            seen = ""
            if options.get("backend", "auto") == "auto":
                try:
                    seen = equipment_runtime.usb_enumerate(cv2)
                except Exception:
                    seen = ""
            emit_status(opened=False, error="Caméra USB introuvable ou déjà utilisée par un autre programme"
                        + (f" ; Media Foundation : {mf_failure}" if mf_failure else ""), seen=seen)
            return 2
        emit_status(opened=True, backend=options.get("backend_used", ""))
        interval, misses, quality = 1.0 / options["fps"], 0, 80
        while True:
            started = time.monotonic()
            ok, image = capture.read()
            if not ok or image is None:
                misses += 1
                if misses >= MISSES_MAX:
                    emit_status(opened=True, error="La caméra USB ne fournit plus d’images")
                    return 2
                time.sleep(0.2)
                continue
            misses = 0
            frame, quality = equipment_runtime.usb_encode_frame(cv2, image, quality)
            if frame is not None:
                sys.stdout.buffer.write(b"FRAME %d %d\n" % (len(frame), quality))
                sys.stdout.buffer.write(frame)
                sys.stdout.buffer.flush()
            rest = interval - (time.monotonic() - started)
            if rest > 0:
                time.sleep(rest)
    except BrokenPipeError:
        return 0   # l'agent a fermé le tube : arrêt demandé
    except Exception as error:
        try:
            emit_status(opened=capture is not None and capture.isOpened(), error=f"{type(error).__name__}: {error}"[:300])
        except Exception:
            pass
        return 2
    finally:
        if capture is not None:
            try:
                capture.release()
            except Exception:
                pass


if __name__ == "__main__":
    if os.name == "nt":
        import msvcrt
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    raise SystemExit(main())
