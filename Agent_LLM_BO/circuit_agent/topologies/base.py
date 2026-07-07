"""Abstract base class for circuit topology generators."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from models import CircuitFiles, ParamSpace

if TYPE_CHECKING:
    from models import DesignTarget


@dataclass
class TopologyMeta:
    """Metadata describing a topology's characteristics and capabilities.

    Used by the topology selector to match topologies to user requirements.
    """

    name: str  # registry key, e.g. "5t_ota"
    display_name: str  # human-readable, e.g. "5-Transistor OTA"
    description: str

    # Approximate capability ranges (SI units)
    min_gain_db: float = 0.0
    max_gain_db: float = 140.0
    min_gbw_hz: float = 0.0
    max_gbw_hz: float = 1e12
    typical_power_w: float = 1e-3

    complexity: int = 1  # 1 (simple) to 5 (complex)
    escalation: str | None = None  # next topology if this one can't reach targets

    @property
    def min_bw_hz(self) -> float:
        """Backward-compatible alias; current AC metric is GBW/UGF."""
        return self.min_gbw_hz

    @property
    def max_bw_hz(self) -> float:
        """Backward-compatible alias; current AC metric is GBW/UGF."""
        return self.max_gbw_hz


class BaseTopology(ABC):
    """Abstract base for all circuit topology generators.

    Each concrete subclass encodes a fixed circuit structure (transistor
    connections hard-coded) with parameterized dimensions (W, L, C, R, I).

    The generated netlist files are syntactically correct by construction —
    no LLM hallucination risk.

    **gm/Id mode (optional):**  Subclasses may override ``get_gmid_spec()``
    to return a :class:`GmidTopologySpec`.  When they do, the optimizer
    automatically switches to gm/Id-based sizing — BO searches over gm_id,
    L, and branch currents instead of raw W/L.  The :class:`GmidSizer`
    (from ``gmid_lookup.py``) converts back to physical W/L before netlist
    rendering, so ``generate_circuit()`` always receives W/L values
    regardless of mode.
    """

    meta: TopologyMeta  # set by each subclass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_circuit_files(
        self, params: dict[str, float] | None = None
    ) -> CircuitFiles:
        """Produce CircuitFiles ready for the existing optimisation pipeline.

        Returns a CircuitFiles object indistinguishable from what
        LLMClient.generate_initial_netlist() used to produce.
        """
        circuit_content = self.generate_circuit(params)
        tb_content = self.generate_testbench(params)
        circuit_name = CircuitFiles.extract_subckt_name(circuit_content)
        return CircuitFiles(
            circuit_netlist=circuit_content,
            testbenches=[tb_content],
            circuit_name=circuit_name,
        )

    def write_project(
        self,
        project_dir: str | Path,
        targets: DesignTarget | None = None,
        params: dict[str, float] | None = None,
        original_requirement: str = "",
    ) -> Path:
        """Write all project files into a single directory, ready for main.py.

        Creates:
            <project_dir>/
            ├── <topo_name>.cir          # DUT subcircuit
            ├── tb_<topo_name>_ac.scs    # AC testbench (always)
            ├── tb_<topo_name>_sr.scs    # Slew-rate testbench
            ├── tb_<topo_name>_st.scs    # 0.1% settling-time testbench
            └── requirements.json        # Design targets

        Args:
            project_dir: Target directory (created if missing).
            targets: Design targets. If given, writes requirements.json.
            params: Override default sizing.  Uses topology defaults if None.
            original_requirement: Free-text description for traceability.

        Returns:
            Path to the created project directory.
        """
        import datetime

        out = Path(project_dir)
        out.mkdir(parents=True, exist_ok=True)

        generation_params = dict(params or {})
        if targets and targets.load_cap_f is not None:
            generation_params.setdefault("CL", targets.load_cap_f)
        cf = self.get_circuit_files(generation_params)

        # --- .cir ---
        cir_path = out / f"{self.meta.name}.cir"
        cir_path.write_text(cf.circuit_netlist, encoding="utf-8")

        # --- testbench files ---
        tb_files: list[Path] = []
        tb_suffixes = ["ac", "sr", "st", "dc", "noise"]
        for i, tb_content in enumerate(cf.testbenches):
            suffix = tb_suffixes[i] if i < len(tb_suffixes) else f"tb{i}"
            tb_path = out / f"tb_{self.meta.name}_{suffix}.scs"
            tb_path.write_text(tb_content, encoding="utf-8")
            tb_files.append(tb_path)

        # --- requirements.json ---
        if targets:
            req = targets.to_requirements_dict(
                original_text=original_requirement
            )
            req["topology_name"] = self.meta.name
            req["topology_display_name"] = self.meta.display_name
            req["generated_at"] = datetime.datetime.now().isoformat()
            req["default_params"] = self.get_default_params()

            req_path = out / "requirements.json"
            req_path.write_text(
                json.dumps(req, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )

        return out

    # ------------------------------------------------------------------
    # gm/Id support (optional — override to enable gm/Id sizing mode)
    # ------------------------------------------------------------------

    def get_gmid_spec(self, targets: DesignTarget | None = None):
        """Return a :class:`GmidTopologySpec` to enable gm/Id-based sizing.

        The default returns ``None`` — no gm/Id mode (backward compatible).
        Subclasses that want gm/Id sizing override this to return a spec.

        When this returns a non-None value, the optimizer automatically:
        1. Searches gm_id, L, and branch currents (instead of raw W/L).
        2. Uses :class:`GmidSizer` to convert back to physical W/L before
           netlist rendering.
        3. ``generate_circuit()`` still receives W/L values as before.
        """
        return None

    def required_model_roles(self) -> tuple[str, ...]:
        """Return PDK model roles required by this topology.

        The names correspond to :attr:`pdk_profiles.PDKProfile.model_names`.
        Subclasses using special devices should override this, e.g. folded
        cascode currently requires LVT devices.
        """
        return ("nmos", "pmos")

    def critical_operating_point_instances(self) -> set[str]:
        """Return MOS instances whose saturation strongly affects reward.

        Diagnostics may include bias generators and diode-connected devices.
        Those are useful warnings, but the first OP reward pass only strongly
        constrains the main signal path declared by each topology.
        """
        return set()

    # ------------------------------------------------------------------
    # Subclass contract
    # ------------------------------------------------------------------

    @abstractmethod
    def generate_circuit(self, params: dict[str, float] | None = None) -> str:
        """Generate the DUT .cir subcircuit netlist.

        Args:
            params: Override default parameter values.  If None, uses defaults.

        Returns:
            Complete Spectre-native .cir content (parameters + subckt + ends).
        """

    @abstractmethod
    def generate_testbench(
        self,
        params: dict[str, float] | None = None,
        analysis_type: str = "ac",
    ) -> str:
        """Generate the Spectre-native testbench .scs file.

        Args:
            params: Override default values (e.g., bias voltages).
            analysis_type: "ac" or "tran".

        Returns:
            Complete .sp testbench content referencing circuit.cir.
        """

    @abstractmethod
    def get_default_params(self) -> dict[str, float]:
        """Return default sizing values for this topology."""

    @abstractmethod
    def get_param_space(self) -> ParamSpace:
        """Return the search space (bounds and scales) for all tunable params."""
