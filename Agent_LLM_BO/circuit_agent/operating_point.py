"""DC operating-point saturation checks for BO reward and reports."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path


NEAR_EDGE_MARGIN_V = 0.05


@dataclass
class OperatingPointDevice:
    """One MOS operating-point row from diagnostics CSV."""

    instance: str
    model: str = ""
    vds: float | None = None
    vdsat: float | None = None
    gm: float | None = None
    gds: float | None = None
    gmoverid: float | None = None
    margin_v: float | None = None
    region: str = "unknown"
    critical: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "instance": self.instance,
            "model": self.model,
            "vds": self.vds,
            "vdsat": self.vdsat,
            "gm": self.gm,
            "gds": self.gds,
            "gmoverid": self.gmoverid,
            "saturation_margin_v": self.margin_v,
            "region": self.region,
            "critical": self.critical,
        }


@dataclass
class OperatingPointStatus:
    """Aggregated saturation status for one simulated design."""

    critical_linear: list[str] = field(default_factory=list)
    critical_near_edge: list[str] = field(default_factory=list)
    noncritical_linear: list[str] = field(default_factory=list)
    noncritical_near_edge: list[str] = field(default_factory=list)
    min_margin_v: float | None = None
    penalty: float = 0.0
    devices: list[OperatingPointDevice] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def critical_linear_count(self) -> int:
        return len(self.critical_linear)

    @property
    def critical_near_edge_count(self) -> int:
        return len(self.critical_near_edge)

    @property
    def linear_count(self) -> int:
        return len(self.critical_linear) + len(self.noncritical_linear)

    @property
    def near_edge_count(self) -> int:
        return len(self.critical_near_edge) + len(self.noncritical_near_edge)

    @property
    def passed(self) -> bool:
        return self.critical_linear_count == 0

    def to_dict(self, include_devices: bool = False) -> dict[str, object]:
        data: dict[str, object] = {
            "critical_linear_count": self.critical_linear_count,
            "critical_near_edge_count": self.critical_near_edge_count,
            "linear_count": self.linear_count,
            "near_edge_count": self.near_edge_count,
            "min_margin_v": self.min_margin_v,
            "passed": self.passed,
            "penalty": self.penalty,
            "critical_linear": self.critical_linear,
            "critical_near_edge": self.critical_near_edge,
            "noncritical_linear": self.noncritical_linear,
            "noncritical_near_edge": self.noncritical_near_edge,
            "warnings": self.warnings,
        }
        if include_devices:
            data["devices"] = [device.to_dict() for device in self.devices]
        return data

    def summary_lines(self) -> list[str]:
        if not self.devices and self.warnings:
            return ["Critical OP status:", *[f"  warning: {w}" for w in self.warnings]]
        lines = [
            "Critical OP status:",
            f"  critical linear: {_join_or_none(self.critical_linear)}",
            f"  critical near_edge: {_join_or_none(self.critical_near_edge)}",
            f"  noncritical linear: {_join_or_none(self.noncritical_linear)}",
            f"  noncritical near_edge: {_join_or_none(self.noncritical_near_edge)}",
            f"  min_margin: {_fmt_margin_mv(self.min_margin_v)}",
            f"  op_penalty: {self.penalty:.2f}",
        ]
        for warning in self.warnings:
            lines.append(f"  warning: {warning}")
        return lines


def evaluate_dc_operating_points(
    csv_path: Path,
    critical_instances: set[str] | None = None,
    near_edge_margin_v: float = NEAR_EDGE_MARGIN_V,
) -> OperatingPointStatus:
    """Evaluate MOS saturation margins from diagnostics/dc_operating_points.csv."""

    critical = set(critical_instances or set())
    status = OperatingPointStatus()
    path = Path(csv_path)
    if not path.exists():
        status.warnings.append(f"OP diagnostics not found: {path}")
        return status

    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            instance = (row.get("instance") or "").strip()
            if not instance:
                continue
            vds = _safe_float(row.get("vds"))
            vdsat = _safe_float(row.get("vdsat"))
            margin = None if vds is None or vdsat is None else abs(vds) - abs(vdsat)
            region = _region_from_margin(margin, near_edge_margin_v)
            device = OperatingPointDevice(
                instance=instance,
                model=(row.get("model") or "").strip(),
                vds=vds,
                vdsat=vdsat,
                gm=_safe_float(row.get("gm")),
                gds=_safe_float(row.get("gds")),
                gmoverid=_safe_float(row.get("gmoverid")),
                margin_v=margin,
                region=region,
                critical=instance in critical,
            )
            status.devices.append(device)
            if margin is not None:
                if status.min_margin_v is None or margin < status.min_margin_v:
                    status.min_margin_v = margin
            if device.critical and region == "linear":
                status.critical_linear.append(instance)
            elif device.critical and region == "near_edge":
                status.critical_near_edge.append(instance)
            elif not device.critical and region == "linear":
                status.noncritical_linear.append(instance)
            elif not device.critical and region == "near_edge":
                status.noncritical_near_edge.append(instance)

    status.penalty = compute_op_penalty(status)
    return status


def compute_op_penalty(status: OperatingPointStatus) -> float:
    """Return negative reward contribution from OP saturation issues."""

    return -(
        120.0 * status.critical_linear_count
        + 35.0 * status.critical_near_edge_count
    )


def _region_from_margin(
    margin_v: float | None,
    near_edge_margin_v: float = NEAR_EDGE_MARGIN_V,
) -> str:
    if margin_v is None:
        return "unknown"
    if margin_v < 0:
        return "linear"
    if margin_v < near_edge_margin_v:
        return "near_edge"
    return "saturated"


def _safe_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _join_or_none(items: list[str]) -> str:
    return ", ".join(items) if items else "none"


def _fmt_margin_mv(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value * 1e3:.2f} mV"
