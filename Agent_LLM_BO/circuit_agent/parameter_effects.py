"""Infer empirical parameter-to-metric trends from BO history."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Any

METRICS = (
    ("gain_db", "Gain", "higher"),
    ("bandwidth_hz", "GBW", "higher"),
    ("phase_margin_deg", "Phase Margin", "higher"),
    ("power_w", "Power", "lower"),
    ("slew_rate_v_per_s", "Slew Rate", "higher"),
    ("settling_time_s", "Settling Time", "lower"),
    ("op_linear_count", "Critical Linear Count", "lower"),
    ("op_near_edge_count", "Critical Near-edge Count", "lower"),
    ("op_min_margin_v", "Minimum OP Margin", "higher"),
)


def analyze_optimization_history(
    history_path: str | Path,
    output_dir: str | Path | None = None,
    min_samples: int = 15,
) -> dict[str, Any]:
    history_file = Path(history_path)
    history = json.loads(history_file.read_text(encoding="utf-8"))
    analysis = analyze_history(history, min_samples=min_samples)
    analysis["source_history"] = str(history_file)
    destination = Path(output_dir) if output_dir else history_file.parent / "parameter_analysis"
    write_analysis(analysis, destination)
    return analysis


def analyze_history(
    history: dict[str, Any],
    min_samples: int = 15,
) -> dict[str, Any]:
    records = history.get("history", [])
    bounds = {
        item["name"]: item
        for item in history.get("search_space", [])
        if isinstance(item, dict) and item.get("name")
    }
    effects: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    recommendations: list[dict[str, Any]] = []

    for domain, field in (("search", "params"), ("physical", "physical_params")):
        parameter_names = sorted({
            name
            for record in records
            for name in _parameter_dict(record, field)
        })
        for parameter in parameter_names:
            for metric, label, preference in METRICS:
                pairs = []
                for record in records:
                    if not _is_converged(record):
                        continue
                    parameter_value = _number(_parameter_dict(record, field).get(parameter))
                    metric_value = _metric_value(record, metric)
                    if parameter_value is not None and metric_value is not None:
                        pairs.append((parameter_value, metric_value))
                effects.append(
                    _analyze_pair(
                        domain=domain,
                        parameter=parameter,
                        metric=metric,
                        metric_label=label,
                        preference=preference,
                        pairs=pairs,
                        min_samples=min_samples,
                    )
                )

            summary, parameter_recommendations = _summarize_parameter(
                records=records,
                domain=domain,
                field=field,
                parameter=parameter,
                bound=bounds.get(parameter) if domain == "search" else None,
                min_samples=min_samples,
            )
            summaries.append(summary)
            recommendations.extend(parameter_recommendations)

    return {
        "schema_version": 1,
        "method": "Spearman rank correlation on converged BO iterations",
        "min_samples": min_samples,
        "total_iterations": len(records),
        "converged_iterations": sum(_is_converged(record) for record in records),
        "effects": effects,
        "parameter_summaries": summaries,
        "recommendations": recommendations,
        "limitations": [
            "Correlations describe BO history and do not prove causality.",
            "Simultaneously changing parameters can hide interactions and confounding.",
            "Recommendations are review hints and do not modify the search space automatically.",
        ],
    }


def write_analysis(analysis: dict[str, Any], output_dir: str | Path) -> None:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "parameter_effects.json").write_text(
        json.dumps(analysis, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    _write_effects_csv(destination / "parameter_effects.csv", analysis["effects"])
    (destination / "parameter_effects.md").write_text(
        render_markdown(analysis), encoding="utf-8"
    )


def render_markdown(analysis: dict[str, Any]) -> str:
    lines = [
        "# BO Parameter Effect Analysis",
        "",
        f"- Iterations: `{analysis.get('total_iterations', 0)}`",
        f"- Converged iterations: `{analysis.get('converged_iterations', 0)}`",
        f"- Minimum samples per trend: `{analysis.get('min_samples', 15)}`",
        "- Interpretation: empirical association, not causal proof.",
        "",
        "## Strongest Trends",
        "",
        "| Domain | Parameter | Metric | N | Rho | Trend | Helpful Direction | Strength | Confidence |",
        "|---|---|---|---:|---:|---|---|---|---|",
    ]
    usable = [effect for effect in analysis.get("effects", []) if effect["status"] == "ok"]
    usable.sort(key=lambda effect: abs(effect.get("spearman_rho") or 0.0), reverse=True)
    for effect in usable[:60]:
        lines.append(
            f"| {effect['domain']} | `{effect['parameter']}` | {effect['metric_label']} "
            f"| {effect['sample_count']} | {effect['spearman_rho']:.3f} "
            f"| {effect['direction']} | {effect['helpful_direction']} "
            f"| {effect['strength']} | {effect['confidence']} |"
        )
    if not usable:
        lines.append("| - | - | Insufficient usable data | 0 | - | - | - | - | - |")

    lines.extend(["", "## Search-space Review Hints", ""])
    recommendations = analysis.get("recommendations", [])
    if recommendations:
        for item in recommendations:
            lines.append(
                f"- `{item['parameter']}`: **{item['action']}** — {item['reason']}"
            )
    else:
        lines.append("- No boundary or convergence warning has enough evidence yet.")

    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in analysis.get("limitations", []))
    return "\n".join(lines) + "\n"


def _analyze_pair(
    domain: str,
    parameter: str,
    metric: str,
    metric_label: str,
    preference: str,
    pairs: list[tuple[float, float]],
    min_samples: int,
) -> dict[str, Any]:
    base = {
        "domain": domain,
        "parameter": parameter,
        "metric": metric,
        "metric_label": metric_label,
        "metric_preference": preference,
        "sample_count": len(pairs),
        "spearman_rho": None,
        "p_value": None,
        "direction": "unknown",
        "helpful_direction": "unknown",
        "strength": "unknown",
        "confidence": "insufficient",
    }
    if len(pairs) < min_samples:
        return {**base, "status": "insufficient_data"}
    parameter_values, metric_values = zip(*pairs)
    if len(set(parameter_values)) < 2 or len(set(metric_values)) < 2:
        return {**base, "status": "constant_data"}

    rho = _spearman_correlation(parameter_values, metric_values)
    if rho is None:
        return {**base, "status": "constant_data"}
    direction = "flat"
    if rho >= 0.2:
        direction = "positive"
    elif rho <= -0.2:
        direction = "negative"
    helpful_direction = _helpful_direction(direction, preference)
    return {
        **base,
        "status": "ok",
        "spearman_rho": rho,
        "p_value": None,
        "direction": direction,
        "helpful_direction": helpful_direction,
        "strength": _effect_strength(abs(rho)),
        "confidence": _confidence(len(pairs), abs(rho)),
    }


def _summarize_parameter(
    records: list[dict[str, Any]],
    domain: str,
    field: str,
    parameter: str,
    bound: dict[str, Any] | None,
    min_samples: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    samples = []
    for record in records:
        value = _number(_parameter_dict(record, field).get(parameter))
        if value is not None:
            samples.append((value, _is_converged(record), _number(record.get("reward"))))
    values = [sample[0] for sample in samples]
    summary = {
        "domain": domain,
        "parameter": parameter,
        "sample_count": len(samples),
        "sample_min": min(values) if values else None,
        "sample_max": max(values) if values else None,
        "convergence_rate": (
            sum(sample[1] for sample in samples) / len(samples) if samples else None
        ),
    }
    recommendations: list[dict[str, Any]] = []
    if domain != "search" or len(samples) < min_samples:
        return summary, recommendations

    ordered = sorted(samples, key=lambda sample: sample[0])
    quartile_size = max(3, math.ceil(len(ordered) / 4))
    low_rate = sum(sample[1] for sample in ordered[:quartile_size]) / quartile_size
    high_rate = sum(sample[1] for sample in ordered[-quartile_size:]) / quartile_size
    summary["low_quartile_convergence_rate"] = low_rate
    summary["high_quartile_convergence_rate"] = high_rate
    if high_rate + 0.25 <= low_rate:
        recommendations.append({
            "parameter": parameter,
            "action": "inspect_or_reduce_upper_range",
            "reason": (
                f"upper-quartile convergence is {high_rate:.0%}, versus "
                f"{low_rate:.0%} in the lower quartile"
            ),
        })
    elif low_rate + 0.25 <= high_rate:
        recommendations.append({
            "parameter": parameter,
            "action": "inspect_or_raise_lower_range",
            "reason": (
                f"lower-quartile convergence is {low_rate:.0%}, versus "
                f"{high_rate:.0%} in the upper quartile"
            ),
        })

    if bound:
        best = [sample for sample in samples if sample[1] and sample[2] is not None]
        best.sort(key=lambda sample: sample[2], reverse=True)
        best = best[:max(5, math.ceil(len(best) / 4))]
        positions = [
            _normalized_position(sample[0], bound)
            for sample in best
        ]
        positions = [position for position in positions if position is not None]
        if positions:
            median_position = sorted(positions)[len(positions) // 2]
            summary["best_region_median_position"] = median_position
            if median_position >= 0.8:
                recommendations.append({
                    "parameter": parameter,
                    "action": "inspect_upper_bound",
                    "reason": f"top-reward samples cluster near the upper bound ({median_position:.0%})",
                })
            elif median_position <= 0.2:
                recommendations.append({
                    "parameter": parameter,
                    "action": "inspect_lower_bound",
                    "reason": f"top-reward samples cluster near the lower bound ({median_position:.0%})",
                })
    return summary, recommendations


def _metric_value(record: dict[str, Any], metric: str) -> float | None:
    result = record.get("result") or {}
    if metric == "bandwidth_hz":
        return _number(result.get("bandwidth_hz", result.get("gbw_hz")))
    operating_point = result.get("operating_point_status") or {}
    if metric == "op_linear_count":
        return _number(operating_point.get("linear_count"))
    if metric == "op_near_edge_count":
        return _number(operating_point.get("near_edge_count"))
    if metric == "op_min_margin_v":
        return _number(operating_point.get("min_margin_v"))
    return _number(result.get(metric))


def _parameter_dict(record: dict[str, Any], field: str) -> dict[str, Any]:
    value = record.get(field)
    return value if isinstance(value, dict) else {}


def _is_converged(record: dict[str, Any]) -> bool:
    return (record.get("result") or {}).get("converged", True) is not False


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _effect_strength(value: float) -> str:
    if value < 0.2:
        return "very_weak"
    if value < 0.4:
        return "weak"
    if value < 0.6:
        return "moderate"
    if value < 0.8:
        return "strong"
    return "very_strong"


def _confidence(sample_count: int, effect_size: float) -> str:
    if sample_count >= 60 and effect_size >= 0.4:
        return "high"
    if sample_count >= 30 and effect_size >= 0.3:
        return "medium"
    return "low"


def _spearman_correlation(
    left: tuple[float, ...], right: tuple[float, ...]
) -> float | None:
    left_ranks = _average_ranks(left)
    right_ranks = _average_ranks(right)
    left_mean = sum(left_ranks) / len(left_ranks)
    right_mean = sum(right_ranks) / len(right_ranks)
    covariance = sum(
        (left_rank - left_mean) * (right_rank - right_mean)
        for left_rank, right_rank in zip(left_ranks, right_ranks)
    )
    left_variance = sum((rank - left_mean) ** 2 for rank in left_ranks)
    right_variance = sum((rank - right_mean) ** 2 for rank in right_ranks)
    denominator = math.sqrt(left_variance * right_variance)
    return covariance / denominator if denominator else None


def _average_ranks(values: tuple[float, ...]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and ordered[end][1] == ordered[start][1]:
            end += 1
        average_rank = (start + 1 + end) / 2
        for position in range(start, end):
            ranks[ordered[position][0]] = average_rank
        start = end
    return ranks


def _helpful_direction(direction: str, preference: str) -> str:
    if direction == "flat":
        return "unclear"
    if direction == "positive":
        return "increase" if preference == "higher" else "decrease"
    if direction == "negative":
        return "decrease" if preference == "higher" else "increase"
    return "unknown"


def _normalized_position(value: float, bound: dict[str, Any]) -> float | None:
    low = _number(bound.get("low"))
    high = _number(bound.get("high"))
    if low is None or high is None or high <= low:
        return None
    if bound.get("log_scale") and low > 0 and value > 0:
        return (math.log(value) - math.log(low)) / (math.log(high) - math.log(low))
    return (value - low) / (high - low)


def _write_effects_csv(path: Path, effects: list[dict[str, Any]]) -> None:
    fieldnames = [
        "domain", "parameter", "metric", "metric_label", "metric_preference",
        "sample_count", "status", "spearman_rho", "p_value", "direction",
        "helpful_direction", "strength", "confidence",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for effect in effects:
            writer.writerow({name: effect.get(name) for name in fieldnames})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze empirical parameter effects from BO history."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--project", help="outputs/<project> directory")
    source.add_argument("--history", help="optimization_log.json or history.json")
    parser.add_argument("--output", help="Output directory override")
    parser.add_argument("--min-samples", type=int, default=15)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.project:
        project = Path(args.project)
        history_path = project / "optimization_log.json"
        output = Path(args.output) if args.output else project / "parameter_analysis"
    else:
        history_path = Path(args.history)
        output = Path(args.output) if args.output else history_path.parent / "parameter_analysis"
    analysis = analyze_optimization_history(history_path, output, args.min_samples)
    print(f"Parameter analysis: {output / 'parameter_effects.md'}")
    print(f"Usable effects: {sum(item['status'] == 'ok' for item in analysis['effects'])}")


if __name__ == "__main__":
    main()
