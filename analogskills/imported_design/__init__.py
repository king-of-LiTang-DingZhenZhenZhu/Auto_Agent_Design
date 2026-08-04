"""Stable bridge from Auto_Agent_Design netlists to physical implementation."""

from .adapters import PhysicalAdapterRequired, adapt_topology
from .flow import (
    ImportedPhysicalResult,
    compile_imported_design,
    prepare_imported_physical_run,
    run_imported_design_signoff,
)
from .handoff import build_imported_design_handoff
from .eco import accept_eco_candidate
from .schema import HandoffDevice, ImportedDesignHandoff

__all__ = [
    "HandoffDevice",
    "ImportedDesignHandoff",
    "ImportedPhysicalResult",
    "PhysicalAdapterRequired",
    "adapt_topology",
    "accept_eco_candidate",
    "build_imported_design_handoff",
    "compile_imported_design",
    "prepare_imported_physical_run",
    "run_imported_design_signoff",
]
