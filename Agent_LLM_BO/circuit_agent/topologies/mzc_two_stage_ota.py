"""Two-stage Miller zero-cancellation OTAs based on Leung and Mok Fig. 1(c).

The serial gain stages are inherited from the existing NMOS-input and
PMOS-input two-stage OTAs.  A polarity-reversed differential transconductance
stage senses the same input and drives ``vout`` directly.  At
``fts_ratio=1``, its devices and current density match the first stage, so the
first-order condition gm_fts = gm_1 is satisfied.

Reference: K. N. Leung and P. K. T. Mok, "Analysis of Multistage
Amplifier-Frequency Compensation," IEEE TCAS-I, vol. 48, no. 9, 2001,
Fig. 1(c) and (8).
"""

from __future__ import annotations

from models import ParamDef, ParamSpace, format_spice_value
from pdk_profiles import get_pdk_profile_for_params
from topologies.base import TopologyMeta
from topologies.pmos_input_two_stage_ota import PMOSInputTwoStageOTA
from topologies.two_stage_ota import TwoStageOTA


def _mzc_defaults(base: dict[str, float]) -> dict[str, float]:
    defaults = {name: value for name, value in base.items() if name != "Rz"}
    defaults["fts_ratio"] = 1.0
    return defaults


def _mzc_param_space(base: ParamSpace) -> ParamSpace:
    params = [param for param in base.params if param.name != "Rz"]
    params.append(
        ParamDef(
            name="fts_ratio",
            low=0.5,
            high=1.5,
            log_scale=False,
            unit="x",
        )
    )
    return ParamSpace(params=params)


def _render_mzc(
    circuit: str,
    *,
    base_name: str,
    topology_name: str,
    fts_ratio: float,
    fts_devices: str,
) -> str:
    old_compensation = """\
// Miller compensation
Rz (n_s1 n_rz) resistor r=Rz
Cc (n_rz vout) capacitor c=Cc"""
    new_compensation = f"""\
// Feedforward transconductance stage: opposite polarity to the first stage
{fts_devices}
// Direct Miller capacitor; the FTS replaces the nulling resistor in Fig. 1(c)
Cc (n_s1 vout) capacitor c=Cc"""
    if old_compensation not in circuit:
        raise ValueError("Base two-stage OTA compensation block was not found")

    circuit = circuit.replace(old_compensation, new_compensation, 1)
    circuit = circuit.replace(base_name, topology_name)
    parameter_prefix = "parameters Cc="
    for line in circuit.splitlines():
        if line.startswith(parameter_prefix):
            cc_value = line.split()[1].split("=", 1)[1]
            circuit = circuit.replace(
                line,
                f"parameters Cc={cc_value} fts_ratio={format_spice_value(fts_ratio)}",
                1,
            )
            break
    else:
        raise ValueError("Base two-stage OTA compensation parameters were not found")
    return circuit


class MZCTwoStageOTA(TwoStageOTA):
    """NMOS-input two-stage OTA with a feedforward zero-cancellation stage."""

    meta = TopologyMeta(
        name="mzc_two_stage_ota",
        display_name="MZC Two-Stage OTA",
        description=(
            "NMOS-input two-stage OTA with direct Miller compensation and a "
            "matched feedforward transconductance stage for RHP-zero cancellation."
        ),
        min_gain_db=45,
        max_gain_db=80,
        min_gbw_hz=10e6,
        max_gbw_hz=5e8,
        typical_power_w=1.5e-3,
        complexity=3,
        escalation="folded_cascode",
    )

    DEFAULT_PARAMS = _mzc_defaults(TwoStageOTA.DEFAULT_PARAMS)

    def critical_operating_point_instances(self) -> set[str]:
        return super().critical_operating_point_instances() | {
            "Mffdiff1",
            "Mffdiff2",
            "Mffmirr1",
            "Mffmirr2",
            "Mtailff",
        }

    def generate_circuit(self, params: dict[str, float] | None = None) -> str:
        p = self._merge_params_with_preset(params)
        base_params = dict(p)
        base_params["Rz"] = 1.0
        pdk = get_pdk_profile_for_params(params)
        fts_devices = f"""\
Mffdiff1 (n_ff_mirr vip n_ff_tail vss) {pdk.nmos_model} w=Wdiff*fts_ratio l=Ldiff nf=1
Mffdiff2 (vout vin n_ff_tail vss) {pdk.nmos_model} w=Wdiff*fts_ratio l=Ldiff nf=1
Mffmirr1 (n_ff_mirr n_ff_mirr vdd vdd) {pdk.pmos_model} w=Wmirr*fts_ratio l=Lmirr nf=1
Mffmirr2 (vout n_ff_mirr vdd vdd) {pdk.pmos_model} w=Wmirr*fts_ratio l=Lmirr nf=1
Mtailff (n_ff_tail ibias vss vss) {pdk.nmos_model} w=Wbias*fts_ratio l=Lbias nf=1 m=m_tail_unit"""
        return _render_mzc(
            super().generate_circuit(base_params),
            base_name="two_stage_ota",
            topology_name=self.meta.name,
            fts_ratio=p["fts_ratio"],
            fts_devices=fts_devices,
        )

    def generate_testbench(
        self,
        params: dict[str, float] | None = None,
        analysis_type: str = "ac",
    ) -> str:
        return super().generate_testbench(params, analysis_type).replace(
            "two_stage_ota", self.meta.name
        )

    def get_param_space(self) -> ParamSpace:
        return _mzc_param_space(super().get_param_space())

    def get_gmid_spec(self, targets=None):
        spec = super().get_gmid_spec(targets)
        spec.pass_through_params = [
            param for param in spec.pass_through_params if param.name != "Rz"
        ]
        spec.pass_through_params.append(
            ParamDef(
                name="fts_ratio",
                low=0.5,
                high=1.5,
                log_scale=False,
                unit="x",
            )
        )
        return spec


class PMOSInputMZCTwoStageOTA(PMOSInputTwoStageOTA):
    """PMOS-input two-stage OTA with a feedforward zero-cancellation stage."""

    meta = TopologyMeta(
        name="pmos_input_mzc_two_stage_ota",
        display_name="PMOS-Input MZC Two-Stage OTA",
        description=(
            "PMOS-input two-stage OTA with direct Miller compensation and a "
            "matched feedforward transconductance stage for RHP-zero cancellation."
        ),
        min_gain_db=45,
        max_gain_db=80,
        min_gbw_hz=10e6,
        max_gbw_hz=5e8,
        typical_power_w=1.5e-3,
        complexity=3,
        escalation="folded_cascode",
    )

    DEFAULT_PARAMS = _mzc_defaults(PMOSInputTwoStageOTA.DEFAULT_PARAMS)

    def critical_operating_point_instances(self) -> set[str]:
        return super().critical_operating_point_instances() | {
            "Mffdiff1",
            "Mffdiff2",
            "Mffmirr1",
            "Mffmirr2",
            "Mtailff",
        }

    def generate_circuit(self, params: dict[str, float] | None = None) -> str:
        p = self._merge_params_with_preset(params)
        base_params = dict(p)
        base_params["Rz"] = 1.0
        pdk = get_pdk_profile_for_params(params)
        fts_devices = f"""\
Mffdiff1 (n_ff_mirr vin n_ff_tail vdd) {pdk.pmos_lvt_model} w=Wdiff*fts_ratio l=Ldiff nf=1
Mffdiff2 (vout vip n_ff_tail vdd) {pdk.pmos_lvt_model} w=Wdiff*fts_ratio l=Ldiff nf=1
Mffmirr1 (n_ff_mirr n_ff_mirr vss vss) {pdk.nmos_lvt_model} w=Wmirr*fts_ratio l=Lmirr nf=1
Mffmirr2 (vout n_ff_mirr vss vss) {pdk.nmos_lvt_model} w=Wmirr*fts_ratio l=Lmirr nf=1
Mtailff (n_ff_tail ibias vdd vdd) {pdk.pmos_lvt_model} w=Wbias*fts_ratio l=Lbias nf=1 m=m_tail_unit"""
        return _render_mzc(
            super().generate_circuit(base_params),
            base_name="pmos_input_two_stage_ota",
            topology_name=self.meta.name,
            fts_ratio=p["fts_ratio"],
            fts_devices=fts_devices,
        )

    def generate_testbench(
        self,
        params: dict[str, float] | None = None,
        analysis_type: str = "ac",
    ) -> str:
        return super().generate_testbench(params, analysis_type).replace(
            "pmos_input_two_stage_ota", self.meta.name
        )

    def get_param_space(self) -> ParamSpace:
        return _mzc_param_space(super().get_param_space())

    def get_gmid_spec(self, targets=None):
        spec = super().get_gmid_spec(targets)
        spec.pass_through_params = [
            param for param in spec.pass_through_params if param.name != "Rz"
        ]
        spec.pass_through_params.append(
            ParamDef(
                name="fts_ratio",
                low=0.5,
                high=1.5,
                log_scale=False,
                unit="x",
            )
        )
        return spec
