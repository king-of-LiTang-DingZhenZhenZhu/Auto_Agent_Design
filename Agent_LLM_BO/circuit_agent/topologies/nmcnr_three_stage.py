"""NMCNR three-stage OTA based on Leung and Mok Fig. 1(e).

The serial amplifier is a conventional three-stage nested-Miller OTA.  The
outer capacitor connects the first-stage output to ``vout``.  The inner
compensation path connects ``s2_out`` to ``vout`` through series ``Cc2`` and
``Rm``; there is no feedforward transconductance stage.

Reference: K. N. Leung and P. K. T. Mok, "Analysis of Multistage
Amplifier-Frequency Compensation," IEEE TCAS-I, vol. 48, no. 9, 2001,
Fig. 1(e) and (19)-(22).
"""

from __future__ import annotations

from models import ParamDef, ParamSpace, format_spice_value
from topologies.base import PassiveImplementation, TopologyMeta
from topologies.mnmc_three_stage import MNMCThreeStageOTA


_FTS_PARAMS = {
    "Wtailf1", "Ltailf1", "Wgmf1", "Lgmf1", "Wloadf1", "Lloadf1",
}
_FTS_ROLES = {
    "feedforward_tail_pmos",
    "feedforward_diff_pmos",
    "feedforward_load_nmos",
}
_FTS_INSTANCES = {
    "Mtailf1", "Mgmf1a", "Mgmf1b", "Mloadf1a", "Mloadf1b",
}


def _nmcnr_defaults() -> dict[str, float]:
    defaults = {
        name: value
        for name, value in MNMCThreeStageOTA.DEFAULT_PARAMS.items()
        if name not in _FTS_PARAMS
    }
    defaults.update({"Cc1": 6e-12, "Cc2": 2.5e-12, "Rm": 1.7e3})
    return defaults


class NMCNRThreeStageOTA(MNMCThreeStageOTA):
    PASSIVE_IMPLEMENTATIONS = MNMCThreeStageOTA.PASSIVE_IMPLEMENTATIONS + (
        PassiveImplementation("RmDev", "resistor", "compensation_resistor"),
    )
    """Three-stage nested-Miller OTA with a series nulling resistor."""

    meta = TopologyMeta(
        name="nmcnr_three_stage",
        display_name="NMCNR Three-Stage OTA",
        description=(
            "Conventional three-stage nested-Miller OTA with a nulling "
            "resistor in series with the inner compensation capacitor."
        ),
        min_gain_db=75,
        max_gain_db=115,
        min_gbw_hz=5e5,
        max_gbw_hz=7e8,
        typical_power_w=4e-3,
        complexity=4,
        escalation="mnmc_three_stage",
    )

    DEFAULT_PARAMS = _nmcnr_defaults()

    def critical_operating_point_instances(self) -> set[str]:
        return super().critical_operating_point_instances() - _FTS_INSTANCES

    def generate_circuit(self, params: dict[str, float] | None = None) -> str:
        p = self._merge_params_with_preset(params)
        base_params = dict(p)
        base_params.update(
            {
                name: MNMCThreeStageOTA.DEFAULT_PARAMS[name]
                for name in _FTS_PARAMS
            }
        )
        circuit = MNMCThreeStageOTA().generate_circuit(base_params)

        for line_prefix in (
            "parameters Wtailf1=",
            "parameters Wloadf1=",
        ):
            circuit = _remove_parameter_line(circuit, line_prefix)

        feedforward_start = circuit.index("// Feedforward stage (-Avf1):")
        stage3_start = circuit.index("// Stage 3 (-Av3):", feedforward_start)
        circuit = circuit[:feedforward_start] + circuit[stage3_start:]

        old_compensation = """\
// Nested Miller compensation from Fig. 1(f)
Cc1 (s1_out vout) capacitor c=Cc1
Cc2 (s2_out vout) capacitor c=Cc2"""
        new_compensation = """\
// NMCNR compensation from Fig. 1(e): Cc2 and Rm are in series
Cc1 (s1_out vout) capacitor c=Cc1
Cc2 (s2_out n_rm) capacitor c=Cc2
RmDev (n_rm vout) resistor r=Rm"""
        if old_compensation not in circuit:
            raise ValueError("MNMC compensation block was not found")
        circuit = circuit.replace(old_compensation, new_compensation, 1)

        old_parameters = f"parameters Cc1={_fmt(p['Cc1'])} Cc2={_fmt(p['Cc2'])}"
        new_parameters = old_parameters + f" Rm={_fmt(p['Rm'])}"
        if old_parameters not in circuit:
            raise ValueError("Compensation parameter line was not found")
        circuit = circuit.replace(old_parameters, new_parameters, 1)
        return circuit.replace("mnmc_three_stage", self.meta.name).replace(
            "MNMC", "NMCNR"
        )

    def generate_testbench(
        self,
        params: dict[str, float] | None = None,
        analysis_type: str = "ac",
    ) -> str:
        return super().generate_testbench(params, analysis_type).replace(
            "MNMC Three-Stage", "NMCNR Three-Stage"
        )

    def get_gmid_spec(self, targets=None):
        spec = super().get_gmid_spec(targets)
        spec.branch_currents = [
            branch for branch in spec.branch_currents if branch.name != "I_f1"
        ]
        spec.transistors = [
            transistor
            for transistor in spec.transistors
            if transistor.role not in _FTS_ROLES
        ]
        spec.pass_through_params = [
            param
            for param in spec.pass_through_params
            if param.name not in {"Cc1", "Cc2"}
        ]
        spec.pass_through_params.extend(_compensation_params())
        return spec

    def get_param_space(self) -> ParamSpace:
        params = [
            param
            for param in super().get_param_space().params
            if param.name not in _FTS_PARAMS | {"Cc1", "Cc2"}
        ]
        params.extend(_compensation_params())
        return self._apply_param_space_overrides(ParamSpace(params=params))


def _remove_parameter_line(circuit: str, prefix: str) -> str:
    lines = circuit.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            del lines[index]
            return "\n".join(lines) + "\n"
    raise ValueError(f"Base parameter line was not found: {prefix}")


def _compensation_params() -> list[ParamDef]:
    return [
        ParamDef(
            name="Cc1", low=0.05e-12, high=100e-12,
            log_scale=True, unit="F",
        ),
        ParamDef(
            name="Cc2", low=0.05e-12, high=100e-12,
            log_scale=True, unit="F",
        ),
        ParamDef(
            name="Rm", low=50.0, high=100e3,
            log_scale=True, unit="ohm",
        ),
    ]


def _fmt(value: float) -> str:
    return format_spice_value(value)
