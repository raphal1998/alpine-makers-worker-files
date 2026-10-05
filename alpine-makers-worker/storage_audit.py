"""Read-only, size-filtered duplicate audit of explicit local storage roots.

No file is removed, rewritten or linked. Symlinks/junctions are not traversed;
hardlinks are counted once when estimating space that could be recovered.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import time
from collections import defaultdict


def inventory_roots(roots):
    selected, errors, files, aliases = [], [], [], []
    for label, value in roots.items():
        path = Path(os.path.abspath(os.path.normpath(os.fspath(value))))
        try:
            root_info = path.lstat()
            if stat.S_ISLNK(root_info.st_mode) or getattr(root_info, "st_file_attributes", 0) & 0x400:
                aliases.append({"path": str(path), "root": str(label)})
                errors.append({"path": str(path), "error": "Racine liée/jonction non parcourue ; sélectionner le dossier réel explicitement"})
                continue
            if not stat.S_ISDIR(root_info.st_mode):
                raise OSError("La racine n’est pas un dossier")
        except OSError as error:
            errors.append({"path": str(path), "error": str(error)})
            continue
        # Collapse overlapping roots so a nested configured engine isn't scanned twice.
        key = os.path.normcase(str(path))
        if any(key == item["key"] or key.startswith(item["key"] + os.sep) for item in selected):
            continue
        selected = [item for item in selected if not item["key"].startswith(key + os.sep)]
        selected.append({"label": str(label), "path": str(path), "key": key})
    for root in selected:
        pending = [root["path"]]
        while pending:
            folder = pending.pop()
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        try:
                            info = entry.stat(follow_symlinks=False)
                            reparse = getattr(info, "st_file_attributes", 0) & 0x400
                            if stat.S_ISLNK(info.st_mode) or reparse:
                                aliases.append({"path": entry.path, "root": root["label"]})
                            elif stat.S_ISDIR(info.st_mode):
                                pending.append(entry.path)
                            elif stat.S_ISREG(info.st_mode):
                                # Windows DirEntry caches size/time but returns zero for
                                # st_ino/st_dev/st_nlink; stat the path for real file IDs.
                                info = os.stat(entry.path, follow_symlinks=False)
                                files.append({
                                    "path": entry.path, "root": root["label"],
                                    "size_bytes": info.st_size, "mtime_ns": info.st_mtime_ns,
                                    "device": info.st_dev, "inode": info.st_ino,
                                    "links": info.st_nlink,
                                })
                        except OSError as error:
                            errors.append({"path": entry.path, "error": str(error)})
            except OSError as error:
                errors.append({"path": folder, "error": str(error)})
    return {
        "roots": [{"label": item["label"], "path": item["path"]} for item in selected],
        "files": files, "errors": errors, "aliases": aliases,
    }


def _signature(info):
    return info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino


def _digest(item, sample=False, advance=None):
    path = Path(item["path"])
    before = path.lstat()
    expected = item["size_bytes"], item["mtime_ns"], item["device"], item["inode"]
    if (_signature(before) != expected or not stat.S_ISREG(before.st_mode)
            or getattr(before, "st_file_attributes", 0) & 0x400):
        raise OSError("Fichier changé depuis l’inventaire ; comparaison non confirmée")
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as handle:
        if sample:
            for offset in sorted({0, max(0, before.st_size // 2 - 32768), max(0, before.st_size - 65536)}):
                handle.seek(offset)
                digest.update(handle.read(65536))
        else:
            while chunk := handle.read(4 * 1024 * 1024):
                digest.update(chunk)
                if advance:
                    advance(len(chunk))
                # Give the concurrently running backup opportunities for disk I/O.
                time.sleep(0.001)
        if _signature(os.fstat(handle.fileno())) != expected:
            raise OSError("Fichier modifié pendant la lecture ; comparaison non confirmée")
    if _signature(path.stat()) != expected:
        raise OSError("Fichier changé pendant la comparaison")
    return digest.hexdigest()


def scan_roots(roots, progress=None, min_size_bytes=1048576, *, inventory=None):
    def notify(percent, message):
        if progress:
            progress(percent, message)

    notify(1, "Inventaire des dossiers d’installation et de modèles…")
    snapshot = inventory if inventory is not None else inventory_roots(roots)
    errors = list(snapshot["errors"])
    candidates = defaultdict(list)
    hardlink_aliases = 0
    identities = set()
    for item in snapshot["files"]:
        # Git storage isn't a second install. It has a separate retention policy.
        if item["size_bytes"] < min_size_bytes or ".git" in Path(item["path"]).parts:
            continue
        identity = (item["device"], item["inode"]) if item["inode"] else item["path"]
        if identity in identities:
            hardlink_aliases += 1
            continue
        identities.add(identity)
        candidates[item["size_bytes"]].append(item)
    same_size = [items for items in candidates.values() if len(items) > 1]
    sampling_total = sum(len(items) for items in same_size)
    sampled, full_groups = 0, []
    for items in same_size:
        samples = defaultdict(list)
        for item in items:
            try:
                samples[_digest(item, sample=True)].append(item)
            except OSError as error:
                errors.append({"path": item["path"], "error": str(error)})
            sampled += 1
            if sampled % 20 == 0:
                notify(5 + 15 * sampled / max(1, sampling_total), f"Comparaison préliminaire : {sampled}/{sampling_total} fichiers")
        full_groups.extend(group for group in samples.values() if len(group) > 1)
    full_bytes = sum(item["size_bytes"] for items in full_groups for item in items)
    read_bytes, last_notify = 0, 0.0

    def advance(count):
        nonlocal read_bytes, last_notify
        read_bytes += count
        now = time.monotonic()
        if now - last_notify >= 2:
            last_notify = now
            notify(20 + 75 * read_bytes / max(1, full_bytes), f"Vérification SHA-256 : {read_bytes / 1024**3:.1f}/{full_bytes / 1024**3:.1f} Gio lus")

    duplicates = []
    for items in full_groups:
        hashes = defaultdict(list)
        for item in items:
            try:
                hashes[_digest(item, advance=advance)].append(item)
            except OSError as error:
                errors.append({"path": item["path"], "error": str(error)})
        for digest, group in hashes.items():
            if len(group) < 2:
                continue
            # Last check: no group is reported if a file changed after it was hashed.
            try:
                for item in group:
                    current = Path(item["path"]).stat()
                    if _signature(current) != (item["size_bytes"], item["mtime_ns"], item["device"], item["inode"]):
                        raise OSError("Fichier changé après vérification SHA-256")
            except OSError as error:
                errors.append({"path": item["path"], "error": str(error)})
                continue
            independent_copies = sum(item["links"] <= 1 for item in group)
            reclaimable = min(len(group) - 1, independent_copies) * group[0]["size_bytes"]
            duplicates.append({
                "sha256": digest, "size_bytes": group[0]["size_bytes"],
                "files": [{"path": item["path"], "root": item["root"]} for item in group],
                "reclaimable_bytes": reclaimable,
            })
    duplicates.sort(key=lambda item: item["reclaimable_bytes"], reverse=True)
    notify(99, f"Audit terminé : {len(duplicates)} groupes de doublons confirmés")
    return {
        "roots": snapshot["roots"], "total_files": len(snapshot["files"]),
        "total_bytes": sum(item["size_bytes"] for item in snapshot["files"]),
        "duplicate_groups": duplicates,
        "duplicate_bytes": sum(item["reclaimable_bytes"] for item in duplicates),
        "errors": errors, "complete": not errors,
        "min_size_bytes": min_size_bytes, "hash_algorithm": "sha256",
        "hardlink_aliases_skipped": hardlink_aliases, "links_skipped": len(snapshot["aliases"]),
        "note": "Doublons identiques ne signifie pas supprimables : vérifier les dépendances avant nettoyage.",
    }
