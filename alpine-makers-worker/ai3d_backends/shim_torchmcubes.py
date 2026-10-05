"""Pure-Python replacement for the compiled `torchmcubes` extension used by TripoSR.

`tsr.models.isosurface.MarchingCubeHelper` calls `marching_cubes(level, 0.0)` with a
cubic float tensor and expects `(vertices, triangles)` in the index space of the
grid, vertices ordered (x, y, z) = (dim2, dim1, dim0) — the helper reverses the
columns afterwards. scikit-image's marching cubes returns index-order (dim0,
dim1, dim2) coordinates, so the columns are flipped here to match.
"""
import numpy as np
import torch


def marching_cubes(volume, threshold=0.0):
    grid = volume.detach().to("cpu", torch.float32).numpy()
    if grid.ndim != 3:
        raise ValueError("torchmcubes shim: a cubic (D, H, W) volume is required.")
    try:
        from skimage import measure
    except ImportError as error:
        raise RuntimeError("scikit-image est requis pour l'extraction de surface (shim torchmcubes).") from error
    level = float(threshold)
    low, high = float(np.nanmin(grid)), float(np.nanmax(grid))
    if not low < level < high:
        # No crossing: return an empty mesh rather than raising inside the model.
        return torch.zeros((0, 3), dtype=torch.float32), torch.zeros((0, 3), dtype=torch.int64)
    vertices, faces, _normals, _values = measure.marching_cubes(grid, level=level)
    vertices = np.ascontiguousarray(vertices[:, ::-1], dtype=np.float32)
    faces = np.ascontiguousarray(faces, dtype=np.int64)
    return torch.from_numpy(vertices), torch.from_numpy(faces)


def grid_interp(*args, **kwargs):
    raise NotImplementedError("torchmcubes shim: grid_interp n'est pas disponible.")
