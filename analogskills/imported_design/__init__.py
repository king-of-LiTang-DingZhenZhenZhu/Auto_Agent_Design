"""Stable bridge from Auto_Agent_Design netlists to physical implementation."""

from .adapters import PhysicalAdapterRequired, adapt_topology
from .flow import (
    ImportedPhysicalResult,
    ImportedSchematicResult,
    compile_imported_design,
    import_prepared_schematic,
    prepare_imported_schematic,
    prepare_imported_physical_run,
    run_imported_design_signoff,
)
from .handoff import build_imported_design_handoff
from .eco import accept_eco_candidate
from .schema import HandoffDevice, ImportedDesignHandoff
from .physical_intent import (
    ImportedPhysicalSmtResult,
    PhysicalDesignIntent,
    PhysicalIntentError,
    compile_physical_intent,
    solve_imported_physical_smt,
)

__all__ = [
    "HandoffDevice",
    "ImportedDesignHandoff",
    "ImportedPhysicalResult",
    "ImportedSchematicResult",
    "PhysicalAdapterRequired",
    "PhysicalDesignIntent",
    "PhysicalIntentError",
    "ImportedPhysicalSmtResult",
    "adapt_topology",
    "accept_eco_candidate",
    "build_imported_design_handoff",
    "compile_imported_design",
    "compile_physical_intent",
    "import_prepared_schematic",
    "prepare_imported_schematic",
    "prepare_imported_physical_run",
    "run_imported_design_signoff",
    "solve_imported_physical_smt",
]
