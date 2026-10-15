# -*- coding: utf-8 -*-
"""Copie de travail d'une image source du Labo image d'Alpine Makers Desktop (agent, outil « image-lab »).

Lancé par ``desktop_image_lab.py`` avec le Python de l'environnement du moteur image (``<ComfyUI>/.venv``, options
``-I -B``), comme les scripts du Labo (``runners/image_maps_script.py``) : Pillow est là, pas dans le Python de l'agent.
Mêmes règles que la validation du site (``image_lab.normalize_source``) : format établi par la signature puis confirmé
par le décodage, dimensions vérifiées avant décodage (16 mégapixels, 8192 px de côté, 16 px au moins), orientation
appliquée, EXIF / GPS / profils retirés, réenregistrement en PNG (JPEG de haute qualité au-delà de 24 Mo sans
transparence). En plus : « grayscale » (toutes les couches égales), pour refuser « Normales » sur une photo en couleurs
avant tout calcul, comme le site après « Analyser ».

Usage : ``python -I -B desktop_image_lab_normalize.py <demande.json>`` ; la demande nomme ``input``, ``output``,
``report`` et ``max_bytes``. Le compte rendu (JSON) porte ``ok`` et ``properties``, ou ``code`` : jamais de chemin.
"""
from __future__ import annotations

import io
import json
import math
import sys
import warnings
from pathlib import Path

MEBIBYTE = 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_SIDE = 8192
MIN_SIDE = 16
WORK_COPY_PNG_MAX_BYTES = 24 * MEBIBYTE
GRAYSCALE_TOLERANCE = 8
FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


class Refused(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def signature_format(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "WEBP"
    return ""


def ratio(width, height):
    divisor = math.gcd(width, height) or 1
    a, b = width // divisor, height // divisor
    if max(a, b) <= 40:
        return f"{a}:{b}"
    for known_a, known_b in ((1, 1), (5, 4), (4, 3), (3, 2), (16, 10), (16, 9), (2, 1), (21, 9)):
        for x, y in ((known_a, known_b), (known_b, known_a)):
            if abs(width / height - x / y) < 0.012:
                return f"≈ {x}:{y}"
    return f"{width / height:.2f}:1".replace(".", ",")


def normalize(data, max_bytes):
    from PIL import Image, ImageChops, ImageOps, UnidentifiedImageError

    if not data:
        raise Refused("SOURCE_EMPTY")
    if len(data) > max_bytes:
        raise Refused("SOURCE_TOO_LARGE")
    signature = signature_format(data)
    if not signature:
        raise Refused("SOURCE_FORMAT")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(data))
            width, height = image.size
            if width < 1 or height < 1 or width > MAX_SIDE or height > MAX_SIDE or width * height > MAX_PIXELS:
                image.close()
                raise Refused("SOURCE_TOO_MANY_PIXELS")
            with image:
                if image.format != signature:
                    raise Refused("SOURCE_FORMAT")
                frames = int(getattr(image, "n_frames", 1) or 1)
                had_metadata = bool(image.getexif()) or any(key in image.info for key in ("exif", "icc_profile", "xmp", "XML:com.adobe.xmp", "comment"))
                original_mode = image.mode
                image.seek(0)
                image.load()
                oriented = ImageOps.exif_transpose(image)
                has_alpha = oriented.mode in ("RGBA", "LA", "PA") or (oriented.mode == "P" and "transparency" in oriented.info)
                pixels = oriented.convert("RGBA" if has_alpha else "RGB")
    except Refused:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, MemoryError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise Refused("SOURCE_UNREADABLE") from None
    width, height = pixels.size
    if min(width, height) < MIN_SIDE:
        raise Refused("SOURCE_TOO_SMALL")
    alpha_used = False
    if has_alpha:
        low, _high = pixels.getchannel("A").getextrema()
        alpha_used = low < 255
        if not alpha_used:
            pixels = pixels.convert("RGB")
    red, green, blue = pixels.getchannel("R"), pixels.getchannel("G"), pixels.getchannel("B")
    # Même tolérance que le module de cartes (runners/image_maps_script.py, GRAYSCALE_TOLERANCE) : une profondeur
    # passée par un JPEG reste reconnue.
    grayscale = max(ImageChops.difference(red, green).getextrema()[1], ImageChops.difference(green, blue).getextrema()[1]) <= GRAYSCALE_TOLERANCE
    output = io.BytesIO()
    pixels.save(output, format="PNG", compress_level=3)
    mime = "image/png"
    if output.tell() > WORK_COPY_PNG_MAX_BYTES:
        if alpha_used:
            raise Refused("ALPHA_TOO_LARGE")
        output = io.BytesIO()
        pixels.save(output, format="JPEG", quality=95, subsampling=0)
        mime = "image/jpeg"
    properties = {
        "format": signature, "mime_type": FORMATS[signature], "bytes": len(data),
        "width": width, "height": height, "megapixels": round(width * height / 1_000_000, 2),
        "ratio": ratio(width, height), "orientation": "paysage" if width > height else "portrait" if height > width else "carré",
        "mode": original_mode, "has_alpha": bool(alpha_used), "grayscale": bool(grayscale), "animated": frames > 1, "frames": frames,
        "metadata_removed": had_metadata, "work_mime": mime,
    }
    return output.getvalue(), properties


def main(argv):
    request = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    report = Path(request["report"])
    try:
        try:
            import PIL  # noqa: F401
        except ImportError:
            raise Refused("PILLOW_MISSING") from None
        work, properties = normalize(Path(request["input"]).read_bytes(), int(request["max_bytes"]))
        Path(request["output"]).write_bytes(work)
        value = {"ok": True, "properties": properties}
    except Refused as refused:
        value = {"ok": False, "code": refused.code}
    except MemoryError:
        value = {"ok": False, "code": "OUT_OF_MEMORY"}
    report.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
