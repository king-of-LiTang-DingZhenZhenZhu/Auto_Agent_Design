"""Topology Registry — hard-constrained circuit generators.

Each topology is a Python class that produces correct-by-construction
.cir and .sp netlist files. No LLM involvement in netlist generation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from topologies.references.bandgap_ptat import BandgapPTAT
from topologies.references.banba_sub1v_bandgap import BanbaSub1VBandgap
from topologies.base import BaseTopology, TopologyMeta
from topologies.regulators.capless_ldo import CaplessLDO
from topologies.regulators.dfc_capless_ldo import DFCCaplessLDO
from topologies.amplifiers.five_t_ota import FiveTOTA
from topologies.amplifiers.folded_cascode import FoldedCascodeOTA
from topologies.amplifiers.folded_cascode_two_stage import FoldedCascodeTwoStageOTA
from topologies.references.leung_mok_sub1v_bandgap import LeungMokSub1VBandgap
from topologies.amplifiers.mzc_two_stage_ota import MZCTwoStageOTA, PMOSInputMZCTwoStageOTA
from topologies.amplifiers.mnmc_three_stage import MNMCThreeStageOTA
from topologies.amplifiers.nmcnr_three_stage import NMCNRThreeStageOTA
from topologies.amplifiers.nmcf_three_stage import NMCFThreeStageOTA
from topologies.amplifiers.pmos_input_two_stage_ota import PMOSInputTwoStageOTA
from topologies.comparators.strongarm_latch import StrongARMLatch
from topologies.converters.sar_adc_functional_4bit import SARADCFunctional4Bit
from topologies.amplifiers.two_stage_ota import TwoStageOTA

if TYPE_CHECKING:
    from models import DesignTarget

# ---------------------------------------------------------------------------
# Registry: add new topologies here
# ---------------------------------------------------------------------------
TOPOLOGY_REGISTRY: dict[str, type[BaseTopology]] = {
    "5t_ota": FiveTOTA,
    "two_stage_ota": TwoStageOTA,
    "pmos_input_two_stage_ota": PMOSInputTwoStageOTA,
    "mzc_two_stage_ota": MZCTwoStageOTA,
    "pmos_input_mzc_two_stage_ota": PMOSInputMZCTwoStageOTA,
    "folded_cascode": FoldedCascodeOTA,
    "folded_cascode_two_stage": FoldedCascodeTwoStageOTA,
    "nmcnr_three_stage": NMCNRThreeStageOTA,
    "mnmc_three_stage": MNMCThreeStageOTA,
    "nmcf_three_stage": NMCFThreeStageOTA,
    "strongarm_latch": StrongARMLatch,
    "sar_adc_functional_4bit": SARADCFunctional4Bit,
    "bandgap_ptat": BandgapPTAT,
    "banba_sub1v_bandgap": BanbaSub1VBandgap,
    "leung_mok_sub1v_bandgap": LeungMokSub1VBandgap,
    "capless_ldo": CaplessLDO,
    "dfc_capless_ldo": DFCCaplessLDO,
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
    if "strongarm" in topology_hint or (
        "comparator" in topology_hint and "latch" in topology_hint
    ):
        return "strongarm_latch"
    if (
        "leung" in topology_hint
        or "mok" in topology_hint
        or "15-ppm" in topology_hint
        or "15 ppm" in topology_hint
        or "without requiring low threshold" in topology_hint
        or "603 mv" in topology_hint
    ) and "bandgap" in topology_hint:
        return "leung_mok_sub1v_bandgap"
    if (
        "banba" in topology_hint
        or "sub-1-v" in topology_hint
        or "sub1v" in topology_hint
    ):
        return "banba_sub1v_bandgap"
    if "bandgap" in topology_hint or "ptat" in topology_hint:
        return "bandgap_ptat"
    if "dfc" in topology_hint and (
        "ldo" in topology_hint or "low dropout" in topology_hint
    ):
        return "dfc_capless_ldo"
    if "ldo" in topology_hint or "low dropout" in topology_hint:
        return "capless_ldo"
    if "nmcnr" in topology_hint or (
        "nested miller" in topology_hint and "nulling resistor" in topology_hint
    ):
        return "nmcnr_three_stage"
    if "mnmc" in topology_hint or "multipath nested miller" in topology_hint:
        return "mnmc_three_stage"
    if "nmcf" in topology_hint:
        return "nmcf_three_stage"
    if (
        "mzc" in topology_hint
        or "feedforward" in topology_hint
        or "fts" in topology_hint
    ):
        if "pmos" in topology_hint:
            return "pmos_input_mzc_two_stage_ota"
        return "mzc_two_stage_ota"

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
        if name in {
            "bandgap_ptat", "banba_sub1v_bandgap", "leung_mok_sub1v_bandgap",
            "strongarm_latch",
            "sar_adc_functional_4bit",
            "capless_ldo", "dfc_capless_ldo",
        }:
            continue
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
