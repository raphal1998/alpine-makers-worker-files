"""Vision classique d'Alpine Guard : santé de la caméra et signaux d'échec d'impression (numpy + OpenCV).

Une instance ``MonitorVision`` par moniteur garde l'état nécessaire entre les images (image précédente, fond
médian, références de l'exécution). Toutes les mesures se font sur une version réduite (320 px de large) en niveaux
de gris NORMALISÉS dans la zone d'impression (moyenne / écart-type de la zone) : un changement d'éclairage global ne
ressemble ni à un amas, ni à un décollement, et il est en plus détecté et écarté explicitement (``lighting``).

Fond médian : médiane des 7 dernières images espacées d'au moins 0,8 s. La tête qui passe (objet rapide) en est
retirée ; ce qui reste est la pièce, le plateau et un éventuel amas.

Signaux (0–1) :
- ``camera_dark`` : image très sombre ; ``camera_masked`` : image presque sans détail (objectif couvert, buée) ;
  ``diff_prev`` (mesure brute) sert à l'agent pour juger une image figée ;
- ``spaghetti`` : densité de contours COURBES (les segments droits — bords du plateau, parois — sont retirés) dans une
  cellule de la zone, au-delà de la référence prise pendant le délai de grâce ;
- ``detachment`` : la pièce (fond qui diffère du plateau du début) se déplace nettement ou disparaît ;
- ``blob`` : la silhouette de l'objet rapide (la tête) grossit nettement par rapport au début de l'impression ;
- ``bg_change_60`` (mesure brute) : changement du fond en 60 s, que l'agent croise avec les couches de la
  télémétrie pour juger une impression dans le vide.
"""
import math
from collections import deque

import cv2
import numpy as np

WIDTH = 320
BG_FRAMES = 7
BG_SPACING_S = 0.8
RING_EVERY_S = 10.0
RING_KEEP_S = 420.0
GRID = 4
LIGHTING_JUMP = 20.0         # saut de luminance moyenne (niveaux) qui signe un changement d'éclairage
LIGHTING_HOLD_S = 12.0
PART_DIFF = 28               # écart (image normalisée) entre le fond et le plateau du début : pièce
TRANSIENT_DIFF = 40          # écart entre l'image et le fond : objet rapide (tête)


def _clamp(value):
    if value is None or not math.isfinite(float(value)):
        return 0.0
    return float(max(0.0, min(1.0, value)))


def decode(jpeg):
    data = np.frombuffer(jpeg, dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None or image.ndim != 3 or image.shape[0] < 16 or image.shape[1] < 16:
        raise ValueError("image JPEG illisible")
    return image


def zone_mask(shape, zone):
    height, width = shape[:2]
    mask = np.zeros((height, width), dtype=np.uint8)
    if not zone:
        mask[:, :] = 255
        return mask
    points = np.array([[int(round(x * (width - 1))), int(round(y * (height - 1)))] for x, y in zone], dtype=np.int32)
    cv2.fillPoly(mask, [points], 255)
    if int(mask.sum()) < 255 * 64:
        mask[:, :] = 255
    return mask


class MonitorVision:
    def __init__(self):
        self.reset()

    def reset(self, run=None):
        self.run = run
        self.prev = None
        self.bg_frames = deque(maxlen=BG_FRAMES)
        self.bg_last = None
        self.bg = None
        self.ring = deque()
        self.empty_ref = None
        self.cell_base = None
        self.cell_samples = []
        self.part_hist = deque()
        self.part_max = 0.0
        self.head_areas = []
        self.last_mean = None
        self.last_mean_ts = None
        self.lighting_until = 0.0
        self.shape = None

    # -------------------------------------------------------------------------------------------- mesures
    def _health(self, gray):
        mean = float(gray.mean())
        std = float(gray.std())
        gray_u8 = gray.astype(np.uint8)
        sharp = float(cv2.Laplacian(gray_u8, cv2.CV_64F).var())
        h, w = gray.shape
        hb, wb = (h // 8) * 8, (w // 8) * 8
        blocks = gray[:hb, :wb].reshape(hb // 8, 8, wb // 8, 8).std(axis=(1, 3))
        flat = float((blocks < 2.5).mean())
        dark = _clamp((22.0 - mean) / 12.0)
        # Objectif couvert, buée, caméra tournée vers un mur : image presque uniforme ET sans détail. Une scène
        # normale garde toujours des contours (plateau, pièce, tête) ; ``flat`` reste une mesure d'information.
        masked = 0.0 if dark >= 0.5 else _clamp((12.0 - std) / 6.0) * (1.0 if sharp < 15.0 else 0.0)
        diff = None if self.prev is None or self.prev.shape != gray.shape else float(np.abs(gray - self.prev).mean())
        return {"mean": round(mean, 2), "std": round(std, 2), "sharpness": round(sharp, 1), "flat": round(flat, 3),
                "diff_prev": None if diff is None else round(diff, 3)}, {"camera_dark": round(dark, 4),
                                                                         "camera_masked": round(masked, 4)}

    @staticmethod
    def _normalise(gray, mask):
        values = gray[mask > 0]
        mean, std = float(values.mean()), float(values.std())
        return np.clip((gray - mean) / (std + 1.0) * 40.0 + 128.0, 0, 255).astype(np.float32)

    def _background(self, norm, ts):
        if self.bg_last is None or ts - self.bg_last >= BG_SPACING_S:
            self.bg_frames.append(norm)
            self.bg_last = ts
        if len(self.bg_frames) >= 3:
            self.bg = np.median(np.stack(self.bg_frames), axis=0).astype(np.float32)
        else:
            self.bg = norm
        if not self.ring or ts - self.ring[-1][0] >= RING_EVERY_S:
            self.ring.append((ts, self.bg.copy()))
        while self.ring and ts - self.ring[0][0] > RING_KEEP_S:
            self.ring.popleft()

    def _old_bg(self, ts, lag):
        old = None
        for stamp, image in self.ring:
            if ts - stamp >= lag:
                old = image
            else:
                break
        return old

    def _cells(self, mask):
        ys, xs = np.nonzero(mask)
        x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
        cells = []
        for row in range(GRID):
            for col in range(GRID):
                cx0 = x0 + (x1 - x0) * col // GRID
                cx1 = x0 + (x1 - x0) * (col + 1) // GRID
                cy0 = y0 + (y1 - y0) * row // GRID
                cy1 = y0 + (y1 - y0) * (row + 1) // GRID
                cells.append((cy0, cy1, cx0, cx1))
        return cells

    def _chaos(self, bg, mask):
        """Densité de contours courbes par cellule et part courbe des contours (segments droits retirés)."""
        image = bg.astype(np.uint8)
        edges = cv2.Canny(image, 50, 140)
        edges[mask == 0] = 0
        curvy = edges.copy()
        lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=22, minLineLength=16, maxLineGap=3)
        if lines is not None:
            for x1, y1, x2, y2 in lines[:, 0, :]:
                cv2.line(curvy, (int(x1), int(y1)), (int(x2), int(y2)), 0, 3)
        density, ratio = [], []
        for cy0, cy1, cx0, cx1 in self._cells(mask):
            area = int((mask[cy0:cy1, cx0:cx1] > 0).sum())
            if area < 40:
                density.append(0.0)
                ratio.append(0.0)
                continue
            total = int((edges[cy0:cy1, cx0:cx1] > 0).sum())
            kept = int((curvy[cy0:cy1, cx0:cx1] > 0).sum())
            density.append(kept / area)
            ratio.append(kept / total if total else 0.0)
        return np.array(density, dtype=np.float64), np.array(ratio, dtype=np.float64)

    def _part(self, bg, mask):
        diff = np.abs(bg - self.empty_ref)
        part = ((diff > PART_DIFF) & (mask > 0)).astype(np.uint8)
        part = cv2.morphologyEx(part, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        area = float(part.sum()) / max(1.0, float((mask > 0).sum()))
        if part.sum() == 0:
            return area, None
        ys, xs = np.nonzero(part)
        return area, (float(xs.mean()), float(ys.mean()))

    def _head_area(self, norm, mask):
        moving = (np.abs(norm - self.bg) > TRANSIENT_DIFF).astype(np.uint8)
        moving[mask == 0] = 0
        moving = cv2.morphologyEx(moving, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        count, _, stats, _ = cv2.connectedComponentsWithStats(moving, connectivity=8)
        if count <= 1:
            return 0.0
        largest = float(stats[1:, cv2.CC_STAT_AREA].max())
        return largest / max(1.0, float((mask > 0).sum()))

    # -------------------------------------------------------------------------------------------- analyse
    def analyse(self, image, ts, zone=None, armed=False, in_grace=False, run=None):
        if run != self.run:
            self.reset(run)
        height = max(16, int(round(WIDTH * image.shape[0] / image.shape[1])))
        small = cv2.resize(image, (WIDTH, height), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
        if self.shape != gray.shape:
            self.reset(run)
            self.shape = gray.shape
        mask = zone_mask(gray.shape, zone)
        health, camera = self._health(gray)
        self.prev = gray
        signals = {"spaghetti": 0.0, "detachment": 0.0, "blob": 0.0}
        metrics = {"bg_change_60": None, "part_area": None, "head_ratio": None, "lighting": False, "cells": None}
        # Changement d'éclairage : saut de luminance moyenne en moins de 3 s ; fond et références repris ensuite.
        mean = health["mean"]
        if self.last_mean is not None and ts - (self.last_mean_ts or ts) <= 3.0 and abs(mean - self.last_mean) > LIGHTING_JUMP:
            # Les références de l'exécution sont reprises après la stabilisation : un éclairage non uniforme ne
            # doit ressembler ni à une pièce déplacée, ni à un amas (la pièce déjà imprimée entre dans la référence).
            self.lighting_until = ts + LIGHTING_HOLD_S
            self.bg_frames.clear()
            self.ring.clear()
            self.part_hist.clear()
            self.empty_ref = None
            self.cell_base = None
            self.cell_samples = []
        self.last_mean, self.last_mean_ts = mean, ts
        if camera["camera_dark"] >= 0.5 or camera["camera_masked"] >= 0.5:
            return {"health": health, "camera": camera, "signals": signals, "metrics": metrics}
        norm = self._normalise(gray, mask)
        self._background(norm, ts)
        old = self._old_bg(ts, 60.0)
        if old is not None:
            # Part (pour mille) de la zone dont le fond a nettement changé en 60 s : une pièce qui monte change peu de
            # pixels mais fortement (son bord supérieur) ; une impression dans le vide n'en change aucun.
            changed = (np.abs(self.bg - old) > 20.0)[mask > 0]
            metrics["bg_change_60"] = round(float(changed.mean()) * 1000.0, 3)
        if ts < self.lighting_until:
            metrics["lighting"] = True
            return {"health": health, "camera": camera, "signals": signals, "metrics": metrics}
        # Fond complet exigé (7 images) : avec moins, deux passages de tête superposés restent dans la médiane.
        if not armed or len(self.bg_frames) < BG_FRAMES:
            return {"health": health, "camera": camera, "signals": signals, "metrics": metrics}
        if self.empty_ref is None:
            self.empty_ref = self.bg.copy()
        # Amas « spaghetti » : contours courbes au-delà de la référence de la cellule.
        density, ratio = self._chaos(self.bg, mask)
        if in_grace or self.cell_base is None:
            self.cell_samples.append(density)
            self.cell_samples = self.cell_samples[-20:]
            self.cell_base = np.median(np.stack(self.cell_samples), axis=0)
        if not in_grace:
            # Part courbe : facteur progressif (des filaments serrés gardent des bouts presque droits), nul pour une
            # cellule faite surtout de segments droits (bords du plateau, parois, remplissage vu de face).
            excess = density - self.cell_base - 0.03
            cell_scores = np.clip(excess / 0.06, 0.0, 1.0) * np.clip((ratio - 0.2) / 0.3, 0.0, 1.0)
            signals["spaghetti"] = round(float(cell_scores.max()), 4)
            metrics["cells"] = [round(float(value), 3) for value in cell_scores]
            # La pièce grandit normalement : référence suivie lentement tant que rien n'est suspect.
            if signals["spaghetti"] < 0.2:
                self.cell_base = self.cell_base * 0.98 + density * 0.02
        # Pièce décollée / déplacée : déplacement du centre de la pièce en 30–90 s, ou pièce disparue.
        area, centre = self._part(self.bg, mask)
        metrics["part_area"] = round(area, 4)
        if not self.part_hist or ts - self.part_hist[-1][0] >= 5.0:
            self.part_hist.append((ts, area, centre))
        while self.part_hist and ts - self.part_hist[0][0] > 120.0:
            self.part_hist.popleft()
        if not in_grace:
            ys, xs = np.nonzero(mask)
            diagonal = math.hypot(float(xs.max() - xs.min()), float(ys.max() - ys.min())) or 1.0
            # Référence : relevés de 30 à 90 s plus tôt (médiane des surfaces, centre du plus ancien) ; jamais un
            # maximum isolé (un reflet ou la tête restée immobile une seconde ne fait pas une pièce).
            window = [(old_area, old_centre) for stamp, old_area, old_centre in self.part_hist if 30.0 <= ts - stamp <= 90.0]
            score = 0.0
            if window:
                ref_area = float(np.median([item[0] for item in window]))
                # Centre de référence : le relevé valide le plus récent de la fenêtre (≈ 30 s), pour que la croissance
                # normale de la pièce (qui déplace un peu son centre) ne s'accumule pas.
                ref_centre = next((item[1] for item in reversed(window) if item[1] and item[0] >= 0.004), None)
                if ref_centre and centre and ref_area >= 0.004 and area >= 0.004:
                    shift = math.hypot(centre[0] - ref_centre[0], centre[1] - ref_centre[1]) / diagonal
                    score = _clamp((shift - 0.05) / 0.05)
                # Pièce disparue de la zone (arrachée, tombée) : la surface baisse nettement (une pièce normale ne fait
                # que grandir). Il reste toujours un peu de surface « bruit » (≈ 0,7 % mesuré) : baisse absolue ET relative.
                if ref_area >= 0.01 and ref_area - area >= max(0.008, 0.45 * ref_area):
                    score = 1.0
                jump = area - ref_area
                if jump > 0.03 and area > 2.5 * ref_area + 0.01:
                    score = max(score, _clamp((jump - 0.03) / 0.03))   # surface qui change d'un coup (pièce couchée, amas)
            signals["detachment"] = round(score, 4)
        # Amas sur la buse : silhouette de l'objet rapide nettement plus grande qu'au début.
        head = self._head_area(norm, mask)
        metrics["head_ratio"] = None
        if head > 0.003:
            if len(self.head_areas) < 60:
                self.head_areas.append(head)
            if len(self.head_areas) >= 15 and not in_grace:
                base = float(np.median(self.head_areas))
                ratio_head = head / base if base > 0 else 0.0
                metrics["head_ratio"] = round(ratio_head, 3)
                signals["blob"] = round(_clamp((ratio_head - 1.7) / 1.0), 4)
        return {"health": health, "camera": camera, "signals": signals, "metrics": metrics}


def annotate(image, zone=None, boxes=(), label="", max_side=960, max_bytes=300 * 1024):
    """Image d'alerte : zone en vert, boîtes du modèle en rouge, libellé ; JPEG de ``max_bytes`` au plus."""
    canvas = image.copy()
    height, width = canvas.shape[:2]
    scale = min(1.0, float(max_side) / max(height, width))
    if scale < 1.0:
        canvas = cv2.resize(canvas, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)
        height, width = canvas.shape[:2]
    if zone:
        points = np.array([[int(x * (width - 1)), int(y * (height - 1))] for x, y in zone], dtype=np.int32)
        cv2.polylines(canvas, [points], True, (60, 200, 90), 2)
    for box in boxes or ():
        x1, y1, x2, y2 = (int(box[0] * width), int(box[1] * height), int(box[2] * width), int(box[3] * height))
        cv2.rectangle(canvas, (x1, y1), (x2, y2), (40, 40, 230), 2)
    if label:
        text = label.encode("ascii", "replace").decode("ascii")[:80]
        cv2.rectangle(canvas, (0, 0), (min(width, 12 + 9 * len(text)), 26), (20, 20, 20), -1)
        cv2.putText(canvas, text, (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    quality = 85
    while True:
        ok, data = cv2.imencode(".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not ok:
            raise ValueError("encodage JPEG impossible")
        if len(data) <= max_bytes or quality <= 35:
            if len(data) > max_bytes:
                canvas = cv2.resize(canvas, (canvas.shape[1] * 2 // 3, canvas.shape[0] * 2 // 3), interpolation=cv2.INTER_AREA)
                quality = 70
                continue
            return data.tobytes()
        quality -= 10
