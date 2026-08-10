"""PDK-black-box mapping from ideal R/C values to legal device geometry.

Analytic sheet-resistance and capacitance-density data are used only to seed
the numerical search.  Every accepted value comes from a
``PassiveDeviceEvaluator`` so the search algorithm is independent of foundry
model details and can be connected to CDF/PCell callbacks, Spectre probes, or
pre-characterized lookup tables.
"""

from __future__ import annotations

import itertools
import json
import math
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from pdk_profiles import PassiveDeviceProfile, PDKProfile, get_pdk_profile


class PassiveMappingError(ValueError):
    """Raised when no configured PDK passive can meet the target."""


class IllegalDeviceGeometry(ValueError):
    """PDK callback signal that one proposed geometry is not legal."""


@dataclass(frozen=True)
class DeviceEvaluation:
    """One authoritative result returned by a PDK device evaluator."""

    actual_value: float
    area_m2: float | None = None
    resolved_params: dict[str, object] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)


class PassiveDeviceEvaluator(Protocol):
    """Black-box PDK value interface used by the geometry search."""

    backend_name: str

    def evaluate_device(
        self,
        device: PassiveDeviceProfile,
        params: Mapping[str, object],
    ) -> DeviceEvaluation:
        """Return actual R (ohm) or C (farad) for one legal PCell geometry."""


class PassiveTargetMapper(Protocol):
    """Map one target using a backend that evaluates geometries in batches."""

    backend_name: str

    def map_candidates(
        self,
        device_name: str,
        device: PassiveDeviceProfile,
        target_value: float,
        constraints: "PassiveMappingConstraints",
    ) -> list["PassiveMappingResult"]:
        """Return legal implementations ranked by the backend."""


class CallablePassiveEvaluator:
    """Adapter for an existing CDF/PCell/Spectre Python callback."""

    def __init__(
        self,
        callback: Callable[
            [PassiveDeviceProfile, Mapping[str, object]],
            DeviceEvaluation | float,
        ],
        *,
        backend_name: str = "pdk_callback",
    ) -> None:
        self._callback = callback
        self.backend_name = backend_name

    def evaluate_device(
        self,
        device: PassiveDeviceProfile,
        params: Mapping[str, object],
    ) -> DeviceEvaluation:
        result = self._callback(device, params)
        if isinstance(result, DeviceEvaluation):
            return result
        return DeviceEvaluation(actual_value=float(result))


_EVALUATORS: dict[str, PassiveDeviceEvaluator] = {}


def register_passive_evaluator(
    key: str,
    evaluator: PassiveDeviceEvaluator,
) -> None:
    """Register a runtime PDK callback without storing code in the profile."""

    if not key:
        raise ValueError("Passive evaluator key must not be empty")
    _EVALUATORS[key] = evaluator


def unregister_passive_evaluator(key: str) -> None:
    """Remove one runtime evaluator, primarily for tests and process teardown."""

    _EVALUATORS.pop(key, None)


@dataclass(frozen=True)
class PassiveMappingConstraints:
    """Optional overrides shared by resistor and capacitor mapping."""

    tolerance: float | None = None
    fixed_width_m: float | None = None
    max_area_m2: float | None = None
    max_series_units: int | None = None
    max_parallel_units: int | None = None
    preferred_aspect_ratio: float | None = None
    max_aspect_ratio: float | None = None
    matching_required: bool = False
    candidate_limit: int = 32

    @classmethod
    def coerce(
        cls,
        value: "PassiveMappingConstraints | Mapping[str, object] | None",
    ) -> "PassiveMappingConstraints":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        return cls(**dict(value))


@dataclass(frozen=True)
class PassiveMappingResult:
    """Selected legal PDK implementation and its mapping audit data."""

    device_kind: str
    device_type: str
    target_value: float
    actual_value: float
    relative_error: float
    params: dict[str, object]
    series_units: int = 1
    parallel_units: int = 1
    unit_value: float | None = None
    unit_area_m2: float | None = None
    area_m2: float | None = None
    evaluator_backend: str = ""
    evaluator_metadata: dict[str, object] = field(default_factory=dict)
    matching: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        if self.device_kind == "resistor":
            data.update(target_R=self.target_value, actual_R=self.actual_value)
        elif self.device_kind == "capacitor":
            data.update(target_C=self.target_value, actual_C=self.actual_value)
        return data


@dataclass(frozen=True)
class _Candidate:
    result: PassiveMappingResult

    def score(self) -> tuple[float, float, int, str]:
        area = self.result.area_m2
        return (
            round(self.result.relative_error, 12),
            area if area is not None else math.inf,
            self.result.series_units * self.result.parallel_units,
            self.result.device_type,
        )


class _BaseMapper:
    kind: str

    def __init__(
        self,
        device_name: str,
        device: PassiveDeviceProfile,
        evaluator: PassiveDeviceEvaluator,
    ) -> None:
        if device.kind != self.kind:
            raise ValueError(
                f"Device '{device_name}' is {device.kind}, expected {self.kind}"
            )
        self.device_name = device_name
        self.device = device
        self.evaluator = evaluator

    def map(
        self,
        target: float,
        constraints: PassiveMappingConstraints | Mapping[str, object] | None = None,
    ) -> list[PassiveMappingResult]:
        limits = PassiveMappingConstraints.coerce(constraints)
        _validate_target(target)
        _validate_constraints(limits)
        if self.device.mapping_mode == "value":
            return [self._map_direct_value(target, limits)]
        if self.device.mapping_mode == "lookup" and isinstance(
            self.evaluator, _LookupEvaluator
        ):
            return self._map_lookup(target, limits, self.evaluator)
        self._require_geometry()
        candidates: list[_Candidate] = []
        max_series = limits.max_series_units or self.device.max_series_units
        max_parallel = limits.max_parallel_units or self.device.max_parallel_units
        tolerance = (
            limits.tolerance
            if limits.tolerance is not None
            else self.device.value_tolerance
        )
        decompositions = [(1, 1)] + [
            (series, parallel)
            for series in range(1, max_series + 1)
            for parallel in range(1, max_parallel + 1)
            if (series, parallel) != (1, 1)
        ]
        for series, parallel in decompositions:
            unit_target = self._unit_target(target, series, parallel)
            for integer_params in _integer_parameter_options(self.device):
                for width in self._width_candidates(unit_target, limits):
                    candidates.extend(
                        self._search_length(
                            target=target,
                            unit_target=unit_target,
                            width=width,
                            series=series,
                            parallel=parallel,
                            integer_params=integer_params,
                            constraints=limits,
                        )
                    )
            # External series/parallel decomposition is a fallback.  If one
            # legal PCell already meets tolerance, preserve the simpler unit.
            if (
                (series, parallel) == (1, 1)
                and candidates
                and min(item.result.relative_error for item in candidates)
                <= tolerance
            ):
                break
        if not candidates:
            raise PassiveMappingError(
                f"No legal {self.kind} geometry for {target:g} using "
                f"'{self.device_name}'"
            )
        ordered = sorted(candidates, key=lambda item: item.score())
        unique: list[PassiveMappingResult] = []
        signatures: set[tuple[object, ...]] = set()
        for candidate in ordered:
            result = candidate.result
            signature = (
                result.device_type,
                result.series_units,
                result.parallel_units,
                tuple(sorted((key, str(value)) for key, value in result.params.items())),
            )
            if signature in signatures:
                continue
            signatures.add(signature)
            unique.append(result)
            if len(unique) >= limits.candidate_limit:
                break
        if unique[0].relative_error > tolerance:
            raise PassiveMappingError(
                f"{self.device_name} cannot realize {target:g} within "
                f"{tolerance:.2%}; best error is {unique[0].relative_error:.2%}"
            )
        return unique

    def _map_lookup(
        self,
        target: float,
        constraints: PassiveMappingConstraints,
        evaluator: "_LookupEvaluator",
    ) -> list[PassiveMappingResult]:
        max_series = constraints.max_series_units or self.device.max_series_units
        max_parallel = constraints.max_parallel_units or self.device.max_parallel_units
        tolerance = (
            constraints.tolerance
            if constraints.tolerance is not None
            else self.device.value_tolerance
        )
        candidates: list[_Candidate] = []
        decompositions = [(1, 1)] + [
            (series, parallel)
            for series in range(1, max_series + 1)
            for parallel in range(1, max_parallel + 1)
            if (series, parallel) != (1, 1)
        ]
        for series, parallel in decompositions:
            for point in evaluator.points:
                unit_value = float(point["value"])
                actual = self._combined_value(unit_value, series, parallel)
                params = dict(point.get("params") or {})
                unit_area = _lookup_point_area(point, self.device, params)
                area = (
                    unit_area * series * parallel
                    if unit_area is not None
                    else None
                )
                if (
                    constraints.max_area_m2 is not None
                    and (area is None or area > constraints.max_area_m2)
                ):
                    continue
                matching = (
                    {
                        "required": True,
                        "unit_count": series * parallel,
                        "decomposition": "series_parallel",
                    }
                    if constraints.matching_required
                    else {}
                )
                candidates.append(_Candidate(PassiveMappingResult(
                    device_kind=self.kind,
                    device_type=self.device_name,
                    target_value=target,
                    actual_value=actual,
                    relative_error=abs(actual - target) / target,
                    params=params,
                    series_units=series,
                    parallel_units=parallel,
                    unit_value=unit_value,
                    unit_area_m2=unit_area,
                    area_m2=area,
                    evaluator_backend=evaluator.backend_name,
                    evaluator_metadata={
                        "lookup_table": str(evaluator.path),
                        "point": dict(point),
                    },
                    matching=matching,
                )))
            if (
                (series, parallel) == (1, 1)
                and candidates
                and min(item.result.relative_error for item in candidates) <= tolerance
            ):
                break
        if not candidates:
            raise PassiveMappingError(
                f"No characterized lookup points for '{self.device_name}'"
            )
        ordered = sorted(candidates, key=lambda item: item.score())
        results = [item.result for item in ordered[: constraints.candidate_limit]]
        if results[0].relative_error > tolerance:
            raise PassiveMappingError(
                f"{self.device_name} cannot realize {target:g} within "
                f"{tolerance:.2%}; best error is {results[0].relative_error:.2%}"
            )
        return results

    def _map_direct_value(
        self,
        target: float,
        constraints: PassiveMappingConstraints,
    ) -> PassiveMappingResult:
        if not self.device.value_parameter:
            raise PassiveMappingError(
                f"Direct-value device '{self.device_name}' has no value_parameter"
            )
        params = {
            **self.device.fixed_parameters,
            self.device.value_parameter: target,
        }
        evaluation = _checked_evaluation(
            self.evaluator.evaluate_device(self.device, params), self.device_name
        )
        error = abs(evaluation.actual_value - target) / target
        tolerance = (
            constraints.tolerance
            if constraints.tolerance is not None
            else self.device.value_tolerance
        )
        if error > tolerance:
            raise PassiveMappingError(
                f"{self.device_name} direct value error {error:.2%} exceeds "
                f"{tolerance:.2%}"
            )
        return PassiveMappingResult(
            device_kind=self.kind,
            device_type=self.device_name,
            target_value=target,
            actual_value=evaluation.actual_value,
            relative_error=error,
            params=evaluation.resolved_params or params,
            unit_value=evaluation.actual_value,
            unit_area_m2=evaluation.area_m2,
            area_m2=evaluation.area_m2,
            evaluator_backend=self.evaluator.backend_name,
            evaluator_metadata=evaluation.metadata,
        )

    def _search_length(
        self,
        *,
        target: float,
        unit_target: float,
        width: float,
        series: int,
        parallel: int,
        integer_params: Mapping[str, int],
        constraints: PassiveMappingConstraints,
    ) -> list[_Candidate]:
        grid = self.device.geometry_grid_m
        assert grid is not None
        min_length = self.device.min_length_m
        max_length = self.device.max_length_m
        assert min_length is not None and max_length is not None
        low = math.ceil((min_length / grid) - 1e-12)
        high = math.floor((max_length / grid) + 1e-12)
        initial = _snap_index(self._initial_length(unit_target, width), grid, low, high)
        sample_indices = {
            low,
            high,
            initial,
            low + (high - low) // 4,
            low + (high - low) // 2,
            low + 3 * (high - low) // 4,
        }
        evaluated: dict[int, tuple[DeviceEvaluation, dict[str, object]]] = {}

        def evaluate(index: int) -> tuple[DeviceEvaluation, dict[str, object]] | None:
            index = min(max(index, low), high)
            if index in evaluated:
                return evaluated[index]
            length = index * grid
            if not self._legal_geometry(width, length, constraints):
                return None
            params: dict[str, object] = {
                **self.device.fixed_parameters,
                self.device.width_parameter: width,
                self.device.length_parameter: length,
                **integer_params,
            }
            try:
                result = _checked_evaluation(
                    self.evaluator.evaluate_device(self.device, params),
                    self.device_name,
                )
            except IllegalDeviceGeometry:
                return None
            evaluated[index] = (result, result.resolved_params or params)
            return evaluated[index]

        for index in sample_indices:
            evaluate(index)
        ordered_indices = sorted(evaluated)
        for left, right in zip(ordered_indices, ordered_indices[1:]):
            left_value = evaluated[left][0].actual_value - unit_target
            right_value = evaluated[right][0].actual_value - unit_target
            if left_value == 0 or right_value == 0 or left_value * right_value < 0:
                _bisect_grid(left, right, unit_target, evaluate)
        best_indices = sorted(
            evaluated,
            key=lambda index: abs(evaluated[index][0].actual_value - unit_target),
        )[:4]
        for index in list(best_indices):
            for neighbor in range(index - 2, index + 3):
                evaluate(neighbor)

        candidates: list[_Candidate] = []
        for evaluation, params in evaluated.values():
            actual = self._combined_value(
                evaluation.actual_value, series, parallel
            )
            unit_area = evaluation.area_m2
            if unit_area is None:
                unit_area = width * float(params[self.device.length_parameter])
            area = unit_area * series * parallel
            if constraints.max_area_m2 is not None and area > constraints.max_area_m2:
                continue
            matching = (
                {
                    "required": True,
                    "unit_count": series * parallel,
                    "decomposition": "series_parallel",
                }
                if constraints.matching_required
                else {}
            )
            result = PassiveMappingResult(
                device_kind=self.kind,
                device_type=self.device_name,
                target_value=target,
                actual_value=actual,
                relative_error=abs(actual - target) / target,
                params=params,
                series_units=series,
                parallel_units=parallel,
                unit_value=evaluation.actual_value,
                unit_area_m2=unit_area,
                area_m2=area,
                evaluator_backend=self.evaluator.backend_name,
                evaluator_metadata=evaluation.metadata,
                matching=matching,
            )
            candidates.append(_Candidate(result))
        return candidates

    def _legal_geometry(
        self,
        width: float,
        length: float,
        constraints: PassiveMappingConstraints,
    ) -> bool:
        device = self.device
        assert device.min_width_m is not None and device.max_width_m is not None
        assert device.min_length_m is not None and device.max_length_m is not None
        if not device.min_width_m <= width <= device.max_width_m:
            return False
        if not device.min_length_m <= length <= device.max_length_m:
            return False
        area = width * length
        if device.max_unit_area_m2 is not None and area > device.max_unit_area_m2:
            return False
        max_aspect = constraints.max_aspect_ratio or device.max_aspect_ratio
        if max_aspect is not None and max(width / length, length / width) > max_aspect:
            return False
        return True

    def _require_geometry(self) -> None:
        required = (
            self.device.min_width_m,
            self.device.max_width_m,
            self.device.min_length_m,
            self.device.max_length_m,
            self.device.geometry_grid_m,
        )
        if any(value is None or value <= 0 for value in required):
            raise PassiveMappingError(
                f"Device '{self.device_name}' has incomplete geometry constraints"
            )

    def _width_candidates(
        self,
        unit_target: float,
        constraints: PassiveMappingConstraints,
    ) -> Sequence[float]:
        raise NotImplementedError

    def _initial_length(self, unit_target: float, width: float) -> float:
        raise NotImplementedError

    def _unit_target(self, target: float, series: int, parallel: int) -> float:
        raise NotImplementedError

    def _combined_value(self, unit: float, series: int, parallel: int) -> float:
        raise NotImplementedError


class ResistorMapper(_BaseMapper):
    kind = "resistor"

    def map(
        self,
        target: float,
        constraints: PassiveMappingConstraints | Mapping[str, object] | None = None,
    ) -> list[PassiveMappingResult]:
        limits = PassiveMappingConstraints.coerce(constraints)
        if limits.fixed_width_m is not None:
            return super().map(target, limits)
        assert self.device.min_width_m is not None
        assert self.device.max_width_m is not None
        default_width = self.device.default_width_m or self.device.min_width_m
        try:
            return super().map(
                target, replace(limits, fixed_width_m=default_width)
            )
        except PassiveMappingError:
            # Broaden W only when the preferred strip cannot meet tolerance.
            return super().map(target, limits)

    def _width_candidates(
        self,
        unit_target: float,
        constraints: PassiveMappingConstraints,
    ) -> Sequence[float]:
        device = self.device
        assert device.geometry_grid_m is not None
        assert device.min_width_m is not None and device.max_width_m is not None
        if constraints.fixed_width_m is not None:
            raw = [constraints.fixed_width_m]
        else:
            default = device.default_width_m or device.min_width_m
            raw = [default, device.min_width_m, math.sqrt(
                device.min_width_m * device.max_width_m
            ), device.max_width_m]
        return _legal_snapped_widths(raw, device)

    def _initial_length(self, unit_target: float, width: float) -> float:
        rsh = self.device.sheet_resistance_ohm_per_square
        if rsh is not None and rsh > 0:
            return unit_target * width / rsh
        aspect = self.device.default_aspect_ratio or 4.0
        return width * aspect

    def _unit_target(self, target: float, series: int, parallel: int) -> float:
        return target * parallel / series

    def _combined_value(self, unit: float, series: int, parallel: int) -> float:
        return unit * series / parallel


class CapacitorMapper(_BaseMapper):
    kind = "capacitor"

    def _width_candidates(
        self,
        unit_target: float,
        constraints: PassiveMappingConstraints,
    ) -> Sequence[float]:
        device = self.device
        assert device.min_width_m is not None and device.max_width_m is not None
        if constraints.fixed_width_m is not None:
            raw = [constraints.fixed_width_m]
        else:
            aspect = (
                constraints.preferred_aspect_ratio
                or device.default_aspect_ratio
                or 1.0
            )
            density = device.capacitance_per_area_f_per_m2
            estimated = None
            if density is not None and density > 0:
                estimated = math.sqrt(max(unit_target / density / aspect, 0.0))
            default = device.default_width_m or estimated or device.min_width_m
            raw = [
                default,
                device.min_width_m,
                math.sqrt(device.min_width_m * device.max_width_m),
                device.max_width_m,
            ]
            if estimated is not None:
                raw.extend((estimated * 0.75, estimated, estimated * 1.25))
        return _legal_snapped_widths(raw, device)

    def _initial_length(self, unit_target: float, width: float) -> float:
        density = self.device.capacitance_per_area_f_per_m2
        if density is not None and density > 0:
            return unit_target / (density * width)
        aspect = self.device.default_aspect_ratio or 1.0
        return width * aspect

    def _unit_target(self, target: float, series: int, parallel: int) -> float:
        return target * series / parallel

    def _combined_value(self, unit: float, series: int, parallel: int) -> float:
        return unit * parallel / series


def map_passive(
    device_kind: str,
    target_value: float,
    device_type: str | None = None,
    constraints: PassiveMappingConstraints | Mapping[str, object] | None = None,
    *,
    profile: PDKProfile | None = None,
    evaluator: PassiveDeviceEvaluator | None = None,
    evaluators: Mapping[str, PassiveDeviceEvaluator] | None = None,
) -> PassiveMappingResult:
    """Map one ideal passive, optionally ranking all configured PDK types."""

    return map_passive_candidates(
        device_kind,
        target_value,
        device_type,
        constraints,
        profile=profile,
        evaluator=evaluator,
        evaluators=evaluators,
    )[0]


def map_passive_candidates(
    device_kind: str,
    target_value: float,
    device_type: str | None = None,
    constraints: PassiveMappingConstraints | Mapping[str, object] | None = None,
    *,
    profile: PDKProfile | None = None,
    evaluator: PassiveDeviceEvaluator | None = None,
    evaluators: Mapping[str, PassiveDeviceEvaluator] | None = None,
) -> list[PassiveMappingResult]:
    """Return legal implementations ranked by error, area, and unit count."""

    kind = _normalize_kind(device_kind)
    pdk = profile or get_pdk_profile()
    if device_type:
        names = [device_type]
    else:
        names = [
            name for name, device in pdk.passive_devices.items()
            if device.kind == kind
        ]
    if not names:
        raise PassiveMappingError(
            f"PDK '{pdk.name}' has no configured {kind} device candidate"
        )
    results: list[PassiveMappingResult] = []
    failures: list[str] = []
    for name in names:
        try:
            device = pdk.passive_devices[name]
        except KeyError:
            failures.append(f"unknown device '{name}'")
            continue
        try:
            limits = PassiveMappingConstraints.coerce(constraints)
            target_mapper = (
                None
                if evaluator or (evaluators or {}).get(name)
                else build_passive_target_mapper(name, device, pdk)
            )
            if target_mapper is not None:
                results.extend(
                    target_mapper.map_candidates(name, device, target_value, limits)
                )
                continue
            selected_evaluator = (
                evaluator
                or (evaluators or {}).get(name)
                or build_passive_evaluator(name, device)
            )
            mapper_cls = ResistorMapper if kind == "resistor" else CapacitorMapper
            results.extend(
                mapper_cls(name, device, selected_evaluator).map(
                    target_value, constraints
                )
            )
        except (PassiveMappingError, ValueError) as exc:
            failures.append(f"{name}: {exc}")
    if not results:
        detail = "; ".join(failures) or "no candidates"
        raise PassiveMappingError(
            f"Unable to map {kind} target {target_value:g}: {detail}"
        )
    ordered = sorted(results, key=lambda result: _Candidate(result).score())
    return ordered


def build_passive_target_mapper(
    device_name: str,
    device: PassiveDeviceProfile,
    profile: PDKProfile,
) -> PassiveTargetMapper | None:
    """Build an in-tree batched mapper selected by the profile evaluator key."""

    if device.mapping_mode != "callback":
        return None
    if device.evaluator_key == "virtuoso_cdf_cfmom_2t":
        from pdk_cdf_evaluator import CdfCfmomTargetMapper

        return CdfCfmomTargetMapper(profile=profile, device_name=device_name)
    return None


def map_resistor(
    target_R: float,
    resistor_type: str | None = None,
    constraints: PassiveMappingConstraints | Mapping[str, object] | None = None,
    **kwargs,
) -> PassiveMappingResult:
    """Map an ideal resistance to one legal PDK resistor implementation."""

    return map_passive(
        "resistor", target_R, resistor_type, constraints, **kwargs
    )


def map_capacitor(
    target_C: float,
    capacitor_type: str | None = None,
    constraints: PassiveMappingConstraints | Mapping[str, object] | None = None,
    **kwargs,
) -> PassiveMappingResult:
    """Map an ideal capacitance to one legal PDK capacitor implementation."""

    return map_passive(
        "capacitor", target_C, capacitor_type, constraints, **kwargs
    )


class _FormulaEvaluator:
    """Offline fallback; production profiles should prefer callback or LUT."""

    backend_name = "analytic_profile_fallback"

    def evaluate_device(
        self,
        device: PassiveDeviceProfile,
        params: Mapping[str, object],
    ) -> DeviceEvaluation:
        width = float(params[device.width_parameter])
        length = float(params[device.length_parameter])
        if device.kind == "resistor":
            rsh = device.sheet_resistance_ohm_per_square
            if rsh is None or rsh <= 0:
                raise ValueError("Formula resistor requires positive sheet resistance")
            value = rsh * length / width
        else:
            density = device.capacitance_per_area_f_per_m2
            if density is None or density <= 0:
                raise ValueError("Formula capacitor requires positive area density")
            value = (
                density * width * length
                + 2 * device.capacitance_perimeter_f_per_m * (width + length)
            )
        return DeviceEvaluation(value, width * length, dict(params))


class _DirectValueEvaluator:
    backend_name = "pdk_value_parameter"

    def evaluate_device(
        self,
        device: PassiveDeviceProfile,
        params: Mapping[str, object],
    ) -> DeviceEvaluation:
        return DeviceEvaluation(
            float(params[device.value_parameter]), resolved_params=dict(params)
        )


class _LookupEvaluator:
    backend_name = "characterized_lookup"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.points = raw.get("points", []) if isinstance(raw, dict) else raw
        if not isinstance(self.points, list) or not self.points:
            raise ValueError(f"Passive lookup table has no points: {self.path}")

    def evaluate_device(
        self,
        device: PassiveDeviceProfile,
        params: Mapping[str, object],
    ) -> DeviceEvaluation:
        width = float(params[device.width_parameter])
        length = float(params[device.length_parameter])
        best = min(
            self.points,
            key=lambda point: _lookup_distance(point, device, width, length),
        )
        return DeviceEvaluation(
            actual_value=float(best["value"]),
            area_m2=(
                float(best["area_m2"])
                if best.get("area_m2") is not None
                else width * length
            ),
            resolved_params=dict(best.get("params") or params),
            metadata={"lookup_table": str(self.path), "point": dict(best)},
        )


def _lookup_point_area(
    point: Mapping[str, object],
    device: PassiveDeviceProfile,
    params: Mapping[str, object],
) -> float | None:
    for key in ("area_m2", "estimated_area_m2"):
        if point.get(key) is not None:
            return float(point[key])
    try:
        return float(params[device.width_parameter]) * float(
            params[device.length_parameter]
        )
    except (KeyError, TypeError, ValueError):
        return None


def build_passive_evaluator(
    device_name: str,
    device: PassiveDeviceProfile,
) -> PassiveDeviceEvaluator:
    """Resolve the configured evaluator without embedding foundry equations."""

    key = device.evaluator_key or device_name
    if key in _EVALUATORS:
        return _EVALUATORS[key]
    if device.mapping_mode == "callback":
        raise PassiveMappingError(
            f"PDK callback evaluator '{key}' is not registered"
        )
    if device.mapping_mode == "lookup":
        return _LookupEvaluator(device.lookup_table_path)
    if device.mapping_mode == "formula":
        return _FormulaEvaluator()
    if device.mapping_mode == "value":
        return _DirectValueEvaluator()
    raise PassiveMappingError(
        f"Unsupported mapping mode '{device.mapping_mode}' for '{device_name}'"
    )


def _integer_parameter_options(
    device: PassiveDeviceProfile,
) -> Sequence[dict[str, int]]:
    ranges: list[tuple[str, Sequence[int]]] = []
    for name, maximum in (
        (device.multiplier_parameter, device.max_multiplier),
        (device.segment_parameter, device.max_segments),
        (device.finger_parameter, device.max_fingers),
        (device.array_rows_parameter, device.max_array_rows),
        (device.array_columns_parameter, device.max_array_columns),
    ):
        if name and maximum > 1:
            values = list(range(1, min(maximum, 8) + 1))
            if maximum not in values:
                values.append(maximum)
            ranges.append((name, values))
    if not ranges:
        return ({},)
    combinations: list[dict[str, int]] = [{}]
    for name, values in ranges:
        combinations.extend({name: int(value)} for value in values if value != 1)
    array_names = {
        device.array_rows_parameter,
        device.array_columns_parameter,
    }
    array_ranges = [item for item in ranges if item[0] in array_names]
    if len(array_ranges) == 2:
        rows, columns = array_ranges
        for row, column in itertools.product(rows[1], columns[1]):
            if row == column or row == 1 or column == 1:
                combinations.append({rows[0]: int(row), columns[0]: int(column)})
    return combinations


def _bisect_grid(
    low: int,
    high: int,
    target: float,
    evaluate: Callable[
        [int], tuple[DeviceEvaluation, dict[str, object]] | None
    ],
) -> None:
    left = evaluate(low)
    right = evaluate(high)
    if left is None or right is None:
        return
    left_delta = left[0].actual_value - target
    while high - low > 1:
        mid = (low + high) // 2
        current = evaluate(mid)
        if current is None:
            return
        delta = current[0].actual_value - target
        if delta == 0:
            return
        if left_delta == 0 or left_delta * delta < 0:
            high = mid
        else:
            low = mid
            left_delta = delta


def _legal_snapped_widths(
    values: Sequence[float],
    device: PassiveDeviceProfile,
) -> Sequence[float]:
    assert device.geometry_grid_m is not None
    assert device.min_width_m is not None and device.max_width_m is not None
    widths = {
        round(value / device.geometry_grid_m) * device.geometry_grid_m
        for value in values
        if math.isfinite(value)
    }
    return sorted(
        width for width in widths
        if device.min_width_m <= width <= device.max_width_m
    )


def _snap_index(value: float, grid: float, low: int, high: int) -> int:
    if not math.isfinite(value):
        return low + (high - low) // 2
    return min(max(round(value / grid), low), high)


def _checked_evaluation(
    evaluation: DeviceEvaluation,
    device_name: str,
) -> DeviceEvaluation:
    if not math.isfinite(evaluation.actual_value) or evaluation.actual_value <= 0:
        raise ValueError(
            f"Evaluator for '{device_name}' returned invalid value "
            f"{evaluation.actual_value}"
        )
    if evaluation.area_m2 is not None and (
        not math.isfinite(evaluation.area_m2) or evaluation.area_m2 <= 0
    ):
        raise ValueError(
            f"Evaluator for '{device_name}' returned invalid area "
            f"{evaluation.area_m2}"
        )
    return evaluation


def _lookup_distance(
    point: Mapping[str, object],
    device: PassiveDeviceProfile,
    width: float,
    length: float,
) -> float:
    params = dict(point.get("params") or {})
    try:
        point_w = _engineering_float(params[device.width_parameter])
        point_l = _engineering_float(params[device.length_parameter])
    except (KeyError, TypeError, ValueError):
        return math.inf
    return abs(math.log(point_w / width)) + abs(math.log(point_l / length))


def _engineering_float(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    scales = {
        "f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6,
        "m": 1e-3, "k": 1e3, "meg": 1e6, "g": 1e9,
    }
    for suffix in sorted(scales, key=len, reverse=True):
        if text.endswith(suffix):
            return float(text[:-len(suffix)]) * scales[suffix]
    return float(text)


def _normalize_kind(kind: str) -> str:
    normalized = kind.lower().strip()
    aliases = {"r": "resistor", "res": "resistor", "c": "capacitor", "cap": "capacitor"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"resistor", "capacitor"}:
        raise ValueError(f"Unsupported passive kind '{kind}'")
    return normalized


def _validate_target(target: float) -> None:
    if not math.isfinite(target) or target <= 0:
        raise ValueError(f"Passive target must be positive and finite: {target}")


def _validate_constraints(constraints: PassiveMappingConstraints) -> None:
    if constraints.tolerance is not None and not 0 <= constraints.tolerance < 1:
        raise ValueError("Passive mapping tolerance must be in [0, 1)")
    for name, value in (
        ("fixed_width_m", constraints.fixed_width_m),
        ("max_area_m2", constraints.max_area_m2),
        ("preferred_aspect_ratio", constraints.preferred_aspect_ratio),
        ("max_aspect_ratio", constraints.max_aspect_ratio),
    ):
        if value is not None and value <= 0:
            raise ValueError(f"Passive mapping {name} must be positive")
    for name, value in (
        ("max_series_units", constraints.max_series_units),
        ("max_parallel_units", constraints.max_parallel_units),
    ):
        if value is not None and value < 1:
            raise ValueError(f"Passive mapping {name} must be positive")
    if constraints.candidate_limit < 1:
        raise ValueError("Passive mapping candidate_limit must be positive")
