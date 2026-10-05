"""Stand-in for the compiled `diso` package (DiffDMC).

TripoSG/PartCrafter import `from diso import DiffDMC` at module load even though
their hierarchical extraction path only needs scikit-image marching cubes. The
Worker always calls those pipelines with `use_flash_decoder=False`, so this
shim only has to make the import succeed; using it is an explicit error rather
than a silent wrong mesh.
"""


class DiffDMC:  # noqa: D101 - mirrors the upstream class name
    def __init__(self, *args, **kwargs):
        pass

    def to(self, *args, **kwargs):
        return self

    def __call__(self, *args, **kwargs):
        raise RuntimeError(
            "diso (DiffDMC) n'est pas compilé sur ce Worker : le décodeur flash est désactivé, "
            "utilise l'extraction hiérarchique (use_flash_decoder=False)."
        )


class DiffMC(DiffDMC):
    pass
