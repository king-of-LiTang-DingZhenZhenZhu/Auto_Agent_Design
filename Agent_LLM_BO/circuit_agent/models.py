"""Data models for Circuit Agent."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DesignTarget:
    """User-specified performance targets for the circuit."""

    gain_db: float | None = None  # Minimum gain in dB
    bandwidth_hz: float | None = None  # Minimum bandwidth in Hz
    phase_margin_deg: float | None = None  # Minimum phase margin in degrees
    power_w: float | None = None  # Maximum power in Watts
    load_cap_f: float | None = None  # Load capacitance in Farads
    topology_hint: str = ""  # e.g., "5T OTA", "two-stage Miller"
    custom_specs: dict[str, Any] = field(default_factory=dict)

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
            },
            "topology_hint": self.topology_hint,
        }

    def to_prompt_str(self) -> str:
        """Format targets as a string for LLM prompts."""
        lines = []
        if self.gain_db is not None:
            lines.append(f"- Gain >= {self.gain_db} dB")
        if self.bandwidth_hz is not None:
            lines.append(f"- Bandwidth >= {_eng(self.bandwidth_hz)}Hz")
        if self.phase_margin_deg is not None:
            lines.append(f"- Phase Margin >= {self.phase_margin_deg} degrees")
        if self.power_w is not None:
            lines.append(f"- Power <= {_eng(self.power_w)}W")
        if self.load_cap_f is not None:
            lines.append(f"- Load Capacitance = {_eng(self.load_cap_f)}F")
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
    max_per_finger: float | None = None  # If set, split into multiple fingers (e.g., W <= 3um)


@dataclass
class ParamSpace:
    """Search space definition for Bayesian Optimization."""

    params: list[ParamDef] = field(default_factory=list)

    def suggest_from_trial(self, trial) -> dict[str, float]:
        """Use Optuna trial to suggest parameter values."""
        values = {}
        for p in self.params:
            if p.log_scale:
                values[p.name] = trial.suggest_float(p.name, p.low, p.high, log=True)
            else:
                values[p.name] = trial.suggest_float(p.name, p.low, p.high)
        return values

    def get_param_names(self) -> list[str]:
        return [p.name for p in self.params]

    def resolve_params(
        self,
        raw_params: dict[str, float],
        global_max_per_finger: float | None = None,
    ) -> dict[str, float]:
        """Split wide transistor parameters into (W_finger, nf) pairs.

        Example: {Wtail: 12u} -> {Wtail: 3u, nf_Wtail: 4}
                 {Wdp: 2u}    -> {Wdp: 2u, nf_Wdp: 1}
        """
        resolved = dict(raw_params)
        for p in self.params:
            limit = p.max_per_finger or global_max_per_finger
            if limit is None:
                continue
            if p.name not in raw_params:
                continue
            total_w = raw_params[p.name]
            if total_w > limit:
                nf = int(math.ceil(total_w / limit))
                resolved[p.name] = total_w / nf
            else:
                nf = 1
            resolved[f"nf_{p.name}"] = nf
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
                )
            )
        return cls(params=params)


@dataclass
class SimResult:
    """Parsed simulation output."""

    gain_db: float | None = None
    bandwidth_hz: float | None = None
    phase_margin_deg: float | None = None
    power_w: float | None = None
    unity_gain_freq_hz: float | None = None
    converged: bool = True
    error_message: str = ""
    raw_metrics: dict[str, float] = field(default_factory=dict)

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
                "bandwidth_hz": self.bandwidth_hz,
                "unity_gain_freq_hz": self.unity_gain_freq_hz,
                "phase_margin_deg": self.phase_margin_deg,
                "power_w": self.power_w,
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
            lines.append(f"BW = {_eng(self.bandwidth_hz)}Hz")
        if self.phase_margin_deg is not None:
            lines.append(f"PM = {self.phase_margin_deg:.1f} deg")
        if self.power_w is not None:
            lines.append(f"Power = {_eng(self.power_w)}W")
        if self.unity_gain_freq_hz is not None:
            lines.append(f"UGF = {_eng(self.unity_gain_freq_hz)}Hz")
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
    ) -> str:
        """Substitute parameter values into the template.

        If param_space is provided, wide transistors are automatically split
        into multiple fingers: W_total > max_per_finger → W_finger × nf.

        Replaces .param lines: `.param W1 = 5u`
        Also injects nf values on transistor lines: `nf=1` → `nf=4`
        """
        # Resolve finger splitting
        resolved = param_space.resolve_params(params, max_width_per_finger) if param_space else dict(params)

        content = self.template_content

        # Phase 1: substitute .param values for non-nf parameters
        # Use \b word boundary to handle multiple params on same line:
        #   .param Wtail=2u Ltail=200n Wdp=2u
        for name, value in resolved.items():
            if name.startswith("nf_"):
                continue
            pattern = rf"(\b{re.escape(name)}\s*=\s*)\S+"
            replacement = rf"\g<1>{_format_spice_value(value)}"
            content = re.sub(pattern, replacement, content, flags=re.IGNORECASE)

        # Phase 2: inject nf values on transistor lines
        for name, value in resolved.items():
            if not name.startswith("nf_"):
                continue
            wname = name[3:]  # nf_Wtail → Wtail
            nf_int = int(value)

            # Also substitute .param nf_Wxx if present in template
            pattern = rf"(\b{re.escape(name)}\s*=\s*)\S+"
            content = re.sub(pattern, rf"\g<1>{nf_int}", content, flags=re.IGNORECASE)

            # Replace nf on transistor lines that reference this width parameter
            # Handles both HSPICE (w='Wtail') and Spectre (w=Wtail) formats
            line_pattern = rf"(w='?{re.escape(wname)}'?.*?nf=)\S+"
            line_replacement = rf"\g<1>{nf_int}"
            content = re.sub(line_pattern, line_replacement, content)

        return content

    @classmethod
    def from_netlist(cls, content: str) -> NetlistTemplate:
        """Parse a netlist to identify .param parameter names."""
        param_lines = re.findall(r"\.param\s+(.+)", content, re.IGNORECASE)
        param_names = []
        for line in param_lines:
            names = re.findall(r"(\w+)\s*=", line)
            param_names.extend(names)
        return cls(template_content=content, param_names=param_names)


@dataclass
class CircuitFiles:
    """Holds the split circuit design: DUT netlist + testbench."""

    circuit_netlist: str   # DUT subcircuit content (.subckt with .param)
    testbench: str         # Testbench content (.include, stimulus, analysis, .meas)
    circuit_name: str      # Subcircuit name extracted from .subckt line

    @staticmethod
    def extract_subckt_name(circuit_content: str) -> str:
        """Extract the subcircuit name from a .subckt declaration."""
        m = re.search(r'\.subckt\s+(\w+)', circuit_content, re.IGNORECASE)
        return m.group(1) if m else "dut"


@dataclass
class IterationRecord:
    """Record of a single optimization iteration."""

    iteration: int
    params: dict[str, float]
    result: SimResult
    reward: float


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
