"""ArrayAPI planning helpers for native standard-cell carrier decomposition."""
from __future__ import annotations

from dataclasses import dataclass
import os

from analogskills.contracts import TopologyGraph
from analogskills.env import get_env

from .stdcell_carriers import NativeStdCellCarrier, build_native_stdcell_carriers


@dataclass(frozen=True)
class NativeStdCellArrayApiCarrierPlan:
    carrier_name: str
    row: str
    carrier_kind: str
    generator_name: str
    invocation_mode: str
    source_cell: str
    array_api_symbol_cell: str
    direct_layout_view_available: bool
    interactive_layout_required: bool
    common_od_preferred: bool
    supported_in_batch: bool
    status: str
    notes: tuple[str, ...]


@dataclass(frozen=True)
class NativeStdCellArrayApiPlan:
    carrier_plans: tuple[NativeStdCellArrayApiCarrierPlan, ...]
    ready_for_direct_batch_layout: bool
    recommended_flow: str
    blocked_reasons: tuple[str, ...]


def _forced_stackseries_carrier_names() -> frozenset[str]:
    raw = get_env("ARRAYAPI_FORCE_STACKSERIES_CARRIERS", "") or ""
    names = [item.strip() for item in raw.split(",")]
    return frozenset(name for name in names if name)


def _force_stackseries_for_carrier(carrier: NativeStdCellCarrier) -> bool:
    return carrier.name in _forced_stackseries_carrier_names()


def build_native_stdcell_arrayapi_plan(
    graph: TopologyGraph,
    *,
    carriers: tuple[NativeStdCellCarrier, ...] | None = None,
) -> NativeStdCellArrayApiPlan:
    resolved_carriers = tuple(carriers or build_native_stdcell_carriers(graph))
    plans = tuple(_plan_for_carrier(carrier) for carrier in resolved_carriers)
    blocked = tuple(dict.fromkeys(reason for plan in plans for reason in _blocked_reasons_for_plan(plan)))
    return NativeStdCellArrayApiPlan(
        carrier_plans=plans,
        ready_for_direct_batch_layout=not blocked and all(plan.supported_in_batch for plan in plans),
        recommended_flow="arrayapi_frontend_then_lx_generate_from_source",
        blocked_reasons=blocked,
    )


def _plan_for_carrier(carrier: NativeStdCellCarrier) -> NativeStdCellArrayApiCarrierPlan:
    if carrier.kind == "series":
        if carrier.device_count == 2 and len(set(carrier.gate_nets)) == 2:
            if _force_stackseries_for_carrier(carrier):
                selected_gate = carrier.gate_nets[0] if carrier.gate_nets else ""
                return NativeStdCellArrayApiCarrierPlan(
                    carrier_name=carrier.name,
                    row=carrier.row,
                    carrier_kind=carrier.kind,
                    generator_name="TSMC_StackOfSeries",
                    invocation_mode="tsmc_pdk_plus_assistant",
                    source_cell=carrier.model,
                    array_api_symbol_cell="mosfet_StackSeries",
                    direct_layout_view_available=False,
                    interactive_layout_required=False,
                    common_od_preferred=True,
                    supported_in_batch=True,
                    status="constraint_to_layout_generation_proven",
                    notes=(
                        "Dual-gate two-device series carrier is forcibly collapsed onto a StackSeries ArrayAPI front end for this experiment.",
                        f"The single StackSeries gate is driven from carrier-order gate net {selected_gate!r}; the remaining gate topology is intentionally not preserved in the companion schematic.",
                        "This override exists to test whether the NMOS child can re-enter the proven constraint -> Generate From Source -> layout modgen path used by StackSeries.",
                        "Use this only for ArrayAPI layout/mergeback experiments, not as a faithful electrical representation of the original NAND2 pull-down stack.",
                    ),
                )
            return NativeStdCellArrayApiCarrierPlan(
                carrier_name=carrier.name,
                row=carrier.row,
                carrier_kind=carrier.kind,
                generator_name="TSMC_StackOfSeries",
                invocation_mode="tsmc_pdk_plus_assistant",
                source_cell=carrier.model,
                array_api_symbol_cell="",
                direct_layout_view_available=False,
                interactive_layout_required=False,
                common_od_preferred=True,
                supported_in_batch=True,
                status="dual_gate_series_discrete_assistant_frontend",
                notes=(
                    "Dual-gate two-device series carriers should prefer a discrete native-device front end instead of a CasCode symbol front end.",
                    "The CasCode front end was proven batch-runnable, but its resulting child layout tends to be vertically organized and is not stdcell-compatible for NAND2.",
                    "This plan keeps the real two-gate topology in the schematic and lets TSMC PDK+ assistant infer the appropriate series constraint from the selected native MOS instances.",
                    "The framework still uses Generate From Source after assistant expansion, but the ownership of the series topology moves back to the assistant/finder rather than being hard-coded as CasCode.",
                ),
            )
        return NativeStdCellArrayApiCarrierPlan(
            carrier_name=carrier.name,
            row=carrier.row,
            carrier_kind=carrier.kind,
            generator_name="TSMC_StackOfSeries",
            invocation_mode="tsmc_pdk_plus_assistant",
            source_cell=carrier.model,
            array_api_symbol_cell="mosfet_StackSeries",
            direct_layout_view_available=False,
            interactive_layout_required=False,
            common_od_preferred=True,
            supported_in_batch=True,
            status="constraint_to_layout_generation_proven",
            notes=(
                "ArrayAPI exposes a schematic/symbol front end for StackSeries.",
                "PDK documentation states stack MOS cells do not provide a direct layout view.",
                "In an N7-only GUI session, TSMC PDK+ finder resolves the stack into /coreInst members.",
                "The generator requires schematic Check-and-Save before it can run without a connectivity warning.",
                "A minimal N7-only probe has already proven the full handoff: schematic constraint -> lxGenFromSource -> layout modgen figGroup.",
                "The generated layout contains two tsmcN7/nch_svt_mac layout instances and a layout-side modgen figGroup named Constr_0.",
            ),
        )
    if carrier.kind == "parallel" and carrier.device_count == 2 and len(set(carrier.gate_nets)) == 2:
        return NativeStdCellArrayApiCarrierPlan(
            carrier_name=carrier.name,
            row=carrier.row,
            carrier_kind=carrier.kind,
            generator_name="TSMC_DifferentialPair",
            invocation_mode="tsmc_pdk_plus_assistant",
            source_cell=carrier.model,
            array_api_symbol_cell="mosfet_DiffPair",
            direct_layout_view_available=False,
            interactive_layout_required=False,
            common_od_preferred=True,
            supported_in_batch=True,
            status="dual_gate_parallel_diffpair_frontend",
            notes=(
                "Two-device parallel carriers with distinct gate nets can be represented by the N7 ArrayAPI DiffPair front end.",
                "For NAND2 PMOS pull-up, tying D1/D2 to the same output net preserves the true connectivity while reusing a real ArrayAPI symbol that batch-materializes a modgen constraint.",
                "A dedicated probe has confirmed assistant -> constraint -> lxGenFromSource for mosfet_DiffPair, including Constr_0 on both schematic and layout.",
                "This path is preferable to the discrete CustomArray pseudo-success path, which generated layout instances without materializing any OA constraint.",
            ),
        )
    if carrier.kind in {"parallel", "single"}:
        return NativeStdCellArrayApiCarrierPlan(
            carrier_name=carrier.name,
            row=carrier.row,
            carrier_kind=carrier.kind,
            generator_name="TSMC_CustomArray",
            invocation_mode="tsmc_pdk_plus_assistant",
            source_cell=carrier.model,
            array_api_symbol_cell="",
            direct_layout_view_available=False,
            interactive_layout_required=False,
            common_od_preferred=carrier.kind == "parallel",
            supported_in_batch=True,
            status="parallel_group_generate_from_source_proven" if carrier.kind == "parallel" else "single_device_generate_from_source_proven",
            notes=(
                "Custom N/P MOS is documented as a Device Array API structure.",
                "Current PDK install does not expose a dedicated CustomArray OA symbol cell in tsmcN7_ArrayAPILib.",
                "In an N7-only GUI session, TSMC PDK+ reaches TSMC_CustomArray after finder expansion.",
                "A discrete two-device PMOS front end with native pcell symbols can be selected and expanded by TSMC PDK+ in batch mode.",
                "The same flow has now been exercised from the NAND2 mainline, where pmos_parallel_0 produces two native pch_svt_mac layout instances via Generate From Source.",
            ),
        )
    return NativeStdCellArrayApiCarrierPlan(
        carrier_name=carrier.name,
        row=carrier.row,
        carrier_kind=carrier.kind,
        generator_name="",
        invocation_mode="unsupported",
        source_cell=carrier.model,
        array_api_symbol_cell="",
        direct_layout_view_available=False,
        interactive_layout_required=True,
        common_od_preferred=False,
        supported_in_batch=False,
        status="unsupported_carrier_topology",
        notes=(
            "Carrier topology is not yet mapped to an ArrayAPI generator.",
        ),
    )


def _blocked_reasons_for_plan(plan: NativeStdCellArrayApiCarrierPlan) -> tuple[str, ...]:
    reasons: list[str] = []
    if not plan.generator_name:
        reasons.append(f"{plan.carrier_name}:no_generator_mapping")
    if not plan.direct_layout_view_available:
        reasons.append(f"{plan.carrier_name}:no_direct_layout_view")
    if plan.interactive_layout_required:
        reasons.append(f"{plan.carrier_name}:requires_modgen_or_constraint_gui")
    if plan.generator_name:
        reasons.append(f"{plan.carrier_name}:requires_single_pdk_project")
        if plan.invocation_mode == "tsmc_pdk_plus_assistant":
            reasons.append(f"{plan.carrier_name}:requires_tsmc_pdk_plus_assistant")
            reasons.append(f"{plan.carrier_name}:requires_schematic_check_and_save")
    if plan.status == "constraint_to_layout_generation_proven":
        reasons.append(f"{plan.carrier_name}:requires_lx_generate_from_source_handoff")
        reasons.append(f"{plan.carrier_name}:framework_layout_writeback_not_integrated")
    elif plan.status == "dual_gate_series_discrete_assistant_frontend":
        reasons.append(f"{plan.carrier_name}:requires_discrete_series_frontend")
        reasons.append(f"{plan.carrier_name}:requires_lx_generate_from_source_handoff")
        reasons.append(f"{plan.carrier_name}:framework_layout_writeback_not_integrated")
    elif plan.status == "dual_gate_parallel_diffpair_frontend":
        reasons.append(f"{plan.carrier_name}:requires_diffpair_frontend")
        reasons.append(f"{plan.carrier_name}:requires_lx_generate_from_source_handoff")
        reasons.append(f"{plan.carrier_name}:framework_layout_writeback_not_integrated")
    elif plan.status in {"parallel_group_generate_from_source_proven", "single_device_generate_from_source_proven"}:
        reasons.append(f"{plan.carrier_name}:requires_discrete_customarray_frontend")
        reasons.append(f"{plan.carrier_name}:requires_lx_generate_from_source_handoff")
        reasons.append(f"{plan.carrier_name}:framework_layout_writeback_not_integrated")
    elif not plan.supported_in_batch:
        reasons.append(f"{plan.carrier_name}:batch_generator_not_closed")
    return tuple(reasons)
