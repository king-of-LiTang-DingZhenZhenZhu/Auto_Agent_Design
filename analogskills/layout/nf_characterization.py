"""PCell characterization inputs for fixed-size StrongARM nf optimization."""
from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import json
from typing import Any, Mapping

from analogskills.pcell.calibration_run import PCellCalibrationManifest, PCellCalibrationTarget
from analogskills.pcell.generation import enumerate_mos_finger_choices
from analogskills.pdk import PdkConfig
from .nf_smt import FingerRealization, FixedSizeDevice, MatchedPair, FingerSmtSolution, solve_fixed_size_finger_placement
from .smt_design_rules import strongarm_device_placement_rule_sites


def build_strongarm_nf_manifest(
    sizing: Mapping[str, Mapping[str, float]],
    *,
    calibration_lib: str = "skillsZSmtNfCalib",
    max_fingers: int = 16,
) -> PCellCalibrationManifest:
    """Build unique CRN28 PCell probes while preserving every total W/L."""
    device_kind = {
        "MIN_P": "nmos", "MIN_N": "nmos", "MCLK": "nmos",
        "MLATN_P": "nmos", "MLATN_N": "nmos",
        "MLATP_P": "pmos", "MLATP_N": "pmos",
        "MRST_P": "pmos", "MRST_N": "pmos",
    }
    targets: list[PCellCalibrationTarget] = []
    seen: set[tuple[str, float, float, int]] = set()
    for name, values in sizing.items():
        kind = device_kind[name]
        width = float(values["W"])
        length = float(values["L"])
        choices = enumerate_mos_finger_choices(
            width_m=width,
            length_m=length,
            min_finger_width_m=0.4e-6,
            max_finger_width_m=4.0e-6,
            max_fingers=max_fingers,
            max_multiplier=1,
        )
        for choice in choices:
            width_nm = int(round(width * 1e9))
            if width_nm % choice.nf:
                continue
            key = (kind, width, length, choice.nf)
            if key in seen:
                continue
            seen.add(key)
            cell = "nch_mac" if kind == "nmos" else "pch_mac"
            targets.append(
                PCellCalibrationTarget(
                    logical_name=kind,
                    lib_name="tsmcN28",
                    cell_name=cell,
                    params={"Wfg": (width_nm // choice.nf) * 1e-9, "fingers": choice.nf, "l": length, "simM": 1},
                    orient="R0",
                    calibration_lib=calibration_lib,
                    calibration_cell=f"nf_{kind}_{round(width * 1e9)}_{choice.nf}",
                )
            )
    return PCellCalibrationManifest("crn28hpcp", tuple(targets), {"fixed_total_width": True, "max_fingers": max_fingers})


def build_mos_template_repair_manifest() -> PCellCalibrationManifest:
    """Probe compact MOS candidates with PO and PMET enclosure knobs."""
    targets = []
    for po_nm in (0, 30, 60, 90):
        targets.append(
            PCellCalibrationTarget(
                logical_name="nmos", lib_name="tsmcN28", cell_name="nch_mac",
                params={"Wfg": 1e-6, "fingers": 4, "l": 0.18e-6, "simM": 1, "PO_EX_INC": f"{po_nm}n"},
                calibration_lib="skillsZSmtNfRepairCalib", calibration_cell=f"repair_nmos_po{po_nm}",
            )
        )
    for po_nm in (0, 30, 60, 90):
        for pmetal_nm in (50, 80, 110):
            targets.append(
                PCellCalibrationTarget(
                    logical_name="pmos", lib_name="tsmcN28", cell_name="pch_mac",
                    params={
                        "Wfg": 1.2e-6, "fingers": 5, "l": 0.18e-6, "simM": 1,
                        "PO_EX_INC": f"{po_nm}n",
                        "pMetalEncNS": f"{pmetal_nm}n",
                        "pMetalEncEW": f"{pmetal_nm}n",
                    },
                    calibration_lib="skillsZSmtNfRepairCalib",
                    calibration_cell=f"repair_pmos_po{po_nm}_pm{pmetal_nm}",
                )
            )
    targets.append(
        PCellCalibrationTarget(
            logical_name="pmos", lib_name="tsmcN28", cell_name="pch_mac",
            params={
                "Wfg": 0.6e-6, "fingers": 5, "l": 0.18e-6, "simM": 1,
                "PO_EX_INC": "0n", "pMetalEncNS": "50n", "pMetalEncEW": "50n",
            },
            calibration_lib="skillsZSmtNfRepairCalib", calibration_cell="repair_pmos3_nf5_pm50",
        )
    )
    return PCellCalibrationManifest("crn28hpcp", tuple(targets), {"experiment": "po_pmetal_template_repair"})


def build_strongarm_mos_construction_sweep_manifest(
    pdk: PdkConfig,
    *,
    calibration_lib: str = "skillsZSmtMosDrcCalib",
) -> PCellCalibrationManifest:
    """Build a native-MOS DRC sweep entirely from PDK configuration.

    The configuration owns both the signoff-rule-to-CDF traceability and the
    candidate values.  This deliberately keeps foundry numbers out of the
    placement solver: the solver consumes only measured Calibre costs after
    this manifest has been characterized.
    """

    metadata = dict(getattr(pdk, "metadata", {}) or {})
    sweep_root = dict(metadata.get("pcell_drc_sweep", {}) or {})
    sweep = dict(sweep_root.get("strongarm_mos", {}) or {})
    targets_data = tuple(sweep.get("targets", ()) or ())
    variants = tuple(sweep.get("variants", ()) or ())
    if not targets_data or not variants:
        raise ValueError(f"PDK {pdk.name} has no strongarm_mos PCell DRC sweep configuration")

    pmos_params = dict(sweep.get("pmos_params", {}) or {})
    targets: list[PCellCalibrationTarget] = []
    for target_data in targets_data:
        row = dict(target_data or {})
        logical_name = str(row.get("logical_name", ""))
        if logical_name not in {"nmos", "pmos"}:
            raise ValueError(f"unsupported PCell DRC sweep device kind: {logical_name!r}")
        total_width_nm = int(row.get("total_width_nm", 0))
        fingers = int(row.get("fingers", 0))
        length_nm = int(row.get("length_nm", 0))
        if total_width_nm <= 0 or fingers <= 0 or length_nm <= 0 or total_width_nm % fingers:
            raise ValueError(f"invalid PCell DRC sweep target: {row!r}")
        cell_name = "nch_mac" if logical_name == "nmos" else "pch_mac"
        base_params: dict[str, Any] = {
            "Wfg": (total_width_nm // fingers) * 1e-9,
            "fingers": fingers,
            "l": length_nm * 1e-9,
            "simM": 1,
        }
        if logical_name == "pmos":
            base_params.update(pmos_params)
        for variant_data in variants:
            variant = dict(variant_data or {})
            variant_name = _sweep_slug(str(variant.get("name", "candidate")))
            if not variant_name:
                raise ValueError(f"PCell DRC sweep variant has no name: {variant_data!r}")
            params = {**base_params, **dict(variant.get("params", {}) or {})}
            targets.append(
                PCellCalibrationTarget(
                    logical_name=logical_name,
                    lib_name="tsmcN28",
                    cell_name=cell_name,
                    params=params,
                    calibration_lib=calibration_lib,
                    calibration_cell=f"mos_{logical_name}_{total_width_nm}_{fingers}_{variant_name}",
                )
            )
    return PCellCalibrationManifest(
        pdk.name,
        tuple(targets),
        {
            "experiment": "strongarm_mos_construction_sweep",
            "configuration_path": "metadata.pcell_drc_sweep.strongarm_mos",
            "rule_parameter_sources": dict(sweep.get("rule_parameter_sources", {}) or {}),
        },
    )


def introspection_to_finger_realization(result: object, *, site_nm: int = 10) -> FingerRealization:
    request = getattr(result, "request")
    bbox = getattr(result, "instance_bbox_um", None) or getattr(result, "master_bbox_um", None)
    if bbox is None:
        raise ValueError("PCell introspection has no bbox")
    params = dict(request.params)
    nf = int(params["fingers"])
    wf_nm = int(round(float(params["Wfg"]) * 1e9))
    width_nm = int(round((float(bbox[2]) - float(bbox[0])) * 1000.0))
    height_nm = int(round((float(bbox[3]) - float(bbox[1])) * 1000.0))
    terminals = tuple(getattr(result, "terms", ()) or ())
    access_cost = sum(1 for term in terminals if not tuple(getattr(term, "pins", ()) or ()))
    return FingerRealization(
        nf=nf,
        m=1,
        finger_width_nm=wf_nm,
        width_sites=max(1, (width_nm + site_nm - 1) // site_nm),
        height_sites=max(1, (height_nm + site_nm - 1) // site_nm),
        access_cost=access_cost,
        drc_clean=bool(getattr(result, "ok", False)),
        bbox_x0_sites=int(round(float(bbox[0]) * 1000.0 / site_nm)),
        bbox_y0_sites=int(round(float(bbox[1]) * 1000.0 / site_nm)),
        electrical_total_width_nm=int(round(float(params["Wfg"]) * nf * 1e9)),
        pcell_params={
            key: value for key, value in params.items()
            if key not in {"Wfg", "fingers", "l", "simM"}
        },
    )


def load_strongarm_nf_catalog(
    artifacts_dir: str | Path,
    *,
    site_nm: int = 10,
    pdk: PdkConfig | None = None,
) -> dict[tuple[str, int, int], tuple[FingerRealization, ...]]:
    """Load OA introspection artifacts keyed by (device kind, Wtotal_nm, L_nm)."""
    from analogskills.eda.pcell_introspection import load_pcell_introspection_json

    artifacts_path = Path(artifacts_dir)
    required_pcell_params = _required_strongarm_pcell_params(pdk)
    drc_costs = _load_nf_drc_costs(artifacts_path.parent.parent / "nf_candidate_drc" / "summary.json")
    catalog: dict[tuple[str, int, int], list[FingerRealization]] = {}
    for path in sorted(artifacts_path.glob("*.json")):
        result = load_pcell_introspection_json(path)
        realization = introspection_to_finger_realization(result, site_nm=site_nm)
        if not _realization_matches_required_pcell_params(realization, required_pcell_params):
            continue
        params = dict(result.request.params)
        total_width_nm = realization.electrical_total_width_nm
        length_nm = int(round(float(params["l"]) * 1e9))
        realization = replace(
            realization,
            intrinsic_drc_cost=int(drc_costs.get((result.request.logical_name, total_width_nm, realization.nf), 0)),
        )
        catalog.setdefault((result.request.logical_name, total_width_nm, length_nm), []).append(realization)
    repair_sweeps = (
        (
            artifacts_path.parent.parent / "nf_template_repair" / "artifacts",
            (
                artifacts_path.parent.parent / "nf_template_repair_drc" / "summary.json",
                artifacts_path.parent.parent / "nf_template_repair_drc_reset" / "summary.json",
            ),
        ),
        (
            artifacts_path.parent.parent / "nf_mos_construction" / "artifacts",
            (artifacts_path.parent.parent / "nf_mos_construction_drc" / "summary.json",),
            _manifest_rule_names(artifacts_path.parent.parent / "nf_mos_construction" / "manifest.json"),
        ),
        (
            artifacts_path.parent.parent / "nf_mos_construction_mfmarker" / "artifacts",
            (artifacts_path.parent.parent / "nf_mos_construction_mfmarker_drc" / "summary.json",),
            _manifest_rule_names(artifacts_path.parent.parent / "nf_mos_construction_mfmarker" / "manifest.json"),
        ),
    )
    for repair_entry in repair_sweeps:
        if len(repair_entry) == 2:
            repair_artifacts, repair_summaries = repair_entry
            local_rule_names: frozenset[str] = frozenset()
        else:
            repair_artifacts, repair_summaries, local_rule_names = repair_entry
        if not repair_artifacts.exists() or not any(path.exists() for path in repair_summaries):
            continue
        repair_cost_by_cell = {}
        for repair_summary in repair_summaries:
            if not repair_summary.exists():
                continue
            repair_cost_by_cell.update({
                str(row["cell"]): _candidate_drc_cost(row, local_rule_names=local_rule_names)
                for row in json.loads(repair_summary.read_text(encoding="utf-8")).get("candidates", ())
                if bool(row.get("ok", False))
            })
        from analogskills.eda.pcell_introspection import load_pcell_introspection_json
        for path in sorted(repair_artifacts.glob("*.json")):
            try:
                result = load_pcell_introspection_json(path)
            except (ValueError, json.JSONDecodeError):
                continue
            if result.request.calibration_cell not in repair_cost_by_cell:
                continue
            realization = introspection_to_finger_realization(result, site_nm=site_nm)
            if not _realization_matches_required_pcell_params(realization, required_pcell_params):
                continue
            params = dict(result.request.params)
            realization = replace(realization, intrinsic_drc_cost=repair_cost_by_cell[result.request.calibration_cell])
            key = (result.request.logical_name, realization.electrical_total_width_nm, int(round(float(params["l"]) * 1e9)))
            catalog.setdefault(key, []).append(realization)
    return {key: tuple(sorted(rows, key=lambda row: (row.nf, row.intrinsic_drc_cost, repr(sorted(row.pcell_params.items()))))) for key, rows in catalog.items()}


def _required_strongarm_pcell_params(pdk: PdkConfig | None) -> dict[str, Any]:
    if pdk is None:
        return {}
    required: dict[str, Any] = {}
    for logical_name in ("nmos", "pmos"):
        template = getattr(pdk, "pcell_templates", {}).get(logical_name)
        if template is None:
            continue
        defaults = dict(getattr(template, "default_params", {}) or {})
        for key in ("nfLayerOption",):
            value = defaults.get(key)
            if value not in (None, ""):
                required[key] = value
    return required


def _realization_matches_required_pcell_params(realization: FingerRealization, required: Mapping[str, Any]) -> bool:
    params = dict(realization.pcell_params)
    return all(str(params.get(key, "")) == str(value) for key, value in required.items())


def _load_nf_drc_costs(path: str | Path) -> dict[tuple[str, int, int], int]:
    source = Path(path)
    if not source.exists():
        return {}
    payload = json.loads(source.read_text(encoding="utf-8"))
    return {
        (str(row["logical_name"]), int(row["total_width_nm"]), int(row["nf"])): int(row["actionable_results"])
        for row in payload.get("candidates", ())
        if bool(row.get("ok", False))
    }


def _manifest_rule_names(path: str | Path) -> frozenset[str]:
    """Read the signoff-rule subset owned by a configured PCell sweep."""

    source = Path(path)
    if not source.exists():
        return frozenset()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset()
    metadata = dict(payload.get("metadata", {}) or {})
    return frozenset(str(rule) for rule in dict(metadata.get("rule_parameter_sources", {}) or {}))


def _candidate_drc_cost(row: Mapping[str, object], *, local_rule_names: frozenset[str] = frozenset()) -> int:
    """Score a PCell candidate by the rule scope used to construct it.

    Isolated PCells naturally trigger whole-chip density and option markers.
    Those remain in the Calibre report but must not outrank a candidate based
    on a different deck configuration.  When the sweep declares its owned
    signoff rules, only that declared subset becomes the SMT intrinsic cost.
    """

    if not local_rule_names:
        return int(row.get("actionable_results", 0) or 0)
    counts = dict(row.get("actionable_rule_counts", {}) or {})
    return sum(int(counts.get(rule, 0) or 0) for rule in local_rule_names)


def build_strongarm_calibration_cache(artifacts_dir: str | Path, *, pdk: PdkConfig | None = None):
    """Build an exact OA access cache for base and repaired MOS realizations."""
    from analogskills.eda.pcell_introspection import load_pcell_introspection_json
    from analogskills.pcell.calibration import PCellCalibrationCache

    base = Path(artifacts_dir)
    paths = [
        *sorted(base.glob("*.json")),
        *sorted((base.parent.parent / "nf_template_repair" / "artifacts").glob("*.json")),
        *sorted((base.parent.parent / "nf_mos_construction" / "artifacts").glob("*.json")),
        *sorted((base.parent.parent / "nf_mos_construction_mfmarker" / "artifacts").glob("*.json")),
    ]
    results = []
    required_pcell_params = _required_strongarm_pcell_params(pdk)
    for path in paths:
        try:
            result = load_pcell_introspection_json(path)
        except (ValueError, json.JSONDecodeError):
            continue
        realization = introspection_to_finger_realization(result)
        if result.ok and _realization_matches_required_pcell_params(realization, required_pcell_params):
            results.append(result)
    return PCellCalibrationCache.from_results("crn28hpcp", results, preferred_layers=("M1", "PO", "OD"))


def _sweep_slug(value: str) -> str:
    return "".join(char if char.isalnum() or char == "_" else "_" for char in value.strip()).strip("_")


def _strongarm_smt_spacing_sites(
    pdk: PdkConfig | None,
    *,
    site_nm: int,
) -> dict[str, int]:
    """Load characterized-StrongARM placement distances from PDK metadata.

    The generic rule table only captures unconditional layer spacing.  The
    PP.S.9/NP.S.9 implant rule is conditional on a sufficiently long parallel
    run, so its proven 250 nm requirement is kept with the StrongARM placement
    template and converted conservatively to its discrete SMT site grid here.
    """

    if site_nm <= 0:
        raise ValueError("site_nm must be positive")
    result = strongarm_device_placement_rule_sites(pdk, site_nm=site_nm)
    if result["max_matched_pair_gap_sites"] < result["spacing_sites"]:
        raise ValueError("max_matched_pair_gap_nm cannot be smaller than intra_row_spacing_nm")
    if result["max_row_spacing_sites"] < result["row_spacing_sites"]:
        raise ValueError("max_row_spacing_nm cannot be smaller than row_spacing_nm")
    return result


def solve_strongarm_characterized_nf(
    sizing: Mapping[str, Mapping[str, float]],
    artifacts_dir: str | Path,
    *,
    site_nm: int = 10,
    pdk: PdkConfig | None = None,
) -> FingerSmtSolution:
    kind = {
        "MCLK": "nmos", "MIN_P": "nmos", "MIN_N": "nmos",
        "MLATN_P": "nmos", "MLATN_N": "nmos",
        "MLATP_P": "pmos", "MLATP_N": "pmos", "MRST_P": "pmos", "MRST_N": "pmos",
    }
    row = {"MCLK": 0, "MIN_P": 1, "MIN_N": 1, "MLATN_P": 2, "MLATN_N": 2, "MLATP_P": 3, "MLATP_N": 3, "MRST_P": 4, "MRST_N": 4}
    catalog = load_strongarm_nf_catalog(artifacts_dir, site_nm=site_nm, pdk=pdk)
    devices = []
    for name, values in sizing.items():
        width_nm = int(round(float(values["W"]) * 1e9))
        length_nm = int(round(float(values["L"]) * 1e9))
        devices.append(FixedSizeDevice(name, width_nm, length_nm, catalog[(kind[name], width_nm, length_nm)], row[name]))
    spacing = _strongarm_smt_spacing_sites(pdk, site_nm=site_nm)
    return solve_fixed_size_finger_placement(
        tuple(devices),
        matched_pairs=(MatchedPair("MIN_P", "MIN_N"), MatchedPair("MLATN_P", "MLATN_N"), MatchedPair("MLATP_P", "MLATP_N"), MatchedPair("MRST_P", "MRST_N")),
        **spacing,
    )
