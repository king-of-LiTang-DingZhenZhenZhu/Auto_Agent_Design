"""Topology Registry — hard-constrained circuit generators.

Each topology is a Python class that produces correct-by-construction
.cir and .sp netlist files. No LLM involvement in netlist generation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from topologies.bandgap_ptat import BandgapPTAT
from topologies.base import BaseTopology, TopologyMeta
from topologies.five_t_ota import FiveTOTA
from topologies.folded_cascode import FoldedCascodeOTA
from topologies.folded_cascode_two_stage import FoldedCascodeTwoStageOTA
from topologies.nmcf_three_stage import NMCFThreeStageOTA
from topologies.pmos_input_two_stage_ota import PMOSInputTwoStageOTA
from topologies.two_stage_ota import TwoStageOTA

if TYPE_CHECKING:
    from models import DesignTarget

# ---------------------------------------------------------------------------
# Registry: add new topologies here
# ---------------------------------------------------------------------------
TOPOLOGY_REGISTRY: dict[str, type[BaseTopology]] = {
    "5t_ota": FiveTOTA,
    "two_stage_ota": TwoStageOTA,
    "pmos_input_two_stage_ota": PMOSInputTwoStageOTA,
    "folded_cascode": FoldedCascodeOTA,
    "folded_cascode_two_stage": FoldedCascodeTwoStageOTA,
    "nmcf_three_stage": NMCFThreeStageOTA,
    "bandgap_ptat": BandgapPTAT,
}


def get_topology(name: str) -> BaseTopology:
    """Factory: instantiate a topology by name."""
    cls = TOPOLOGY_REGISTRY.get(name)
    if cls is None:
        available = ", ".join(TOPOLOGY_REGISTRY.keys())
        raise ValueError(
            f"Unknown topology '{name}'. Available: {available}"
        )
    return cls()


def list_topologies() -> list[TopologyMeta]:
    """Return metadata for every registered topology."""
    return [cls().meta for cls in TOPOLOGY_REGISTRY.values()]


def get_topology_for_targets(targets: DesignTarget) -> str | None:
    """Rule-based heuristic: pick the best topology for the given targets.

    Scores each topology on how well its capability range covers the
    requested targets.  Ties are broken by complexity (simpler first).

    Returns None only when no topology can plausibly meet the targets.
    """
    topology_hint = (targets.topology_hint or "").lower()
    if "bandgap" in topology_hint or "ptat" in topology_hint:
        return "bandgap_ptat"

    if "nmcf_three_stage" in TOPOLOGY_REGISTRY:
        very_high_gain = (
            targets.gain_db is not None and targets.gain_db >= 85
        )
        high_gain_heavy_load = (
            targets.gain_db is not None
            and targets.gain_db >= 75
            and targets.load_cap_f is not None
            and targets.load_cap_f >= 5e-12
        )
        if very_high_gain or high_gain_heavy_load:
            return "nmcf_three_stage"

    candidates: list[tuple[int, int, str]] = []  # (score, complexity, name)
    for name, cls in TOPOLOGY_REGISTRY.items():
        meta = cls().meta
        score = 0

        if targets.gain_db is not None:
            if meta.min_gain_db <= targets.gain_db <= meta.max_gain_db:
                score += 2
            elif targets.gain_db <= meta.max_gain_db * 1.1:
                score += 1  # slightly out of range — marginal

        if targets.bandwidth_hz is not None:
            if meta.min_gbw_hz <= targets.bandwidth_hz <= meta.max_gbw_hz:
                score += 2
            elif targets.bandwidth_hz <= meta.max_gbw_hz * 1.1:
                score += 1

        if targets.phase_margin_deg is not None:
            if targets.phase_margin_deg <= 80:
                score += 1  # most topologies can achieve >60°

        if targets.power_w is not None:
            if targets.power_w >= meta.typical_power_w * 0.1:
                score += 1

        candidates.append((score, meta.complexity, name))

    # Sort: highest score first, then lowest complexity
    candidates.sort(key=lambda x: (-x[0], x[1]))

    if candidates and candidates[0][0] > 0:
        return candidates[0][2]

    # Default fallback
    return "5t_ota"
