"""Write a compact text summary of parsed simulation metrics.

Usage examples:
    python summarize_metrics.py --results outputs/my_project/results.json
    python summarize_metrics.py --run-dir workspace/run_000 --testbench workspace/run_000/tb.scs
    python summarize_metrics.py --run-dir workspace/run_000 --testbench workspace/run_000/tb.scs workspace/run_000/tb_1.scs workspace/run_000/tb_2.scs
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from models import SimResult
from psf_results import parse_psf_results
from simulator import Simulator
from config import Settings


def main() -> None:
    args = _parse_args()

    if args.results:
        data = json.loads(args.results.read_text(encoding="utf-8"))
        report = build_report_from_results_json(data, source=args.results)
        out_path = args.out or args.results.with_name("metrics_summary.txt")
    else:
        if not args.run_dir or not args.testbench:
            raise SystemExit("--run-dir and --testbench are required without --results")
        result = parse_run_metrics(args.run_dir, args.testbench)
        report = build_report_from_sim_result(
            result,
            source=args.run_dir,
            testbenches=args.testbench,
        )
        out_path = args.out or (args.run_dir / "metrics_summary.txt")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(f"Wrote {out_path}")


def parse_run_metrics(run_dir: Path, testbench_paths: list[Path]) -> SimResult:
    """Parse metrics from one run directory and one or more testbenches."""
    merged: SimResult | None = None
    for tb_path in testbench_paths:
        testbench = tb_path.read_text(encoding="utf-8", errors="replace")
        parsed = parse_psf_results(run_dir / "raw", testbench)
        if parsed is None:
            parsed = _parse_text_measurements(run_dir)
        merged = parsed if merged is None else SimResult.merge(merged, parsed)

    return merged or SimResult(converged=False, error_message="No metrics parsed")


def build_report_from_results_json(data: dict[str, Any], source: Path) -> str:
    """Format metrics already stored in outputs/<project>/results.json."""
    metrics = data.get("metrics", {})
    lines = _report_header(source=source, converged=data.get("converged"))
    lines.extend(_metric_lines(metrics))

    raw_metrics = data.get("raw_metrics")
    if isinstance(raw_metrics, dict) and raw_metrics:
        lines.extend(["", "Raw Metrics:"])
        lines.extend(f"  {name}: {_format_value(value)}" for name, value in sorted(raw_metrics.items()))

    params = data.get("params")
    if isinstance(params, dict) and params:
        lines.extend(["", "Parameters:"])
        lines.extend(f"  {name}: {_format_value(value)}" for name, value in sorted(params.items()))

    if "target_status" in data:
        lines.extend(["", "Target Status:"])
        for name, status in sorted(data["target_status"].items()):
            lines.append(f"  {name}: {'PASS' if status else 'FAIL'}")

    if "gap" in data:
        lines.extend(["", "Target Gaps:"])
        for name, value in sorted(data["gap"].items()):
            lines.append(f"  {name}: {_format_value(value)}")

    return "\n".join(lines) + "\n"


def build_report_from_sim_result(
    result: SimResult,
    source: Path,
    testbenches: list[Path] | None = None,
) -> str:
    """Format metrics parsed directly from a workspace/run_xxx directory."""
    metrics = result.to_result_dict()["metrics"]
    lines = _report_header(
        source=source,
        converged=result.converged,
        error_message=result.error_message,
    )
    if testbenches:
        lines.extend(["", "Testbenches:"])
        lines.extend(f"  {path}" for path in testbenches)

    lines.extend(_metric_lines(metrics))

    if result.raw_metrics:
        lines.extend(["", "Raw Metrics:"])
        lines.extend(
            f"  {name}: {_format_value(value)}"
            for name, value in sorted(result.raw_metrics.items())
        )

    return "\n".join(lines) + "\n"


def _parse_text_measurements(run_dir: Path) -> SimResult:
    """Fallback parser for sim.log plus any Spectre .measure files."""
    parts: list[str] = []
    sim_log = run_dir / "sim.log"
    if sim_log.exists():
        parts.append(sim_log.read_text(encoding="utf-8", errors="replace"))
    for measure_file in sorted(run_dir.glob("*.measure")):
        parts.append(f"\n--- {measure_file.name} ---\n")
        parts.append(measure_file.read_text(encoding="utf-8", errors="replace"))
    return Simulator(Settings(dry_run=True)).parse_simulation_log("\n".join(parts))


def _report_header(
    source: Path,
    converged: bool | None,
    error_message: str = "",
) -> list[str]:
    lines = [
        "Simulation Metrics Summary",
        "=" * 60,
        f"Source: {source}",
        f"Converged: {_format_bool(converged)}",
    ]
    if error_message:
        lines.append(f"Error: {error_message}")
    return lines


def _metric_lines(metrics: dict[str, Any]) -> list[str]:
    ordered = [
        ("gain_db", "Gain", "dB"),
        ("gbw_hz", "GBW", "Hz"),
        ("bandwidth_hz", "Bandwidth/GBW", "Hz"),
        ("unity_gain_freq_hz", "UGF", "Hz"),
        ("phase_margin_deg", "Phase Margin", "deg"),
        ("power_w", "Power", "W"),
        ("slew_rate_v_per_s", "Slew Rate", "V/s"),
        ("slew_rate_positive_v_per_s", "SR+", "V/s"),
        ("slew_rate_negative_v_per_s", "SR-", "V/s"),
        ("settling_time_s", "Settling Time 0.1%", "s"),
    ]
    lines = ["", "Metrics:"]
    seen: set[str] = set()
    for key, label, unit in ordered:
        seen.add(key)
        value = metrics.get(key)
        lines.append(f"  {label:<20} {_format_value(value, unit)}")

    extra_keys = sorted(k for k in metrics if k not in seen)
    if extra_keys:
        lines.extend(["", "Extra Metrics:"])
        for key in extra_keys:
            lines.append(f"  {key}: {_format_value(metrics[key])}")
    return lines


def _format_value(value: Any, unit: str = "") -> str:
    if value is None:
        return "N/A"
    if isinstance(value, bool):
        return _format_bool(value)
    if isinstance(value, (int, float)):
        formatted = _eng(float(value))
        return f"{formatted}{unit}" if unit else formatted
    return str(value)


def _format_bool(value: bool | None) -> str:
    if value is None:
        return "UNKNOWN"
    return "YES" if value else "NO"


def _eng(value: float) -> str:
    abs_v = abs(value)
    for scale, suffix in (
        (1e9, "G"),
        (1e6, "M"),
        (1e3, "k"),
        (1.0, ""),
        (1e-3, "m"),
        (1e-6, "u"),
        (1e-9, "n"),
        (1e-12, "p"),
        (1e-15, "f"),
    ):
        if abs_v >= scale or scale == 1e-15:
            return f"{value / scale:.6g}{suffix}"
    return f"{value:.6g}"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect gain/GBW/PM/power/SR/ST metrics into one text file."
    )
    parser.add_argument(
        "--results",
        type=Path,
        help="Path to outputs/<project>/results.json.",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        help="Path to a workspace/run_xxx directory containing raw/.",
    )
    parser.add_argument(
        "--testbench",
        type=Path,
        nargs="*",
        help="Rendered testbench file(s), e.g. workspace/run_000/tb.scs.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Output text path. Defaults beside results.json or inside run-dir.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    main()
