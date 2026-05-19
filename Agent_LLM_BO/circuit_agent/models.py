"""Data models for Circuit Agent."""

from __future__ import annotations

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

    template_content: str  # Full netlist with {PARAM} placeholders
    param_names: list[str] = field(default_factory=list)

    def render(self, params: dict[str, float]) -> str:
        """Substitute parameter values into the template.

        Replaces .param lines: `.param W1 = {W1}` -> `.param W1 = 5u`
        """
        content = self.template_content
        for name, value in params.items():
            # Replace in .param definition lines
            pattern = rf"(\.param\s+{re.escape(name)}\s*=\s*)\S+"
            replacement = rf"\g<1>{_format_spice_value(value)}"
            content = re.sub(pattern, replacement, content, flags=re.IGNORECASE)
        return content

    @classmethod
    def from_netlist(cls, content: str) -> NetlistTemplate:
        """Parse a netlist to identify .param parameter names."""
        param_names = re.findall(
            r"\.param\s+(\w+)\s*=", content, re.IGNORECASE
        )
        return cls(template_content=content, param_names=param_names)


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
