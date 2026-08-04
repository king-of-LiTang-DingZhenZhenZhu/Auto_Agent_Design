from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


class PhysicalAdapterRequired(ValueError):
    """The frontend topology has no exact, reviewed physical adapter."""


@dataclass(frozen=True)
class AdaptedTopology:
    device_roles: dict[str, str]
    net_roles: dict[str, str]
    matched_groups: tuple[dict[str, Any], ...]
    symmetry_groups: tuple[tuple[str, ...], ...]
    routing_constraints: tuple[dict[str, Any], ...]
    critical_nets: tuple[str, ...]


def adapt_topology(topology: str, instances: Iterable[object], ports: Iterable[str]) -> AdaptedTopology:
    name = str(topology).strip().lower()
    rows = {str(getattr(item, "name")): item for item in instances}
    port_set = set(str(item) for item in ports)
    if name == "two_stage_ota":
        return _adapt_two_stage(rows, port_set)
    if name == "strongarm_latch":
        return _adapt_strongarm(rows, port_set)
    raise PhysicalAdapterRequired(f"physical_adapter_required: unsupported topology {topology!r}")


def _adapt_two_stage(rows: dict[str, object], ports: set[str]) -> AdaptedTopology:
    expected = {"Mbias", "Mdiff1", "Mdiff2", "Mmirr1", "Mmirr2", "Mtail", "Mcs", "Mload", "Rz", "Cc"}
    expected_ports = {"vip", "vin", "vout", "ibias", "vdd", "vss"}
    _require_exact("two_stage_ota", rows, expected, ports, expected_ports)
    _require_nodes(rows, {
        "Mdiff1": ("n_mirr", "vin", "n_tail", "vss"),
        "Mdiff2": ("n_s1", "vip", "n_tail", "vss"),
        "Mmirr1": ("n_mirr", "n_mirr", "vdd", "vdd"),
        "Mmirr2": ("n_s1", "n_mirr", "vdd", "vdd"),
        "Rz": ("n_s1", "n_rz"),
        "Cc": ("n_rz", "vout"),
    })
    roles = {
        "Mbias": "bias", "Mdiff1": "input_pair", "Mdiff2": "input_pair",
        "Mmirr1": "current_mirror", "Mmirr2": "current_mirror", "Mtail": "tail",
        "Mcs": "driver", "Mload": "load", "Rz": "comp_resistor", "Cc": "comp_capacitor",
    }
    net_roles = _base_net_roles(expected_ports, input_nets={"vip", "vin", "ibias"}, output_nets={"vout"})
    net_roles.update({"n_s1": "high_z", "n_rz": "compensation", "n_mirr": "bias", "n_tail": "bias"})
    return AdaptedTopology(
        roles,
        net_roles,
        (
            {"name": "input_pair", "devices": ["Mdiff1", "Mdiff2"], "style": "common_centroid", "require_dummies": True},
            {"name": "mirror_load", "devices": ["Mmirr1", "Mmirr2"], "style": "interdigitated", "require_dummies": True},
        ),
        (("Mdiff1", "Mdiff2"), ("Mmirr1", "Mmirr2")),
        (
            {"net": "vip", "kind": "matched_with", "value": ["vin"], "reason": "differential input"},
            {"net": "vout", "kind": "preferred_layer", "value": "M4", "reason": "output load"},
            {"net": "n_s1", "kind": "short", "value": True, "reason": "high impedance first-stage output"},
        ),
        ("vip", "vin", "n_s1", "vout", "vdd", "vss"),
    )


def _adapt_strongarm(rows: dict[str, object], ports: set[str]) -> AdaptedTopology:
    expected = {"M1", "M2", "M3", "M4", "M5", "M6", "M7", "S1", "S2", "S3", "S4"}
    expected_ports = {"vip", "vin", "clk", "outp", "outn", "vdd", "vss"}
    _require_exact("strongarm_latch", rows, expected, ports, expected_ports)
    _require_nodes(rows, {
        "M1": ("p", "vip", "ntail", "vss"), "M2": ("q", "vin", "ntail", "vss"),
        "M7": ("ntail", "clk", "vss", "vss"),
        "M3": ("outn", "outp", "p", "vss"), "M4": ("outp", "outn", "q", "vss"),
        "M5": ("outn", "outp", "vdd", "vdd"), "M6": ("outp", "outn", "vdd", "vdd"),
        "S1": ("p", "clk", "vdd", "vdd"), "S2": ("q", "clk", "vdd", "vdd"),
        "S3": ("outn", "clk", "vdd", "vdd"), "S4": ("outp", "clk", "vdd", "vdd"),
    })
    roles = {name: "load" for name in expected}
    roles.update({"M1": "input_pair", "M2": "input_pair", "M3": "driver", "M4": "driver", "M5": "driver", "M6": "driver", "M7": "tail", "S1": "bias", "S2": "bias", "S3": "bias", "S4": "bias"})
    net_roles = _base_net_roles(expected_ports, input_nets={"vip", "vin", "clk"}, output_nets={"outp", "outn"})
    net_roles.update({"clk": "clock", "p": "differential", "q": "differential", "ntail": "bias"})
    return AdaptedTopology(
        roles,
        net_roles,
        (
            {"name": "input_pair", "devices": ["M1", "M2"], "style": "common_centroid", "require_dummies": True},
            {"name": "regen_n", "devices": ["M3", "M4"], "style": "common_centroid", "require_dummies": True},
            {"name": "regen_p", "devices": ["M5", "M6"], "style": "common_centroid", "require_dummies": True},
            {"name": "precharge_internal", "devices": ["S1", "S2"], "style": "interdigitated", "require_dummies": True},
            {"name": "precharge_output", "devices": ["S3", "S4"], "style": "interdigitated", "require_dummies": True},
        ),
        (("M1", "M2"), ("M3", "M4"), ("M5", "M6"), ("S1", "S2"), ("S3", "S4")),
        (
            {"net": "vip", "kind": "matched_with", "value": ["vin"], "reason": "differential input"},
            {"net": "outp", "kind": "matched_with", "value": ["outn"], "reason": "differential output"},
            {"net": "clk", "kind": "preferred_layer", "value": "M4", "reason": "clock fanout"},
        ),
        ("vip", "vin", "clk", "outp", "outn", "p", "q", "ntail", "vdd", "vss"),
    )


def _require_exact(name: str, rows: dict[str, object], expected: set[str], ports: set[str], expected_ports: set[str]) -> None:
    if set(rows) != expected or ports != expected_ports:
        missing = sorted(expected - set(rows))
        extra = sorted(set(rows) - expected)
        raise PhysicalAdapterRequired(
            f"physical_adapter_required: {name} signature mismatch; missing={missing}, extra={extra}, ports={sorted(ports)}"
        )


def _require_nodes(rows: dict[str, object], expected: dict[str, tuple[str, ...]]) -> None:
    for name, nodes in expected.items():
        actual = tuple(str(item) for item in getattr(rows[name], "nodes"))
        if actual != nodes:
            raise PhysicalAdapterRequired(
                f"physical_adapter_required: {name} connectivity mismatch; expected={nodes}, actual={actual}"
            )


def _base_net_roles(ports: set[str], *, input_nets: set[str], output_nets: set[str]) -> dict[str, str]:
    result = {name: "internal" for name in ports}
    result.update({name: "input" for name in input_nets})
    result.update({name: "output" for name in output_nets})
    if "vdd" in ports:
        result["vdd"] = "supply"
    if "vss" in ports:
        result["vss"] = "ground"
    return result
