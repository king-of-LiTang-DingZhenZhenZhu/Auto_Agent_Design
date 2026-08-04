"""PCell terminal access-point helpers."""
from __future__ import annotations

from dataclasses import dataclass, replace
import ast
import json
from math import hypot
from pathlib import Path
from typing import Any, Mapping, Sequence

from analogskills.pcell.calibration import PCellCalibrationCache
from analogskills.pcell.generation import PCellInstancePlan
from analogskills.pdk import PCellTemplate, PdkConfig


@dataclass(frozen=True)
class PCellPin:
    instance: str
    terminal: str
    xy_um: tuple[float, float]
    layer: str
    contact_layer: str = ""
    net: str = ""
    source: str = "pdk_template"
    confidence: float = 1.0
    bbox_um: tuple[float, float, float, float] | None = None
    warnings: tuple[str, ...] = ()
    access_kind: str = "routable"
    lvs_safe: bool = True
    access_priority: int = 50


@dataclass(frozen=True)
class PCellTerminalAccessIssue:
    instance: str
    terminal: str
    message: str
    xy_um: tuple[float, float]
    bbox_um: tuple[float, float, float, float]
    severity: str = "warning"
    net: str = ""
    source: str = ""
    confidence: float = 0.0


@dataclass(frozen=True)
class PCellTerminalAccessReport:
    pins: tuple[PCellPin, ...] = ()
    issues: tuple[PCellTerminalAccessIssue, ...] = ()
    fallback_risks: tuple[PCellTerminalAccessIssue, ...] = ()

    @property
    def passed(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    @property
    def blocking_issues(self) -> tuple[PCellTerminalAccessIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "pins": tuple(_pin_to_dict(pin) for pin in self.pins),
            "fallback_risks": tuple(_issue_to_dict(issue) for issue in self.fallback_risks),
            "issues": tuple(_issue_to_dict(issue) for issue in self.issues),
        }


class PCellTerminalRequiresTap(ValueError):
    """Raised when calibration proves a terminal has no direct routable access."""

    def __init__(self, instance: str, terminal: str, message: str | None = None) -> None:
        self.instance = str(instance)
        self.terminal = str(terminal)
        super().__init__(
            message
            or f"terminal {self.instance}.{self.terminal} requires external tap or explicit routable access; "
            "calibration found no routable access candidate"
        )


class PCellTerminalAccessor:
    """Resolve terminal access points from calibration data or PDK metadata.

    The accessor prefers OA-derived calibration entries when available. If no
    calibration entry matches the instance parameters, it falls back to
    ``PCellTemplate.terminal_access`` and finally built-in coarse defaults.
    """

    def __init__(
        self,
        pdk: PdkConfig,
        calibration_cache: PCellCalibrationCache | None = None,
        *,
        allow_nearest_calibration: bool = False,
        max_nearest_distance: float = 0.25,
    ) -> None:
        self.pdk = pdk
        self.calibration_cache = calibration_cache
        self.allow_nearest_calibration = allow_nearest_calibration
        self.max_nearest_distance = float(max_nearest_distance)

    def get_terminal_pin(self, instance: PCellInstancePlan, terminal: str) -> PCellPin:
        metadata_pins = _metadata_terminal_pins(self.pdk, instance, terminal)
        if metadata_pins:
            return metadata_pins[0]
        if self.pdk.name == "tsmcn7" and instance.logical_name in {"nmos", "pmos"} and str(terminal) == "B":
            raise PCellTerminalRequiresTap(
                instance.name,
                terminal,
                f"terminal {instance.name}.{terminal} requires external tap for tsmcn7 native MOS body connection",
            )
        crn28_body_pin = _crn28_mos_body_tap_pin(self.pdk, instance, terminal)
        if crn28_body_pin is not None:
            return crn28_body_pin
        calibrated = self.get_terminal_pins(instance, terminal)
        if calibrated:
            return calibrated[0]
        template = self.pdk.pcell_template_for(instance.logical_name)
        entry = _terminal_entry(instance, terminal, template, self.pdk)
        local_xy = _entry_xy(entry, _terminal_context(instance))
        xy = _absolute_xy(instance.xy_um, local_xy, instance.orient)
        source = str(entry.get("source", "pdk_template"))
        return _annotate_terminal_access(
            self.pdk,
            instance,
            terminal,
            PCellPin(
            instance=instance.name,
            terminal=terminal,
            xy_um=self.pdk.rules.snap_point_um(xy),
            layer=str(entry.get("layer", self.pdk.layer_map.metals[0])),
            contact_layer=str(entry.get("contact_layer", self.pdk.layer_map.contact)),
            net=instance.connections.get(terminal, ""),
            source=source,
            confidence=float(entry.get("confidence", 0.4 if "fallback" in source else 0.6)),
            warnings=tuple(str(item) for item in entry.get("warnings", ())),
            ),
        )

    def get_terminal_pins(
        self,
        instance: PCellInstancePlan,
        terminal: str,
        *,
        preferred_layers: Sequence[str] | None = None,
    ) -> tuple[PCellPin, ...]:
        metadata_pins = _metadata_terminal_pins(self.pdk, instance, terminal, preferred_layers=preferred_layers)
        if metadata_pins:
            return metadata_pins
        if _force_template_terminal_access(self.pdk, instance, terminal):
            return ()
        calibrated = self._calibrated_terminal_pins(instance, terminal, preferred_layers=preferred_layers)
        if calibrated:
            return calibrated
        return ()

    def synthetic_terminal_pins(
        self,
        instance: PCellInstancePlan,
        terminal: str,
        *,
        preferred_layers: Sequence[str] | None = None,
    ) -> tuple[PCellPin, ...]:
        return _fallback_terminal_pins(self.pdk, instance, terminal, preferred_layers=preferred_layers)

    def get_terminal_xy(self, instance: PCellInstancePlan, terminal: str) -> tuple[float, float, str]:
        pin = self.get_terminal_pin(instance, terminal)
        x, y = pin.xy_um
        return (x, y, pin.layer)

    def select_terminal_pin(
        self,
        instance: PCellInstancePlan,
        terminal: str,
        *,
        require_lvs_safe: bool = False,
        preferred_layers: Sequence[str] | None = None,
    ) -> PCellPin:
        pins = list(self.get_terminal_pins(instance, terminal, preferred_layers=preferred_layers))
        if require_lvs_safe:
            pins = [pin for pin in pins if pin.lvs_safe]
        if pins:
            return sorted(
                pins,
                key=lambda pin: _oriented_pin_selection_key(
                    self.pdk,
                    instance,
                    terminal,
                    pin,
                    preferred_layers=preferred_layers,
                ),
            )[0]
        pin = self.get_terminal_pin(instance, terminal)
        if require_lvs_safe and not pin.lvs_safe:
            raise ValueError(
                f"no LVS-safe terminal access candidate for {instance.name}.{terminal}; "
                f"best fallback source={pin.source} layer={pin.layer}"
            )
        return pin

    def select_terminal_breakout(
        self,
        instance: PCellInstancePlan,
        terminal: str,
        *,
        require_lvs_safe: bool = False,
        preferred_layers: Sequence[str] | None = None,
        escape_margin_um: float = 0.02,
    ) -> PCellPin:
        pin = self.select_terminal_pin(
            instance,
            terminal,
            require_lvs_safe=require_lvs_safe,
            preferred_layers=preferred_layers,
        )
        if pin.bbox_um is None:
            return pin
        x0, y0, x1, y1 = pin.bbox_um
        cx, cy = pin.xy_um
        margin = max(float(escape_margin_um), 0.0)
        if terminal == "S":
            ex = x0 + margin
            ey = cy
        elif terminal == "D":
            ex = x1 - margin
            ey = cy
        else:
            ex = cx
            ey = y1 - margin if terminal == "G" else cy
        return replace(pin, xy_um=self.pdk.rules.snap_point_um((ex, ey)))

    def audit_instance(self, instance: PCellInstancePlan, *, margin_um: float = 0.25) -> tuple[PCellTerminalAccessIssue, ...]:
        """Report terminal access points that look inconsistent with the instance bbox."""

        metadata_access = _metadata_terminal_access(instance)
        try:
            template = self.pdk.pcell_template_for(instance.logical_name)
            template_terms = tuple(template.terminal_access.keys())
        except KeyError:
            template_terms = ()
        terminals = tuple(dict.fromkeys([*instance.connections.keys(), *metadata_access.keys(), *template_terms]))
        issues: list[PCellTerminalAccessIssue] = []
        bbox = _instance_bbox(instance)
        for terminal in terminals:
            try:
                pin = self.get_terminal_pin(instance, terminal)
            except PCellTerminalRequiresTap as exc:
                issues.append(
                    PCellTerminalAccessIssue(
                        instance.name,
                        terminal,
                        str(exc),
                        (0.0, 0.0),
                        bbox,
                        "warning",
                        instance.connections.get(terminal, ""),
                        "external_tap_required",
                        0.0,
                    )
                )
                continue
            except (KeyError, ValueError) as exc:
                issues.append(PCellTerminalAccessIssue(instance.name, terminal, str(exc), (0.0, 0.0), bbox, "error"))
                continue
            if not _point_in_bbox(pin.xy_um, bbox, margin_um):
                issues.append(
                    PCellTerminalAccessIssue(
                        instance.name,
                        terminal,
                        f"terminal access point outside instance bbox by more than {margin_um:g}um",
                        pin.xy_um,
                        bbox,
                    )
                )
        return tuple(issues)

    def audit_plan(self, plan: object, *, margin_um: float = 0.25) -> tuple[PCellTerminalAccessIssue, ...]:
        issues: list[PCellTerminalAccessIssue] = []
        for instance in getattr(plan, "instances", ()):
            issues.extend(self.audit_instance(instance, margin_um=margin_um))
        return tuple(issues)

    def _calibrated_terminal_pins(
        self,
        instance: PCellInstancePlan,
        terminal: str,
        *,
        preferred_layers: Sequence[str] | None = None,
    ) -> tuple[PCellPin, ...]:
        if self.calibration_cache is None:
            return ()
        if self.pdk.name == "tsmcn7" and instance.logical_name in {"nmos", "pmos"} and str(terminal) == "B":
            raise PCellTerminalRequiresTap(
                instance.name,
                terminal,
                f"terminal {instance.name}.{terminal} requires external tap for tsmcn7 native MOS body connection",
            )
        entry = self.calibration_cache.lookup_instance(
            instance,
            allow_nearest=self.allow_nearest_calibration,
            max_normalized_distance=self.max_nearest_distance,
        )
        if entry is None:
            return ()
        candidates = entry.terminal_access_candidates(
            terminal,
            preferred_layers=tuple(preferred_layers or self.pdk.preferred_signal_layers or self.pdk.layer_map.metals),
        )
        if str(terminal) == "B" and candidates and all(not _is_body_terminal_routable_candidate(candidate.layer) for candidate in candidates):
            raise PCellTerminalRequiresTap(
                instance.name,
                terminal,
                f"terminal {instance.name}.{terminal} requires external tap or explicit body-contact helper; "
                f"calibration candidates are non-routable body markers on {[str(candidate.layer) for candidate in candidates]}",
            )
        if not candidates:
            if _terminal_requires_external_tap(entry.warnings, terminal):
                raise PCellTerminalRequiresTap(instance.name, terminal)
            return ()
        resolved: list[PCellPin] = []
        if self.pdk.name == "tsmcn7" and instance.logical_name in {"nmos", "pmos"} and str(terminal) in {"S", "D"}:
            # N7 native MOS extraction is much more reliable when S/D routing
            # lands on the calibrated M0 pin strips.  OD/MD candidates may be
            # geometrically present but do not consistently collapse into the
            # intended device terminals during LVS.
            prioritized_layers = ("M0", "MD", "OD")
            reordered: list[object] = []
            for layer_name in prioritized_layers:
                reordered.extend(candidate for candidate in candidates if str(candidate.layer) == layer_name)
            reordered.extend(candidate for candidate in candidates if str(candidate.layer) not in set(prioritized_layers))
            if reordered:
                candidates = tuple(reordered)
        calibration_errors = tuple(f"calibration error: {error}" for error in entry.errors)
        seen: set[tuple[str, tuple[float, float], tuple[float, float, float, float] | None, str]] = {
            (pin.layer, pin.xy_um, pin.bbox_um, pin.source)
            for pin in resolved
        }
        for candidate in candidates:
            xy = _absolute_xy(instance.xy_um, candidate.xy_um, instance.orient)
            bbox_um = None if candidate.bbox_um is None else _absolute_bbox(instance.xy_um, candidate.bbox_um, instance.orient)
            warnings = tuple(dict.fromkeys([*calibration_errors, *entry.warnings, *candidate.warnings]))
            pin = _annotate_terminal_access(
                self.pdk,
                instance,
                terminal,
                PCellPin(
                instance=instance.name,
                terminal=terminal,
                xy_um=self.pdk.rules.snap_point_um(xy),
                layer=candidate.layer,
                contact_layer=_calibrated_contact_layer(self.pdk, instance, terminal, candidate.layer),
                net=instance.connections.get(terminal, ""),
                source=candidate.source,
                confidence=candidate.confidence,
                bbox_um=None if bbox_um is None else _snap_bbox_um(self.pdk, bbox_um),
                warnings=warnings,
                ),
            )
            key = (pin.layer, pin.xy_um, pin.bbox_um, pin.source)
            if key in seen:
                continue
            resolved.append(pin)
            seen.add(key)
        if self.pdk.name == "tsmcn7" and instance.logical_name in {"nmos", "pmos"} and str(terminal) == "G":
            gate_overlays = _n7_native_gate_pins(self.pdk, instance, entry)
            if gate_overlays:
                resolved.extend(pin for pin in gate_overlays if (pin.layer, pin.xy_um, pin.bbox_um, pin.source) not in seen)
        return tuple(resolved)

    def _calibrated_terminal_pin(self, instance: PCellInstancePlan, terminal: str) -> PCellPin | None:
        pins = self._calibrated_terminal_pins(instance, terminal)
        if not pins:
            return None
        return pins[0]


def analyze_pcell_terminal_access(
    plan: object,
    pdk: PdkConfig,
    *,
    calibration_cache: PCellCalibrationCache | None = None,
    allow_nearest_calibration: bool = False,
    max_nearest_distance: float = 0.25,
    margin_um: float = 0.25,
    require_calibrated: bool = False,
    require_conductive_access: bool = False,
    require_single_access_candidate: bool = False,
    require_high_confidence: bool = False,
    require_exact_calibration: bool = False,
    require_error_free_calibration: bool = False,
    min_confidence: float = 0.5,
    conflict_distance_um: float = 0.5,
    short_risk_distance_um: float = 0.12,
) -> PCellTerminalAccessReport:
    """Analyze connected PCell terminal access points before routing."""

    accessor = PCellTerminalAccessor(
        pdk,
        calibration_cache=calibration_cache,
        allow_nearest_calibration=allow_nearest_calibration,
        max_nearest_distance=max_nearest_distance,
    )
    pins: list[PCellPin] = []
    issues: list[PCellTerminalAccessIssue] = []
    fallback_risks: list[PCellTerminalAccessIssue] = []
    for instance in getattr(plan, "instances", ()):
        bbox = _instance_bbox(instance)
        instance_pins: dict[str, PCellPin] = {}
        for terminal, net in sorted(getattr(instance, "connections", {}).items()):
            if not net:
                continue
            try:
                pin = accessor.get_terminal_pin(instance, terminal)
            except PCellTerminalRequiresTap as exc:
                severity = "error" if require_calibrated or require_conductive_access else "warning"
                issues.append(
                    PCellTerminalAccessIssue(
                        instance.name,
                        terminal,
                        str(exc),
                        (0.0, 0.0),
                        bbox,
                        severity,
                        str(net),
                        "external_tap_required",
                        0.0,
                    )
                )
                continue
            except (KeyError, ValueError) as exc:
                issues.append(PCellTerminalAccessIssue(instance.name, terminal, str(exc), (0.0, 0.0), bbox, "error", str(net)))
                continue
            pins.append(pin)
            instance_pins[terminal] = pin
            if not _point_in_bbox(pin.xy_um, bbox, margin_um):
                issues.append(
                    PCellTerminalAccessIssue(
                        instance.name,
                        terminal,
                        f"terminal access point outside instance bbox by more than {margin_um:g}um",
                        pin.xy_um,
                        bbox,
                        "error",
                        str(net),
                        pin.source,
                        pin.confidence,
                    )
                )
            if _is_fallback_source(pin.source):
                severity = "error" if require_calibrated else "warning"
                issue = PCellTerminalAccessIssue(
                    instance.name,
                    terminal,
                    f"terminal access fallback risk {instance.name}.{terminal} net {net} source {pin.source}",
                    pin.xy_um,
                    bbox,
                    severity,
                    str(net),
                    pin.source,
                    pin.confidence,
                )
                issues.append(issue)
                fallback_risks.append(issue)
            if pin.confidence < min_confidence:
                issues.append(
                    PCellTerminalAccessIssue(
                        instance.name,
                        terminal,
                        f"terminal access confidence {pin.confidence:.3g} below {min_confidence:.3g}",
                        pin.xy_um,
                        bbox,
                        "error" if require_high_confidence else "warning",
                        str(net),
                        pin.source,
                        pin.confidence,
                    )
                )
            for warning in pin.warnings:
                severity = "warning"
                if require_conductive_access and _is_unbacked_conductive_warning(warning):
                    severity = "error"
                if require_exact_calibration and _is_nearest_calibration_warning(warning):
                    severity = "error"
                if require_error_free_calibration and _is_calibration_error_warning(warning):
                    severity = "error"
                issues.append(PCellTerminalAccessIssue(instance.name, terminal, warning, pin.xy_um, bbox, severity, str(net), pin.source, pin.confidence))
            if require_exact_calibration and _is_nearest_calibration_source(pin.source) and not any(_is_nearest_calibration_warning(warning) for warning in pin.warnings):
                issues.append(
                    PCellTerminalAccessIssue(
                        instance.name,
                        terminal,
                        f"nearest calibration access used for {instance.name}.{terminal} source {pin.source}",
                        pin.xy_um,
                        bbox,
                        "error",
                        str(net),
                        pin.source,
                        pin.confidence,
                    )
                )
            issues.extend(_terminal_candidate_conflict_issues(accessor, instance, terminal, pin, bbox, conflict_distance_um, require_single_access_candidate))
        issues.extend(_mos_terminal_proximity_issues(instance, instance_pins, bbox, short_risk_distance_um))
    return PCellTerminalAccessReport(tuple(pins), tuple(issues), tuple(fallback_risks))


def _metadata_terminal_pins(
    pdk: PdkConfig,
    instance: PCellInstancePlan,
    terminal: str,
    *,
    preferred_layers: Sequence[str] | None = None,
) -> tuple[PCellPin, ...]:
    access = _metadata_terminal_access(instance)
    if not access:
        return ()
    entry_obj = _metadata_terminal_entry(access, terminal)
    if entry_obj is None:
        return ()
    entries = tuple(entry_obj) if isinstance(entry_obj, (list, tuple)) else (entry_obj,)
    preferred = {str(layer) for layer in tuple(preferred_layers or ()) if str(layer)}
    context = _terminal_context(instance)
    pins: list[PCellPin] = []
    for index, raw_entry in enumerate(entries):
        if not isinstance(raw_entry, Mapping):
            continue
        entry = dict(raw_entry)
        layer = str(entry.get("layer", pdk.layer_map.metals[0] if pdk.layer_map.metals else ""))
        if preferred and layer not in preferred:
            continue
        bbox = _metadata_entry_bbox(entry, context)
        if bbox is not None:
            local_center = _metadata_entry_xy(entry, context)
            if local_center is None:
                local_center = ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)
            xy = _absolute_xy(instance.xy_um, local_center, instance.orient)
            bbox_um = _absolute_bbox(instance.xy_um, bbox, instance.orient)
        else:
            local_center = _metadata_entry_xy(entry, context)
            if local_center is None:
                continue
            xy = _absolute_xy(instance.xy_um, local_center, instance.orient)
            bbox_um = None
        source = str(entry.get("source", "instance_metadata_terminal_access"))
        try:
            confidence = float(entry.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 1.0
        try:
            priority = int(entry.get("access_priority", entry.get("priority", 5)))
        except (TypeError, ValueError):
            priority = 5
        pins.append(
            _annotate_terminal_access(
                pdk,
                instance,
                terminal,
                PCellPin(
                    instance=instance.name,
                    terminal=str(terminal),
                    xy_um=pdk.rules.snap_point_um(xy),
                    layer=layer,
                    contact_layer=str(entry.get("contact_layer", entry.get("via", "")) or ""),
                    net=instance.connections.get(terminal, ""),
                    source=source,
                    confidence=confidence,
                    bbox_um=None if bbox_um is None else _snap_bbox_um(pdk, bbox_um),
                    warnings=tuple(str(item) for item in tuple(entry.get("warnings", ()) or ())),
                    access_kind=str(entry.get("access_kind", "routable") or "routable"),
                    lvs_safe=bool(entry.get("lvs_safe", True)),
                    access_priority=priority + index,
                ),
            )
        )
    return tuple(sorted(pins, key=lambda pin: _pin_selection_key(pin, preferred_layers=preferred_layers)))


def _metadata_terminal_access(instance: PCellInstancePlan) -> dict[str, object]:
    metadata = getattr(instance, "metadata", {})
    if not isinstance(metadata, Mapping):
        return {}
    for key in ("terminal_access", "shared_sd_terminal_access", "pcell_terminal_access"):
        value = metadata.get(key)
        if isinstance(value, Mapping):
            return {str(term): entry for term, entry in value.items()}
    return {}


def _metadata_terminal_entry(access: Mapping[str, object], terminal: str) -> object | None:
    terminal_text = str(terminal)
    for key in (terminal_text, terminal_text.upper(), terminal_text.lower()):
        if key in access:
            return access[key]
    return None


def _metadata_entry_xy(entry: Mapping[str, object], context: Mapping[str, Any]) -> tuple[float, float] | None:
    value = entry.get("xy_um", entry.get("xy", None))
    if value is None:
        return None
    return _entry_xy({"xy": value}, context)


def _metadata_entry_bbox(entry: Mapping[str, object], context: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    value = entry.get("bbox_um", entry.get("bbox", None))
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"terminal access bbox must be a 4-tuple, got {value!r}")
    return (
        _eval_coord(value[0], context),
        _eval_coord(value[1], context),
        _eval_coord(value[2], context),
        _eval_coord(value[3], context),
    )


def _terminal_entry(instance: PCellInstancePlan, terminal: str, template: PCellTemplate, pdk: PdkConfig) -> dict[str, Any]:
    if str(getattr(instance, "instantiation_method", "")) == "drawn_primitive":
        fallback = _fallback_terminal_access(instance, terminal, pdk)
        if fallback is None:
            raise KeyError(f"no drawn-primitive terminal access metadata for {instance.name}.{terminal}")
        return fallback
    entry = template.terminal_access.get(terminal)
    if entry is not None:
        return dict(entry)
    fallback = _fallback_terminal_access(instance, terminal, pdk)
    if fallback is None:
        raise KeyError(f"no terminal access metadata for {instance.name}.{terminal}")
    return fallback


def _pin_to_dict(pin: PCellPin) -> dict[str, object]:
    return {
        "instance": pin.instance,
        "terminal": pin.terminal,
        "net": pin.net,
        "xy_um": pin.xy_um,
        "layer": pin.layer,
        "contact_layer": pin.contact_layer,
        "source": pin.source,
        "confidence": pin.confidence,
        "bbox_um": pin.bbox_um,
        "warnings": pin.warnings,
        "access_kind": pin.access_kind,
        "lvs_safe": pin.lvs_safe,
        "access_priority": pin.access_priority,
    }


def _issue_to_dict(issue: PCellTerminalAccessIssue) -> dict[str, object]:
    return {
        "instance": issue.instance,
        "terminal": issue.terminal,
        "net": issue.net,
        "message": issue.message,
        "xy_um": issue.xy_um,
        "bbox_um": issue.bbox_um,
        "severity": issue.severity,
        "source": issue.source,
        "confidence": issue.confidence,
    }


def _is_fallback_source(source: str) -> bool:
    return source in {"pdk_template", "pdk_builtin_fallback"} or "fallback" in source


def _is_body_terminal_routable_candidate(layer: str) -> bool:
    return str(layer) not in {"PDK", "NW", "OD"}


def _pin_selection_key(
    pin: PCellPin,
    *,
    preferred_layers: Sequence[str] | None = None,
) -> tuple[int, int, float, float, float, str]:
    layer_order = tuple(str(layer) for layer in (preferred_layers or ()))
    if layer_order:
        try:
            layer_rank = layer_order.index(str(pin.layer))
        except ValueError:
            layer_rank = len(layer_order)
    else:
        layer_rank = 0
    return (
        int(pin.access_priority),
        layer_rank,
        -float(pin.confidence),
        float(pin.xy_um[1]),
        float(pin.xy_um[0]),
        str(pin.source),
    )


def _oriented_pin_selection_key(
    pdk: PdkConfig,
    instance: PCellInstancePlan,
    terminal: str,
    pin: PCellPin,
    *,
    preferred_layers: Sequence[str] | None = None,
) -> tuple[int, int, float, float, float, str]:
    layer_order = tuple(str(layer) for layer in (preferred_layers or ()))
    if layer_order:
        try:
            layer_rank = layer_order.index(str(pin.layer))
        except ValueError:
            layer_rank = len(layer_order)
    else:
        layer_rank = 0
    x_rank = float(pin.xy_um[0])
    # CRN28 PCells with nfLayerOption=ON expose every gate finger as a
    # candidate.  For mirrored row pairs, choosing the absolute leftmost gate
    # on the MY instance places the top-level PO landing on the row-internal
    # side and can prevent Calibre from reducing the native MOS into a valid
    # device.  Prefer the outside edge of mirrored MOS instances instead.
    if (
        str(getattr(pdk, "name", "")) == "crn28hpcp"
        and str(getattr(instance, "logical_name", "")) in {"nmos", "pmos"}
        and str(terminal) == "G"
        and str(getattr(instance, "orient", "")) == "MY"
    ):
        x_rank = -x_rank
    return (
        int(pin.access_priority),
        layer_rank,
        -float(pin.confidence),
        float(pin.xy_um[1]),
        x_rank,
        str(pin.source),
    )


def _force_template_terminal_access(pdk: PdkConfig, instance: PCellInstancePlan, terminal: str) -> bool:
    """Return True when PDK metadata asks to bypass calibrated terminal access.

    This is intentionally opt-in.  It lets a PDK use calibrated pins for normal
    routing while forcing selected native-PCell terminals through the
    ``PCellTemplate.terminal_access`` path when Calibre extraction depends on
    template-aligned access geometry.
    """

    metadata = dict(getattr(pdk, "metadata", {}) or {})
    access = dict(metadata.get("pcell_access", {}) or {})
    logical_name = str(getattr(instance, "logical_name", "")).lower()
    terminal_name = str(terminal)
    terminal_key = terminal_name.lower()
    candidates: list[object] = []
    candidates.append(access.get(f"{logical_name}_{terminal_key}_access"))
    candidates.append(access.get(f"{logical_name}_{terminal_name}_access"))
    terminal_modes = access.get(f"{logical_name}_terminal_access")
    if isinstance(terminal_modes, Mapping):
        candidates.append(terminal_modes.get(terminal_name))
        candidates.append(terminal_modes.get(terminal_key))
    if logical_name in {"nmos", "pmos"}:
        candidates.append(access.get(f"mos_{terminal_key}_access"))
        candidates.append(access.get(f"mos_{terminal_name}_access"))
        mos_modes = access.get("mos_terminal_access")
        if isinstance(mos_modes, Mapping):
            candidates.append(mos_modes.get(terminal_name))
            candidates.append(mos_modes.get(terminal_key))
        if terminal_name == "G":
            candidates.append(access.get("mos_gate_access"))
    for candidate in candidates:
        if _terminal_access_mode_forces_template(candidate):
            return True
    return False


def _terminal_access_mode_forces_template(mode: object) -> bool:
    if mode is None:
        return False
    if isinstance(mode, bool):
        return not mode
    return str(mode).strip().lower().replace("-", "_") in {
        "template",
        "pdk_template",
        "fallback",
        "force_template",
        "disable_calibration",
        "calibration_off",
        "off",
    }


def _terminal_requires_external_tap(warnings: tuple[str, ...], terminal: str) -> bool:
    prefix = f"terminal {terminal} has no routable access candidate"
    return any(str(warning).startswith(prefix) and "external tap" in str(warning) for warning in warnings)


def _is_unbacked_conductive_warning(message: str) -> bool:
    message_text = str(message)
    return (
        "has no overlapping conductive shape" in message_text
        or "overlaps conductive shape tagged" in message_text
        or "overlaps conductive shape on" in message_text
        or "overlaps multiple conductive shape tags" in message_text
    )


def _is_nearest_calibration_warning(message: str) -> bool:
    return "nearest calibration match used" in str(message)


def _is_calibration_error_warning(message: str) -> bool:
    return str(message).startswith("calibration error:")


def _is_nearest_calibration_source(source: str) -> bool:
    return str(source).startswith("nearest_")


def _terminal_candidate_conflict_issues(
    accessor: PCellTerminalAccessor,
    instance: PCellInstancePlan,
    terminal: str,
    selected: PCellPin,
    bbox: tuple[float, float, float, float],
    conflict_distance_um: float,
    require_single_access_candidate: bool,
) -> tuple[PCellTerminalAccessIssue, ...]:
    if accessor.calibration_cache is None or conflict_distance_um <= 0.0:
        return ()
    entry = accessor.calibration_cache.lookup_instance(
        instance,
        allow_nearest=accessor.allow_nearest_calibration,
        max_normalized_distance=accessor.max_nearest_distance,
    )
    if entry is None:
        return ()
    candidates = entry.terminal_access_candidates(terminal, preferred_layers=accessor.pdk.preferred_signal_layers or accessor.pdk.layer_map.metals)
    if len(candidates) < 2:
        return ()
    absolute_points = tuple(accessor.pdk.rules.snap_point_um(_absolute_xy(instance.xy_um, candidate.xy_um, instance.orient)) for candidate in candidates)
    max_distance = max(_point_distance(left, right) for idx, left in enumerate(absolute_points) for right in absolute_points[idx + 1 :])
    if max_distance <= conflict_distance_um:
        return ()
    return (
        PCellTerminalAccessIssue(
            instance.name,
            terminal,
            f"terminal access has {len(candidates)} candidates spanning {max_distance:.4g}um",
            selected.xy_um,
            bbox,
            "error" if require_single_access_candidate else "warning",
            selected.net,
            selected.source,
            selected.confidence,
        ),
    )


def _mos_terminal_proximity_issues(
    instance: PCellInstancePlan,
    pins: Mapping[str, PCellPin],
    bbox: tuple[float, float, float, float],
    short_risk_distance_um: float,
) -> tuple[PCellTerminalAccessIssue, ...]:
    if instance.logical_name not in {"nmos", "pmos"} or short_risk_distance_um <= 0.0:
        return ()
    issues: list[PCellTerminalAccessIssue] = []
    for left, right in (("S", "B"), ("D", "B"), ("S", "D")):
        left_pin = pins.get(left)
        right_pin = pins.get(right)
        if left_pin is None or right_pin is None:
            continue
        if left_pin.layer != right_pin.layer:
            continue
        distance = _point_distance(left_pin.xy_um, right_pin.xy_um)
        if distance > short_risk_distance_um:
            continue
        severity = "warning" if left_pin.net == right_pin.net else "error"
        issues.append(
            PCellTerminalAccessIssue(
                instance.name,
                f"{left}/{right}",
                f"MOS terminals {left}/{right} access points are {distance:.4g}um apart on {left_pin.layer}",
                left_pin.xy_um,
                bbox,
                severity,
                f"{left_pin.net}/{right_pin.net}",
                f"{left_pin.source}/{right_pin.source}",
                min(left_pin.confidence, right_pin.confidence),
            )
        )
    return tuple(issues)


def _point_distance(left: tuple[float, float], right: tuple[float, float]) -> float:
    return hypot(float(right[0]) - float(left[0]), float(right[1]) - float(left[1]))


def _fallback_terminal_access(instance: PCellInstancePlan, terminal: str, pdk: PdkConfig) -> dict[str, Any] | None:
    metal = pdk.layer_map.metals[0]
    contact = pdk.layer_map.contact
    if instance.logical_name in {"nmos", "pmos"}:
        width_um = _device_width_um(instance)
        raw_height_um = float(instance.height_um or 0.0)
        height_um = max(raw_height_um, width_um if raw_height_um <= 0.0 else raw_height_um, 0.2)
        access = {
            "G": {"xy": (0.015, -0.04), "layer": pdk.layer_map.gate, "contact_layer": contact, "source": "pdk_builtin_fallback"},
            "S": {"xy": (-0.05, height_um / 2.0), "layer": metal, "contact_layer": contact, "source": "pdk_builtin_fallback"},
            "D": {"xy": (0.08, height_um / 2.0), "layer": metal, "contact_layer": contact, "source": "pdk_builtin_fallback"},
            "B": {"xy": (0.0, -0.18), "layer": metal, "contact_layer": contact, "source": "pdk_builtin_fallback"},
        }
        return access.get(terminal)
    if instance.logical_name == "resistor":
        if str(getattr(instance, "instantiation_method", "")) == "drawn_primitive":
            pad = max(0.08, min(0.16, 0.35 * max(instance.width_um, 0.3)))
            return {
                "PLUS": {"xy": (pad / 2.0, instance.height_um / 2.0), "layer": metal, "contact_layer": contact, "source": "drawn_primitive"},
                "MINUS": {"xy": (max(instance.width_um - pad / 2.0, pad / 2.0), instance.height_um / 2.0), "layer": metal, "contact_layer": contact, "source": "drawn_primitive"},
            }.get(terminal)
        return {
            "PLUS": {"xy": (0.0, instance.height_um / 2), "layer": metal, "source": "pdk_builtin_fallback"},
            "MINUS": {"xy": (instance.width_um, instance.height_um / 2), "layer": metal, "source": "pdk_builtin_fallback"},
        }.get(terminal)
    if instance.logical_name == "capacitor":
        if str(getattr(instance, "instantiation_method", "")) == "drawn_primitive":
            if str(instance.params.get("__drawn_capacitor_style", "")).strip().lower() == "mom":
                try:
                    start_number = max(1, int(instance.params.get("__mom_start_metal", 4)))
                except (TypeError, ValueError):
                    start_number = 4
                start_number = min(start_number, len(pdk.layer_map.metals))
                layer = pdk.layer_map.metals[start_number - 1]
                edge = max(float(instance.params.get("__mom_edge_margin_um", 0.20) or 0.20), 0.05)
                bus = max(float(instance.params.get("__mom_bus_width_um", 0.30) or 0.30), 0.05)
                via_index = max(0, start_number - 2)
                via = pdk.layer_map.vias[min(via_index, len(pdk.layer_map.vias) - 1)] if pdk.layer_map.vias else contact
                return {
                    "PLUS": {
                        "xy": (edge + 0.5 * bus, edge + 0.5 * bus),
                        "layer": layer,
                        "contact_layer": via,
                        "source": "drawn_mom",
                    },
                    "MINUS": {
                        "xy": (
                            instance.width_um - edge - 0.5 * bus,
                            instance.height_um - edge - 0.5 * bus,
                        ),
                        "layer": layer,
                        "contact_layer": via,
                        "source": "drawn_mom",
                    },
                }.get(terminal)
            inset = max(0.06, min(min(instance.width_um, instance.height_um) * 0.18, 0.14))
            rim_escape = min(max(inset * 0.4, 0.04), 0.06)
            return {
                "PLUS": {"xy": (instance.width_um / 2.0, instance.height_um * 0.75), "layer": pdk.layer_map.metals[min(1, len(pdk.layer_map.metals) - 1)], "contact_layer": "VIA1", "source": "drawn_primitive"},
                # Keep the bottom-plate breakout on the exposed M1 rim so
                # upper-plate M2 routing can land without shorting through the
                # capacitor top plate.
                "MINUS": {"xy": (rim_escape, instance.height_um / 2.0), "layer": metal, "contact_layer": contact, "source": "drawn_primitive"},
            }.get(terminal)
        return {
            "PLUS": {"xy": (instance.width_um / 2, instance.height_um / 2), "layer": metal, "source": "pdk_builtin_fallback"},
            "MINUS": {"xy": (instance.width_um / 2, 0.0), "layer": metal, "source": "pdk_builtin_fallback"},
        }.get(terminal)
    if instance.logical_name == "bjt":
        width = instance.width_um
        height = instance.height_um
        return {
            "C": {"xy": (width * 0.5, height), "layer": metal, "contact_layer": contact, "source": "pdk_builtin_fallback"},
            "B": {"xy": (0.0, height * 0.5), "layer": metal, "contact_layer": contact, "source": "pdk_builtin_fallback"},
            "E": {"xy": (width * 0.5, 0.0), "layer": metal, "contact_layer": contact, "source": "pdk_builtin_fallback"},
        }.get(terminal)
    return None


def _fallback_terminal_pins(
    pdk: PdkConfig,
    instance: PCellInstancePlan,
    terminal: str,
    *,
    preferred_layers: Sequence[str] | None = None,
) -> tuple[PCellPin, ...]:
    entry = _fallback_terminal_access(instance, terminal, pdk)
    if entry is None:
        return ()
    if instance.logical_name not in {"nmos", "pmos"} or str(terminal) not in {"S", "D", "B"}:
        local_xy = _entry_xy(entry, _terminal_context(instance))
        xy = _absolute_xy(instance.xy_um, local_xy, instance.orient)
        return (
            _annotate_terminal_access(
                pdk,
                instance,
                terminal,
                PCellPin(
                    instance=instance.name,
                    terminal=terminal,
                    xy_um=pdk.rules.snap_point_um(xy),
                    layer=str(entry.get("layer", pdk.layer_map.metals[0])),
                    contact_layer=str(entry.get("contact_layer", pdk.layer_map.contact)),
                    net=instance.connections.get(terminal, ""),
                    source=str(entry.get("source", "pdk_builtin_fallback")),
                    confidence=float(entry.get("confidence", 0.35)),
                    warnings=tuple(str(item) for item in entry.get("warnings", ())),
                ),
            ),
        )
    height_um = max(float(instance.height_um or 0.0), 0.2)
    metal = str(entry.get("layer", pdk.layer_map.metals[0]))
    contact_layer = str(entry.get("contact_layer", pdk.layer_map.contact))
    source = str(entry.get("source", "pdk_builtin_fallback"))
    y = height_um * 0.5
    local_candidates: list[tuple[float, float, float]] = []
    if str(terminal) in {"S", "B"}:
        local_candidates = [
            (-0.12, y, 0.38),
            (-0.08, y, 0.34),
            (-0.05, y, 0.30),
        ]
    elif str(terminal) == "D":
        local_candidates = [
            (0.14, y, 0.38),
            (0.11, y, 0.34),
            (0.08, y, 0.30),
        ]
    pins: list[PCellPin] = []
    for index, (local_x, local_y, confidence) in enumerate(local_candidates):
        xy = _absolute_xy(instance.xy_um, (local_x, local_y), instance.orient)
        pin = _annotate_terminal_access(
            pdk,
            instance,
            terminal,
            PCellPin(
                instance=instance.name,
                terminal=terminal,
                xy_um=pdk.rules.snap_point_um(xy),
                layer=metal,
                contact_layer=contact_layer,
                net=instance.connections.get(terminal, ""),
                source=f"{source}_candidate_{index}",
                confidence=confidence,
                warnings=(f"synthetic fallback breakout candidate {index} for {instance.name}.{terminal}; prefer calibrated terminal access when available",),
            ),
        )
        pin = replace(pin, access_priority=200 + index)
        pins.append(pin)
    layer_order = tuple(str(layer) for layer in (preferred_layers or ()))
    return tuple(sorted(pins, key=lambda pin: _pin_selection_key(pin, preferred_layers=layer_order)))


def _calibrated_contact_layer(pdk: PdkConfig, instance: PCellInstancePlan, terminal: str, layer: str) -> str:
    layer_text = str(layer)
    if pdk.name == "tsmcn7":
        if terminal == "G" and layer_text == pdk.layer_map.gate:
            return "M0_PO"
        if terminal == "B" and layer_text in {"OD", "PDK", "NW"}:
            return "M0_NW" if instance.logical_name == "pmos" else "M0_SUB"
    template = pdk.pcell_template_for(instance.logical_name)
    entry = template.terminal_access.get(terminal, {})
    contact_layer = str(entry.get("contact_layer", ""))
    return contact_layer or pdk.layer_map.contact


def _annotate_terminal_access(
    pdk: PdkConfig,
    instance: PCellInstancePlan,
    terminal: str,
    pin: PCellPin,
) -> PCellPin:
    access_kind = "routable"
    lvs_safe = True
    access_priority = 50

    source = str(pin.source)
    layer = str(pin.layer)
    contact_layer = str(pin.contact_layer)

    if _is_fallback_source(source):
        access_kind = "fallback"
        lvs_safe = False
        access_priority = 200

    if _force_template_terminal_access(pdk, instance, terminal) and _is_fallback_source(source):
        access_kind = "lvs_extraction_assist"
        lvs_safe = True
        access_priority = 0

    if source.startswith("nearest_"):
        access_kind = "nearest_calibration"
        lvs_safe = False
        access_priority = 120

    if pdk.name == "tsmcn7" and instance.logical_name in {"nmos", "pmos"}:
        if terminal == "G":
            if source == "oa_pin" and layer == pdk.layer_map.gate:
                access_kind = "routable"
                lvs_safe = True
                access_priority = 0
            elif source.startswith("n7_native_pode_gate_"):
                access_kind = "geometry_hint"
                lvs_safe = False
                access_priority = 150
            else:
                access_kind = "routable_candidate"
                lvs_safe = False
                access_priority = 80
        elif terminal in {"S", "D"}:
            if layer == "M0":
                access_kind = "routable"
                lvs_safe = True
                access_priority = 0
            elif layer == "MD":
                access_kind = "routable_candidate"
                lvs_safe = False
                access_priority = 40
            elif layer == "OD":
                access_kind = "routable_candidate"
                lvs_safe = False
                access_priority = 80
        elif terminal == "B":
            access_kind = "requires_external_tap"
            lvs_safe = False
            access_priority = 255

    if contact_layer in {"M0_SUB", "M0_NW"}:
        access_kind = "helper_contact"
        lvs_safe = False
        access_priority = min(access_priority, 160)

    return replace(
        pin,
        access_kind=access_kind,
        lvs_safe=lvs_safe,
        access_priority=access_priority,
    )


def _terminal_context(instance: PCellInstancePlan) -> dict[str, Any]:
    width_um = _device_width_um(instance)
    height_um = instance.height_um or width_um
    nf = max(1, int(getattr(instance.finger_choice, "nf", 0) or instance.params.get("nf", instance.params.get("fingers", 1)) or 1))
    m = max(1, int(getattr(instance.finger_choice, "m", 0) or instance.params.get("m", instance.params.get("simM", 1)) or 1))
    finger_width_um = width_um / nf
    if instance.finger_choice is not None:
        finger_width_um = instance.finger_choice.finger_width_m * 1e6
    context: dict[str, Any] = {
        "W": width_um,
        "w": width_um,
        "Wfg": finger_width_um,
        "wfg": finger_width_um,
        "wf": finger_width_um,
        "NF": nf,
        "nf": nf,
        "fingers": nf,
        "M": m,
        "m": m,
        "simM": m,
        "width": width_um,
        "H": height_um,
        "h": height_um,
        "height": height_um,
        "width_um": width_um,
        "height_um": height_um,
    }
    for key, value in instance.params.items():
        key_text = str(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (float, int)):
            num = float(value)
            context_value = num * 1e6 if _looks_like_si_dimension(key_text, num) else num
            context[key_text] = context_value
            context[key_text.lower()] = context_value
            continue
        if isinstance(value, str) and _looks_like_dimension_key(key_text):
            parsed_um = _dimension_text_um(value)
            if parsed_um is not None:
                context[key_text] = parsed_um
                context[key_text.lower()] = parsed_um
    if "Wfg" not in context and "W" in context and "nf" in context:
        context["Wfg"] = context["W"] / max(float(context["nf"]), 1.0)
        context["wfg"] = context["Wfg"]
    _add_passive_dimension_fallbacks(instance, context)
    return context


def _add_passive_dimension_fallbacks(instance: PCellInstancePlan, context: dict[str, Any]) -> None:
    logical = str(getattr(instance, "logical_name", "") or "").lower()
    params = {str(key) for key in getattr(instance, "params", {})}
    if logical == "resistor":
        if not params.intersection({"l", "L", "length", "sumL"}):
            length_um = max(float(getattr(instance, "width_um", 0.0) or 0.0) - 0.5, 0.1)
            context["l"] = length_um
            context["L"] = length_um
            context["sumL"] = length_um
        if not params.intersection({"w", "W", "width", "sumW"}):
            width_um = max(float(getattr(instance, "height_um", 0.0) or 0.0) - 0.98, 0.1)
            context["w"] = width_um
            context["W"] = width_um
            context["sumW"] = width_um
    elif logical == "capacitor":
        if not params.intersection({"lr", "l", "L", "length"}):
            length_um = max(float(getattr(instance, "width_um", 0.0) or 0.0) - 0.56, 0.1)
            context["lr"] = length_um
            context["l"] = length_um
            context["L"] = length_um
        if not params.intersection({"wr", "w", "W", "width"}):
            width_um = max(float(getattr(instance, "height_um", 0.0) or 0.0) - 0.32, 0.1)
            context["wr"] = width_um
            context["w"] = width_um
            context["W"] = width_um


def _device_width_um(instance: PCellInstancePlan) -> float:
    if instance.finger_choice is not None:
        return instance.finger_choice.total_width_m * 1e6
    for key in ("W_um", "w_um", "width_um"):
        value = instance.params.get(key)
        if isinstance(value, (float, int)):
            return float(value)
    for key in ("W", "w", "Wfg", "wf", "width", "wr", "sumW"):
        value = instance.params.get(key)
        if isinstance(value, (float, int)):
            return float(value) * 1e6
        if isinstance(value, str):
            parsed_um = _dimension_text_um(value)
            if parsed_um is not None:
                return parsed_um
    return max(instance.height_um, 0.2)


def _looks_like_si_dimension(key: str, value: float) -> bool:
    return _looks_like_dimension_key(key) and abs(value) < 1e-3


def _looks_like_dimension_key(key: str) -> bool:
    return key.lower() in {"w", "l", "wf", "wfg", "wr", "lr", "sumw", "suml", "width", "length", "h", "height"}


def _dimension_text_um(value: str) -> float | None:
    text = str(value).strip()
    if not text:
        return None
    lower = text.lower().replace(" ", "")
    suffixes = (
        ("micron", 1.0),
        ("um", 1.0),
        ("u", 1.0),
        ("nm", 1e-3),
        ("n", 1e-3),
        ("pm", 1e-6),
        ("p", 1e-6),
        ("mm", 1e3),
        ("m", 1e6),
    )
    for suffix, multiplier_um in suffixes:
        if lower.endswith(suffix):
            raw = lower[: -len(suffix)]
            if not raw:
                return None
            try:
                return float(raw) * multiplier_um
            except ValueError:
                return None
    try:
        number = float(lower)
    except ValueError:
        return None
    if abs(number) < 1e-3:
        return number * 1e6
    if abs(number) < 100:
        return number
    return number * 1e-3


def _n7_native_gate_pins(
    pdk: PdkConfig,
    instance: PCellInstancePlan,
    entry: object,
) -> tuple[PCellPin, ...]:
    try:
        template = pdk.pcell_template_for(instance.logical_name)
    except KeyError:
        return ()
    if not str(template.resolved_layout_cell_name()).lower().endswith("macx"):
        return ()
    raw_artifact_path = str(getattr(entry, "metadata", {}).get("raw_artifact_path", "") or "")
    if not raw_artifact_path:
        return ()
    artifact_file = Path(raw_artifact_path)
    if not artifact_file.exists():
        return ()
    try:
        payload = json.loads(artifact_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    shapes = tuple(payload.get("conductive_shapes", ()))
    pode_shapes = []
    for shape in shapes:
        if str(shape.get("layer", "")) != "PODE_GATE":
            continue
        bbox = shape.get("bbox_um")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        bbox_tuple = tuple(float(value) for value in bbox)
        cx = (bbox_tuple[0] + bbox_tuple[2]) / 2.0
        cy = (bbox_tuple[1] + bbox_tuple[3]) / 2.0
        pode_shapes.append((cx, cy, bbox_tuple))
    if not pode_shapes:
        return ()
    # Keep these as low-priority auxiliary hints only.  They are useful for
    # geometry inspection, but direct PO/PODE breakout on N7 does not
    # necessarily map to LVS-safe gate connectivity.
    sorted_shapes = sorted(pode_shapes, key=lambda item: (item[0], item[1]))
    if len(sorted_shapes) == 1:
        labeled_shapes = (("center", sorted_shapes[0]),)
    else:
        labeled_shapes = (
            ("left", sorted_shapes[0]),
            ("right", sorted_shapes[-1]),
        )
    pins: list[PCellPin] = []
    for side, (local_x, local_y, local_bbox) in labeled_shapes:
        xy = _absolute_xy(instance.xy_um, (local_x, local_y), instance.orient)
        bbox_um = _absolute_bbox(instance.xy_um, local_bbox, instance.orient)
        pins.append(
            _annotate_terminal_access(
                pdk,
                instance,
                "G",
                PCellPin(
                instance=instance.name,
                terminal="G",
                xy_um=pdk.rules.snap_point_um(xy),
                layer=pdk.layer_map.gate,
                contact_layer="M0_PO",
                net=instance.connections.get("G", ""),
                source=f"n7_native_pode_gate_{side}",
                confidence=0.45,
                bbox_um=_snap_bbox_um(pdk, bbox_um),
                warnings=(f"tsmcn7 gate access uses {side}-side PODE_GATE heuristic; prefer calibrated OA gate pin for LVS-safe routing",),
                ),
            )
        )
    return tuple(pins)


def _crn28_mos_body_tap_pin(
    pdk: PdkConfig,
    instance: PCellInstancePlan,
    terminal: str,
) -> PCellPin | None:
    if str(getattr(pdk, "name", "")) != "crn28hpcp":
        return None
    if str(getattr(instance, "logical_name", "")).lower() not in {"nmos", "pmos"}:
        return None
    if str(terminal) != "B":
        return None
    params = dict(getattr(instance, "params", {}) or {})
    nf = _positive_int_param(params, ("fingers", "nf"), 1)
    sim_m = _positive_int_param(params, ("simM", "m", "M"), 1)
    length_um = _dimension_param_um(params, ("l", "L", "length"), 0.18)
    pitch_um = max(0.24, float(length_um) + 0.12)
    column_count = max(1, nf * sim_m + 1)
    min_x = -0.06
    max_x = min_x + float(column_count - 1) * pitch_um
    access_cfg = _crn28_mos_access_config(pdk)
    tap_x = _crn28_mos_body_tap_x_um(pdk, min_x, max_x, access_cfg)
    tap_y = _snap_scalar_um(pdk, -1.18)
    xy = pdk.rules.snap_point_um(_absolute_xy(instance.xy_um, (tap_x, tap_y), instance.orient))
    body_m1_half = 0.5 * _dimension_from_config_um(access_cfg, "body_m1_width_nm", "body_m1_width_um", 0.34)
    bbox = _absolute_bbox(
        instance.xy_um,
        (tap_x - body_m1_half, tap_y - body_m1_half, tap_x + body_m1_half, tap_y + body_m1_half),
        instance.orient,
    )
    return _annotate_terminal_access(
        pdk,
        instance,
        terminal,
        PCellPin(
            instance=instance.name,
            terminal="B",
            xy_um=xy,
            layer=pdk.layer_map.metals[0],
            contact_layer=pdk.layer_map.contact,
            net=instance.connections.get("B", ""),
            source="crn28_body_tap_support",
            confidence=0.85,
            bbox_um=_snap_bbox_um(pdk, bbox),
            warnings=(
                "CRN28 MOS body terminal routed through generated body-tap support; "
                "do not use PMOS template B access because it aliases the source terminal",
            ),
        ),
    )


def _positive_int_param(params: Mapping[str, Any], keys: Sequence[str], default: int) -> int:
    for key in keys:
        if key not in params:
            continue
        try:
            return max(1, int(float(str(params.get(key)).rstrip("nump"))))
        except (TypeError, ValueError):
            continue
    return max(1, int(default))


def _dimension_param_um(params: Mapping[str, Any], keys: Sequence[str], default_um: float) -> float:
    for key in keys:
        value = params.get(key)
        if isinstance(value, (float, int)):
            number = float(value)
            return number * 1e6 if abs(number) < 1e-3 else number
        if isinstance(value, str):
            parsed = _dimension_text_um(value)
            if parsed is not None:
                return parsed
    return float(default_um)


def _crn28_mos_access_config(pdk: PdkConfig) -> Mapping[str, Any]:
    metadata = getattr(pdk, "metadata", {}) or {}
    if not isinstance(metadata, Mapping):
        return {}
    calibre = metadata.get("calibre", {}) or {}
    if not isinstance(calibre, Mapping):
        return {}
    raw = calibre.get("mos_access", {}) or {}
    return raw if isinstance(raw, Mapping) else {}


def _crn28_mos_body_tap_x_um(
    pdk: PdkConfig,
    min_x: float,
    max_x: float,
    access_cfg: Mapping[str, Any],
) -> float:
    mode = str(access_cfg.get("body_tap_x_mode", "center") or "center").strip().lower()
    margin = _dimension_from_config_um(access_cfg, "body_tap_side_margin_nm", "body_tap_side_margin_um", 0.62)
    if mode in {"left", "start", "outside_left"}:
        return _snap_scalar_um(pdk, float(min_x) - margin)
    if mode in {"right", "end", "outside_right"}:
        return _snap_scalar_um(pdk, float(max_x) + margin)
    return _snap_scalar_um(pdk, (float(min_x) + float(max_x)) * 0.5)


def _dimension_from_config_um(
    config: Mapping[str, Any],
    nm_key: str,
    um_key: str,
    default_um: float,
) -> float:
    if um_key in config:
        try:
            return max(float(config.get(um_key, default_um) or default_um), 0.0)
        except (TypeError, ValueError):
            return max(float(default_um), 0.0)
    if nm_key in config:
        try:
            return max(float(config.get(nm_key, default_um * 1000.0) or default_um * 1000.0) * 1e-3, 0.0)
        except (TypeError, ValueError):
            return max(float(default_um), 0.0)
    return max(float(default_um), 0.0)


def _snap_scalar_um(pdk: PdkConfig, value: float) -> float:
    return pdk.rules.snap_point_um((float(value), 0.0))[0]


def _entry_xy(entry: Mapping[str, Any], context: Mapping[str, Any]) -> tuple[float, float]:
    value = entry.get("xy", entry.get("offset_um", (0.0, 0.0)))
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"terminal access xy must be a pair, got {value!r}")
    return (_eval_coord(value[0], context), _eval_coord(value[1], context))


def _eval_coord(value: Any, context: Mapping[str, Any]) -> float:
    if isinstance(value, (float, int)):
        return float(value)
    if not isinstance(value, str):
        raise ValueError(f"unsupported terminal coordinate {value!r}")
    tree = ast.parse(value, mode="eval")
    return float(_eval_node(tree.body, context))


def _eval_node(node: ast.AST, context: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in context:
            raise ValueError(f"unknown terminal access variable {node.id!r}")
        return context[node.id]
    if isinstance(node, ast.UnaryOp):
        value = _eval_node(node.operand, context)
        if isinstance(node.op, ast.UAdd):
            return +value
        if isinstance(node.op, ast.USub):
            return -value
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, context)
        right = _eval_node(node.right, context)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
    raise ValueError(f"unsupported terminal access expression: {ast.dump(node, include_attributes=False)}")


def _absolute_xy(origin: tuple[float, float], local: tuple[float, float], orient: str) -> tuple[float, float]:
    x, y = local
    if orient == "R0":
        dx, dy = x, y
    elif orient == "R90":
        dx, dy = -y, x
    elif orient == "R180":
        dx, dy = -x, -y
    elif orient == "R270":
        dx, dy = y, -x
    elif orient == "MX":
        dx, dy = x, -y
    elif orient == "MY":
        dx, dy = -x, y
    elif orient == "MXR90":
        dx, dy = y, x
    elif orient == "MYR90":
        dx, dy = -y, -x
    else:
        raise ValueError(f"unsupported orientation {orient!r}")
    return (origin[0] + dx, origin[1] + dy)


def _absolute_bbox(origin: tuple[float, float], bbox: tuple[float, float, float, float], orient: str) -> tuple[float, float, float, float]:
    left_bottom = _absolute_xy(origin, (bbox[0], bbox[1]), orient)
    right_top = _absolute_xy(origin, (bbox[2], bbox[3]), orient)
    left_top = _absolute_xy(origin, (bbox[0], bbox[3]), orient)
    right_bottom = _absolute_xy(origin, (bbox[2], bbox[1]), orient)
    xs = (left_bottom[0], right_top[0], left_top[0], right_bottom[0])
    ys = (left_bottom[1], right_top[1], left_top[1], right_bottom[1])
    return (min(xs), min(ys), max(xs), max(ys))


def _snap_bbox_um(pdk: PdkConfig, bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    return pdk.rules.snap_bbox_um(bbox, mode="outward")


def _instance_bbox(instance: PCellInstancePlan) -> tuple[float, float, float, float]:
    x0 = float(getattr(instance, "bbox_x0_um", 0.0) or 0.0)
    y0 = float(getattr(instance, "bbox_y0_um", 0.0) or 0.0)
    return _absolute_bbox(instance.xy_um, (x0, y0, x0 + instance.width_um, y0 + instance.height_um), instance.orient)


def _point_in_bbox(point: tuple[float, float], bbox: tuple[float, float, float, float], margin_um: float) -> bool:
    x, y = point
    x0, y0, x1, y1 = bbox
    lo_x, hi_x = sorted((x0, x1))
    lo_y, hi_y = sorted((y0, y1))
    margin = max(0.0, float(margin_um))
    return lo_x - margin <= x <= hi_x + margin and lo_y - margin <= y <= hi_y + margin
