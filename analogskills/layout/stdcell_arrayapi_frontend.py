"""Companion schematic builders for N7 ArrayAPI frontend cells."""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import TYPE_CHECKING, Mapping

from analogskills.contracts import Device, DeviceRole, NetRole, TerminalRef, TopologyGraph
from analogskills.env import get_env
from analogskills.layout.stdcell_arrayapi import NativeStdCellArrayApiPlan
from analogskills.layout.stdcell_arrayapi_runtime import NativeStdCellArrayApiGenerateRequest
from analogskills.layout.stdcell_carriers import NativeStdCellCarrier

if TYPE_CHECKING:
    from analogskills.eda.oa import OaWritePlan
    from analogskills.pcell import PCellLayoutPlan
    from analogskills.pdk import PdkConfig


@dataclass(frozen=True)
class NativeStdCellArrayApiFrontendArtifact:
    carrier_name: str
    generator_name: str
    invocation_mode: str
    array_api_symbol_cell: str
    cell_name: str
    instance_name: str
    selected_instance_names: tuple[str, ...]
    schematic_plan: OaWritePlan
    notes: tuple[str, ...]

    def build_runtime_request(
        self,
        *,
        report_path: str,
        layout_view_name: str = "layout_arrayapi",
    ) -> NativeStdCellArrayApiGenerateRequest:
        return NativeStdCellArrayApiGenerateRequest(
            lib_name=self.schematic_plan.cellview.lib,
            cell_name=self.schematic_plan.cellview.cell,
            schematic_view_name=self.schematic_plan.cellview.view,
            layout_view_name=layout_view_name,
            report_path=report_path,
            selected_instance_names=self.selected_instance_names or ((self.instance_name,) if self.instance_name else ()),
        )


def _forced_stackseries_carrier_names() -> frozenset[str]:
    raw = get_env("ARRAYAPI_FORCE_STACKSERIES_CARRIERS", "") or ""
    names = [item.strip() for item in raw.split(",")]
    return frozenset(name for name in names if name)


def _force_stackseries_for_carrier(carrier: NativeStdCellCarrier) -> bool:
    return carrier.name in _forced_stackseries_carrier_names()


def _stackseries_gate_net(carrier: NativeStdCellCarrier) -> str | None:
    gate_nets = tuple(net for net in carrier.gate_nets if net)
    if not gate_nets:
        return None
    if len(set(gate_nets)) == 1:
        return gate_nets[0]
    if not _force_stackseries_for_carrier(carrier):
        return None
    requested = (get_env("ARRAYAPI_STACKSERIES_GATE_NET", "") or "").strip()
    if requested and requested in gate_nets:
        return requested
    return gate_nets[0]


def build_native_stdcell_arrayapi_frontends(
    graph: TopologyGraph,
    arrayapi_plan: NativeStdCellArrayApiPlan,
    *,
    carriers: tuple[NativeStdCellCarrier, ...],
    pcell_plan: PCellLayoutPlan,
    lib: str,
    top_cell: str,
    view: str = "schematic",
    pdk: PdkConfig | None = None,
    schematic_sizing: Mapping[str, Mapping[str, object]] | None = None,
) -> tuple[NativeStdCellArrayApiFrontendArtifact, ...]:
    carrier_by_name = {carrier.name: carrier for carrier in carriers}
    pcell_by_name = pcell_plan.instance_map()
    artifacts: list[NativeStdCellArrayApiFrontendArtifact] = []
    for carrier_plan in arrayapi_plan.carrier_plans:
        carrier = carrier_by_name.get(carrier_plan.carrier_name)
        if carrier is None:
            continue
        if carrier_plan.generator_name == "TSMC_CustomArray" and carrier.kind == "parallel":
            artifact = _build_customarray_discrete_frontend(
                graph,
                carrier,
                carrier_plan,
                lib=lib,
                top_cell=top_cell,
                view=view,
                pdk=pdk,
                schematic_sizing=schematic_sizing,
            )
        elif carrier_plan.generator_name == "TSMC_DifferentialPair" and carrier.kind == "parallel" and len(set(carrier.gate_nets)) == 2:
            artifact = _build_diffpair_frontend(
                graph,
                carrier,
                carrier_plan,
                pcell_by_name=pcell_by_name,
                lib=lib,
                top_cell=top_cell,
                view=view,
            )
        elif (
            carrier_plan.generator_name == "TSMC_StackOfSeries"
            and carrier.kind == "series"
            and len(set(carrier.gate_nets)) == 2
            and not carrier_plan.array_api_symbol_cell
        ):
            artifact = _build_series_discrete_frontend(
                graph,
                carrier,
                carrier_plan,
                lib=lib,
                top_cell=top_cell,
                view=view,
                pdk=pdk,
                schematic_sizing=schematic_sizing,
            )
        elif carrier_plan.array_api_symbol_cell == "mosfet_CasCode":
            artifact = _build_cascode_frontend(
                graph,
                carrier,
                carrier_plan,
                pcell_by_name=pcell_by_name,
                lib=lib,
                top_cell=top_cell,
                view=view,
            )
        elif carrier_plan.array_api_symbol_cell == "mosfet_StackSeries":
            artifact = _build_stackseries_frontend(
                graph,
                carrier,
                carrier_plan,
                pcell_by_name=pcell_by_name,
                lib=lib,
                top_cell=top_cell,
                view=view,
            )
        else:
            artifact = None
        if artifact is not None:
            artifacts.append(artifact)
    return tuple(artifacts)


def _build_cascode_frontend(
    graph: TopologyGraph,
    carrier: NativeStdCellCarrier,
    carrier_plan,
    *,
    pcell_by_name: Mapping[str, object],
    lib: str,
    top_cell: str,
    view: str,
) -> NativeStdCellArrayApiFrontendArtifact | None:
    if carrier.device_count != 2 or len(carrier.gate_nets) != 2:
        return None
    lower = pcell_by_name.get(carrier.device_names[0])
    upper = pcell_by_name.get(carrier.device_names[1])
    if lower is None or upper is None:
        return None
    lower_params = getattr(lower, "params", {})
    upper_params = getattr(upper, "params", {})
    instance_name = f"X{carrier.name.upper()}"
    cell_name = f"{top_cell}_{carrier.name}_arrayapi"
    bulk_net = carrier.bulk_nets[0] if carrier.bulk_nets else carrier.endpoint_nets[0]
    params = {
        "srcLibrary": "tsmcN7",
        "srcCell": carrier_plan.source_cell,
        "fins": int(lower_params.get("nfin", upper_params.get("nfin", 4))),
        "l1": _format_length_param(lower_params.get("l", 3e-08)),
        "fingers1": int(lower_params.get("fingers", 1)),
        "l2": _format_length_param(upper_params.get("l", 3e-08)),
        "fingers2": int(upper_params.get("fingers", 1)),
        "stackCnt": 1,
        "simM": int(lower_params.get("simM", upper_params.get("simM", 1))),
        "pinStyle": "AAAABBBB",
        "polyPITCH": "P57",
    }
    connections = {
        "S": carrier.endpoint_nets[0],
        "G1": carrier.gate_nets[0],
        "G2": carrier.gate_nets[1],
        "D": carrier.endpoint_nets[1],
        "B": bulk_net,
    }
    plan = _build_frontend_schematic_plan(
        graph,
        lib=lib,
        cell=cell_name,
        view=view,
        instance_name=instance_name,
        symbol_cell="mosfet_CasCode",
        connections=connections,
        params=params,
        emit_pins=False,
    )
    return NativeStdCellArrayApiFrontendArtifact(
        carrier_name=carrier.name,
        generator_name=carrier_plan.generator_name,
        invocation_mode=carrier_plan.invocation_mode,
        array_api_symbol_cell=carrier_plan.array_api_symbol_cell,
        cell_name=cell_name,
        instance_name=instance_name,
        selected_instance_names=(instance_name,),
        schematic_plan=plan,
        notes=(
            "G1 is mapped to the source-side device gate in carrier order.",
            "G2 is mapped to the drain-side device gate in carrier order.",
        ),
    )


def _build_stackseries_frontend(
    graph: TopologyGraph,
    carrier: NativeStdCellCarrier,
    carrier_plan,
    *,
    pcell_by_name: Mapping[str, object],
    lib: str,
    top_cell: str,
    view: str,
) -> NativeStdCellArrayApiFrontendArtifact | None:
    first = pcell_by_name.get(carrier.device_names[0]) if carrier.device_names else None
    gate_net = _stackseries_gate_net(carrier)
    if first is None or gate_net is None:
        return None
    first_params = getattr(first, "params", {})
    instance_name = f"X{carrier.name.upper()}"
    cell_name = f"{top_cell}_{carrier.name}_arrayapi"
    bulk_net = carrier.bulk_nets[0] if carrier.bulk_nets else carrier.endpoint_nets[0]
    params = {
        "srcLibrary": "tsmcN7",
        "srcCell": carrier_plan.source_cell,
        "fingers": int(first_params.get("fingers", 1)),
        "fins": int(first_params.get("nfin", 4)),
        "l": _format_length_param(first_params.get("l", 3e-08)),
        "simM": int(first_params.get("simM", 1)),
        "decompDev": "In_Serial(L)",
        "decompCnt": carrier.device_count,
        "pinStyle": "AAAABBBB",
        "polyPITCH": "P57",
        "splitMultiFingers": True,
    }
    connections = {
        "S": carrier.endpoint_nets[0],
        "G": gate_net,
        "D": carrier.endpoint_nets[1],
        "B": bulk_net,
    }
    plan = _build_frontend_schematic_plan(
        graph,
        lib=lib,
        cell=cell_name,
        view=view,
        instance_name=instance_name,
        symbol_cell="mosfet_StackSeries",
        connections=connections,
        params=params,
        emit_pins=False,
    )
    note_items = [
        "Single-gate series carrier is collapsed into one StackSeries schematic front end.",
    ]
    if len(set(carrier.gate_nets)) > 1:
        note_items.append(
            f"Experimental dual-gate collapse active: StackSeries G is driven from {gate_net!r} while the remaining gate net is intentionally dropped from the companion schematic."
        )
    return NativeStdCellArrayApiFrontendArtifact(
        carrier_name=carrier.name,
        generator_name=carrier_plan.generator_name,
        invocation_mode=carrier_plan.invocation_mode,
        array_api_symbol_cell=carrier_plan.array_api_symbol_cell,
        cell_name=cell_name,
        instance_name=instance_name,
        selected_instance_names=(instance_name,),
        schematic_plan=plan,
        notes=tuple(note_items),
    )


def _build_diffpair_frontend(
    graph: TopologyGraph,
    carrier: NativeStdCellCarrier,
    carrier_plan,
    *,
    pcell_by_name: Mapping[str, object],
    lib: str,
    top_cell: str,
    view: str,
) -> NativeStdCellArrayApiFrontendArtifact | None:
    if carrier.device_count != 2 or len(set(carrier.gate_nets)) != 2:
        return None
    first = pcell_by_name.get(carrier.device_names[0])
    second = pcell_by_name.get(carrier.device_names[1])
    if first is None or second is None:
        return None
    first_params = getattr(first, "params", {})
    second_params = getattr(second, "params", {})
    instance_name = f"X{carrier.name.upper()}"
    cell_name = f"{top_cell}_{carrier.name}_arrayapi"
    bulk_net = carrier.bulk_nets[0] if carrier.bulk_nets else carrier.endpoint_nets[0]
    params = {
        "srcLibrary": "tsmcN7",
        "srcCell": carrier_plan.source_cell,
        "fingers": int(first_params.get("fingers", second_params.get("fingers", 1))),
        "fins": int(first_params.get("nfin", second_params.get("nfin", 4))),
        "l": _format_length_param(first_params.get("l", second_params.get("l", 3e-08))),
        "simM": int(first_params.get("simM", second_params.get("simM", 1))),
        "decompDev": "In_Parallel(W)",
        "decompCnt": carrier.device_count,
        "stackCnt": 1,
        "pinStyle": "AAAABBBB",
        "polyPITCH": "P57",
        "matching_flag": "0",
        "edge_flag": "0",
    }
    connections = {
        "D1": carrier.endpoint_nets[1],
        "D2": carrier.endpoint_nets[1],
        "G1": carrier.gate_nets[0],
        "G2": carrier.gate_nets[1],
        "S": carrier.endpoint_nets[0],
        "B": bulk_net,
    }
    plan = _build_frontend_schematic_plan(
        graph,
        lib=lib,
        cell=cell_name,
        view=view,
        instance_name=instance_name,
        symbol_cell="mosfet_DiffPair",
        connections=connections,
        params=params,
        emit_pins=False,
    )
    return NativeStdCellArrayApiFrontendArtifact(
        carrier_name=carrier.name,
        generator_name=carrier_plan.generator_name,
        invocation_mode=carrier_plan.invocation_mode,
        array_api_symbol_cell=carrier_plan.array_api_symbol_cell,
        cell_name=cell_name,
        instance_name=instance_name,
        selected_instance_names=(instance_name,),
        schematic_plan=plan,
        notes=(
            "D1/D2 are intentionally tied to the same output net to encode a true two-device parallel pull-up pair with distinct gates.",
            "The N7 DiffPair symbol is used here as the closest real ArrayAPI front end for a dual-gate parallel stdcell carrier.",
        ),
    )


def _build_customarray_discrete_frontend(
    graph: TopologyGraph,
    carrier: NativeStdCellCarrier,
    carrier_plan,
    *,
    lib: str,
    top_cell: str,
    view: str,
    pdk: PdkConfig | None,
    schematic_sizing: Mapping[str, Mapping[str, object]] | None,
) -> NativeStdCellArrayApiFrontendArtifact | None:
    if carrier.kind != "parallel" or carrier.device_count < 2:
        return None
    from analogskills.eda.oa import build_oa_schematic_plan

    cell_name = f"{top_cell}_{carrier.name}_arrayapi"
    selected_names = tuple(carrier.device_names)
    frontend_graph = _build_discrete_carrier_graph(graph, carrier, name=cell_name)
    sizing = {
        device_name: dict((schematic_sizing or {}).get(device_name, {}))
        for device_name in carrier.device_names
    }
    plan = build_oa_schematic_plan(
        frontend_graph,
        lib=lib,
        cell=cell_name,
        view=view,
        sizing=sizing,
        pdk=pdk,
    )
    return NativeStdCellArrayApiFrontendArtifact(
        carrier_name=carrier.name,
        generator_name=carrier_plan.generator_name,
        invocation_mode=carrier_plan.invocation_mode,
        array_api_symbol_cell=carrier_plan.array_api_symbol_cell,
        cell_name=cell_name,
        instance_name=selected_names[0],
        selected_instance_names=selected_names,
        schematic_plan=plan,
        notes=(
            "Parallel carriers use the native pcell symbols as the schematic front end.",
            "TSMC PDK+ assistant is expected to expand the selected instances into a TSMC_CustomArray-compatible group.",
        ),
    )


def _build_series_discrete_frontend(
    graph: TopologyGraph,
    carrier: NativeStdCellCarrier,
    carrier_plan,
    *,
    lib: str,
    top_cell: str,
    view: str,
    pdk: PdkConfig | None,
    schematic_sizing: Mapping[str, Mapping[str, object]] | None,
) -> NativeStdCellArrayApiFrontendArtifact | None:
    if carrier.kind != "series" or carrier.device_count < 2 or len(set(carrier.gate_nets)) != carrier.device_count:
        return None
    from analogskills.eda.oa import build_oa_schematic_plan

    cell_name = f"{top_cell}_{carrier.name}_arrayapi"
    selected_names = tuple(carrier.device_names)
    frontend_graph = _build_discrete_carrier_graph(graph, carrier, name=cell_name)
    sizing = {
        device_name: dict((schematic_sizing or {}).get(device_name, {}))
        for device_name in carrier.device_names
    }
    plan = build_oa_schematic_plan(
        frontend_graph,
        lib=lib,
        cell=cell_name,
        view=view,
        sizing=sizing,
        pdk=pdk,
    )
    return NativeStdCellArrayApiFrontendArtifact(
        carrier_name=carrier.name,
        generator_name=carrier_plan.generator_name,
        invocation_mode=carrier_plan.invocation_mode,
        array_api_symbol_cell=carrier_plan.array_api_symbol_cell,
        cell_name=cell_name,
        instance_name=selected_names[0],
        selected_instance_names=selected_names,
        schematic_plan=plan,
        notes=(
            "Dual-gate series carriers now keep their native two-device schematic shape.",
            "TSMC PDK+ assistant is expected to infer a series constraint from the selected NMOS instances instead of starting from a CasCode symbol.",
        ),
    )


def _build_frontend_schematic_plan(
    graph: TopologyGraph,
    *,
    lib: str,
    cell: str,
    view: str,
    instance_name: str,
    symbol_cell: str,
    connections: dict[str, str],
    params: dict[str, object],
    emit_pins: bool = True,
) -> OaWritePlan:
    from analogskills.eda.oa import OaCellView, OaInstance, OaPin, OaWritePlan

    nets = tuple(dict.fromkeys(str(net) for net in connections.values() if net))
    pins = tuple(OaPin(net, net, _pin_direction_for_net(graph, net)) for net in nets) if emit_pins else ()
    instance = OaInstance(
        name=instance_name,
        lib="tsmcN7_ArrayAPILib",
        cell=symbol_cell,
        view="symbol",
        xy=(0.0, 0.0),
        orient="R0",
        connections=dict(connections),
        params=dict(params),
    )
    return OaWritePlan(
        OaCellView(lib, cell, view, "schematic"),
        nets=nets,
        pins=pins,
        instances=(instance,),
    )


def _build_discrete_carrier_graph(
    graph: TopologyGraph,
    carrier: NativeStdCellCarrier,
    *,
    name: str,
) -> TopologyGraph:
    carrier_graph = TopologyGraph(name)
    nets = _carrier_unique_nets(carrier)
    for net in nets:
        carrier_graph.add_pin(net, _net_role_for_net(graph, net))
    for record in carrier.devices:
        original = graph.devices[record.device]
        carrier_graph.add_device(
            Device(
                record.device,
                original.role if isinstance(original.role, DeviceRole) else DeviceRole.DRIVER,
                original.model,
                ("D", "G", "S", "B"),
            )
        )
    memberships: dict[str, list[str]] = {net: [f"{net}.PIN"] for net in nets}
    for record in carrier.devices:
        memberships[record.drain_net].append(f"{record.device}.D")
        memberships[record.gate_net].append(f"{record.device}.G")
        memberships[record.source_net].append(f"{record.device}.S")
        memberships[record.bulk_net].append(f"{record.device}.B")
    for net in nets:
        carrier_graph.add_net(net, _net_role_for_net(graph, net), tuple(memberships[net]))
    return carrier_graph


def _carrier_unique_nets(carrier: NativeStdCellCarrier) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for record in carrier.devices:
        for net in (record.gate_net, record.drain_net, record.source_net, record.bulk_net):
            if net and net not in seen:
                seen.add(net)
                ordered.append(net)
    return tuple(ordered)


def _net_role_for_net(graph: TopologyGraph, net: str) -> NetRole:
    if net in graph.pins:
        return graph.pins[net]
    if net in graph.nets:
        return graph.nets[net].role
    return NetRole.INTERNAL


def _pin_direction_for_net(graph: TopologyGraph, net: str) -> str:
    if net in graph.pins:
        role = graph.pins[net]
    elif net in graph.nets:
        role = graph.nets[net].role
    else:
        role = NetRole.INTERNAL
    if role is NetRole.INPUT:
        return "input"
    if role is NetRole.OUTPUT:
        return "output"
    return "inputOutput"


def _format_length_param(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        length_m = float(value)
        rounded_nm = round(length_m * 1e9, 3)
        if abs(rounded_nm - round(rounded_nm)) < 1e-9:
            return f"{int(round(rounded_nm))}n"
        text = f"{rounded_nm:.3f}".rstrip("0").rstrip(".")
        return f"{text}n"
    return str(value)
