"""Shared small utilities for analogskills modules."""
from __future__ import annotations


def coerce_dimension_m(value: float) -> float:
    """Coerce common dimension inputs to meters.

    Values below 1e-3 are treated as meters, values below 100 as microns, and
    larger values as nanometers. This preserves the historical analogskills sizing
    convention used by schematic, netlist, and PCell helpers.
    """
    if value <= 0:
        raise ValueError("physical dimensions must be positive")
    if value < 1e-3:
        return value
    if value < 100:
        return value * 1e-6
    return value * 1e-9
