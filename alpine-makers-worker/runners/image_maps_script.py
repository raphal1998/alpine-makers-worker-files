# -*- coding: utf-8 -*-
"""Labo image — module « Outils de cartes du Labo » (agent 1.46.0), script autonome lancé par l'agent.

Lancé par ``runners/image_maps.py`` avec le Python de l'environnement du moteur image (ComfyUI), en sous-processus :
``python -I -B image_maps_script.py <request.json>``. Ce n'est pas un nœud de ComfyUI et rien n'est installé : le script
n'importe que Pillow (déjà présent dans cet environnement) et la bibliothèque standard. Aucun modèle, aucun fichier de
poids : chaque carte est un calcul d'image déterministe.

Huit calculs, repris à l'identique de l'ancien calcul du site (``image_lab.py`` jusqu'à l'agent 1.45.0) — mêmes réglages,
mêmes bornes, mêmes sorties :

* ``properties`` : dimensions, rapport, orientation, transparence, niveaux de gris, couleur moyenne, empreinte visuelle
  (dHash 64 bits) et couleurs dominantes ;
* ``control_normal`` (normales depuis une profondeur), ``control_lineart`` (trait), ``control_scribble`` (gribouillage),
  ``control_tile`` (tuile / flou), ``control_color`` (grille de couleurs), ``control_gray`` (niveaux de gris) et
  ``mask_alpha`` (masque depuis un détourage) : une image PNG aux dimensions de la source.

Sécurité : aucun accès réseau (sockets coupées), demande revalidée champ par champ (clés exactes, type de calcul fermé,
réglages bornés, chemins dans le dossier du job), image bornée avant décodage (16 mégapixels, 8192 px de côté).

Demande (JSON) : workdir, input, output, report (chemins absolus, les trois derniers dans workdir), tool, settings.
Compte rendu (JSON, fichier « report ») : {"ok": true, "tool", "width", "height", "mode", "seconds"} (+ « properties »
pour ``properties``) ou {"ok": false, "code", "error"}.
"""
from __future__ import annotations

import io
import json
import math
import os
import sys
import time
import warnings
from pathlib import Path

MAX_PIXELS = 16_000_000       # bornes du Labo image (image_lab.SOURCE_MAX_PIXELS, SOURCE_MAX_SIDE)
MAX_SIDE = 8192
RESULT_MAX_BYTES = 32 * 1024 * 1024
INPUT_MAX_BYTES = 64 * 1024 * 1024
# Les réglages en pixels des traits sont donnés pour une image de 1024 px de côté, puis adaptés à la taille réelle.
REFERENCE_SIDE = 1024
GRAYSCALE_TOLERANCE = 8
REQUEST_KEYS = frozenset({"workdir", "input", "output", "report", "tool", "settings"})
# Réglages admis par calcul : (genre, bornes). « number » : nombre fini borné ; « integer » : entier borné ;
# « choice » : une valeur de la liste, type compris ; « boolean » : vrai ou faux. Même table que
# worker_agent/safety.py (IMAGE_LAB_MAPS_SETTINGS) et que les réglages du site (image_lab.TOOLS) : un test les compare.
SETTINGS = {
    "properties": {},
    "control_normal": {"relief": ("number", 0.1, 10), "resolution": ("choice", (256, 512, 1024)), "background": ("number", 0, 0.5)},
    "control_lineart": {"detail": ("number", 0.5, 4), "intensity": ("integer", 1, 40), "cleanup": ("number", 0, 0.5),
                        "polarity": ("choice", ("white_on_black", "black_on_white"))},
    "control_scribble": {"simplify": ("number", 1, 8), "thickness": ("integer", 1, 24), "threshold": ("number", 0.02, 0.5)},
    "control_tile": {"factor": ("choice", (2, 4, 8)), "blur": ("number", 0, 8)},
    "control_color": {"block": ("integer", 4, 256)},
    "control_gray": {"autocontrast": ("boolean",)},
    "mask_alpha": {"white": ("choice", ("background", "subject")), "edge": ("choice", ("soft", "hard")), "grow": ("integer", 0, 64)},
}
DEPTH_MESSAGE = "Cette image n’est pas une carte de profondeur : lance d’abord « Profondeur », puis « Normales » sur son résultat."
ALPHA_MESSAGE = "Cette image n’a pas de transparence : lance d’abord « Détourer le sujet », puis « Masque » sur son résultat."


class MapsError(Exception):
    """Refus ou échec explicable : code stable lu par le lanceur, message en français sans chemin."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ demande
def _inside(path, folder):
    try:
        return Path(os.path.realpath(path)).is_relative_to(Path(os.path.realpath(folder)))
    except (OSError, ValueError):
        return False


def valid_settings(tool, settings):
    """Réglages d'un calcul : clés exactes, types et bornes ; aucun champ libre."""
    spec = SETTINGS.get(tool) if isinstance(tool, str) else None
    if spec is None:
        raise MapsError("REQUEST_INVALID", "Calcul du Labo inconnu de ce Worker.")
    if not isinstance(settings, dict) or set(settings) != set(spec):
        raise MapsError("REQUEST_INVALID", "Réglages du calcul invalides.")
    for name, rule in spec.items():
        value, kind = settings[name], rule[0]
        if kind == "boolean":
            ok = type(value) is bool
        elif kind == "choice":
            ok = any(type(value) is type(option) and value == option for option in rule[1])
        elif kind == "integer":
            ok = type(value) is int and rule[1] <= value <= rule[2]
        else:
            ok = type(value) in (int, float) and math.isfinite(value) and rule[1] <= value <= rule[2]
        if not ok:
            raise MapsError("REQUEST_INVALID", "Réglage du calcul hors limites.")
    return dict(settings)


def validate_request(raw):
    """Demande du lanceur, revérifiée champ par champ : clés exactes, calcul fermé, chemins dans le dossier du job."""
    if not isinstance(raw, dict) or set(raw) != REQUEST_KEYS:
        raise MapsError("REQUEST_INVALID", "Demande de calcul invalide.")
    settings = valid_settings(raw["tool"], raw["settings"])
    paths = {}
    for key in ("workdir", "input", "output", "report"):
        value = raw[key]
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise MapsError("REQUEST_INVALID", "Chemin de la demande invalide.")
        paths[key] = Path(value)
    for key in ("input", "output", "report"):
        if not _inside(paths[key], paths["workdir"]):
            raise MapsError("REQUEST_INVALID", "Fichier hors du dossier du job.")
    if paths["output"].suffix.lower() != ".png" or paths["report"].suffix.lower() != ".json":
        raise MapsError("REQUEST_INVALID", "Nom de sortie non autorisé.")
    return {**paths, "tool": raw["tool"], "settings": settings}


# ------------------------------------------------------------------ isolement
def block_network():
    """Coupe le réseau du processus : toute tentative lève une erreur."""
    import socket

    def refused(*_args, **_kwargs):
        raise OSError("Accès réseau interdit aux outils de cartes du Labo.")

    socket.socket.connect = refused
    socket.socket.connect_ex = refused
    socket.create_connection = refused
    socket.getaddrinfo = refused


# ------------------------------------------------------------------ image (Pillow seul)
def open_image(path):
    """Image du job décodée, bornée avant décodage, en RGB ou RGBA (comme le site la lisait)."""
    from PIL import Image, UnidentifiedImageError

    try:
        size = path.stat().st_size
    except OSError:
        raise MapsError("IMAGE_INVALID", "Image source absente sur le Worker.") from None
    if size <= 0 or size > INPUT_MAX_BYTES:
        raise MapsError("IMAGE_INVALID", "Image source vide ou trop lourde.")
    data = path.read_bytes()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as opened:
                width, height = opened.size
                if width < 1 or height < 1 or width > MAX_SIDE or height > MAX_SIDE or width * height > MAX_PIXELS:
                    raise MapsError("IMAGE_TOO_LARGE", "Dimensions refusées : au plus 16 mégapixels et 8192 px de côté.")
                opened.load()
                return opened.convert("RGBA" if opened.mode in ("RGBA", "LA", "PA") or "transparency" in opened.info else "RGB")
    except MapsError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise MapsError("IMAGE_INVALID", "Cette image est illisible ou endommagée.") from None


def _ratio(width, height):
    divisor = math.gcd(width, height) or 1
    a, b = width // divisor, height // divisor
    if max(a, b) <= 40:
        return f"{a}:{b}"
    for known_a, known_b in ((1, 1), (5, 4), (4, 3), (3, 2), (16, 10), (16, 9), (2, 1), (21, 9)):
        for x, y in ((known_a, known_b), (known_b, known_a)):
            if abs(width / height - x / y) < 0.012:
                return f"≈ {x}:{y}"
    return f"{width / height:.2f}:1".replace(".", ",")


def _difference_hash(image):
    """Empreinte visuelle 64 bits (dHash) : deux images très proches ont des empreintes voisines."""
    from PIL import Image

    small = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = small.tobytes()
    bits = 0
    for row in range(8):
        for column in range(8):
            bits = (bits << 1) | (1 if pixels[row * 9 + column] > pixels[row * 9 + column + 1] else 0)
    return f"{bits:016x}"


def _palette(image, colors=6):
    """Couleurs dominantes (hex + part en %) ; les pixels transparents ne comptent pas."""
    from PIL import Image

    thumb = image.copy()
    thumb.thumbnail((96, 96), Image.Resampling.BILINEAR)
    raw = thumb.convert("RGBA").tobytes()
    opaque = [raw[offset:offset + 3] for offset in range(0, len(raw), 4) if raw[offset + 3] >= 128]
    if not opaque:
        return []
    strip = Image.frombytes("RGB", (len(opaque), 1), b"".join(opaque))
    quantized = strip.quantize(colors=colors, method=Image.Quantize.MEDIANCUT)
    palette = quantized.getpalette() or []
    counts = sorted(quantized.getcolors() or [], reverse=True)
    result = []
    for count, index in counts:
        red, green, blue = palette[index * 3: index * 3 + 3]
        result.append({"hex": f"#{red:02x}{green:02x}{blue:02x}", "percent": round(100 * count / len(opaque), 1)})
    return result


def _is_grayscale(image):
    """Vrai si les trois canaux sont égaux à une petite tolérance près (carte de profondeur, image en gris)."""
    from PIL import ImageChops

    if image.mode in ("L", "LA", "I", "F", "1"):
        return True
    red, green, blue = image.convert("RGB").split()[:3]
    return max(ImageChops.difference(red, green).getextrema()[1], ImageChops.difference(green, blue).getextrema()[1]) <= GRAYSCALE_TOLERANCE


def image_properties(image):
    """Propriétés et palette de l'image du job (copie de travail du site : orientation appliquée, sans métadonnées)."""
    from PIL import Image

    pixels = image
    alpha_used = False
    if pixels.mode == "RGBA":
        alpha_used = pixels.getchannel("A").getextrema()[0] < 255
        if not alpha_used:
            pixels = pixels.convert("RGB")
    width, height = pixels.size
    average = pixels.convert("RGB").resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
    return {
        "width": width, "height": height, "megapixels": round(width * height / 1_000_000, 2),
        "ratio": _ratio(width, height), "orientation": "paysage" if width > height else "portrait" if height > width else "carré",
        "has_alpha": bool(alpha_used), "grayscale": _is_grayscale(pixels),
        "average_color": "#{:02x}{:02x}{:02x}".format(*average[:3]), "dhash": _difference_hash(pixels), "palette": _palette(pixels),
    }


# ------------------------------------------------------------------ cartes (Pillow seul)
def _flatten(image):
    """RGB ; une image transparente est posée sur du blanc (ses pixels cachés ne créent ni trait ni couleur)."""
    from PIL import Image

    if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel("A"))
        return flat
    return image.convert("RGB")


def _scale(image):
    return max(1.0, max(image.size) / REFERENCE_SIDE)


def _dog(gray, sigma, gain):
    """Différence de gaussiennes : clair là où un trait sombre se détache d'un fond plus clair."""
    from PIL import ImageChops, ImageFilter

    fine = gray.filter(ImageFilter.GaussianBlur(sigma))
    coarse = gray.filter(ImageFilter.GaussianBlur(sigma * 1.6))
    return ImageChops.subtract(coarse, fine, scale=1.0 / gain)


def _binary(image, threshold):
    return image.point([255 if value >= threshold else 0 for value in range(256)])


def _grow(mask, radius):
    """Dilatation du blanc d'environ ``radius`` px : flou en boîte puis tout pixel touché redevient blanc."""
    from PIL import ImageChops, ImageFilter

    if radius <= 0:
        return mask
    return ImageChops.lighter(mask, _binary(mask.filter(ImageFilter.BoxBlur(radius)), 1))


def map_lineart(image, settings):
    from PIL import ImageOps

    lines = _dog(_flatten(image).convert("L"), float(settings["detail"]) * _scale(image), float(settings["intensity"]))
    cut = int(round(float(settings["cleanup"]) * 255))
    lines = lines.point([0 if value <= cut else min(255, round((value - cut) * 255 / (255 - cut))) for value in range(256)])
    return ImageOps.invert(lines) if settings["polarity"] == "black_on_white" else lines


def map_scribble(image, settings):
    scale = _scale(image)
    edges = _binary(_dog(_flatten(image).convert("L"), float(settings["simplify"]) * scale, 8.0), int(round(float(settings["threshold"]) * 255)))
    radius = int(round((int(settings["thickness"]) - 1) * scale / 2))
    return _grow(edges, radius)


def map_tile(image, settings):
    from PIL import Image, ImageFilter

    rgb = _flatten(image)
    factor = int(settings["factor"])
    small = rgb.resize((max(1, rgb.width // factor), max(1, rgb.height // factor)), Image.Resampling.BOX)
    result = small.resize(rgb.size, Image.Resampling.BICUBIC)
    blur = float(settings["blur"])
    return result.filter(ImageFilter.GaussianBlur(blur)) if blur > 0 else result


def map_color(image, settings):
    from PIL import Image

    rgb = _flatten(image)
    block = int(settings["block"])
    small = rgb.resize((max(1, math.ceil(rgb.width / block)), max(1, math.ceil(rgb.height / block))), Image.Resampling.BOX)
    return small.resize(rgb.size, Image.Resampling.NEAREST)


def map_gray(image, settings):
    from PIL import ImageOps

    gray = _flatten(image).convert("L")
    return ImageOps.autocontrast(gray, cutoff=1) if settings["autocontrast"] else gray


def map_normal(image, settings):
    """Carte de normales (convention Normal BAE) tirée d'une profondeur où le clair est proche.

    Pentes mesurées en flottants (différences centrées) sur une copie réduite puis lissée, remises à la taille de
    l'image : R = vers la gauche, G = vers le haut, B = face à l'objectif ; chaque vecteur est normé.
    """
    from PIL import Image, ImageMath

    if not _is_grayscale(_flatten(image)):
        raise MapsError("DEPTH_REQUIRED", DEPTH_MESSAGE)
    depth = _flatten(image).convert("L")
    width, height = depth.size
    ratio = min(1.0, int(settings["resolution"]) / max(width, height))
    size = (max(8, round(width * ratio)), max(8, round(height * ratio)))
    field = depth.convert("F").resize(size, Image.Resampling.BOX)
    # Lissage léger : atténue les marches d'une profondeur en 256 niveaux.
    field = field.resize((max(4, size[0] // 2), max(4, size[1] // 2)), Image.Resampling.BOX).resize(size, Image.Resampling.BICUBIC)
    padded = Image.new("F", (size[0] + 2, size[1] + 2))
    padded.paste(field, (1, 1))
    padded.paste(field.crop((0, 0, 1, size[1])), (0, 1))
    padded.paste(field.crop((size[0] - 1, 0, size[0], size[1])), (size[0] + 1, 1))
    padded.paste(padded.crop((0, 1, size[0] + 2, 2)), (0, 0))
    padded.paste(padded.crop((0, size[1], size[0] + 2, size[1] + 1)), (0, size[1] + 1))
    shifted = {name: padded.crop(box) for name, box in {
        "left": (0, 1, size[0], size[1] + 1), "right": (2, 1, size[0] + 2, size[1] + 1),
        "up": (1, 0, size[0] + 1, size[1]), "down": (1, 2, size[0] + 1, size[1] + 2)}.items()}
    # Pente en « profondeur 0–1 par pixel » × côté long × relief : une rampe 0 → 1 sur toute l'image incline à 45° au relief 1.
    gain = float(settings["relief"]) * max(size) / 255.0 / 2.0
    floor = float(settings["background"]) * 255.0
    gx = ImageMath.lambda_eval(lambda a: (a["right"] - a["left"]) * gain * (a["d"] >= floor), d=field, **shifted)
    gy = ImageMath.lambda_eval(lambda a: (a["down"] - a["up"]) * gain * (a["d"] >= floor), d=field, **shifted)
    inverse = ImageMath.lambda_eval(lambda a: (a["x"] * a["x"] + a["y"] * a["y"] + 1.0) ** -0.5, x=gx, y=gy)
    # (v + 1) × 127,5 arrondi : la conversion en 8 bits tronque, d'où + 128 (plan face à l'objectif = 128, 128, 255).
    channels = [ImageMath.lambda_eval(lambda a: a["v"] * a["n"] * 127.5 + 128.0, v=value, n=inverse).convert("L") for value in (gx, gy)]
    channels.append(ImageMath.lambda_eval(lambda a: a["n"] * 127.5 + 128.0, n=inverse).convert("L"))
    return Image.merge("RGB", channels).resize((width, height), Image.Resampling.BICUBIC)


def map_mask(image, settings):
    from PIL import ImageOps

    if image.mode not in ("RGBA", "LA", "PA") and "transparency" not in image.info:
        raise MapsError("ALPHA_REQUIRED", ALPHA_MESSAGE)
    alpha = image.convert("RGBA").getchannel("A")
    if alpha.getextrema()[0] == 255:
        raise MapsError("ALPHA_REQUIRED", ALPHA_MESSAGE)
    if settings["edge"] == "hard":
        alpha = _binary(alpha, 128)
    mask = ImageOps.invert(alpha) if settings["white"] == "background" else alpha
    mask = _grow(mask, int(settings["grow"]))
    return _binary(mask, 128) if settings["edge"] == "hard" else mask


MAPS = {
    "control_normal": map_normal, "control_lineart": map_lineart, "control_scribble": map_scribble, "control_tile": map_tile,
    "control_color": map_color, "control_gray": map_gray, "mask_alpha": map_mask,
}


def compute_map(tool, settings, image):
    """Carte d'un outil ; mêmes dimensions que l'image. Réglages déjà validés."""
    from PIL import Image

    function = MAPS.get(tool)
    if function is None:
        raise MapsError("REQUEST_INVALID", "Calcul du Labo inconnu de ce Worker.")
    width, height = image.size
    if width < 1 or height < 1 or width > MAX_SIDE or height > MAX_SIDE or width * height > MAX_PIXELS:
        raise MapsError("IMAGE_TOO_LARGE", "Dimensions refusées : au plus 16 mégapixels et 8192 px de côté.")
    result = function(image, settings)
    if result.size != image.size:   # garde-fou : une carte garde toujours les dimensions de son image
        result = result.resize(image.size, Image.Resampling.BICUBIC)
    return result


# ------------------------------------------------------------------ calcul
def run(request):
    started = time.monotonic()
    image = open_image(request["input"])
    tool = request["tool"]
    if tool == "properties":
        properties = image_properties(image)
        return {"ok": True, "tool": tool, "width": properties["width"], "height": properties["height"], "mode": image.mode,
                "properties": properties, "seconds": round(time.monotonic() - started, 3)}
    result = compute_map(tool, request["settings"], image)
    output = io.BytesIO()
    result.save(output, format="PNG", compress_level=4)
    if output.tell() > RESULT_MAX_BYTES:
        raise MapsError("RESULT_TOO_LARGE", "La carte obtenue dépasse 32 Mo.")
    temporary = request["output"].with_name(request["output"].name + ".part")
    temporary.write_bytes(output.getvalue())
    os.replace(temporary, request["output"])
    return {"ok": True, "tool": tool, "width": result.width, "height": result.height, "mode": result.mode,
            "seconds": round(time.monotonic() - started, 3)}


def _write_report(path, value):
    temporary = Path(str(path) + ".part")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def main(argv):
    if len(argv) != 2:
        print("Usage : image_maps_script.py <request.json>", file=sys.stderr)
        return 2
    block_network()
    try:
        raw = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("Demande de calcul illisible.", file=sys.stderr)
        return 2
    try:
        request = validate_request(raw)
    except MapsError as error:
        print(str(error), file=sys.stderr)
        return 2
    try:
        report = run(request)
    except MapsError as error:
        report = {"ok": False, "code": error.code, "error": str(error)}
    except MemoryError:
        report = {"ok": False, "code": "OUT_OF_MEMORY", "error": "Mémoire insuffisante pour ce calcul."}
    except ImportError:
        report = {"ok": False, "code": "PILLOW_MISSING", "error": "Pillow manque dans l’environnement du moteur image."}
    except Exception as error:  # noqa: BLE001 - seule la nature de l'erreur est rapportée (jamais un chemin)
        report = {"ok": False, "code": "INTERNAL", "error": f"Échec du calcul ({type(error).__name__})."}
    _write_report(request["report"], report)
    return 0 if report.get("ok") else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
