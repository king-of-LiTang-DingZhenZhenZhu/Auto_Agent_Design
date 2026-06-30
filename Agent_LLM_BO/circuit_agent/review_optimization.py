"""Generate knowledge-guided candidate netlists from top BO iterations.

This review step is intentionally separate from the BO loop. It selects the
best completed iterations, applies conservative parameter edits to their
rendered parameter values, writes candidate netlists, and optionally simulates
the candidates with the same testbenches.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import Settings
from models import NetlistTemplate, ParamDef, SimResult
from simulator import Simulator
from summarize_metrics import build_report_from_sim_result
from topologies import get_topology


@dataclass
class Candidate:
    original_iteration: int
    original_reward: float
    source_run_dir: Path
    candidate_dir: Path
    changes: dict[str, tuple[float, float]] = field(default_factory=dict)
    review_reason: str = ""
    result: SimResult | None = None


def main() -> None:
    args = _parse_args()
    prepare_agent_review = args.prepare_agent_review or args.agent_plan
    if prepare_agent_review and args.patch_plan:
        raise ValueError("Use either --prepare-agent-review or --patch-plan, not both.")
    settings = Settings(dry_run=args.dry_run)
    project = args.project
    workspace = args.workspace

    history_path = _find_history(project, workspace)
    history = json.loads(history_path.read_text(encoding="utf-8"))
    topology = get_topology(args.topology)
    param_bounds = {p.name: p for p in topology.get_param_space().params}
    patch_plan = None
    if args.patch_plan:
        patch_plan = json.loads(args.patch_plan.read_text(encoding="utf-8"))

    review_root = project / "agent_review"
    candidates_root = review_root / "candidates"
    review_root.mkdir(parents=True, exist_ok=True)
    if candidates_root.exists():
        shutil.rmtree(candidates_root)
    for stale_name in ("candidate_metrics.csv", "review_report.md"):
        stale_path = review_root / stale_name
        if stale_path.exists():
            stale_path.unlink()
    candidates_root.mkdir(parents=True, exist_ok=True)

    records = select_top_records(history.get("history", []))
    if prepare_agent_review:
        write_local_agent_review_package(
            project=project,
            workspace=workspace,
            topology_name=args.topology,
            history=history,
            history_path=history_path,
            records=records,
            param_bounds=param_bounds,
            review_root=review_root,
        )
        print(
            f"Wrote local Agent review package to {review_root}. "
            "Ask Claude/Codex to fill patch_plan.json, then rerun with --patch-plan."
        )
        return

    candidates: list[Candidate] = []
    for record in records:
        candidate = generate_candidate(
            record=record,
            history=history,
            workspace=workspace,
            candidates_root=candidates_root,
            param_bounds=param_bounds,
            settings=settings,
            patch_plan=patch_plan,
        )
        if args.simulate:
            simulate_candidate(candidate, settings)
        candidates.append(candidate)

    write_candidate_metrics(review_root / "candidate_metrics.csv", candidates)
    write_review_report(review_root / "review_report.md", candidates, history_path)
    print(f"Wrote review results to {review_root}")


def select_top_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select reward-ranked top 10%, at least 3 and at most 10 records."""
    if len(records) <= 3:
        return sorted(records, key=lambda r: r.get("reward", float("-inf")), reverse=True)

    count = math.ceil(len(records) / 10)
    count = max(3, min(10, count))
    return sorted(records, key=lambda r: r.get("reward", float("-inf")), reverse=True)[:count]


def generate_candidate(
    record: dict[str, Any],
    history: dict[str, Any],
    workspace: Path,
    candidates_root: Path,
    param_bounds: dict[str, ParamDef],
    settings: Settings,
    patch_plan: dict[str, Any] | None = None,
) -> Candidate:
    iteration = int(record["iteration"])
    source_run_dir = workspace / f"run_{iteration:03d}"
    candidate_dir = candidates_root / f"iter_{iteration:03d}_candidate_01"
    candidate_dir.mkdir(parents=True, exist_ok=True)

    source_netlist = source_run_dir / "circuit.cir"
    if not source_netlist.exists():
        raise FileNotFoundError(f"Missing source netlist: {source_netlist}")

    source_text = source_netlist.read_text(encoding="utf-8")
    params = parse_parameter_values(source_text)
    params = inflate_width_params_from_instances(source_text, params)
    plan_entry = _plan_entry_for_iteration(patch_plan, iteration)
    if plan_entry is not None:
        adjusted, changes = apply_patch_plan(
            params=params,
            plan_entry=plan_entry,
            param_bounds=param_bounds,
        )
        review_reason = str(plan_entry.get("reason", ""))
    else:
        adjusted, changes = apply_review_rules(
            params=params,
            result=record.get("result", {}),
            targets=history.get("targets", {}),
            param_bounds=param_bounds,
        )
        review_reason = "Rule-based fallback"

    template_path = workspace / "circuit_template.cir"
    template_text = (
        template_path.read_text(encoding="utf-8")
        if template_path.exists()
        else source_netlist.read_text(encoding="utf-8")
    )
    rendered = NetlistTemplate.from_netlist(template_text).render(
        adjusted,
        param_space=_param_space_from_template(template_text, settings),
        w_l_grid_step=settings.w_l_grid_step,
    )
    (candidate_dir / "circuit.cir").write_text(rendered, encoding="utf-8")
    _copy_testbenches(source_run_dir, candidate_dir)

    return Candidate(
        original_iteration=iteration,
        original_reward=float(record.get("reward", 0.0)),
        source_run_dir=source_run_dir,
        candidate_dir=candidate_dir,
        changes=changes,
        review_reason=review_reason,
    )


def write_local_agent_review_package(
    project: Path,
    workspace: Path,
    topology_name: str,
    history: dict[str, Any],
    history_path: Path,
    records: list[dict[str, Any]],
    param_bounds: dict[str, ParamDef],
    review_root: Path,
) -> None:
    """Write context files for a local Claude/Codex Agent to review."""
    guide_path = (
        Path(__file__).resolve().parents[2]
        / "knowledge_base"
        / "Opamp_knowledge_base"
        / "optimization_review_guide.md"
    )
    review_guide = (
        guide_path.read_text(encoding="utf-8")
        if guide_path.exists()
        else "No optimization review guide found."
    )
    metrics_path = project / "optimization_metrics.csv"
    metrics_csv = (
        metrics_path.read_text(encoding="utf-8")
        if metrics_path.exists()
        else "optimization_metrics.csv not found"
    )
    history_summary = _build_history_summary(history, history_path)
    candidate_context = _build_candidate_context(workspace, records, param_bounds)
    context = _build_local_agent_context(
        topology_name=topology_name,
        review_guide=review_guide,
        metrics_csv=metrics_csv,
        history_summary=history_summary,
        candidate_context=candidate_context,
    )
    review_root.mkdir(parents=True, exist_ok=True)
    (review_root / "agent_context.md").write_text(context, encoding="utf-8")
    (review_root / "patch_plan_template.json").write_text(
        json.dumps(_patch_plan_template(records), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (review_root / "patch_plan.json").write_text(
        json.dumps(_patch_plan_template(records), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def apply_patch_plan(
    params: dict[str, float],
    plan_entry: dict[str, Any],
    param_bounds: dict[str, ParamDef],
) -> tuple[dict[str, float], dict[str, tuple[float, float]]]:
    """Apply a validated structured patch plan to existing parameters only."""
    adjusted = dict(params)
    changes: dict[str, tuple[float, float]] = {}
    actions = plan_entry.get("actions", [])
    if not isinstance(actions, list):
        return adjusted, changes

    for action in actions:
        if not isinstance(action, dict):
            continue
        name = str(action.get("param", ""))
        if name not in adjusted:
            continue
        operation = str(action.get("operation", "scale")).lower()
        old = adjusted[name]
        try:
            if operation == "scale":
                factor = float(action.get("factor", 1.0))
                new = old * factor
            elif operation == "set":
                new = float(action["value"])
            else:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        new = _clamp_param(name, new, param_bounds)
        if not math.isclose(old, new, rel_tol=1e-12, abs_tol=0.0):
            adjusted[name] = new
            changes[name] = (old, new)

    return adjusted, changes


def apply_review_rules(
    params: dict[str, float],
    result: dict[str, Any],
    targets: dict[str, Any],
    param_bounds: dict[str, ParamDef],
) -> tuple[dict[str, float], dict[str, tuple[float, float]]]:
    """Apply conservative metric-deficit rules to existing netlist params."""
    adjusted = dict(params)
    changes: dict[str, tuple[float, float]] = {}

    def scale(names: list[str], factor: float) -> None:
        for name in names:
            if name not in adjusted:
                continue
            old = adjusted[name]
            new = _clamp_param(name, old * factor, param_bounds)
            if not math.isclose(old, new, rel_tol=1e-12, abs_tol=0.0):
                adjusted[name] = new
                changes[name] = (old, new)

    gain_low = _below(result, targets, "gain_db")
    gbw_low = _below(result, targets, "bandwidth_hz", aliases=("gbw_hz",))
    pm_low = _below(result, targets, "phase_margin_deg")
    power_high = _above(result, targets, "power_w")
    sr_low = _below(result, targets, "slew_rate_v_per_s")
    st_slow = _above(result, targets, "settling_time_s")

    if gain_low:
        scale(_matching_params(adjusted, "L"), 1.20)
        scale(["Wcs", "Wgm2", "Wgm3"], 1.10)
    if gbw_low:
        scale(["Wdp", "Wdiff", "Wdiffp", "Wdiff1"], 1.15)
        scale(_matching_params(adjusted, "Cc"), 0.85)
    if pm_low:
        scale(_matching_params(adjusted, "Cc"), 1.25)
        scale(_matching_params(adjusted, "Rz"), 1.20)
    if power_high:
        scale(_matching_params(adjusted, "Wtail"), 0.90)
        scale(_matching_params(adjusted, "Wload"), 0.90)
        scale(["Wcs", "Wgm2", "Wgm3"], 0.90)
    if sr_low:
        scale(["Wcs", "Wgm2", "Wgm3", "Wload", "Wload2", "Wload3"], 1.15)
        scale(_matching_params(adjusted, "Cc"), 0.90)
    if st_slow:
        if pm_low:
            scale(_matching_params(adjusted, "Cc"), 1.10)
        elif gbw_low:
            scale(["Wdp", "Wdiff", "Wdiffp", "Wdiff1"], 1.10)
        elif sr_low:
            scale(["Wcs", "Wgm2", "Wgm3"], 1.10)

    return adjusted, changes


def parse_parameter_values(netlist_text: str) -> dict[str, float]:
    """Parse Spectre/HSPICE parameter declaration values from a netlist."""
    params: dict[str, float] = {}
    for line in netlist_text.splitlines():
        stripped = line.strip()
        if not (
            stripped.lower().startswith("parameters")
            or stripped.lower().startswith(".param")
        ):
            continue
        body = re.sub(r"^\s*(?:parameters|\.param)\s+", "", line, flags=re.IGNORECASE)
        for name, raw in re.findall(r"(\w+)\s*=\s*'?(.*?)'?(?=\s+\w+\s*=|\s*$)", body):
            if name.upper() in {"NF", "M"}:
                continue
            try:
                params[name] = parse_spice_value(raw)
            except ValueError:
                continue
    return params


def inflate_width_params_from_instances(
    netlist_text: str,
    params: dict[str, float],
) -> dict[str, float]:
    """Recover effective W from rendered Spectre instance W and m values."""
    inflated = dict(params)
    width_params = {
        name: value for name, value in params.items() if name.lower().startswith("w")
    }
    if not width_params:
        return inflated

    for name, value in width_params.items():
        max_multiplier = 1
        for line in netlist_text.splitlines():
            stripped = line.strip()
            if not stripped or not stripped[0].lower() == "m":
                continue
            w_match = re.search(
                r"\b[wW]\s*=\s*'?([+-]?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?[a-zA-Z]*)'?",
                line,
            )
            nf_match = re.search(r"\bnf\s*=\s*'?(\d+)'?", line, re.IGNORECASE)
            m_match = re.search(r"\bm\s*=\s*'?(\d+)'?", line, re.IGNORECASE)
            if not w_match:
                continue
            try:
                instance_w = parse_spice_value(w_match.group(1))
            except ValueError:
                continue
            if math.isclose(instance_w, value, rel_tol=1e-6, abs_tol=1e-15):
                m = int(m_match.group(1)) if m_match else 1
                max_multiplier = max(max_multiplier, m)
        inflated[name] = value * max_multiplier
    return inflated


def parse_spice_value(raw: str) -> float:
    value = raw.strip().strip("'\"")
    match = re.fullmatch(r"([+-]?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)([a-zA-Z]*)", value)
    if not match:
        raise ValueError(f"Cannot parse SPICE value: {raw}")
    number = float(match.group(1))
    suffix = match.group(2).lower()
    scales = {
        "f": 1e-15,
        "p": 1e-12,
        "n": 1e-9,
        "u": 1e-6,
        "m": 1e-3,
        "k": 1e3,
        "meg": 1e6,
        "g": 1e9,
        "": 1.0,
    }
    if suffix not in scales:
        raise ValueError(f"Unknown SPICE suffix: {suffix}")
    return number * scales[suffix]


def simulate_candidate(candidate: Candidate, settings: Settings) -> None:
    tb_paths = sorted(candidate.candidate_dir.glob("tb*.scs"))
    if not tb_paths:
        candidate.result = SimResult(
            converged=False, error_message="No candidate testbenches found"
        )
        return
    simulator = Simulator(settings)
    candidate.result = simulator.run_all_testbenches(tb_paths, candidate.candidate_dir)
    report = build_report_from_sim_result(
        candidate.result,
        source=candidate.candidate_dir,
        testbenches=tb_paths,
    )
    (candidate.candidate_dir / "metrics_summary.txt").write_text(
        report, encoding="utf-8"
    )


def _param_space_from_template(template_text: str, settings: Settings):
    from models import ParamSpace

    return ParamSpace.from_netlist(
        template_text,
        max_per_finger=settings.max_width_per_finger,
    )


def write_candidate_metrics(path: Path, candidates: list[Candidate]) -> None:
    fieldnames = [
        "original_iteration",
        "original_reward",
        "candidate_path",
        "changed_params",
        "gain_db(dB)",
        "gbw_hz(MHz)",
        "phase_margin_deg(deg)",
        "power_w(mW)",
        "slew_rate_v_per_s(V/us)",
        "settling_time_s(ns)",
        "error_message",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for candidate in candidates:
            result = candidate.result or SimResult()
            writer.writerow(
                {
                    "original_iteration": candidate.original_iteration,
                    "original_reward": f"{candidate.original_reward:.6g}",
                    "candidate_path": str(candidate.candidate_dir),
                    "changed_params": ";".join(sorted(candidate.changes)),
                    "gain_db(dB)": _fmt_csv(result.gain_db, 2),
                    "gbw_hz(MHz)": _fmt_csv(result.bandwidth_hz, 2, 1e-6),
                    "phase_margin_deg(deg)": _fmt_csv(result.phase_margin_deg, 2),
                    "power_w(mW)": _fmt_csv(result.power_w, 3, 1e3),
                    "slew_rate_v_per_s(V/us)": _fmt_csv(
                        result.slew_rate_v_per_s, 2, 1e-6
                    ),
                    "settling_time_s(ns)": _fmt_csv(result.settling_time_s, 2, 1e9),
                    "error_message": result.error_message,
                }
            )


def write_review_report(path: Path, candidates: list[Candidate], history_path: Path) -> None:
    lines = [
        "# Agent Optimization Review",
        "",
        f"History: `{history_path}`",
        f"Candidates: {len(candidates)}",
        "",
        "## Candidate Changes",
    ]
    for candidate in candidates:
        lines.append("")
        lines.append(
            f"### Iteration {candidate.original_iteration} "
            f"(reward {candidate.original_reward:.6g})"
        )
        lines.append(f"- Candidate: `{candidate.candidate_dir}`")
        if candidate.review_reason:
            lines.append(f"- Review reason: {candidate.review_reason}")
        if candidate.changes:
            for name, (old, new) in sorted(candidate.changes.items()):
                lines.append(f"- {name}: {_eng(old)} -> {_eng(new)}")
        else:
            lines.append("- No parameter changes were triggered.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _copy_testbenches(source_run_dir: Path, candidate_dir: Path) -> None:
    for tb_path in sorted(source_run_dir.glob("tb*.scs")):
        shutil.copy2(tb_path, candidate_dir / tb_path.name)


def _find_history(project: Path, workspace: Path) -> Path:
    project_history = project / "optimization_log.json"
    if project_history.exists():
        return project_history
    workspace_history = workspace / "history.json"
    if workspace_history.exists():
        return workspace_history
    raise FileNotFoundError(
        f"Cannot find optimization_log.json in {project} or history.json in {workspace}"
    )


def _plan_entry_for_iteration(
    patch_plan: dict[str, Any] | None,
    iteration: int,
) -> dict[str, Any] | None:
    if not patch_plan:
        return None
    candidates = patch_plan.get("candidates", [])
    if not isinstance(candidates, list):
        return None
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        try:
            if int(entry.get("iteration")) == iteration:
                return entry
        except (TypeError, ValueError):
            continue
    return None


def _build_history_summary(history: dict[str, Any], history_path: Path) -> str:
    lines = [
        f"history_path: {history_path}",
        f"total_iterations: {history.get('total_iterations')}",
        f"best_iteration: {history.get('best_iteration')}",
        f"best_reward: {history.get('best_reward')}",
        "targets:",
    ]
    targets = history.get("targets", {})
    if isinstance(targets, dict):
        for name, value in sorted(targets.items()):
            lines.append(f"  {name}: {value}")
    return "\n".join(lines)


def _build_candidate_context(
    workspace: Path,
    records: list[dict[str, Any]],
    param_bounds: dict[str, ParamDef],
) -> str:
    sections: list[str] = []
    for record in records:
        iteration = int(record["iteration"])
        run_dir = workspace / f"run_{iteration:03d}"
        netlist_path = run_dir / "circuit.cir"
        params: dict[str, float] = {}
        if netlist_path.exists():
            text = netlist_path.read_text(encoding="utf-8")
            params = inflate_width_params_from_instances(
                text,
                parse_parameter_values(text),
            )
        sections.append(f"iteration: {iteration}")
        sections.append(f"reward: {record.get('reward')}")
        sections.append(f"result: {json.dumps(record.get('result', {}), sort_keys=True)}")
        sections.append("available_params:")
        for name, value in sorted(params.items()):
            bound = param_bounds.get(name)
            if bound:
                sections.append(
                    f"  {name}: current={value:.6e}, "
                    f"low={bound.low:.6e}, high={bound.high:.6e}"
                )
            else:
                sections.append(f"  {name}: current={value:.6e}, unbounded")
        sections.append("")
    return "\n".join(sections)


def _build_local_agent_context(
    topology_name: str,
    review_guide: str,
    metrics_csv: str,
    history_summary: str,
    candidate_context: str,
) -> str:
    return f"""# Local Agent BO Review Context

This file is for a local Agent such as Claude or Codex. Do not call an external
LLM from the Python script. The local Agent should read this context and write
`patch_plan.json` in the same directory.

## Task

Review the BO results, identify why the best runs miss the targets, and propose
conservative parameter edits as a structured patch plan. Python will later
validate the plan, ignore unknown parameters, clamp values to topology bounds,
render candidate netlists, and optionally simulate them.

## Topology

{topology_name}

## Required Patch Plan Schema

```json
{{
  "summary": "one paragraph explaining the strategy",
  "candidates": [
    {{
      "iteration": 3,
      "reason": "why this run should be patched",
      "actions": [
        {{
          "param": "Cc",
          "operation": "scale",
          "factor": 1.25,
          "reason": "PM is low, increase compensation"
        }}
      ]
    }}
  ]
}}
```

Allowed actions:
- `operation="scale"` with `factor`
- `operation="set"` with `value`

Safety rules:
- Only use parameters listed under Candidate Run Context.
- Do not add parameters.
- Do not edit instances, connections, ports, models, or testbenches.
- Prefer conservative scale factors, usually `0.8` to `1.3`.

## Optimization Metrics CSV

```csv
{metrics_csv.strip()}
```

## Optimization History Summary

```text
{history_summary.strip()}
```

## Candidate Run Context

```text
{candidate_context.strip()}
```

## Review Knowledge Base

```text
{review_guide.strip()}
```
"""


def _patch_plan_template(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "summary": "",
        "candidates": [
            {
                "iteration": int(record["iteration"]),
                "reason": "",
                "actions": [],
            }
            for record in records
        ],
    }


def _below(
    result: dict[str, Any],
    targets: dict[str, Any],
    key: str,
    aliases: tuple[str, ...] = (),
) -> bool:
    target = targets.get(key)
    actual = result.get(key)
    for alias in aliases:
        if actual is None:
            actual = result.get(alias)
    return target is not None and (actual is None or actual < target)


def _above(result: dict[str, Any], targets: dict[str, Any], key: str) -> bool:
    target = targets.get(key)
    actual = result.get(key)
    return target is not None and actual is not None and actual > target


def _matching_params(params: dict[str, float], prefix: str) -> list[str]:
    return [name for name in params if name.lower().startswith(prefix.lower())]


def _clamp_param(
    name: str,
    value: float,
    bounds: dict[str, ParamDef],
) -> float:
    bound = bounds.get(name)
    if not bound:
        return value
    return min(max(value, bound.low), bound.high)


def _fmt_csv(value: float | None, digits: int, scale: float = 1.0) -> str:
    if value is None:
        return ""
    return f"{value * scale:.{digits}f}"


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
        description="Generate knowledge-guided candidate netlists from BO results."
    )
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--topology", required=True)
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--agent-plan",
        action="store_true",
        help=(
            "Deprecated alias for --prepare-agent-review. It prepares context "
            "for a local Claude/Codex Agent; it does not call an external LLM."
        ),
    )
    parser.add_argument(
        "--prepare-agent-review",
        action="store_true",
        help=(
            "Write agent_context.md and patch_plan.json template for local "
            "Claude/Codex review, then exit."
        ),
    )
    parser.add_argument(
        "--patch-plan",
        type=Path,
        help="Use an existing structured patch plan JSON instead of built-in rules.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    main()
