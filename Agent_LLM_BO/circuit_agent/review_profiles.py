"""Topology/domain-specific Agent Review instructions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewProfile:
    domain: str
    audit_repair_task: str
    failure_task: str


_OPAMP_PROFILE = ReviewProfile(
    domain="opamp",
    audit_repair_task="""BO met its nominal targets, but Design Audit blocked this opamp. Repair the audited problem:
- inspect critical DC operating points, saturation margins, and branch currents;
- judge whether transistor dimensions, multiplicities, compensation, and current ratios are physically reasonable;
- identify parameters near bounds, excessive area/current, hidden overdesign, and safe power/area optimization opportunities;
- prefer `decision=accept` with no candidate when no evidence-backed improvement is needed.""",
    failure_task="""BO has not met all nominal opamp targets. Diagnose before proposing edits:
- identify the dominant Gain/GBW/PM/SR/settling/power gap and conflicting secondary gaps;
- inspect DC operating points before treating the problem as pure sizing;
- apply only formulas and pole/zero assumptions valid for this topology;
- propose conservative candidates, a parameter-space restart, or a topology change.""",
)


_BANDGAP_PROFILE = ReviewProfile(
    domain="bandgap",
    audit_repair_task="""BO met its nominal targets, but Design Audit blocked this bandgap. Repair it as a bandgap, not as an opamp:
- verify startup escapes the zero-current state and settles with margin;
- inspect mirror, startup, BJT, resistor-current, and child-opamp DC operating points;
- review Vref accuracy, tempco, temperature curvature, PSRR, line regulation, and power together;
- judge resistor geometry/current density, PMOS mirror dimensions, and frozen child-opamp headroom;
- prefer `decision=accept` when no evidence-backed area, current, or robustness improvement exists.""",
    failure_task="""BO has not met all nominal bandgap targets. Diagnose in bandgap order:
- check startup and the zero-current state before interpreting other metrics;
- check DC operating regions, branch-current balance, BJT operation, and child-opamp headroom;
- separate room-temperature Vref error from tempco and curvature errors;
- diagnose PSRR and line regulation without applying opamp Gain/GBW fallback rules to parent parameters;
- use resistor-ratio, current-level, mirror-length, topology knowledge, and empirical parameter effects only when their assumptions are supported;
- request a local perturbation, child-opamp rerun, search-space restart, or architecture change when direction is ambiguous.""",
)

_LDO_PROFILE = ReviewProfile(
    domain="ldo",
    audit_repair_task="""BO met its nominal targets, but Design Audit blocked this LDO. Repair it as a closed-loop power circuit:
- inspect PMOS pass-device saturation/triode operation, gate headroom, full-load current density, and zero-load bias currents;
- verify the zero-load STB result, loop GBW/PM, output accuracy, load regulation, near-DC PSR, and both load-step polarities;
- judge pass-device area, feedback-divider current, bleed current, internal compensation, and error-amplifier drive capability;
- check that performance is not obtained by relying on an unverified external capacitor or an unsafe IO device voltage;
- prefer `decision=accept` when no evidence-backed robustness, quiescent-current, or area improvement exists.""",
    failure_task="""BO has not met all nominal LDO targets. Diagnose in power-loop order:
- confirm the active 1.8 V IO PDK domain, DC output level, pass-device gate range, and full-load current capability;
- inspect critical DC operating points before changing compensation;
- separate DC loop-gain/load-regulation/PSR gaps from zero-load stability and load-transient gaps;
- use STB loop gain and pole/zero evidence when changing Rgate, Ccomp, Cff, bleed, or pass-device size;
- propose a conservative local candidate, child error-amplifier rerun, search-space restart, or LDO architecture change.""",
)

_COMPARATOR_PROFILE = ReviewProfile(
    domain="comparator",
    audit_repair_task="""BO met its nominal targets, but Design Audit blocked this dynamic comparator:
- verify reset, amplification, regeneration, and output-restoration phases from transient waveforms;
- inspect decision polarity, clock-to-decision delay, energy per decision, output loading, and peak supply current;
- treat reset-state DC operating regions as non-evidence for dynamic latch correctness;
- review input-pair matching, latch balance, clock edge assumptions, and kickback risk before resizing.""",
    failure_task="""BO has not met all nominal dynamic-comparator targets:
- confirm both input polarities resolve correctly before optimizing speed or energy;
- separate insufficient input-pair gain from slow regeneration and weak precharge;
- use positive/negative delay, decision margin, energy, clock, and load evidence together;
- request transient diagnostics or a local perturbation when offset, kickback, or metastability dominates.""",
)


def get_review_profile(topology_name: str) -> ReviewProfile:
    if topology_name == "strongarm_latch":
        return _COMPARATOR_PROFILE
    if topology_name in {
        "bandgap_ptat",
        "banba_sub1v_bandgap",
        "leung_mok_sub1v_bandgap",
    }:
        return _BANDGAP_PROFILE
    if topology_name in {"capless_ldo", "dfc_capless_ldo"}:
        return _LDO_PROFILE
    return _OPAMP_PROFILE
