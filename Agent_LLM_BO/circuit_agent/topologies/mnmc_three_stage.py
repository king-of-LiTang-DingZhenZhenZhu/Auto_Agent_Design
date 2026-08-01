"""MNMC three-stage OTA based on Leung and Mok Fig. 1(f).

The serial path is a conventional three-stage nested-Miller amplifier.  An
additional PMOS-input differential transconductance stage senses the amplifier
input and injects its single-ended output current into ``s2_out``, the input of
the final output stage.  This is the multipath path in Fig. 1(f); it does not
connect directly to ``vout``.

Reference: K. N. Leung and P. K. T. Mok, "Analysis of Multistage
Amplifier-Frequency Compensation," IEEE TCAS-I, vol. 48, no. 9, 2001,
Fig. 1(f) and (23)-(27).
"""

from __future__ import annotations

from models import ParamDef, ParamSpace, format_spice_value
from pdk_profiles import get_pdk_profile, get_pdk_profile_for_params
from pdk_profiles import spectre_include_line
from topologies.base import TopologyMeta
from topologies.nmcf_three_stage import NMCFThreeStageOTA


def _mnmc_defaults() -> dict[str, float]:
    defaults = {
        name: value
        for name, value in NMCFThreeStageOTA.DEFAULT_PARAMS.items()
        if name not in {"Wgmf2", "Lgmf2"}
    }
    defaults.update(
        {
            # Conventional PMOS current-source load for the output stage.
            "Wload3": 24e-6,
            "Lload3": 200e-9,
            # Input-to-stage-3-input feedforward transconductance stage gmf1.
            "Wtailf1": 160e-6,
            "Ltailf1": 200e-9,
            "Wgmf1": 64e-6,
            "Lgmf1": 80e-9,
            "Wloadf1": 64e-6,
            "Lloadf1": 200e-9,
            "Cc2": 10e-12,
        }
    )
    return defaults


class MNMCThreeStageOTA(NMCFThreeStageOTA):
    """Three-stage nested-Miller OTA with an input feedforward path."""

    meta = TopologyMeta(
        name="mnmc_three_stage",
        display_name="MNMC Three-Stage OTA",
        description=(
            "Three-stage nested-Miller OTA with a feedforward input "
            "differential stage driving the input of the final output stage."
        ),
        min_gain_db=75,
        max_gain_db=115,
        min_gbw_hz=1e6,
        max_gbw_hz=8e8,
        typical_power_w=5e-3,
        complexity=5,
        escalation=None,
    )

    DEFAULT_PARAMS = _mnmc_defaults()

    def critical_operating_point_instances(self) -> set[str]:
        return (
            super().critical_operating_point_instances() - {"Mgmf2"}
        ) | {
            "Mload3",
            "Mtailf1",
            "Mgmf1a",
            "Mgmf1b",
            "Mloadf1a",
            "Mloadf1b",
        }

    def generate_circuit(self, params: dict[str, float] | None = None) -> str:
        p = self._merge_params_with_preset(params)
        pdk = get_pdk_profile_for_params(params)
        values = {
            name: _fmt(p[name])
            for name in self.DEFAULT_PARAMS
        }
        return _CIRCUIT_TEMPLATE.format(
            spectre_include=spectre_include_line(pdk),
            nmos_lvt_model=pdk.nmos_lvt_model,
            pmos_lvt_model=pdk.pmos_lvt_model,
            **values,
        )

    def generate_testbench(
        self,
        params: dict[str, float] | None = None,
        analysis_type: str = "ac",
    ) -> str:
        return super().generate_testbench(params, analysis_type).replace(
            "nmcf_three_stage", self.meta.name
        ).replace("NMCF Three-Stage", "MNMC Three-Stage")

    def get_gmid_spec(self, targets=None):
        from models import BranchCurrentSpec, TransistorSpec

        spec = super().get_gmid_spec(targets)
        pdk = get_pdk_profile()
        spec.transistors = [
            transistor
            for transistor in spec.transistors
            if transistor.role != "feedforward_gain_pmos"
        ]
        spec.branch_currents.append(
            BranchCurrentSpec(
                # With the default gm/Id values, I_f1 ~= 9*I_s2 gives
                # gmf1 ~= 4.5*gm2 because each FTS input device carries I_f1/2.
                name="I_f1", low=1e-6, high=500e-6, default=270e-6,
            )
        )
        spec.transistors.extend(
            [
                TransistorSpec(
                    role="stage3_load_pmos",
                    w_param="Wload3", l_param="Lload3",
                    model=pdk.pmos_lvt_model,
                    current_source="I_s3", current_fraction=1.0,
                    gm_id_low=5, gm_id_high=20, gm_id_default=8,
                    L_low=200e-9, L_high=600e-9, L_default=200e-9,
                    Vds_estimate=0.45,
                ),
                TransistorSpec(
                    role="feedforward_tail_pmos",
                    w_param="Wtailf1", l_param="Ltailf1",
                    model=pdk.pmos_lvt_model,
                    current_source="I_f1", current_fraction=1.0,
                    gm_id_low=5, gm_id_high=20, gm_id_default=8,
                    L_low=200e-9, L_high=600e-9, L_default=200e-9,
                    Vds_estimate=0.2,
                ),
                TransistorSpec(
                    role="feedforward_diff_pmos",
                    w_param="Wgmf1", l_param="Lgmf1",
                    model=pdk.pmos_lvt_model,
                    current_source="I_f1", current_fraction=0.5,
                    gm_id_low=10, gm_id_high=24, gm_id_default=15,
                    L_low=60e-9, L_high=500e-9, L_default=80e-9,
                    Vds_estimate=0.25, Vbs=-0.2, multiplicity=2,
                ),
                TransistorSpec(
                    role="feedforward_load_nmos",
                    w_param="Wloadf1", l_param="Lloadf1",
                    model=pdk.nmos_lvt_model,
                    current_source="I_f1", current_fraction=0.5,
                    gm_id_low=8, gm_id_high=24, gm_id_default=12,
                    L_low=200e-9, L_high=600e-9, L_default=200e-9,
                    Vds_estimate=0.3, multiplicity=2,
                ),
            ]
        )
        spec.pass_through_params = [
            param
            for param in spec.pass_through_params
            if param.name != "Cc2"
        ]
        spec.pass_through_params.append(_cc2_param())
        return spec

    def get_param_space(self) -> ParamSpace:
        params = [
            param
            for param in super().get_param_space().params
            if param.name not in {"Wgmf2", "Lgmf2", "Cc2"}
        ]
        params.extend(
            [
                _width_param("Wload3"),
                _source_length_param("Lload3"),
                _width_param("Wtailf1"),
                _source_length_param("Ltailf1"),
                _width_param("Wgmf1"),
                ParamDef(
                    name="Lgmf1", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                _width_param("Wloadf1"),
                _source_length_param("Lloadf1"),
                _cc2_param(),
            ]
        )
        return self._apply_param_space_overrides(ParamSpace(params=params))


def _width_param(name: str) -> ParamDef:
    return ParamDef(
        name=name, low=0.5e-6, high=200e-6,
        log_scale=True, unit="m", max_per_finger=2.6e-6,
    )


def _source_length_param(name: str) -> ParamDef:
    return ParamDef(
        name=name, low=200e-9, high=600e-9,
        log_scale=True, unit="m",
    )


def _cc2_param() -> ParamDef:
    # Equation (24) makes Cm2 substantially larger than ordinary NMC values.
    return ParamDef(
        name="Cc2", low=0.1e-12, high=100e-12,
        log_scale=True, unit="F",
    )


_CIRCUIT_TEMPLATE = """\
// mnmc_three_stage.cir -- MNMC Three-Stage OTA (Spectre native syntax)
simulator lang=spectre insensitive=yes

{spectre_include}

parameters Wtail1={Wtail1} Ltail1={Ltail1} Wdiff1={Wdiff1} Ldiff1={Ldiff1}
parameters Wload1={Wload1} Lload1={Lload1} Wgm2={Wgm2} Lgm2={Lgm2}
parameters Wmirror2={Wmirror2} Lmirror2={Lmirror2} Wsource2={Wsource2} Lsource2={Lsource2}
parameters Wgm3={Wgm3} Lgm3={Lgm3} Wload3={Wload3} Lload3={Lload3}
parameters Wtailf1={Wtailf1} Ltailf1={Ltailf1} Wgmf1={Wgmf1} Lgmf1={Lgmf1}
parameters Wloadf1={Wloadf1} Lloadf1={Lloadf1}
parameters Wbiasn={Wbiasn} Lbiasn={Lbiasn} Wbiasp={Wbiasp} Lbiasp={Lbiasp}
parameters Cc1={Cc1} Cc2={Cc2}

subckt mnmc_three_stage (vip vin vout ibias vdd vss)
// Bias generator
Mbn1 (ibias ibias vss vss) {nmos_lvt_model} w=Wbiasn l=Lbiasn nf=1
Mbn2 (vbiasp ibias vss vss) {nmos_lvt_model} w=Wbiasn l=Lbiasn nf=1
Mbp1 (vbiasp vbiasp vdd vdd) {pmos_lvt_model} w=Wbiasp l=Lbiasp nf=1

// Stage 1 (-Av1): PMOS input differential pair and NMOS mirror load
Mtail1 (tail vbiasp vdd vdd) {pmos_lvt_model} w=Wtail1 l=Ltail1 nf=1
Mdiff1a (s1_mirr vin tail vdd) {pmos_lvt_model} w=Wdiff1 l=Ldiff1 nf=1
Mdiff1b (s1_out vip tail vdd) {pmos_lvt_model} w=Wdiff1 l=Ldiff1 nf=1
Mload1a (s1_mirr s1_mirr vss vss) {nmos_lvt_model} w=Wload1 l=Lload1 nf=1
Mload1b (s1_out s1_mirr vss vss) {nmos_lvt_model} w=Wload1 l=Lload1 nf=1

// Stage 2 (+Av2): PMOS gm2, NMOS mirror, and PMOS current-source load
Mgm2 (s2_mirr s1_out vdd vdd) {pmos_lvt_model} w=Wgm2 l=Lgm2 nf=1
Mmirror2a (s2_mirr s2_mirr vss vss) {nmos_lvt_model} w=Wmirror2 l=Lmirror2 nf=1
Mmirror2b (s2_out s2_mirr vss vss) {nmos_lvt_model} w=Wmirror2 l=Lmirror2 nf=1
Msource2 (s2_out vbiasp vdd vdd) {pmos_lvt_model} w=Wsource2 l=Lsource2 nf=1

// Feedforward stage (-Avf1): input directly drives the stage-3 input node.
// This branch terminates at s2_out, not at vout (Fig. 1(f)).
Mtailf1 (f1_tail vbiasp vdd vdd) {pmos_lvt_model} w=Wtailf1 l=Ltailf1 nf=1
Mgmf1a (f1_mirr vin f1_tail vdd) {pmos_lvt_model} w=Wgmf1 l=Lgmf1 nf=1
Mgmf1b (s2_out vip f1_tail vdd) {pmos_lvt_model} w=Wgmf1 l=Lgmf1 nf=1
Mloadf1a (f1_mirr f1_mirr vss vss) {nmos_lvt_model} w=Wloadf1 l=Lloadf1 nf=1
Mloadf1b (s2_out f1_mirr vss vss) {nmos_lvt_model} w=Wloadf1 l=Lloadf1 nf=1

// Stage 3 (-Av3): conventional common-source output stage
Mgm3 (vout s2_out vss vss) {nmos_lvt_model} w=Wgm3 l=Lgm3 nf=1
Mload3 (vout vbiasp vdd vdd) {pmos_lvt_model} w=Wload3 l=Lload3 nf=1

// Nested Miller compensation from Fig. 1(f)
Cc1 (s1_out vout) capacitor c=Cc1
Cc2 (s2_out vout) capacitor c=Cc2
ends mnmc_three_stage
"""


def _fmt(value: float) -> str:
    return format_spice_value(value)
