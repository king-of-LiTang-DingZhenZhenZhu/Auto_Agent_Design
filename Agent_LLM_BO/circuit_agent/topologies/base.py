"""Abstract base class for circuit topology generators."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from models import CircuitFiles, ParamSpace


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
    max_gain_db: float = 120.0
    min_bw_hz: float = 0.0
    max_bw_hz: float = 1e12
    typical_power_w: float = 1e-3

    complexity: int = 1  # 1 (simple) to 5 (complex)
    escalation: str | None = None  # next topology if this one can't reach targets


class BaseTopology(ABC):
    """Abstract base for all circuit topology generators.

    Each concrete subclass encodes a fixed circuit structure (transistor
    connections hard-coded) with parameterized dimensions (W, L, C, R, I).

    The generated netlist files are syntactically correct by construction —
    no LLM hallucination risk.
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

    # ------------------------------------------------------------------
    # Subclass contract
    # ------------------------------------------------------------------

    @abstractmethod
    def generate_circuit(self, params: dict[str, float] | None = None) -> str:
        """Generate the DUT .cir subcircuit netlist.

        Args:
            params: Override default parameter values.  If None, uses defaults.

        Returns:
            Complete .cir file content (headers + .param + .subckt + .ends).
        """

    @abstractmethod
    def generate_testbench(
        self,
        params: dict[str, float] | None = None,
        analysis_type: str = "ac",
    ) -> str:
        """Generate the testbench .sp file.

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
