"""PCell introspection artifacts and Virtuoso-backed readback helpers."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .skill_server import SkillResult, VirtuosoSkillClient

BBox = tuple[float, float, float, float]
Point = tuple[float, float]


@dataclass(frozen=True)
class PCellIntrospectionRequest:
    logical_name: str
    lib_name: str
    cell_name: str
    view_name: str = "layout"
    params: dict[str, Any] = field(default_factory=dict)
    orient: str = "R0"
    instance_name: str = "DUT"
    calibration_lib: str = "analogskills_pcell_calib"
    calibration_cell: str = "pcell_introspect"

    @property
    def pcell_key(self) -> str:
        return f"{self.lib_name}/{self.cell_name}/{self.view_name}"

    @property
    def params_signature(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((str(key), str(value)) for key, value in self.params.items()))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PCellIntrospectionRequest":
        return cls(
            logical_name=str(data.get("logical_name", "")),
            lib_name=str(data.get("lib_name", "")),
            cell_name=str(data.get("cell_name", "")),
            view_name=str(data.get("view_name", "layout")),
            params=dict(data.get("params", {})),
            orient=str(data.get("orient", "R0")),
            instance_name=str(data.get("instance_name", "DUT")),
            calibration_lib=str(data.get("calibration_lib", "analogskills_pcell_calib")),
            calibration_cell=str(data.get("calibration_cell", "pcell_introspect")),
        )


@dataclass(frozen=True)
class PCellPinFigure:
    terminal: str
    pin_name: str
    layer: str
    purpose: str
    bbox_um: BBox
    center_um: Point
    source: str = "oa_pin"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PCellPinFigure":
        bbox = _bbox_tuple(data.get("bbox_um", data.get("bbox", (0.0, 0.0, 0.0, 0.0))))
        center = data.get("center_um", data.get("center"))
        return cls(
            terminal=str(data.get("terminal", "")),
            pin_name=str(data.get("pin_name", data.get("name", ""))),
            layer=str(data.get("layer", "")),
            purpose=str(data.get("purpose", "")),
            bbox_um=bbox,
            center_um=_point_tuple(center) if center is not None else _bbox_center(bbox),
            source=str(data.get("source", "oa_pin")),
        )


@dataclass(frozen=True)
class PCellTerm:
    name: str
    pins: tuple[PCellPinFigure, ...] = ()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PCellTerm":
        name = str(data.get("name", ""))
        pins = tuple(
            PCellPinFigure.from_dict({"terminal": name, **dict(pin)})
            for pin in data.get("pins", ())
        )
        return cls(name, pins)


@dataclass(frozen=True)
class PCellLabel:
    text: str
    layer: str
    purpose: str
    xy_um: Point

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PCellLabel":
        return cls(
            text=str(data.get("text", "")),
            layer=str(data.get("layer", "")),
            purpose=str(data.get("purpose", "")),
            xy_um=_point_tuple(data.get("xy_um", data.get("xy", (0.0, 0.0)))),
        )


@dataclass(frozen=True)
class PCellConductiveShape:
    layer: str
    purpose: str
    bbox_um: BBox
    terminal: str = ""
    net: str = ""
    source: str = "shape"

    @property
    def center_um(self) -> Point:
        return _bbox_center(self.bbox_um)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PCellConductiveShape":
        return cls(
            layer=str(data.get("layer", "")),
            purpose=str(data.get("purpose", "")),
            bbox_um=_bbox_tuple(data.get("bbox_um", data.get("bbox", (0.0, 0.0, 0.0, 0.0)))),
            terminal=str(data.get("terminal", "")),
            net=str(data.get("net", "")),
            source=str(data.get("source", "shape")),
        )


@dataclass(frozen=True)
class PCellAccessCandidate:
    terminal: str
    xy_um: Point
    layer: str
    source: str
    bbox_um: BBox | None = None
    confidence: float = 1.0
    reason: str = ""
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PCellIntrospectionResult:
    request: PCellIntrospectionRequest
    master_bbox_um: BBox | None = None
    instance_bbox_um: BBox | None = None
    terms: tuple[PCellTerm, ...] = ()
    pins: tuple[PCellPinFigure, ...] = ()
    labels: tuple[PCellLabel, ...] = ()
    conductive_shapes: tuple[PCellConductiveShape, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    raw_artifact_path: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "master_bbox_um": self.master_bbox_um,
            "instance_bbox_um": self.instance_bbox_um,
            "terms": [asdict(term) for term in self.terms],
            "pins": [asdict(pin) for pin in self.pins],
            "labels": [asdict(label) for label in self.labels],
            "conductive_shapes": [asdict(shape) for shape in self.conductive_shapes],
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "raw_artifact_path": self.raw_artifact_path,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, artifact_path: str | Path = "") -> "PCellIntrospectionResult":
        request_data = data.get("request", {})
        request = request_data if isinstance(request_data, PCellIntrospectionRequest) else PCellIntrospectionRequest.from_dict(dict(request_data))
        terms = tuple(PCellTerm.from_dict(term) for term in data.get("terms", ()))
        pins = tuple(PCellPinFigure.from_dict(pin) for pin in data.get("pins", ()))
        if not pins:
            pins = tuple(pin for term in terms for pin in term.pins)
        return cls(
            request=request,
            master_bbox_um=_optional_bbox(data.get("master_bbox_um", data.get("master_bbox"))),
            instance_bbox_um=_optional_bbox(data.get("instance_bbox_um", data.get("instance_bbox"))),
            terms=terms,
            pins=pins,
            labels=tuple(PCellLabel.from_dict(label) for label in data.get("labels", ())),
            conductive_shapes=tuple(PCellConductiveShape.from_dict(shape) for shape in data.get("conductive_shapes", data.get("shapes", ()))),
            warnings=tuple(str(item) for item in data.get("warnings", ())),
            errors=tuple(str(item) for item in data.get("errors", ())),
            raw_artifact_path=str(data.get("raw_artifact_path", artifact_path)),
            metadata=dict(data.get("metadata", {})),
        )

    @classmethod
    def from_json(cls, path: str | Path) -> "PCellIntrospectionResult":
        path_obj = Path(path)
        return cls.from_dict(json.loads(path_obj.read_text(encoding="utf-8")), artifact_path=path_obj)

    def save_json(self, path: str | Path) -> Path:
        path_obj = Path(path)
        path_obj.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return path_obj

    def terminal_access_candidates(self, terminal: str, *, preferred_layers: Sequence[str] = ()) -> tuple[PCellAccessCandidate, ...]:
        """Return terminal access candidates in extraction-policy order."""

        terminal_text = str(terminal)
        candidates: list[PCellAccessCandidate] = []
        for pin in self.pins:
            if pin.terminal == terminal_text:
                pin_layer = str(pin.layer)
                reason = "oa pin figure"
                warnings: tuple[str, ...] = ()
                confidence = 1.0
                if pin_layer.lower() == "pin":
                    warnings = ("OA pin figure uses abstract layer 'pin'; likely ivpcell fallback without physical layer semantics",)
                    reason = "oa pin figure on abstract pin layer"
                    confidence = 0.2
                if self.conductive_shapes:
                    overlapping_shapes = _shapes_overlapping_bbox(
                        self.conductive_shapes,
                        pin.bbox_um,
                        layers=(pin_layer,),
                        preferred_layers=(*preferred_layers, pin_layer),
                    )
                    if overlapping_shapes:
                        warnings = tuple(
                            dict.fromkeys(
                                [
                                    *_shape_terminal_mismatch_warnings(terminal_text, overlapping_shapes),
                                    *_ambiguous_shape_overlap_warnings(terminal_text, overlapping_shapes),
                                ]
                            )
                        )
                        reason = "oa pin figure on conductive shape"
                    else:
                        warnings = tuple(
                            dict.fromkeys(
                                [
                                    *warnings,
                                    f"OA pin figure {terminal_text} on {pin_layer} has no overlapping conductive shape",
                                ]
                            )
                        )
                        reason = "oa pin figure without conductive shape overlap"
                        confidence = min(confidence, 0.55)
                candidates.append(PCellAccessCandidate(terminal_text, pin.center_um, pin_layer, pin.source, pin.bbox_um, confidence, reason, warnings))
        if candidates:
            return _sort_candidates(candidates, preferred_layers)

        for label in self.labels:
            if label.text == terminal_text:
                matched_shapes = _shapes_at_point(self.conductive_shapes, label.xy_um, preferred_layers=(*preferred_layers, label.layer))
                if matched_shapes:
                    for shape in matched_shapes:
                        warnings = tuple(
                            dict.fromkeys(
                                [
                                    *_shape_terminal_mismatch_warnings(terminal_text, (shape,)),
                                    *_label_layer_mismatch_warnings(terminal_text, label.layer, (shape,)),
                                    *_ambiguous_shape_overlap_warnings(terminal_text, matched_shapes),
                                ]
                            )
                        )
                        candidates.append(
                            PCellAccessCandidate(
                                terminal_text,
                                label.xy_um,
                                shape.layer,
                                "label_on_shape",
                                shape.bbox_um,
                                0.85,
                                "matching label on conductive shape",
                                warnings,
                            )
                        )
                else:
                    warnings = (f"OA label {terminal_text} on {label.layer} has no overlapping conductive shape",)
                    candidates.append(PCellAccessCandidate(terminal_text, label.xy_um, label.layer, "oa_label", None, 0.55, "matching label without conductive shape overlap", warnings))
        if candidates:
            return _sort_candidates(candidates, preferred_layers)

        for shape in self.conductive_shapes:
            if shape.terminal == terminal_text or shape.net == terminal_text:
                candidates.append(PCellAccessCandidate(terminal_text, shape.center_um, shape.layer, shape.source, shape.bbox_um, 0.5, "shape inference"))
        return _sort_candidates(candidates, preferred_layers)


class PCellIntrospectionBackend(Protocol):
    def introspect(self, request: PCellIntrospectionRequest) -> PCellIntrospectionResult:
        ...


class FakePCellIntrospectionBackend:
    """Deterministic backend for tests and no-license development."""

    def __init__(self, results: Mapping[str, PCellIntrospectionResult | Mapping[str, Any]]):
        self.results = dict(results)
        self.requests: list[PCellIntrospectionRequest] = []

    def introspect(self, request: PCellIntrospectionRequest) -> PCellIntrospectionResult:
        self.requests.append(request)
        result = self.results.get(_request_key(request)) or self.results.get(request.pcell_key) or self.results.get(request.logical_name)
        if result is None:
            return PCellIntrospectionResult(request, errors=(f"no fake PCell introspection result for {request.pcell_key}",))
        if isinstance(result, PCellIntrospectionResult):
            return result
        data = dict(result)
        data.setdefault("request", request.to_dict())
        return PCellIntrospectionResult.from_dict(data)


class VirtuosoSkillPCellIntrospectionBackend:
    """Run PCell introspection through an already running Virtuoso SKILL server."""

    def __init__(
        self,
        client: VirtuosoSkillClient,
        *,
        skill_script: str | Path | None = None,
        out_file: str = "pcell_introspection.json",
    ) -> None:
        self.client = client
        self.skill_script = Path(skill_script) if skill_script is not None else Path(__file__).with_name("pcell_introspect.il")
        self.out_file = out_file

    def introspect(self, request: PCellIntrospectionRequest) -> PCellIntrospectionResult:
        expr = _skill_introspection_expr(request, self.skill_script, self.out_file)
        result = self.client.eval_result(expr, out_file=self.out_file)
        if not result.ok:
            return PCellIntrospectionResult(request, errors=(result.error,), metadata={"skill_result": result.raw})
        try:
            data = json.loads(str(result.data))
        except json.JSONDecodeError as exc:
            return PCellIntrospectionResult(request, errors=(f"invalid introspection JSON: {exc}",), metadata={"raw": result.data})
        data["request"] = request.to_dict()
        data.setdefault("raw_artifact_path", self.out_file)
        return PCellIntrospectionResult.from_dict(data)


class PCellIntrospector:
    def __init__(self, backend: PCellIntrospectionBackend):
        self.backend = backend

    def run(self, request: PCellIntrospectionRequest) -> PCellIntrospectionResult:
        return self.backend.introspect(request)


def load_pcell_introspection_json(path: str | Path) -> PCellIntrospectionResult:
    return PCellIntrospectionResult.from_json(path)


def save_pcell_introspection_json(result: PCellIntrospectionResult, path: str | Path) -> Path:
    return result.save_json(path)


def run_pcell_introspection_via_skill_server(
    client: VirtuosoSkillClient,
    request: PCellIntrospectionRequest,
    *,
    skill_script: str | Path | None = None,
) -> PCellIntrospectionResult:
    return PCellIntrospector(VirtuosoSkillPCellIntrospectionBackend(client, skill_script=skill_script)).run(request)


def _skill_introspection_expr(request: PCellIntrospectionRequest, skill_script: Path, out_file: str) -> str:
    return (
        f'progn(load("{_skill_quote_path(skill_script)}") '
        "analogskills_pcellIntrospectToFile("
        f'"{_skill_quote(request.logical_name)}" '
        f'"{_skill_quote(request.lib_name)}" '
        f'"{_skill_quote(request.cell_name)}" '
        f'"{_skill_quote(request.view_name)}" '
        f'{_skill_param_list(request.params, cell_name=request.cell_name)} '
        f'"{_skill_quote(request.orient)}" '
        f'"{_skill_quote(request.instance_name)}" '
        f'"{_skill_quote(request.calibration_lib)}" '
        f'"{_skill_quote(request.calibration_cell)}" '
        f"__FILE:{_skill_quote(out_file)}__))"
    )


def _skill_param_list(params: Mapping[str, Any], *, cell_name: str = "") -> str:
    entries = []
    param_types = _pcell_param_types_for_cell(cell_name)
    for key, value in sorted(params.items()):
        param_type = param_types.get(str(key))
        if param_type == "string":
            param_value = f'"{_skill_quote(str(value))}"'
        elif isinstance(value, bool):
            param_type = "boolean"
            param_value = "t" if value else "nil"
        elif isinstance(value, int):
            param_type = "int"
            param_value = str(value)
        elif isinstance(value, float):
            param_type = "float"
            param_value = f"{value:g}"
        else:
            param_type = "string"
            param_value = f'"{_skill_quote(str(value))}"'
        entries.append(f'list("{_skill_quote(str(key))}" "{param_type}" {param_value})')
    return "list(" + " ".join(entries) + ")"


def _pcell_param_types_for_cell(cell_name: str) -> dict[str, str]:
    if cell_name in {"nch_mac", "pch_mac", "nch_svt_mac", "pch_svt_mac"}:
        return {
            "Wfg": "string", "fingers": "string", "l": "string", "simM": "string",
            "DFM_display": "string", "DFM_options": "string",
            "DUpper_PO_EX_INC": "string", "DLower_PO_EX_INC": "string",
            "LdiffExt": "string", "RdiffExt": "string",
            # The T28 MOS CDF expresses both dimensional quantities and
            # enumerated layout-construction switches as strings.  Passing a
            # numeric SKILL type silently falls back to the CDF default for
            # several of these fields, which makes calibration sweeps appear
            # to have no effect.  Keep this list in lockstep with oa.py.
            "PO_EX_INC": "string", "pMetalOption": "string",
            "pMetalEncNS": "string", "pMetalEncEW": "string",
            "dummyPolyLayer": "string", "leftDummyPoly": "string",
            "rightDummyPoly": "string", "secondLeftDummy": "string",
            "secondRightDummy": "string", "secondDummyPolySpacing": "string",
            "dummyPolyWidth": "string", "dummyPolyWidth2": "string",
            "secondDummyPolyWidth": "string", "firstDummyPolySpacing": "string",
            "dummyPolyNumLeft": "string", "dummyPolyNumRight": "string",
            "DPO_CO_EN_INC": "string", "DM1_CO_EN_INC": "string",
            "DM1_CO_EN_INCX": "string", "DCO_CO_SP_INC": "string",
            "DGA_CO_SP_INC": "string", "DGA_GA_SP_INC": "string",
            "LGA_CO_SP_INC": "string", "RGA_CO_SP_INC": "string",
            "CO_EN_1_1_INC": "string", "gateToContactExtension": "string",
            "routePolydir": "string", "polyContactsEnh": "string",
            "polyContactNumTop": "string", "polyContactNumBot": "string",
            "routeUPoly_SP_INC": "string", "routeDPoly_SP_INC": "string",
            "MatchDpoWithGate": "string", "Poly_HardCons": "string",
            "STIdummyGate": "string", "rPD_Ext": "string",
            "rPD_Ext_adj": "string", "rPD_Ext_adj2": "string",
            "dummyPolyInc": "string", "SDISDEnc_inc": "string",
        }
    if cell_name in {"nch_svt_macx", "pch_svt_macx"}:
        return {"fingers": "string", "nfin": "string", "l": "string", "simM": "string"}
    if cell_name == "rnod":
        return {
            "model": "string",
            "macro": "string",
            "ResCalc": "string",
            "connection": "string",
            "w": "string",
            "sumW": "string",
            "l": "string",
            "sumL": "string",
            "res": "string",
            "m": "int",
            "mf": "int",
            "multi": "int",
            "segments": "int",
            "srs": "int",
            "prl": "int",
        }
    if cell_name == "nmoscap":
        return {
            "model": "string",
            "macro": "string",
            "wr": "string",
            "lr": "string",
            "c": "string",
            "cmax": "string",
            "cmin": "string",
            "volt": "string",
            "m": "int",
            "multi": "int",
        }
    if cell_name in {"npn", "pnp"}:
        return {"model": "string", "macro": "string", "Esize": "string", "area": "string", "l": "string", "w": "string", "m": "int", "multi": "int"}
    return {}


def _request_key(request: PCellIntrospectionRequest) -> str:
    params = ",".join(f"{key}={value}" for key, value in request.params_signature)
    return f"{request.pcell_key}|{params}|{request.orient}"


def _sort_candidates(candidates: Sequence[PCellAccessCandidate], preferred_layers: Sequence[str]) -> tuple[PCellAccessCandidate, ...]:
    layer_rank = {str(layer): idx for idx, layer in enumerate(preferred_layers)}
    return tuple(sorted(candidates, key=lambda item: (-item.confidence, layer_rank.get(item.layer, 10_000), item.xy_um)))


def _shapes_at_point(
    shapes: Sequence[PCellConductiveShape],
    point: Point,
    *,
    preferred_layers: Sequence[str] = (),
) -> tuple[PCellConductiveShape, ...]:
    matches = tuple(shape for shape in shapes if _point_in_bbox(point, shape.bbox_um))
    if not matches:
        return ()
    layer_rank = {str(layer): idx for idx, layer in enumerate(preferred_layers) if str(layer)}
    return tuple(sorted(matches, key=lambda shape: (layer_rank.get(shape.layer, 10_000), _bbox_area(shape.bbox_um), shape.layer, shape.bbox_um)))


def _shapes_overlapping_bbox(
    shapes: Sequence[PCellConductiveShape],
    bbox: BBox,
    *,
    layers: Sequence[str] = (),
    preferred_layers: Sequence[str] = (),
) -> tuple[PCellConductiveShape, ...]:
    layer_filter = {str(layer) for layer in layers if str(layer)}
    matches = tuple(
        shape
        for shape in shapes
        if (not layer_filter or shape.layer in layer_filter) and _bbox_overlaps(bbox, shape.bbox_um)
    )
    if not matches:
        return ()
    layer_rank = {str(layer): idx for idx, layer in enumerate(preferred_layers) if str(layer)}
    return tuple(sorted(matches, key=lambda shape: (layer_rank.get(shape.layer, 10_000), _bbox_area(shape.bbox_um), shape.layer, shape.bbox_um)))


def _shape_terminal_mismatch_warnings(
    terminal: str,
    shapes: Sequence[PCellConductiveShape],
) -> tuple[str, ...]:
    terminal_text = str(terminal)
    warnings: list[str] = []
    for shape in shapes:
        shape_terminal = str(shape.terminal or shape.net or "")
        if shape_terminal and shape_terminal != terminal_text:
            warnings.append(f"terminal access {terminal_text} overlaps conductive shape tagged {shape_terminal}")
    return tuple(dict.fromkeys(warnings))


def _label_layer_mismatch_warnings(
    terminal: str,
    label_layer: str,
    shapes: Sequence[PCellConductiveShape],
) -> tuple[str, ...]:
    label_layer_text = str(label_layer)
    if not label_layer_text:
        return ()
    warnings: list[str] = []
    for shape in shapes:
        if shape.layer and shape.layer != label_layer_text:
            warnings.append(f"OA label {terminal} on {label_layer_text} overlaps conductive shape on {shape.layer}")
    return tuple(dict.fromkeys(warnings))


def _ambiguous_shape_overlap_warnings(
    terminal: str,
    shapes: Sequence[PCellConductiveShape],
) -> tuple[str, ...]:
    tags = tuple(dict.fromkeys(str(shape.terminal or shape.net or "") for shape in shapes if str(shape.terminal or shape.net or "")))
    if len(tags) <= 1:
        return ()
    return (f"terminal access {terminal} overlaps multiple conductive shape tags {tags}",)


def _point_in_bbox(point: Point, bbox: BBox) -> bool:
    x, y = point
    x0, y0, x1, y1 = bbox
    lo_x, hi_x = sorted((x0, x1))
    lo_y, hi_y = sorted((y0, y1))
    return lo_x <= x <= hi_x and lo_y <= y <= hi_y


def _bbox_overlaps(left: BBox, right: BBox) -> bool:
    lx0, ly0, lx1, ly1 = left
    rx0, ry0, rx1, ry1 = right
    llo_x, lhi_x = sorted((lx0, lx1))
    llo_y, lhi_y = sorted((ly0, ly1))
    rlo_x, rhi_x = sorted((rx0, rx1))
    rlo_y, rhi_y = sorted((ry0, ry1))
    return max(llo_x, rlo_x) <= min(lhi_x, rhi_x) and max(llo_y, rlo_y) <= min(lhi_y, rhi_y)


def _bbox_area(bbox: BBox) -> float:
    return abs((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))


def _bbox_tuple(value: Any) -> BBox:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"bbox must be a 4-tuple, got {value!r}")
    return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))


def _optional_bbox(value: Any) -> BBox | None:
    if value is None:
        return None
    return _bbox_tuple(value)


def _point_tuple(value: Any) -> Point:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"point must be a 2-tuple, got {value!r}")
    return (float(value[0]), float(value[1]))


def _bbox_center(bbox: BBox) -> Point:
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def _skill_quote(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _skill_quote_path(path: str | Path) -> str:
    return _skill_quote(str(path))
