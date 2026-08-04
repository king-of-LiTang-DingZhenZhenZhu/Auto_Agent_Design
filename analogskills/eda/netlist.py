"""Netlist export and comparison helpers."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from analogskills._utils import coerce_dimension_m
from analogskills.contracts import DeviceRole, TerminalRef, TopologyGraph


@dataclass(frozen=True)
class LvsSourcePrecheckReport:
    source_ports: tuple[str, ...] = ()
    layout_ports: tuple[str, ...] = ()
    missing_layout_ports: tuple[str, ...] = ()
    extra_layout_ports: tuple[str, ...] = ()
    device_issues: tuple[str, ...] = ()
    model_names: dict[str, str] = field(default_factory=dict)

    @property
    def issues(self) -> tuple[str, ...]:
        issues = []
        if self.layout_ports:
            if len(self.layout_ports) != len(self.source_ports):
                issues.append(f"source port count {len(self.source_ports)} does not match layout pin count {len(self.layout_ports)}")
            issues.extend(f"missing layout port for source pin {port}" for port in self.missing_layout_ports)
            issues.extend(f"layout port {port} is not present in source pins" for port in self.extra_layout_ports)
        issues.extend(self.device_issues)
        return tuple(issues)

    @property
    def passed(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "issues": self.issues,
            "source_ports": self.source_ports,
            "layout_ports": self.layout_ports,
            "missing_layout_ports": self.missing_layout_ports,
            "extra_layout_ports": self.extra_layout_ports,
            "device_issues": self.device_issues,
            "model_names": dict(self.model_names),
        }


def _net_pin_alias_map(graph: TopologyGraph, terminal_map: dict[TerminalRef, str]) -> dict[str, str]:
    """Map internal net names to top-level pin names when a pin joins a net."""
    alias: dict[str, str] = {}
    for pin in graph.pins:
        net = terminal_map.get(TerminalRef(pin, "PIN"))
        if net and net != pin:
            alias[net] = pin
    return alias


def _apply_net_pin_alias(body: str, alias: dict[str, str]) -> str:
    if not alias:
        return body
    tokens = body.split()
    replaced = [alias.get(token, token) for token in tokens]
    return " ".join(replaced)


def export_spice_netlist(
    graph: TopologyGraph,
    path: str | Path,
    sizing: Mapping[str, Mapping[str, Any]] | None = None,
) -> Path:
    path = Path(path)
    pins = tuple(graph.pins)
    lines = [f"* analogskills export {graph.name}", ".SUBCKT " + graph.name + " " + " ".join(pins)]
    terminal_map = graph.terminal_net_map()
    pin_alias = _net_pin_alias_map(graph, terminal_map)
    for device in graph.devices.values():
        device_sizing = dict(device.parameters)
        if sizing:
            device_sizing.update(dict(sizing.get(device.name, {})))
        prefix, body = _device_netlist_line(device, terminal_map, sizing=device_sizing or None)
        body = _apply_net_pin_alias(body, pin_alias)
        lines.append(prefix + device.name + " " + body)
    lines.append(".ENDS " + graph.name)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def export_lvs_netlist(
    graph: TopologyGraph,
    sizing: Mapping[str, Mapping[str, Any]],
    path: str | Path,
    *,
    subckt_name: str | None = None,
    model_map: Mapping[str, str] | None = None,
    require_model_map: bool = False,
    mos_expansion: str = "macro",
    passive_device_style: str = "primitive",
) -> Path:
    """Export a Calibre LVS-friendly primitive netlist.

    MOS devices are emitted as ``M`` lines with D/G/S/B order when those
    terminals exist. BJTs are emitted as ``Q`` lines with C/B/E order.
    Resistors and capacitors are emitted as ``R`` and ``C`` primitives by
    default.  ``passive_device_style="subckt"`` emits mapped passive models
    as two-pin ``X`` instances with geometry parameters, matching foundry
    PCell LVS decks that extract native resistor/capacitor subcircuits instead
    of primitive ``R``/``C`` components.  Other devices fall back to
    subckt-style ``X`` lines.

    ``model_map`` maps a generic device ``model`` string (or a compound key
    ``"<role>:<model>"``) to the PDK-specific LVS model name, e.g.
    ``{"nmos": "nch_mac", "pmos": "pch_mac", "npn": "npn10"}``.
    When ``require_model_map`` is true, primitive LVS devices must have an
    explicit mapped model before the source netlist is written.

    ``mos_expansion="finger"`` emits one MOS primitive per layout finger
    instead of one primitive with ``nf=...``.  This is intended for Calibre
    bring-up when the deck extracts native PCells before finger reduction.
    When sizing contains a ``mos_unit_array`` realization selected by the
    layout solver, the source is expanded to the same unit-array abstraction
    before optional finger expansion so Calibre does not compare logical
    devices against explicit layout unit primitives.
    """
    path = Path(path)
    model_map = dict(model_map or {})
    mos_expansion_mode = str(mos_expansion or "macro").strip().lower().replace("-", "_")
    if mos_expansion_mode not in {"macro", "finger", "fingers", "finger_level"}:
        raise ValueError("mos_expansion must be 'macro' or 'finger'")
    if mos_expansion_mode in {"fingers", "finger_level"}:
        mos_expansion_mode = "finger"
    passive_style = _normalize_passive_lvs_style(passive_device_style)
    pins = tuple(graph.pins)
    name = subckt_name or graph.name
    helper_models: set[str] = set()
    passive_subckt_models: set[str] = set()
    lines = [f"* analogskills LVS export {graph.name}"]
    terminal_map = graph.terminal_net_map()
    pin_alias = _net_pin_alias_map(graph, terminal_map)
    device_lines: list[str] = []
    for device in graph.devices.values():
        device_sizing = dict(device.parameters)
        device_sizing.update(dict(sizing.get(device.name, {})))
        lvs_model = _lvs_model_name(device, model_map)
        if require_model_map and _device_requires_lvs_model(device) and lvs_model is None:
            raise ValueError(f"device {device.name} model {device.model} has no LVS model_map entry")
        _raise_for_lvs_export_terminal_issues(device, terminal_map)
        kind, _ = _lvs_required_terms(device)
        if passive_style == "subckt" and kind in {"resistor", "capacitor"} and (lvs_model or device.model):
            passive_subckt_models.add(str(lvs_model or device.model))
        device_lines.extend(
            _device_lvs_netlist_lines(
                device,
                terminal_map,
                sizing=device_sizing,
                lvs_model=lvs_model,
                pin_alias=pin_alias,
                mos_expansion=mos_expansion_mode,
                passive_device_style=passive_style,
            )
        )
        for helper_name, helper_body, helper_model in _n7_native_pode_helper_lines(
            device,
            terminal_map,
            sizing=device_sizing,
            model=lvs_model or device.model,
        ):
            helper_models.add(helper_model)
            device_lines.append(helper_name + " " + _apply_net_pin_alias(helper_body, pin_alias))
    if helper_models:
        lines.extend(_n7_native_pode_helper_stubs(helper_models))
    if passive_subckt_models:
        lines.extend(_passive_lvs_model_stubs(passive_subckt_models))
    lines.append(".SUBCKT " + name + " " + " ".join(pins))
    lines.extend(device_lines)
    lines.append(".ENDS " + name)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def prepare_lvs_source_netlist(
    graph: TopologyGraph,
    sizing: Mapping[str, Mapping[str, Any]],
    path: str | Path,
    *,
    subckt_name: str | None = None,
    layout_plan: object | None = None,
    layout_ports: Sequence[str] | None = None,
    model_map: Mapping[str, str] | None = None,
    require_model_map: bool = True,
    report_path: str | Path | None = None,
    mos_expansion: str = "macro",
    passive_device_style: str = "primitive",
) -> tuple[Path, LvsSourcePrecheckReport]:
    """Run LVS source precheck and export the source netlist only if it passes."""

    report = analyze_lvs_source_precheck(
        graph,
        sizing,
        layout_plan=layout_plan,
        layout_ports=layout_ports,
        model_map=model_map,
        require_model_map=require_model_map,
    )
    if not report.passed:
        _write_lvs_source_precheck_report(report, report_path, source_netlist=path)
        raise ValueError("LVS source precheck failed: " + "; ".join(report.issues))
    exported = export_lvs_netlist(
        graph,
        sizing,
        path,
        subckt_name=subckt_name,
        model_map=model_map,
        require_model_map=require_model_map,
        mos_expansion=mos_expansion,
        passive_device_style=passive_device_style,
    )
    _write_lvs_source_precheck_report(report, report_path, source_netlist=exported)
    return exported, report


def _device_lvs_netlist_lines(
    device: "analogskills.contracts.Device",
    terminal_map: dict[TerminalRef, str],
    *,
    sizing: Mapping[str, Any] | None,
    lvs_model: str | None = None,
    pin_alias: Mapping[str, str] | None = None,
    mos_expansion: str = "macro",
    passive_device_style: str = "primitive",
) -> tuple[str, ...]:
    """Return complete LVS netlist lines for one logical device."""

    if all(term in device.terminals for term in ("D", "G", "S", "B")):
        unit_array = _mos_lvs_unit_array_spec(sizing or {})
        if unit_array is not None:
            return _mos_unit_array_lvs_netlist_lines(
                device,
                terminal_map,
                sizing=sizing or {},
                unit_array=unit_array,
                lvs_model=lvs_model,
                pin_alias=pin_alias or {},
                mos_expansion=mos_expansion,
            )
    if mos_expansion == "finger" and all(term in device.terminals for term in ("D", "G", "S", "B")):
        return _mos_finger_lvs_netlist_lines(device, terminal_map, sizing=sizing or {}, lvs_model=lvs_model, pin_alias=pin_alias or {})
    if device.role == DeviceRole.COMP_CAPACITOR or _model_is_capacitor(device.model):
        unit_array = _passive_unit_array_spec(sizing or {})
        if unit_array is not None and bool(unit_array.get("requires_schematic_expansion", True)):
            return _passive_unit_array_lvs_netlist_lines(
                device,
                terminal_map,
                sizing=sizing or {},
                unit_array=unit_array,
                lvs_model=lvs_model,
                pin_alias=pin_alias or {},
                passive_device_style=passive_device_style,
            )
    prefix, body = _device_netlist_line(
        device,
        terminal_map,
        sizing=sizing,
        lvs_model=lvs_model,
        passive_device_style=passive_device_style,
    )
    return (prefix + device.name + " " + _apply_net_pin_alias(body, pin_alias or {}),)


def _mos_lvs_unit_array_spec(sizing: Mapping[str, Any]) -> Mapping[str, Any] | None:
    raw = sizing.get("mos_unit_array")
    if not isinstance(raw, Mapping) or not bool(raw.get("enabled", False)):
        return None
    try:
        unit_count = max(1, int(float(raw.get("unit_count", 1) or 1)))
        rows = max(1, int(float(raw.get("rows", 1) or 1)))
        cols = max(1, int(float(raw.get("cols", 1) or 1)))
    except (TypeError, ValueError):
        return None
    if unit_count <= 1:
        return None
    if rows * cols < unit_count:
        return None
    return dict(raw)


def _passive_unit_array_spec(sizing: Mapping[str, Any]) -> Mapping[str, Any] | None:
    raw = sizing.get("passive_unit_array")
    if not isinstance(raw, Mapping) or not bool(raw.get("enabled", False)):
        return None
    try:
        unit_count = max(1, int(float(raw.get("unit_count", 1) or 1)))
        rows = max(1, int(float(raw.get("rows", 1) or 1)))
        cols = max(1, int(float(raw.get("cols", 1) or 1)))
    except (TypeError, ValueError):
        return None
    if unit_count <= 1:
        return None
    if rows * cols < unit_count:
        return None
    return dict(raw)


def _passive_unit_array_lvs_netlist_lines(
    device: "analogskills.contracts.Device",
    terminal_map: dict[TerminalRef, str],
    *,
    sizing: Mapping[str, Any],
    unit_array: Mapping[str, Any],
    lvs_model: str | None = None,
    pin_alias: Mapping[str, str],
    passive_device_style: str = "primitive",
) -> tuple[str, ...]:
    unit_count = max(1, int(float(unit_array.get("unit_count", 1) or 1)))
    unit_sizing = _passive_unit_array_unit_lvs_sizing(sizing, unit_array)
    prefix, body = _device_netlist_line(
        device,
        terminal_map,
        sizing=unit_sizing,
        lvs_model=lvs_model,
        passive_device_style=passive_device_style,
    )
    body = _apply_net_pin_alias(body, pin_alias or {})
    return tuple(f"{prefix}{device.name}_u{index} {body}" for index in range(unit_count))


def _passive_unit_array_unit_lvs_sizing(sizing: Mapping[str, Any], unit_array: Mapping[str, Any]) -> dict[str, Any]:
    unit = dict(sizing)
    unit.pop("passive_unit_array", None)
    unit["M"] = 1
    unit["m"] = 1
    unit["multi"] = 1
    unit_pcell_params = unit_array.get("unit_pcell_params", {})
    if isinstance(unit_pcell_params, Mapping) and unit_pcell_params:
        unit["pcell_overrides"] = {str(key): value for key, value in unit_pcell_params.items()}
    return unit


def _mos_unit_array_lvs_netlist_lines(
    device: "analogskills.contracts.Device",
    terminal_map: dict[TerminalRef, str],
    *,
    sizing: Mapping[str, Any],
    unit_array: Mapping[str, Any],
    lvs_model: str | None = None,
    pin_alias: Mapping[str, str],
    mos_expansion: str = "macro",
) -> tuple[str, ...]:
    unit_count = max(1, int(float(unit_array.get("unit_count", 1) or 1)))
    lines: list[str] = []
    for unit_index in range(unit_count):
        instance_name = f"{device.name}_u{unit_index}"
        unit_sizing = _mos_unit_array_unit_lvs_sizing(sizing, unit_array)
        if mos_expansion == "finger":
            lines.extend(
                _mos_finger_lvs_netlist_lines(
                    device,
                    terminal_map,
                    sizing=unit_sizing,
                    lvs_model=lvs_model,
                    pin_alias=pin_alias,
                    instance_name=instance_name,
                )
            )
            continue
        prefix, body = _device_netlist_line(device, terminal_map, sizing=unit_sizing, lvs_model=lvs_model)
        lines.append(prefix + instance_name + " " + _apply_net_pin_alias(body, pin_alias or {}))
    return tuple(lines)


def _mos_unit_array_unit_lvs_sizing(sizing: Mapping[str, Any], unit_array: Mapping[str, Any]) -> dict[str, Any]:
    unit = dict(sizing)
    unit.pop("mos_unit_array", None)
    if unit_array.get("unit_total_width_m") is not None or unit_array.get("unit_width_m") is not None:
        unit["W"] = float(unit_array.get("unit_total_width_m", unit_array.get("unit_width_m")))
    if unit_array.get("unit_length_m") is not None:
        unit["L"] = float(unit_array.get("unit_length_m"))
    if unit_array.get("unit_nf") is not None:
        unit["nf"] = max(1, int(float(unit_array.get("unit_nf") or 1)))
        unit.pop("fingers", None)
    if unit_array.get("unit_m") is not None:
        unit["m"] = max(1, int(float(unit_array.get("unit_m") or 1)))
        unit["M"] = unit["m"]
        unit.pop("simM", None)
    return unit


def _mos_finger_lvs_netlist_lines(
    device: "analogskills.contracts.Device",
    terminal_map: dict[TerminalRef, str],
    *,
    sizing: Mapping[str, Any],
    lvs_model: str | None = None,
    pin_alias: Mapping[str, str],
    instance_name: str | None = None,
) -> tuple[str, ...]:
    model = lvs_model if lvs_model is not None else device.model
    nets = [_apply_net_pin_alias(terminal_map.get(TerminalRef(device.name, term), "__UNCONNECTED__"), pin_alias) for term in ("D", "G", "S", "B")]
    length = _dimension_m(sizing, ("L", "l", "length"), 0.18e-6)
    width = _dimension_m(sizing, ("W", "w", "width"), 1e-6)
    nf = _first_positive_integral_param(sizing, ("nf", "fingers"), 1, "nf/fingers")
    multiplier = _first_positive_integral_param(sizing, ("m", "M", "simM"), 1, "m/M/simM")
    per_finger_width = width / float(nf)
    params = f"W={_format_spice_dimension(per_finger_width)} L={_format_spice_dimension(length)}"
    source_name = instance_name or device.name
    return tuple(
        f"M_{source_name}_F{index + 1} " + " ".join(nets) + f" {model} {params}"
        for index in range(nf * multiplier)
    )


def analyze_lvs_source_precheck(
    graph: TopologyGraph,
    sizing: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    layout_plan: object | None = None,
    layout_ports: Sequence[str] | None = None,
    model_map: Mapping[str, str] | None = None,
    require_model_map: bool = False,
) -> LvsSourcePrecheckReport:
    """Check LVS source/layout structural consistency before running Calibre."""

    source_ports = tuple(str(port) for port in graph.pins)
    observed_layout_ports = _layout_ports(layout_plan, layout_ports)
    missing_ports = tuple(port for port in source_ports if observed_layout_ports and port not in observed_layout_ports)
    extra_ports = tuple(port for port in observed_layout_ports if port not in source_ports)
    sizing_map = {str(name): dict(values) for name, values in dict(sizing or {}).items()}
    model_map_dict = dict(model_map or {})
    device_issues: list[str] = []
    model_names: dict[str, str] = {}

    try:
        terminal_map = graph.terminal_net_map()
    except ValueError as exc:
        terminal_map = {}
        device_issues.append(str(exc))

    for issue in graph.validate():
        if issue not in device_issues:
            device_issues.append(issue)

    for device in graph.devices.values():
        device_sizing = dict(device.parameters)
        device_sizing.update(sizing_map.get(device.name, {}))
        lvs_model = _lvs_model_name(device, model_map_dict)
        model_names[device.name] = lvs_model or device.model
        if require_model_map and _device_requires_lvs_model(device) and lvs_model is None:
            device_issues.append(f"device {device.name} model {device.model} has no LVS model_map entry")
        kind, required_terms = _lvs_required_terms(device)
        device_issues.extend(_device_terminal_precheck(device, kind, required_terms, terminal_map))
        if kind == "MOS":
            device_issues.extend(_mos_param_semantics_issues(device.name, device_sizing))
        elif kind == "resistor":
            device_issues.extend(_positive_scalar_param_issues(device.name, kind, "resistance", ("R", "r", "r_ohm"), device_sizing))
        elif kind == "capacitor":
            device_issues.extend(_positive_scalar_param_issues(device.name, kind, "capacitance", ("C", "c", "c_f"), device_sizing))
        elif kind == "BJT":
            device_issues.extend(_bjt_param_semantics_issues(device.name, device_sizing))

    return LvsSourcePrecheckReport(
        source_ports=source_ports,
        layout_ports=observed_layout_ports,
        missing_layout_ports=missing_ports,
        extra_layout_ports=extra_ports,
        device_issues=tuple(dict.fromkeys(device_issues)),
        model_names=model_names,
    )


def _lvs_model_name(device: "analogskills.contracts.Device", model_map: dict[str, str]) -> str | None:
    if not model_map:
        return None
    key = f"{device.role.name}:{device.model}"
    if key in model_map:
        return model_map[key]
    if device.model in model_map:
        return model_map[device.model]
    role_key = device.role.name.lower()
    if role_key in model_map:
        return model_map[role_key]
    return None


def _write_lvs_source_precheck_report(
    report: LvsSourcePrecheckReport,
    report_path: str | Path | None,
    *,
    source_netlist: str | Path,
) -> Path | None:
    if report_path is None:
        return None
    path_obj = Path(report_path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    payload = report.to_dict()
    payload["source_netlist"] = str(source_netlist)
    path_obj.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path_obj


def _device_netlist_line(
    device: "analogskills.contracts.Device",
    terminal_map: dict[TerminalRef, str],
    sizing: Mapping[str, Any] | None,
    lvs_model: str | None = None,
    passive_device_style: str = "primitive",
) -> tuple[str, str]:
    """Return (prefix, body) for a single device netlist line."""
    model = lvs_model if lvs_model is not None else device.model
    passive_style = _normalize_passive_lvs_style(passive_device_style)
    if all(term in device.terminals for term in ("D", "G", "S", "B")):
        nets = [terminal_map.get(TerminalRef(device.name, term), "__UNCONNECTED__") for term in ("D", "G", "S", "B")]
        params = _mos_lvs_params(sizing or {})
        return "M_", " ".join(nets) + f" {model}" + (f" {params}" if params else "")
    if all(term in device.terminals for term in ("C", "B", "E")):
        nets = [terminal_map.get(TerminalRef(device.name, term), "__UNCONNECTED__") for term in ("C", "B", "E")]
        params = _bjt_lvs_params(sizing or {}, model)
        return "Q_", " ".join(nets) + f" {model}" + (f" {params}" if params else "")
    if device.role == DeviceRole.COMP_CAPACITOR or _model_is_capacitor(device.model):
        nets = [terminal_map.get(TerminalRef(device.name, term), "__UNCONNECTED__") for term in ("PLUS", "MINUS")]
        if passive_style == "subckt":
            params = _capacitor_lvs_subckt_params(sizing or {})
            return "X_", " ".join(nets) + f" {model}" + (f" {params}" if params else "")
        value = ""
        if sizing:
            value = _format_spice_value(_primitive_scalar_value(sizing, ("C", "c", "c_f"), 1e-15, "capacitance"))
        return "C_", " ".join(nets) + (f" {value}" if value else "") + (f" {model}" if model else "")
    if device.role == DeviceRole.COMP_RESISTOR or _model_is_resistor(device.model) or (all(term in device.terminals for term in ("PLUS", "MINUS")) and not _model_is_capacitor(device.model)):
        nets = [terminal_map.get(TerminalRef(device.name, term), "__UNCONNECTED__") for term in ("PLUS", "MINUS")]
        if passive_style == "subckt":
            params = _resistor_lvs_subckt_params(sizing or {})
            return "X_", " ".join(nets) + f" {model}" + (f" {params}" if params else "")
        value = ""
        if sizing:
            value = _format_spice_value(_primitive_scalar_value(sizing, ("R", "r", "r_ohm"), 1e3, "resistance"))
        return "R_", " ".join(nets) + (f" {value}" if value else "") + (f" {model}" if model else "")
    nets = [terminal_map.get(TerminalRef(device.name, term), "__UNCONNECTED__") for term in device.terminals]
    params = " ".join(f"{k}={_format_spice_value(v)}" for k, v in (sizing or {}).items())
    return "X", " ".join(nets) + f" {device.model}" + (f" {params}" if params else "")


def _n7_native_pode_helper_lines(
    device: "analogskills.contracts.Device",
    terminal_map: Mapping[TerminalRef, str],
    *,
    sizing: Mapping[str, Any],
    model: str,
) -> tuple[tuple[str, str, str], ...]:
    if not bool(sizing.get("__emit_pode_helpers__", False)):
        return ()
    model_name = str(model or "").lower()
    if model_name not in {"nch_svt_mac", "pch_svt_mac"}:
        return ()
    nets = {
        term: terminal_map.get(TerminalRef(device.name, term), "__UNCONNECTED__")
        for term in ("D", "G", "S", "B")
    }
    helper_model = "npode_svt_mac" if model_name.startswith("nch") else "ppode_svt_mac"
    helper_params = _n7_native_pode_helper_params(sizing)
    return (
        (f"X_{device.name}_PODE_L", f'{nets["S"]} {nets["G"]} {nets["B"]} {helper_model}{helper_params}', helper_model),
        (f"X_{device.name}_PODE_R", f'{nets["D"]} {nets["G"]} {nets["B"]} {helper_model}{helper_params}', helper_model),
    )


def _normalize_passive_lvs_style(style: str | None) -> str:
    text = str(style or "primitive").strip().lower().replace("-", "_")
    aliases = {
        "": "primitive",
        "primitive": "primitive",
        "spice_primitive": "primitive",
        "rc_primitive": "primitive",
        "subckt": "subckt",
        "subcircuit": "subckt",
        "native_subckt": "subckt",
        "pcell_subckt": "subckt",
        "native_pcell_subckt": "subckt",
    }
    if text not in aliases:
        raise ValueError("passive_device_style must be 'primitive' or 'subckt'")
    return aliases[text]


def _resistor_lvs_subckt_params(params: Mapping[str, Any]) -> str:
    effective = _passive_effective_lvs_params(params)
    width = _dimension_m(effective, ("w", "W", "width", "wr", "sumW"), 2e-6)
    length = _dimension_m(effective, ("l", "L", "length", "lr", "sumL"), 10e-6)
    multiplier = _first_positive_integral_param(effective, ("M", "m", "multi"), 1, "M/m/multi")
    parts = [f"l={_format_spice_dimension(length)}", f"w={_format_spice_dimension(width)}"]
    if multiplier > 1:
        parts.append(f"multi={multiplier}")
    return " ".join(parts)


def _capacitor_lvs_subckt_params(params: Mapping[str, Any]) -> str:
    effective = _passive_effective_lvs_params(params)
    width = _dimension_m(effective, ("wr", "w", "W", "width"), 1e-6)
    length = _dimension_m(effective, ("lr", "l", "L", "length"), 1e-6)
    multiplier = _first_positive_integral_param(effective, ("M", "m", "multi"), 1, "M/m/multi")
    parts = [f"lr={_format_spice_dimension(length)}", f"wr={_format_spice_dimension(width)}"]
    if multiplier > 1:
        parts.append(f"multi={multiplier}")
    return " ".join(parts)


def _passive_effective_lvs_params(params: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(params or {})
    overrides = result.get("pcell_overrides", {})
    if isinstance(overrides, Mapping):
        result.update({str(key): value for key, value in overrides.items()})
    return result


def _passive_lvs_model_stubs(models: set[str]) -> tuple[str, ...]:
    lines: list[str] = []
    for model in sorted(str(model) for model in models if str(model)):
        lines.append(".SUBCKT " + model + " PLUS MINUS")
        lines.append(".ENDS " + model)
    return tuple(lines)


def _n7_native_pode_helper_params(params: Mapping[str, Any]) -> str:
    parts: list[str] = []
    nfin = _optional_positive_integral_param(params, ("nfin", "NFIN"), "nfin/NFIN")
    if nfin is not None:
        parts.append(f"nfin={nfin}")
    # Native N7 PODE helpers consistently extract at 3.6e-08 in this deck.
    # Keep the source helper primitive aligned so LVS reports focus on
    # connectivity rather than repeated helper-property noise.
    length = 0.036e-6
    parts.append(f"l={length:g}")
    return "" if not parts else " " + " ".join(parts)


def _n7_native_pode_helper_stubs(models: set[str]) -> tuple[str, ...]:
    lines: list[str] = []
    for model in sorted(str(model) for model in models if str(model)):
        lines.append(".SUBCKT " + model + " S G B")
        lines.append(".ENDS")
    return tuple(lines)


def compare_topology_terminal_map(graph: TopologyGraph, extracted: dict[str, str]) -> dict[str, tuple[str | None, str | None]]:
    expected = {str(term): net for term, net in graph.terminal_net_map().items()}
    mismatches = {
        term: (expected.get(term), extracted.get(term))
        for term in sorted(set(expected) | set(extracted))
        if expected.get(term) != extracted.get(term)
    }
    for device in graph.devices.values():
        for group in _lvs_swappable_terminal_groups(device):
            group_terms = tuple(f"{device.name}.{terminal}" for terminal in group)
            if not all(term in expected for term in group_terms):
                continue
            if not all(term in extracted for term in group_terms):
                continue
            expected_nets = sorted(str(expected.get(term, "")) for term in group_terms)
            observed_nets = sorted(str(extracted.get(term, "")) for term in group_terms)
            if expected_nets != observed_nets:
                continue
            for term in group_terms:
                mismatches.pop(term, None)
    return mismatches


def _lvs_swappable_terminal_groups(device: "analogskills.contracts.Device") -> tuple[tuple[str, ...], ...]:
    kind, _required_terms = _lvs_required_terms(device)
    if kind == "MOS":
        return (("D", "S"),)
    if kind in {"resistor", "capacitor"}:
        return (("PLUS", "MINUS"),)
    return ()


def _layout_ports(layout_plan: object | None, layout_ports: Sequence[str] | None) -> tuple[str, ...]:
    if layout_ports is not None:
        return tuple(dict.fromkeys(str(port) for port in layout_ports if str(port)))
    if layout_plan is None:
        return ()
    ports = []
    for pin in getattr(layout_plan, "pins", ()):
        # A layout pin can intentionally expose a top-level port name while
        # connecting to an internal implementation net, e.g. source pin VREF
        # mapped onto internal net diode1 in a bandgap core.  LVS source port
        # precheck must compare the exported port name first; using pin.net
        # would falsely report "missing VREF / extra diode1".
        port = str(getattr(pin, "name", "") or getattr(pin, "net", ""))
        if port:
            ports.append(port)
    return tuple(dict.fromkeys(ports))


def _lvs_required_terms(device: "analogskills.contracts.Device") -> tuple[str, tuple[str, ...]]:
    terminals = set(device.terminals)
    model = (device.model or "").lower()
    if {"D", "G", "S"} <= terminals or "mos" in model:
        return "MOS", ("D", "G", "S", "B")
    if {"C", "B", "E"} <= terminals:
        return "BJT", ("C", "B", "E")
    if device.role == DeviceRole.COMP_CAPACITOR or _model_is_capacitor(device.model):
        return "capacitor", ("PLUS", "MINUS")
    if device.role == DeviceRole.COMP_RESISTOR or _model_is_resistor(device.model) or ({"PLUS", "MINUS"} <= terminals and not _model_is_capacitor(device.model)):
        return "resistor", ("PLUS", "MINUS")
    return "subckt", tuple(device.terminals)


def _device_requires_lvs_model(device: "analogskills.contracts.Device") -> bool:
    kind, _terms = _lvs_required_terms(device)
    return kind in {"MOS", "BJT", "resistor", "capacitor"}


def _device_terminal_precheck(
    device: "analogskills.contracts.Device",
    kind: str,
    required_terms: tuple[str, ...],
    terminal_map: Mapping[TerminalRef, str],
) -> tuple[str, ...]:
    issues: list[str] = []
    declared = set(device.terminals)
    missing_declared = tuple(term for term in required_terms if term not in declared)
    if missing_declared:
        issues.append(f"{kind} device {device.name} missing required terminal(s): {', '.join(missing_declared)}")
    extra_declared = tuple(term for term in device.terminals if term not in required_terms)
    if kind != "subckt" and extra_declared:
        issues.append(f"{kind} device {device.name} has unsupported extra LVS terminal(s): {', '.join(extra_declared)}")
    for term in required_terms:
        if term not in declared:
            continue
        net = terminal_map.get(TerminalRef(device.name, term))
        if not net:
            if kind == "MOS" and term == "B":
                issues.append(f"MOS device {device.name} missing bulk connection")
            else:
                issues.append(f"{kind} device {device.name} terminal {term} is unconnected")
    return tuple(issues)


def _raise_for_lvs_export_terminal_issues(
    device: "analogskills.contracts.Device",
    terminal_map: Mapping[TerminalRef, str],
) -> None:
    kind, required_terms = _lvs_required_terms(device)
    if kind == "subckt":
        return
    issues = _device_terminal_precheck(device, kind, required_terms, terminal_map)
    if issues:
        raise ValueError("LVS primitive terminal issues: " + "; ".join(issues))


def _mos_param_semantics_issues(device_name: str, params: Mapping[str, Any]) -> tuple[str, ...]:
    issues = []
    nf_values, nf_invalid = _present_int_params(params, ("nf", "fingers"))
    for key, value in nf_invalid.items():
        issues.append(f"MOS device {device_name} has invalid nf/fingers parameter {key}={value!r}")
    if len(set(nf_values.values())) > 1:
        issues.append(f"MOS device {device_name} has inconsistent nf/fingers values {nf_values}")
    mult_values, mult_invalid = _present_int_params(params, ("m", "M", "simM"))
    for key, value in mult_invalid.items():
        issues.append(f"MOS device {device_name} has invalid m/M/simM parameter {key}={value!r}")
    if len(set(mult_values.values())) > 1:
        issues.append(f"MOS device {device_name} has inconsistent m/M/simM values {mult_values}")
    for label, values in (("nf/fingers", nf_values), ("m/M/simM", mult_values)):
        for key, value in values.items():
            if value < 1:
                issues.append(f"MOS device {device_name} has invalid {label} parameter {key}={value}")
    issues.extend(_mos_dimension_semantics_issues(device_name, params, "width", ("W", "w", "width")))
    issues.extend(_mos_dimension_semantics_issues(device_name, params, "length", ("L", "l", "length")))
    return tuple(issues)


def _positive_scalar_param_issues(
    device_name: str,
    kind: str,
    label: str,
    keys: tuple[str, ...],
    params: Mapping[str, Any],
) -> tuple[str, ...]:
    issues = []
    values: dict[str, float] = {}
    for key in keys:
        if key not in params:
            continue
        value = _parse_positive_finite_scalar(params[key])
        if value is None:
            issues.append(f"{kind} device {device_name} has invalid {label} parameter {key}={params[key]!r}")
        else:
            values[key] = value
    rounded_values = {key: round(value, 24) for key, value in values.items()}
    if len(set(rounded_values.values())) > 1:
        issues.append(f"{kind} device {device_name} has inconsistent {label} parameter aliases {values}")
    return tuple(issues)


def _bjt_param_semantics_issues(device_name: str, params: Mapping[str, Any]) -> tuple[str, ...]:
    issues = []
    mult_values, mult_invalid = _present_int_params(params, ("m", "M"))
    for key, value in mult_invalid.items():
        issues.append(f"BJT device {device_name} has invalid m/M parameter {key}={value!r}")
    if len(set(mult_values.values())) > 1:
        issues.append(f"BJT device {device_name} has inconsistent m/M values {mult_values}")
    for key, value in mult_values.items():
        if value < 1:
            issues.append(f"BJT device {device_name} has invalid m/M parameter {key}={value}")
    return tuple(issues)


def _present_int_params(params: Mapping[str, Any], keys: tuple[str, ...]) -> tuple[dict[str, int], dict[str, Any]]:
    values: dict[str, int] = {}
    invalid: dict[str, Any] = {}
    for key in keys:
        if key not in params:
            continue
        value = _parse_integral_param(params[key])
        if value is None:
            invalid[key] = params[key]
        else:
            values[key] = value
    return values, invalid


def _parse_integral_param(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not number.is_integer():
        return None
    return int(number)


def _mos_dimension_semantics_issues(
    device_name: str,
    params: Mapping[str, Any],
    label: str,
    keys: tuple[str, ...],
) -> tuple[str, ...]:
    issues: list[str] = []
    values: dict[str, float] = {}
    for key, unit in _dimension_param_keys(keys):
        if key not in params:
            continue
        try:
            values[key] = _dimension_value_m(params[key], unit)
        except (TypeError, ValueError):
            issues.append(f"MOS device {device_name} has invalid {label} parameter {key}={params[key]!r}")
    rounded_values = {key: round(value, 18) for key, value in values.items()}
    if len(set(rounded_values.values())) > 1:
        issues.append(f"MOS device {device_name} has inconsistent {label} parameter aliases {values}")
    return tuple(issues)


def _mos_lvs_params(params: Mapping[str, Any]) -> str:
    length = _dimension_m(params, ("L", "l", "length"), 0.18e-6)
    multiplier = _first_positive_integral_param(params, ("m", "M", "simM"), 1, "m/M/simM")
    nfin = _optional_positive_integral_param(params, ("nfin", "NFIN"), "nfin/NFIN")
    if nfin is not None:
        parts = [f"l={_format_spice_dimension(length)}", f"nfin={nfin}"]
        if multiplier > 1:
            parts.append(f"M={multiplier}")
        return " ".join(parts)
    width = _dimension_m(params, ("W", "w", "width"), 1e-6)
    nf = _first_positive_integral_param(params, ("nf", "fingers"), 1, "nf/fingers")
    return f"W={_format_spice_dimension(width)} L={_format_spice_dimension(length)} nf={nf} M={multiplier}"


def _bjt_lvs_params(params: Mapping[str, Any], model: str = "") -> str:
    multiplier = _first_positive_integral_param(params, ("m", "M"), 1, "m/M")
    # Emit the emitter area as a positional area factor.  The TSMC Calibre LVS
    # deck classifies NPN/PNP variants by the area of the emitter OD shape
    # (in um^2) and expects it after the model name.
    area = _bjt_emitter_area_um2(model)
    parts = []
    if area is not None:
        parts.append(_format_spice_area_um2(area))
    if multiplier > 1:
        parts.append(f"M={multiplier}")
    return " ".join(parts)


def _bjt_emitter_area_um2(model: str) -> float | None:
    name = (model or "").lower()
    if "10" in name:
        return 100.0
    if "5" in name:
        return 25.0
    if "2" in name:
        return 4.0
    if "1d6" in name or "1.6" in name:
        return 2.56
    return None


def _format_spice_area_um2(value_um2: float) -> str:
    # Calibre BJT ``a`` is compared in square microns, but SPICE numeric
    # suffixes are in base SI units.  A plain "25" is interpreted as 25 m^2
    # (=2.5e13 um^2); "25p" is 25e-12 m^2 (=25 um^2).
    return f"{float(value_um2):.12g}p"


def _model_is_resistor(model: str) -> bool:
    return "res" in (model or "").lower() or (model or "").lower().startswith("r")


def _model_is_capacitor(model: str) -> bool:
    lowered = (model or "").lower()
    return "cap" in lowered or lowered.startswith("c")


def _dimension_m(params: Mapping[str, Any], keys: tuple[str, ...], default_m: float) -> float:
    for key in keys:
        if key in params:
            return _dimension_value_m(params[key], "auto")
        um_key = f"{key}_um"
        if um_key in params:
            return _dimension_value_m(params[um_key], "um")
        nm_key = f"{key}_nm"
        if nm_key in params:
            return _dimension_value_m(params[nm_key], "nm")
    return default_m


def _coerce_dimension_m(value: float) -> float:
    return coerce_dimension_m(value)


def _dimension_param_keys(keys: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for key in keys:
        result.append((key, "auto"))
        result.append((f"{key}_um", "um"))
        result.append((f"{key}_nm", "nm"))
    return tuple(result)


def _dimension_value_m(value: Any, unit: str) -> float:
    if isinstance(value, bool):
        raise ValueError("physical dimensions must be numeric")
    if isinstance(value, str):
        stripped = value.strip().lower()
        suffix_scale = (
            ("um", 1e-6),
            ("u", 1e-6),
            ("nm", 1e-9),
            ("n", 1e-9),
            ("pm", 1e-12),
            ("p", 1e-12),
        )
        for suffix, scale in suffix_scale:
            if stripped.endswith(suffix):
                number = float(stripped[: -len(suffix)]) * scale
                break
        else:
            number = float(stripped)
            if unit == "auto":
                number = _coerce_dimension_m(number)
            elif unit == "um":
                number *= 1e-6
            elif unit == "nm":
                number *= 1e-9
            else:
                raise ValueError(f"unknown dimension unit {unit!r}")
        if not math.isfinite(number) or number <= 0:
            raise ValueError("physical dimensions must be positive finite values")
        return number
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("physical dimensions must be positive finite values")
    if unit == "auto":
        return _coerce_dimension_m(number)
    if unit == "um":
        return number * 1e-6
    if unit == "nm":
        return number * 1e-9
    raise ValueError(f"unknown dimension unit {unit!r}")


def _parse_positive_finite_scalar(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _primitive_scalar_value(params: Mapping[str, Any], keys: tuple[str, ...], default: float, label: str) -> float:
    for key in keys:
        if key not in params:
            continue
        value = _parse_positive_finite_scalar(params[key])
        if value is None:
            raise ValueError(f"{label} parameter {key} must be a positive finite value, got {params[key]!r}")
        return value
    return default


def _first_positive_integral_param(params: Mapping[str, Any], keys: tuple[str, ...], default: int, label: str) -> int:
    for key in keys:
        if key not in params:
            continue
        value = _parse_integral_param(params[key])
        if value is None or value < 1:
            raise ValueError(f"{label} parameter {key} must be a positive integer, got {params[key]!r}")
        return value
    return default


def _optional_positive_integral_param(params: Mapping[str, Any], keys: tuple[str, ...], label: str) -> int | None:
    for key in keys:
        if key not in params:
            continue
        value = _parse_integral_param(params[key])
        if value is None or value < 1:
            raise ValueError(f"{label} parameter {key} must be a positive integer, got {params[key]!r}")
        return value
    return None


def _format_spice_dimension(value_m: float) -> str:
    value_um = value_m * 1e6
    return f"{value_um:g}u"


def _format_spice_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)
