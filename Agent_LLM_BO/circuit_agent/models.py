"""Data models for Circuit Agent."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

MAX_NF_PER_DEVICE = 32


def split_width(
    total_width: float,
    max_per_finger: float,
    max_nf: int = MAX_NF_PER_DEVICE,
) -> tuple[float, int, int]:
    """Split total MOS width into per-finger W, nf, and m.

    The first 32-way split uses ``nf``. Wider devices use ``m`` as a
    parallel multiplier while keeping ``nf <= max_nf``.
    """
    if total_width <= 0:
        raise ValueError(f"total_width must be positive, got {total_width}")
    if max_per_finger <= 0:
        raise ValueError(
            f"max_per_finger must be positive, got {max_per_finger}"
        )

    nf_needed = max(1, int(math.ceil(total_width / max_per_finger)))
    if nf_needed <= max_nf:
        nf = nf_needed
        m = 1
    else:
        m = int(math.ceil(nf_needed / max_nf))
        nf = int(math.ceil(nf_needed / m))
    w_per_finger = total_width / (nf * m)
    return w_per_finger, nf, m


# ======================================================================
# gm/Id-based transistor sizing models (universal across all topologies)
# ======================================================================


@dataclass
class TransistorSpec:
    """Describes one transistor (or group) for gm/Id-based sizing.

    This is the core abstraction that makes gm/Id sizing universal across
    all circuit topologies.  Each topology declares its transistors by
    functional role, and the :class:`GmidSizer` computes W automatically
    from the gm/Id lookup tables.

    Attributes:
        role: Functional name, e.g. ``"diff_pair_pmos"``.
        w_param: SPICE ``.param`` name for total width, e.g. ``"Wdiffp"``.
        l_param: SPICE ``.param`` name for length, e.g. ``"Ldiffp"``.
        model: SPICE model name (``"nch_mac"``, ``"pch_mac"``, …).
        current_source: Name of the branch current this transistor draws from.
        current_fraction: Fraction of ``current_source`` carried by this
            transistor (0.5 for each side of a diff pair).
        gm_id_low: Lower BO search bound for gm/Id (strong inversion).
        gm_id_high: Upper BO search bound for gm/Id (weak inversion).
        gm_id_default: Initial gm/Id value.
        L_low: Lower BO bound for channel length (m).
        L_high: Upper BO bound for channel length (m).
        L_default: Initial channel length (m).
        Vds_estimate: Estimated Vds for gm/Id lookup (V).
        Vbs: Bulk-source voltage for lookup (default 0).
        max_per_finger: Max W per finger — used by finger splitting.
        multiplicity: How many identical instances share this sizing
            (e.g. 2 for diff pair, 1 for tail).
    """

    role: str
    w_param: str
    l_param: str
    model: str = "nch_mac"
    current_source: str = ""
    current_fraction: float = 1.0

    # gm/Id design ranges
    gm_id_low: float = 5.0
    gm_id_high: float = 25.0
    gm_id_default: float = 12.0
    L_low: float = 30e-9
    L_high: float = 900e-9
    L_default: float = 200e-9

    # Operating point for lookup
    Vds_estimate: float = 0.3
    Vbs: float = 0.0

    # Physical constraints
    max_per_finger: float = 2.6e-6
    multiplicity: int = 1


@dataclass
class BranchCurrentSpec:
    """Defines an independent branch current that BO can tune.

    Attributes:
        name: Parameter name, e.g. ``"I_tail"``.
        low: Lower BO bound (A).
        high: Upper BO bound (A).
        default: Default / initial value (A).
    """

    name: str
    low: float = 1e-6
    high: float = 500e-6
    default: float = 20e-6


@dataclass
class DerivedGateBiasSpec:
    """Gate bias derived from a transistor operating point in the lookup table."""

    role: str
    param_name: str
    supply_voltage: float
    device_type: str = "pmos"
    low: float = 0.0
    high: float | None = None

    def resolve_gate_voltage(self, vgs: float) -> float:
        """Convert a source-referenced VGS/VSG magnitude to gate voltage."""
        vgs_magnitude = abs(vgs)
        if self.device_type.lower() == "nmos":
            return self.supply_voltage + vgs_magnitude
        if self.device_type.lower() == "pmos":
            return self.supply_voltage - vgs_magnitude
        raise ValueError(
            f"Unsupported device_type '{self.device_type}' for "
            f"derived bias {self.param_name}"
        )


@dataclass
class CurrentMirrorRatioSpec:
    """Width/current ratio relation for gm/Id-sized current mirrors."""

    reference_role: str
    output_role: str
    ratio_param: str
    ratio_low: int = 1
    ratio_high: int = 8
    ratio_default: int = 4
    share_length: bool = True
    derived_current_name: str | None = None


@dataclass
class GmidTopologySpec:
    """Complete gm/Id specification for one circuit topology.

    Separates the design space into:

    * **Branch currents** — independent DOFs the BO searches.
    * **Transistors** — each maps to a branch current (or fraction thereof)
      and defines gm/Id + L search bounds.
    * **Pass-through params** — traditional physical parameters (Cc, Rz, …)
      that are still searched directly, not through gm/Id sizing.

    Usage in a topology subclass::

        def get_gmid_spec(self) -> GmidTopologySpec | None:
            return GmidTopologySpec(
                branch_currents=[BranchCurrentSpec(name="I_tail", ...)],
                transistors=[
                    TransistorSpec(role="diff_pair_pmos", w_param="Wdp",
                                   l_param="Ldp", model="pch_mac",
                                   current_source="I_tail",
                                   current_fraction=0.5, ...),
                    ...
                ],
                pass_through_params=[ParamDef(name="Cc", ...)],
            )

    ``build_param_space()`` converts this spec into an Optuna-compatible
    :class:`ParamSpace` for the BO loop.
    """

    transistors: list[TransistorSpec] = field(default_factory=list)
    branch_currents: list[BranchCurrentSpec] = field(default_factory=list)
    pass_through_params: list[ParamDef] = field(default_factory=list)
    derived_gate_biases: list[DerivedGateBiasSpec] = field(default_factory=list)
    current_mirrors: list[CurrentMirrorRatioSpec] = field(default_factory=list)
    derived_length_params: dict[str, str] = field(default_factory=dict)

    def build_param_space(self) -> "ParamSpace":
        """Convert the gm/Id spec into a :class:`ParamSpace` for BO.

        The resulting space contains one param per branch current, one
        ``gm_id_<role>`` + ``L_<role>`` per transistor spec, plus all
        pass-through params.
        """
        params: list[ParamDef] = []

        # Branch currents
        for bc in self.branch_currents:
            params.append(ParamDef(
                name=bc.name, low=bc.low, high=bc.high,
                log_scale=True, unit="A",
            ))

        # Transistor gm_id and L — deduplicate by (role, l_param)
        seen_L: set[str] = set()
        mirror_output_roles = {mirror.output_role for mirror in self.current_mirrors}
        for ts in self.transistors:
            if ts.role in mirror_output_roles:
                continue
            # gm_id param — always unique per role
            params.append(ParamDef(
                name=f"gm_id_{ts.role}",
                low=ts.gm_id_low, high=ts.gm_id_high,
                log_scale=False,  # gm_id is naturally on a linear scale
            ))
            # L param — deduplicate when multiple transistors share the
            # same l_param name (e.g. diff pair both use "Ldp")
            if (
                ts.l_param not in self.derived_length_params
                and ts.l_param not in seen_L
            ):
                seen_L.add(ts.l_param)
                params.append(ParamDef(
                    name=f"L_{ts.role}",
                    low=ts.L_low, high=ts.L_high,
                    log_scale=True, unit="m",
                ))

        for mirror in self.current_mirrors:
            params.append(ParamDef(
                name=mirror.ratio_param,
                low=mirror.ratio_low,
                high=mirror.ratio_high,
                log_scale=False,
                unit="x",
                value_type="int",
            ))

        # Pass-through
        params.extend(self.pass_through_params)

        return ParamSpace(params=params)


# ======================================================================
# Core data models
# ======================================================================


@dataclass
class DesignTarget:
    """User-specified performance targets for the circuit."""

    gain_db: float | None = None  # Minimum gain in dB
    # Backward-compatible storage name. The current AC extractor measures the
    # first 0 dB crossing, so this value represents GBW/UGF, not -3 dB BW.
    bandwidth_hz: float | None = None
    phase_margin_deg: float | None = None  # Minimum phase margin in degrees
    power_w: float | None = None  # Maximum power in Watts
    load_cap_f: float | None = None  # Load capacitance in Farads
    slew_rate_v_per_s: float | None = None  # Minimum slew rate in V/s
    settling_time_s: float | None = None  # Maximum settling time in seconds
    topology_hint: str = ""  # e.g., "5T OTA", "two-stage Miller"
    custom_specs: dict[str, Any] = field(default_factory=dict)

    @property
    def gbw_hz(self) -> float | None:
        """Canonical meaning of the legacy bandwidth_hz field."""
        return self.bandwidth_hz

    def is_satisfied(self, result: SimResult) -> tuple[bool, dict[str, bool]]:
        """Check if all targets are met. Returns (all_pass, per_metric_pass)."""
        status: dict[str, bool] = {}
        if self.gain_db is not None and result.gain_db is not None:
            status["gain_db"] = result.gain_db >= self.gain_db
        if self.bandwidth_hz is not None and result.bandwidth_hz is not None:
            status["bandwidth_hz"] = result.bandwidth_hz >= self.bandwidth_hz
        if self.phase_margin_deg is not None and result.phase_margin_deg is not None:
            status["phase_margin_deg"] = result.phase_margin_deg >= self.phase_margin_deg
        if self.power_w is not None and result.power_w is not None:
            status["power_w"] = result.power_w <= self.power_w
        if self.slew_rate_v_per_s is not None:
            status["slew_rate_v_per_s"] = (
                result.slew_rate_v_per_s is not None
                and result.slew_rate_v_per_s >= self.slew_rate_v_per_s
            )
        if self.settling_time_s is not None:
            status["settling_time_s"] = (
                result.settling_time_s is not None
                and result.settling_time_s <= self.settling_time_s
            )
        all_pass = all(status.values()) if status else False
        return all_pass, status

    def compute_gap(self, result: SimResult) -> dict[str, float | None]:
        """Compute the gap between simulation results and targets.

        Positive = target met (surplus), Negative = target not met (deficit).
        For power (lower-is-better), positive = under budget.
        Returns None if either target or result is unavailable.
        """
        gap: dict[str, float | None] = {}

        if self.gain_db is not None and result.gain_db is not None:
            gap["gain_db"] = result.gain_db - self.gain_db
        else:
            gap["gain_db"] = None

        if self.bandwidth_hz is not None and result.bandwidth_hz is not None:
            gap["bandwidth_hz"] = result.bandwidth_hz - self.bandwidth_hz
        else:
            gap["bandwidth_hz"] = None

        if self.phase_margin_deg is not None and result.phase_margin_deg is not None:
            gap["phase_margin_deg"] = result.phase_margin_deg - self.phase_margin_deg
        else:
            gap["phase_margin_deg"] = None

        if self.power_w is not None and result.power_w is not None:
            # Power: lower is better, gap = target - actual (positive = under budget)
            gap["power_w"] = self.power_w - result.power_w
        else:
            gap["power_w"] = None

        if self.slew_rate_v_per_s is not None and result.slew_rate_v_per_s is not None:
            gap["slew_rate_v_per_s"] = result.slew_rate_v_per_s - self.slew_rate_v_per_s
        else:
            gap["slew_rate_v_per_s"] = None

        if self.settling_time_s is not None and result.settling_time_s is not None:
            # Settling time: lower is better
            gap["settling_time_s"] = self.settling_time_s - result.settling_time_s
        else:
            gap["settling_time_s"] = None

        return gap

    def to_requirements_dict(self, original_text: str = "") -> dict:
        """Export as a requirements dict for persistence (requirements.json)."""
        return {
            "original_requirement": original_text,
            "targets": {
                "gain_db": self.gain_db,
                "bandwidth_hz": self.bandwidth_hz,
                "phase_margin_deg": self.phase_margin_deg,
                "power_w": self.power_w,
                "load_cap_f": self.load_cap_f,
                "slew_rate_v_per_s": self.slew_rate_v_per_s,
                "settling_time_s": self.settling_time_s,
            },
            "topology_hint": self.topology_hint,
        }

    def to_prompt_str(self) -> str:
        """Format targets as a string for LLM prompts."""
        lines = []
        if self.gain_db is not None:
            lines.append(f"- Gain >= {self.gain_db} dB")
        if self.bandwidth_hz is not None:
            lines.append(f"- GBW >= {_eng(self.bandwidth_hz)}Hz")
        if self.phase_margin_deg is not None:
            lines.append(f"- Phase Margin >= {self.phase_margin_deg} degrees")
        if self.power_w is not None:
            lines.append(f"- Power <= {_eng(self.power_w)}W")
        if self.load_cap_f is not None:
            lines.append(f"- Load Capacitance = {_eng(self.load_cap_f)}F")
        if self.slew_rate_v_per_s is not None:
            lines.append(f"- Slew Rate >= {_eng(self.slew_rate_v_per_s)}V/s")
        if self.settling_time_s is not None:
            lines.append(f"- Settling Time <= {_eng(self.settling_time_s)}s")
        if self.topology_hint:
            lines.append(f"- Topology: {self.topology_hint}")
        for k, v in self.custom_specs.items():
            lines.append(f"- {k}: {v}")
        return "\n".join(lines)


@dataclass
class ParamDef:
    """Definition of a single optimizable parameter."""

    name: str
    low: float
    high: float
    log_scale: bool = True  # Use log-scale for W, L, C, R
    unit: str = ""  # e.g., "u", "n", "p"
    max_per_finger: float | None = None  # If set, split into multiple fingers (e.g., W <= 2.6um)
    value_type: str = "float"  # "float" or "int"


@dataclass
class ParamSpace:
    """Search space definition for Bayesian Optimization."""

    params: list[ParamDef] = field(default_factory=list)

    def suggest_from_trial(self, trial) -> dict[str, float]:
        """Use Optuna trial to suggest parameter values."""
        values = {}
        for p in self.params:
            if p.value_type == "int":
                values[p.name] = trial.suggest_int(p.name, int(p.low), int(p.high))
            elif p.log_scale:
                values[p.name] = trial.suggest_float(p.name, p.low, p.high, log=True)
            else:
                values[p.name] = trial.suggest_float(p.name, p.low, p.high)
        return values

    def get_param_names(self) -> list[str]:
        return [p.name for p in self.params]

    def get_initial_params(self, netlist_content: str) -> dict[str, float]:
        """Extract initial parameter values from netlist .param declarations.

        Parses .param lines to find the designer's chosen starting values.
        Falls back to geometric/linear midpoint for parameters not found.
        """
        # Parse .param name=value pairs from the netlist
        netlist_params: dict[str, float] = {}
        for match in _iter_parameter_lines(netlist_content):
            line = match.group(1)
            for kv in re.finditer(r"(\w+)\s*=\s*(\S+)", line):
                name = kv.group(1)
                if name.upper() in ("NF", "M"):
                    continue
                try:
                    netlist_params[name] = _parse_spice_suffix(kv.group(2))
                except ValueError:
                    continue

        initial = {}
        for p in self.params:
            if p.name in netlist_params:
                value = min(max(netlist_params[p.name], p.low), p.high)
                initial[p.name] = int(round(value)) if p.value_type == "int" else value
            elif p.log_scale:
                value = math.exp((math.log(p.low) + math.log(p.high)) / 2)
                initial[p.name] = int(round(value)) if p.value_type == "int" else value
            else:
                value = (p.low + p.high) / 2
                initial[p.name] = int(round(value)) if p.value_type == "int" else value
        return initial

    def resolve_params(
        self,
        raw_params: dict[str, float],
        global_max_per_finger: float | None = None,
    ) -> dict[str, float]:
        """Split wide transistor parameters into (W_finger, nf, m) tuples.

        Example: {Wtail: 12u} -> {Wtail: 2.4u, nf_Wtail: 5, m_Wtail: 1}
                 very wide W  -> {Wtail: W/(nf*m), nf_Wtail <= 32, m_Wtail > 1}
        """
        resolved = dict(raw_params)
        for p in self.params:
            # Only split finger for width parameters that explicitly have max_per_finger.
            # Non-width params (Rz, Cc, Ibias, etc.) must NOT go through this path
            # or they would get nonsensical nf values (e.g. Rz=24k → nf=8e9, Rz=3uΩ).
            if p.max_per_finger is None:
                continue
            if p.name not in raw_params:
                continue
            total_w = raw_params[p.name]
            max_per_finger = (
                global_max_per_finger
                if global_max_per_finger is not None
                else p.max_per_finger
            )
            w_per_finger, nf, m = split_width(total_w, max_per_finger)
            resolved[p.name] = w_per_finger
            resolved[f"nf_{p.name}"] = nf
            resolved[f"m_{p.name}"] = m
        return resolved

    @classmethod
    def from_dict(cls, data: list[dict]) -> ParamSpace:
        """Create from a list of dicts (parsed from LLM JSON output)."""
        params = []
        for d in data:
            params.append(
                ParamDef(
                    name=d["name"],
                    low=float(d["low"]),
                    high=float(d["high"]),
                    log_scale=d.get("log_scale", True),
                    unit=d.get("unit", ""),
                    max_per_finger=d.get("max_per_finger"),
                    value_type=d.get("value_type", "float"),
                )
            )
        return cls(params=params)

    @classmethod
    def from_netlist(cls, content: str, max_per_finger: float = 2.6e-6) -> ParamSpace:
        """Auto-extract parameter search space from netlist .param declarations.

        
        Parses .param lines, guesses parameter type from naming convention,
        and assigns sensible default bounds. No separate params.json needed.
        """
        # Parse .param declarations: name=value pairs
        param_entries: list[tuple[str, float]] = []
        for match in _iter_parameter_lines(content):
            line = match.group(1)
            for kv in re.finditer(r"(\w+)\s*=\s*(\S+)", line):
                name = kv.group(1)
                if name.upper() in ("NF", "M"):
                    continue  # system-managed
                try:
                    value = _parse_spice_suffix(kv.group(2))
                    param_entries.append((name, value))
                except ValueError:
                    continue

        if not param_entries:
            raise ValueError(
                "No .param/parameters declarations found in netlist. "
                "Add parameter declarations for optimizable variables."
            )

        # Assign bounds based on naming convention
        params = []
        for name, initial in param_entries:
            if name.startswith("W") or name.upper().startswith("W"):
                # Transistor total width
                if _is_folded_bias_param(name):
                    low = 0.2e-6
                    high = 5e-6
                    finger_limit = 2.6e-6
                else:
                    low = max(0.1e-6, initial * 0.1)
                    high = max(192e-6, initial * 10)
                    finger_limit = max_per_finger
                params.append(ParamDef(
                    name=name, low=low, high=high,
                    log_scale=True, unit="m", max_per_finger=finger_limit,
                ))
            elif name.startswith("L") or name.upper().startswith("L"):
                # Transistor length — PDK minimum is ~108nm, use 120nm safe margin
                low = max(120e-9, initial * 0.5)
                high = min(max(900e-9, initial * 5), _length_upper_bound(name))
                low = min(low, high)
                params.append(ParamDef(
                    name=name, low=low, high=high,
                    log_scale=True, unit="m",
                ))
            elif name.startswith("V") or name.upper().startswith("V"):
                # Voltage bias/param — tight range around initial value
                # (VDD is only 0.9-1.1V, so bias voltages shouldn't exceed supply)
                low = max(0.05, initial * 0.5)
                high = min(1.5, initial * 1.5)
                low = min(low, high)
                params.append(ParamDef(
                    name=name, low=low, high=high,
                    log_scale=False, unit="V",
                ))
            elif name.startswith("C") or name.upper().startswith("C"):
                # Capacitor
                low = max(0.01e-12, initial * 0.01)
                high = max(10e-12, initial * 100)
                params.append(ParamDef(
                    name=name, low=low, high=high,
                    log_scale=True, unit="F",
                ))
            elif name.startswith("R") or name.upper().startswith("R"):
                # Resistor
                low = max(1, initial * 0.01)
                high = max(2000, initial * 100)
                params.append(ParamDef(
                    name=name, low=low, high=high,
                    log_scale=True, unit="Ohm",
                ))
            elif name.upper().startswith("I"):
                # Current bias
                low = max(1e-6, initial * 0.01)
                high = max(5e-3, initial * 100)
                params.append(ParamDef(
                    name=name, low=low, high=high,
                    log_scale=True, unit="A",
                ))
            else:
                # Generic: ±2 orders of magnitude around initial
                low = initial * 0.01
                high = initial * 100
                params.append(ParamDef(
                    name=name, low=low, high=high,
                    log_scale=True,
                ))

        return cls(params=params)


@dataclass
class SimResult:
    """Parsed simulation output."""

    gain_db: float | None = None
    # Legacy field name retained for results.json compatibility. It stores GBW.
    bandwidth_hz: float | None = None
    phase_margin_deg: float | None = None
    power_w: float | None = None
    unity_gain_freq_hz: float | None = None
    slew_rate_v_per_s: float | None = None     # Transient: min(SR+, SR-)
    slew_rate_positive_v_per_s: float | None = None
    slew_rate_negative_v_per_s: float | None = None
    settling_time_s: float | None = None        # Transient: settling time in seconds
    converged: bool = True
    error_message: str = ""
    raw_metrics: dict[str, float] = field(default_factory=dict)

    @staticmethod
    def merge(primary: "SimResult", extra: "SimResult") -> "SimResult":
        """Merge extra simulation metrics into primary result.

        Non-None fields from extra are copied into primary only when primary's
        field is None (primary metrics take precedence for overlapping fields).
        """
        merged = SimResult(
            gain_db=primary.gain_db,
            bandwidth_hz=primary.bandwidth_hz,
            phase_margin_deg=primary.phase_margin_deg,
            power_w=primary.power_w,
            unity_gain_freq_hz=primary.unity_gain_freq_hz,
            slew_rate_v_per_s=primary.slew_rate_v_per_s or extra.slew_rate_v_per_s,
            slew_rate_positive_v_per_s=(
                primary.slew_rate_positive_v_per_s
                or extra.slew_rate_positive_v_per_s
            ),
            slew_rate_negative_v_per_s=(
                primary.slew_rate_negative_v_per_s
                or extra.slew_rate_negative_v_per_s
            ),
            settling_time_s=primary.settling_time_s or extra.settling_time_s,
            converged=primary.converged,
            error_message=primary.error_message,
            raw_metrics={**extra.raw_metrics, **primary.raw_metrics},
        )
        return merged

    def to_result_dict(
        self, targets: DesignTarget | None = None, params: dict[str, float] | None = None
    ) -> dict:
        """Export simulation result as a structured dict for JSON output.

        If targets are provided, includes gap analysis and satisfaction status.
        If params are provided, includes the parameter values used.
        """
        result: dict[str, Any] = {
            "converged": self.converged,
            "metrics": {
                "gain_db": self.gain_db,
                "gbw_hz": self.bandwidth_hz,
                "bandwidth_hz": self.bandwidth_hz,
                "unity_gain_freq_hz": self.unity_gain_freq_hz,
                "phase_margin_deg": self.phase_margin_deg,
                "power_w": self.power_w,
                "slew_rate_v_per_s": self.slew_rate_v_per_s,
                "slew_rate_positive_v_per_s": self.slew_rate_positive_v_per_s,
                "slew_rate_negative_v_per_s": self.slew_rate_negative_v_per_s,
                "settling_time_s": self.settling_time_s,
            },
        }

        if not self.converged:
            result["error_message"] = self.error_message

        if params is not None:
            result["params"] = params

        if targets is not None:
            all_met, status = targets.is_satisfied(self)
            gap = targets.compute_gap(self)
            result["target_status"] = status
            result["gap"] = gap
            result["all_targets_met"] = all_met

        return result

    def to_summary_str(self) -> str:
        """Format results as a readable string."""
        lines = []
        if self.gain_db is not None:
            lines.append(f"Gain = {self.gain_db:.1f} dB")
        if self.bandwidth_hz is not None:
            lines.append(f"GBW = {_eng(self.bandwidth_hz)}Hz")
        if self.phase_margin_deg is not None:
            lines.append(f"PM = {self.phase_margin_deg:.1f} deg")
        if self.power_w is not None:
            lines.append(f"Power = {_eng(self.power_w)}W")
        if self.unity_gain_freq_hz is not None:
            lines.append(f"UGF = {_eng(self.unity_gain_freq_hz)}Hz")
        if self.slew_rate_v_per_s is not None:
            lines.append(f"SR = {_eng(self.slew_rate_v_per_s)}V/s")
        if self.slew_rate_positive_v_per_s is not None:
            lines.append(f"SR+ = {_eng(self.slew_rate_positive_v_per_s)}V/s")
        if self.slew_rate_negative_v_per_s is not None:
            lines.append(f"SR- = {_eng(self.slew_rate_negative_v_per_s)}V/s")
        if self.settling_time_s is not None:
            lines.append(f"t_settle = {_eng(self.settling_time_s)}s")
        if not self.converged:
            lines.append(f"[NOT CONVERGED] {self.error_message}")
        return " | ".join(lines)


@dataclass
class NetlistTemplate:
    """Parametrized SPICE netlist template."""

    template_content: str  # Full netlist with .param placeholders
    param_names: list[str] = field(default_factory=list)

    def render(
        self,
        params: dict[str, float],
        param_space: ParamSpace | None = None,
        max_width_per_finger: float | None = None,
        w_l_grid_step: float | None = None,
    ) -> str:
        """Substitute parameter values into the template.

        If param_space is provided, wide transistors are automatically split
        into multiple fingers: W_total > max_per_finger → W_finger × nf.

        Replaces .param lines: `.param W1 = 5u`
        Also injects nf values on transistor lines: `nf=1` → `nf=4`
        """
        # Resolve finger splitting.
        # Start with all params (so gm/Id-mode params not in param_space survive),
        # then overlay finger-split versions for params that ARE in param_space.
        resolved = dict(params)
        if param_space:
            resolved.update(param_space.resolve_params(params, max_width_per_finger))

        content = self.template_content

        # Phase 1: substitute .param/parameters values for physical parameters.
        # Only match parameter declaration lines to avoid corrupting instances.
        lines = content.split("\n")
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not (
                stripped.lower().startswith(".param")
                or stripped.lower().startswith("parameters")
            ):
                continue
            for name, value in resolved.items():
                if name.startswith(("nf_", "m_")):
                    continue
                if w_l_grid_step:
                    value = _quantize_wl(name, value, w_l_grid_step)
                pattern = rf"(\b{re.escape(name)}\s*=\s*)\S+"
                replacement = rf"\g<1>{_format_spice_value(value)}"
                line = re.sub(pattern, replacement, line, flags=re.IGNORECASE)
            lines[i] = line
        content = "\n".join(lines)

        # Phase 2: inject nf/m values on transistor lines.
        for name, value in resolved.items():
            if not name.startswith("nf_"):
                continue
            wname = name[3:]  # nf_Wtail → Wtail
            nf_int = int(value)

            # Also substitute .param nf_Wxx if present in template
            pattern = rf"(\b{re.escape(name)}\s*=\s*)\S+"
            content = re.sub(pattern, rf"\g<1>{nf_int}", content, flags=re.IGNORECASE)

            # Replace nf on HSPICE or Spectre transistor lines referencing W.
            line_pattern = rf"([wW]\s*=\s*'?{re.escape(wname)}'?.*?\bnf\s*=\s*)\S+"
            line_replacement = rf"\g<1>{nf_int}"
            content = re.sub(
                line_pattern, line_replacement, content, flags=re.IGNORECASE
            )

        for name, value in resolved.items():
            if not name.startswith("m_"):
                continue
            wname = name[2:]  # m_Wtail → Wtail
            m_int = int(value)

            # Substitute param m_Wxx if present in template.
            pattern = rf"(\b{re.escape(name)}\s*=\s*)\S+"
            content = re.sub(pattern, rf"\g<1>{m_int}", content, flags=re.IGNORECASE)

            # Add or replace m on MOS lines that reference this W parameter.
            updated_lines: list[str] = []
            for line in content.split("\n"):
                stripped = line.lstrip()
                if not stripped or not stripped[0].lower() == "m":
                    updated_lines.append(line)
                    continue
                if not re.search(
                    rf"\b[wW]\s*=\s*'?{re.escape(wname)}'?\b",
                    line,
                    flags=re.IGNORECASE,
                ):
                    updated_lines.append(line)
                    continue
                if re.search(r"\bm\s*=", line, flags=re.IGNORECASE):
                    line = re.sub(
                        r"(\bm\s*=\s*)\S+",
                        rf"\g<1>{m_int}",
                        line,
                        flags=re.IGNORECASE,
                    )
                else:
                    line = f"{line} m={m_int}"
                updated_lines.append(line)
            content = "\n".join(updated_lines)

        # Phase 3: resolve parameter references on transistor lines
        # Spectre (HSPICE mode) may not expand 'L2' to its .param value on
        # instance lines (L='L2'), so we replace references with literal values.
        # Patterns: W='Wtail' / W=Wtail / L='Ltail' / L=Ltail
        for name, value in resolved.items():
            if name.startswith(("nf_", "m_")):
                continue
            # Apply same W/L grid quantization as Phase 1 so that transistor-
            # line values are consistent with .param values and never round
            # down to zero (Spectre rejects L <= 0).
            if w_l_grid_step:
                value = _quantize_wl(name, value, w_l_grid_step)
            formatted = _format_spice_value(value)
            # Replace with-quotes form: W='Wtail' → W=3u / l='Ltail' → l=200n
            content = re.sub(
                rf"(\b[wWlL]\s*=\s*)'{re.escape(name)}'",
                rf"\g<1>{formatted}",
                content,
            )
            # Replace without-quotes form: W=Wtail → W=3u / l=Ltail → l=200n
            content = re.sub(
                rf"(\b[wWlL]\s*=\s*){re.escape(name)}\b",
                rf"\g<1>{formatted}",
                content,
            )

        return content

    @classmethod
    def from_netlist(cls, content: str) -> NetlistTemplate:
        """Parse a netlist to identify HSPICE or Spectre parameter names."""
        param_lines = [
            match.group(1) for match in _iter_parameter_lines(content)
        ]
        param_names = []
        for line in param_lines:
            names = re.findall(r"(\w+)\s*=", line)
            param_names.extend(names)
        return cls(template_content=content, param_names=param_names)


@dataclass
class CircuitFiles:
    """Holds the split circuit design: DUT netlist + testbench(es)."""

    circuit_netlist: str     # DUT subcircuit content (Spectre or HSPICE syntax)
    testbenches: list[str]   # Testbench contents (include, stimulus, analyses, save)
    circuit_name: str        # Subcircuit name extracted from subckt line

    @property
    def testbench(self) -> str | None:
        """Primary testbench (first in list). For backward compatibility."""
        return self.testbenches[0] if self.testbenches else None

    @staticmethod
    def extract_subckt_name(circuit_content: str) -> str:
        """Extract the subcircuit name from a Spectre/HSPICE declaration."""
        m = re.search(r'^\s*\.?subckt\s+(\w+)', circuit_content, re.IGNORECASE | re.MULTILINE)
        return m.group(1) if m else "dut"


@dataclass
class IterationRecord:
    """Record of a single optimization iteration.

    When gm/Id mode is active, ``params`` holds the gm/Id-space parameters
    (gm_id_*, L_*, I_*) and ``physical_params`` (if set) holds the resolved
    W/L physical parameters actually rendered into the netlist.
    """

    iteration: int
    params: dict[str, float]
    result: SimResult
    reward: float
    physical_params: dict[str, float] | None = None


@dataclass
class OptimizationState:
    """Tracks the entire optimization progress."""

    targets: DesignTarget
    param_space: ParamSpace
    history: list[IterationRecord] = field(default_factory=list)
    best_iteration: int = -1
    best_reward: float = float("-inf")
    total_iterations: int = 0
    topology_changes: int = 0
    stop_reason: str = ""

    @property
    def best_record(self) -> IterationRecord | None:
        if self.best_iteration < 0 or not self.history:
            return None
        for rec in self.history:
            if rec.iteration == self.best_iteration:
                return rec
        return None

    def update(self, record: IterationRecord) -> None:
        self.history.append(record)
        self.total_iterations += 1
        if record.reward > self.best_reward:
            self.best_reward = record.reward
            self.best_iteration = record.iteration


# --- Utility functions ---

def _eng(value: float) -> str:
    """Convert a float to engineering notation string."""
    if value == 0:
        return "0"
    abs_val = abs(value)
    prefixes = [
        (1e12, "T"), (1e9, "G"), (1e6, "M"), (1e3, "k"),
        (1, ""), (1e-3, "m"), (1e-6, "u"), (1e-9, "n"),
        (1e-12, "p"), (1e-15, "f"),
    ]
    for threshold, prefix in prefixes:
        if abs_val >= threshold:
            scaled = value / threshold
            if scaled == int(scaled):
                return f"{int(scaled)}{prefix}"
            return f"{scaled:.2g}{prefix}"
    return f"{value:.2e}"


def _format_spice_value(value: float) -> str:
    """Format a float as a SPICE value with appropriate suffix."""
    if value == 0:
        return "0"
    abs_val = abs(value)
    suffixes = [
        (1e-15, "f"), (1e-12, "p"), (1e-9, "n"), (1e-6, "u"),
        (1e-3, "m"), (1, ""), (1e3, "k"), (1e6, "meg"),
    ]
    for threshold, suffix in suffixes:
        if abs_val < threshold * 1000:
            scaled = value / threshold
            if scaled == int(scaled):
                return f"{int(scaled)}{suffix}"
            # Use up to 3 significant figures
            return f"{scaled:.3g}{suffix}"
    return f"{value:.3e}"


def _quantize_wl(name: str, value: float, step: float) -> float:
    """如果参数名以 W 或 L 开头，按 step 取整到最近整数倍，且不低于 step。"""
    if name.startswith(("W", "L")):
        return max(step, round(value / step) * step)
    return value


def _length_upper_bound(name: str) -> float:
    """Return the hard upper bound for transistor length parameters."""
    if _is_folded_bias_param(name):
        return 500e-9
    return 900e-9


def _is_folded_bias_param(name: str) -> bool:
    """Detect folded-cascode internal bias generator W/L parameters."""
    lname = name.lower()
    return lname.startswith(("wbp_", "wbn_", "lbp_", "lbn_"))


def _parse_spice_suffix(s: str) -> float:
    """Parse a SPICE value string with suffix to float.

    Examples: '5u' -> 5e-6, '180n' -> 180e-9, '10k' -> 10e3
    """
    s = s.strip().lower()
    suffixes = {
        "t": 1e12, "g": 1e9, "meg": 1e6, "k": 1e3,
        "m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15,
    }
    for suffix, multiplier in sorted(suffixes.items(), key=lambda x: -len(x[0])):
        if s.endswith(suffix):
            num_part = s[:-len(suffix)]
            return float(num_part) * multiplier
    return float(s)


def _iter_parameter_lines(content: str):
    """Yield .param and Spectre parameters declaration matches."""
    return re.finditer(
        r"^\s*(?:\.param|parameters)\s+(.+)$",
        content,
        re.IGNORECASE | re.MULTILINE,
    )
