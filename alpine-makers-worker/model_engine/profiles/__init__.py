"""Profils Alpine du trancheur : schéma des réglages et préréglages (process), fiches filaments (filament).

Pure Python : importable par le site (sans numpy) pour servir le schéma et les fiches.
"""
from .filament import (FILAMENTS, PLATES, PLATE_SHORT, TYPES as FILAMENT_TYPES, MaterialUnknownError, canonical_type,
                       list_types, normalise_plate, plate_temperatures, resolve_filament, suitable_plates)
from .process import (ALIASES, BED_TYPES, BRIM_TYPES, CATEGORIES, CONTRACT_KEYS, FIELDS, FIELD_BY_KEY, INFILL_PATTERNS,
                      NOZZLES, PRESETS, PRESET_BY_NAME, SEAM_POSITIONS, SUPPORT_TYPES, Field, Preset, SettingsError,
                      coerce, layer_height_bounds, normalise_nozzle, preset_for, presets_for, printer_model_from_profile,
                      resolve, resolve_with_report, schema_for_api)

__all__ = [
    "ALIASES", "BED_TYPES", "BRIM_TYPES", "CATEGORIES", "CONTRACT_KEYS", "FIELDS", "FIELD_BY_KEY", "FILAMENTS",
    "FILAMENT_TYPES", "INFILL_PATTERNS", "NOZZLES", "PLATES", "PLATE_SHORT", "PRESETS", "PRESET_BY_NAME",
    "SEAM_POSITIONS", "SUPPORT_TYPES", "Field", "MaterialUnknownError", "Preset", "SettingsError", "canonical_type",
    "coerce", "layer_height_bounds", "list_types", "normalise_nozzle", "normalise_plate", "plate_temperatures",
    "preset_for", "presets_for", "printer_model_from_profile", "resolve", "resolve_filament", "resolve_with_report",
    "schema_for_api", "suitable_plates",
]
