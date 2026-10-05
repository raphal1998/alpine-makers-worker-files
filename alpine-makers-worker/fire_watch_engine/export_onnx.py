"""Conversion locale d'un checkpoint YOLOv8 (.pt) en ONNX pour la surveillance IA des graveuses.

Exécuté UNIQUEMENT dans l'environnement d'export jetable du composant « fire-watch »
(torch CPU + ultralytics épinglés), jamais dans l'agent ni dans le moteur d'analyse :

    <export-venv python> -m fire_watch_engine.export_onnx --weights best.pt --output model.onnx --expect-sha256 <hex>

Licence : les poids convertis restent sous la licence de leur auteur (AGPL-3.0 pour les deux
modèles du catalogue) ; la conversion est une modification au sens de la licence et elle est
consignée dans le fichier de provenance écrit à côté du modèle. Le code Ultralytics (AGPL-3.0)
ne sert qu'ici ; l'analyse en service n'utilise que ONNX Runtime (MIT).

Sécurité : un checkpoint .pt est un pickle (exécution de code possible au chargement). Il n'est
chargé qu'après vérification de son empreinte SHA-256 épinglée dans le catalogue.
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

OPSET = 17        # ≤ 18 : Resize/ReduceMax restent sur le GPU avec le fournisseur CUDA ; ≤ 20 pour DirectML
IMGSZ = 640


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def export(weights, output, expected_sha256, threads=2):
    weights, output = Path(weights).resolve(), Path(output).resolve()
    actual = sha256_file(weights)
    if expected_sha256 and actual != expected_sha256.lower():
        raise SystemExit(f"Empreinte SHA-256 inattendue pour {weights.name} : {actual} (attendu {expected_sha256}).")
    # Jamais d'installation automatique ni de téléchargement implicite par ultralytics.
    os.environ["YOLO_AUTOINSTALL"] = "False"
    os.environ["YOLO_OFFLINE"] = "True"
    os.environ.setdefault("OMP_NUM_THREADS", str(threads))
    import torch  # noqa: E402  (importé après la configuration de l'environnement)
    torch.set_num_threads(max(1, int(threads)))
    import ultralytics
    from ultralytics import YOLO
    started = time.monotonic()
    model = YOLO(str(weights), task="detect")
    names = {int(key): str(value) for key, value in dict(model.names).items()}
    produced = Path(model.export(format="onnx", imgsz=IMGSZ, opset=OPSET, simplify=True, dynamic=False, batch=1,
                                 device="cpu", half=False, nms=False))
    output.parent.mkdir(parents=True, exist_ok=True)
    if produced.resolve() != output:
        os.replace(produced, output)
    report = {"weights_sha256": actual, "onnx_sha256": sha256_file(output), "onnx_bytes": output.stat().st_size,
              "names": names, "imgsz": IMGSZ, "opset": OPSET, "simplify": True,
              "ultralytics": ultralytics.__version__, "torch": torch.__version__,
              "seconds": round(time.monotonic() - started, 2)}
    try:
        import onnx
        report["onnx"] = onnx.__version__
    except ImportError:
        pass
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Export ONNX local d'un modèle flamme/fumée YOLOv8.")
    parser.add_argument("--weights", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expect-sha256", default="")
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args(argv)
    report = export(args.weights, args.output, args.expect_sha256, args.threads)
    print("FIRE_WATCH_EXPORT=" + json.dumps(report, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
