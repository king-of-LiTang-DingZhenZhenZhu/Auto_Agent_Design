# Embedded physical backend provenance

The physical implementation package was imported from `ephonic/analog_skills`
through `ephonic/analog_skills` hn-dev commit `964f781` and trimmed to the physical contracts, PDK data, PCell,
layout, OA/Calibre, and ECO implementation modules needed by this repository.

The embedded package is intentionally not an editable or Git dependency.  The
frontend-selected final netlist remains the electrical source of truth.
