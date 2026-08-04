"""Reuse the Auto_Agent_Design frontend from a unified full-flow entry point."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, Sequence

from config import Settings, settings
from llm_client import LLMClient
from models import DesignTarget, parse_metric_goals
from topologies import get_topology, get_topology_for_targets


Runner = Callable[..., subprocess.CompletedProcess]
PHYSICAL_TOPOLOGIES = frozenset({"two_stage_ota", "strongarm_latch"})


@dataclass(frozen=True)
class FrontendProject:
    project_name: str
    topology: str
    input_dir: Path
    output_dir: Path
    netlist: Path
    testbenches: tuple[Path, ...]
    requirements: Path
    original_requirement: str


def prepare_frontend_project(
    *,
    request: str | None = None,
    requirements_file: str | Path | None = None,
    topology: str | None = None,
    project_name: str | None = None,
    input_root: str | Path | None = None,
    config: Settings = settings,
) -> FrontendProject:
    """Parse requirements, select a topology, and use its native generator."""
    if bool(request) == bool(requirements_file):
        raise ValueError("provide exactly one of request or requirements_file")

    if request:
        if not config.deepseek_api_key:
            raise ValueError(
                "free-text --request requires DEEPSEEK_API_KEY; "
                "use --requirements for an offline structured request"
            )
        targets, suggested_name = LLMClient(config).parse_user_requirements(request)
        original_requirement = request
        payload: dict[str, Any] = {}
    else:
        path = Path(str(requirements_file)).resolve()
        payload = json.loads(path.read_text(encoding="utf-8"))
        targets = _targets_from_requirements(payload)
        suggested_name = str(payload.get("project_name", ""))
        original_requirement = str(payload.get("original_requirement", ""))

    selected = topology or str(payload.get("topology_name", "")) or get_topology_for_targets(targets)
    if not selected:
        raise ValueError("the Auto_Agent_Design frontend could not select a topology")
    topology_impl = get_topology(selected)
    name = config.sanitize_project_name(
        project_name or suggested_name or f"{selected}_design"
    )
    root = Path(input_root) if input_root is not None else Path(__file__).parent / "generated_projects"
    generated = topology_impl.write_project(
        root / name,
        targets=targets,
        original_requirement=original_requirement,
    ).resolve()
    netlist = generated / f"{selected}.cir"
    requirements = generated / "requirements.json"
    testbenches = tuple(sorted(generated.glob(f"tb_{selected}_*.scs")))
    if not netlist.is_file() or not requirements.is_file() or not testbenches:
        raise RuntimeError(f"frontend project generation is incomplete: {generated}")
    return FrontendProject(
        project_name=name,
        topology=selected,
        input_dir=generated,
        output_dir=config.get_project_path(name).resolve(),
        netlist=netlist,
        testbenches=testbenches,
        requirements=requirements,
        original_requirement=original_requirement,
    )


def optimizer_command(
    project: FrontendProject,
    *,
    max_iterations: int | None = None,
    dry_run: bool = False,
) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).parent / "main.py"),
        "--netlist",
        str(project.netlist),
        "--testbench",
        *(str(path) for path in project.testbenches),
        "--requirements",
        str(project.requirements),
        "--project",
        project.project_name,
    ]
    if max_iterations is not None:
        if max_iterations <= 0:
            raise ValueError("max_iterations must be positive")
        command.extend(("--max-iter", str(max_iterations)))
    if dry_run:
        command.append("--dry-run")
    return command


def run_frontend_optimization(
    project: FrontendProject,
    *,
    max_iterations: int | None = None,
    dry_run: bool = False,
    runner: Runner = subprocess.run,
) -> Path:
    command = optimizer_command(
        project,
        max_iterations=max_iterations,
        dry_run=dry_run,
    )
    completed = runner(command, cwd=Path(__file__).parent, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"Auto_Agent_Design BO frontend failed with exit code {completed.returncode}")
    results = project.output_dir / "results.json"
    if not results.is_file():
        raise RuntimeError(f"BO frontend did not produce {results}")
    _write_frontend_manifest(project, command, dry_run=dry_run)
    return project.output_dir


def run_automatic_review(
    *,
    project_dir: str | Path,
    topology: str,
    workspace: str | Path,
    dry_run: bool,
    runner: Runner = subprocess.run,
) -> None:
    command = [
        sys.executable,
        str(Path(__file__).parent / "review_optimization.py"),
        "--project",
        str(Path(project_dir).resolve()),
        "--workspace",
        str(Path(workspace).resolve()),
        "--topology",
        topology,
        "--simulate",
    ]
    if dry_run:
        command.append("--dry-run")
    completed = runner(command, cwd=Path(__file__).parent, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"Auto_Agent_Design Review failed with exit code {completed.returncode}")


def ensure_physical_topology_supported(topology: str) -> None:
    if topology not in PHYSICAL_TOPOLOGIES:
        supported = ", ".join(sorted(PHYSICAL_TOPOLOGIES))
        raise ValueError(
            f"physical_adapter_required: {topology!r} is not supported; "
            f"supported topologies: {supported}"
        )


def _targets_from_requirements(payload: dict[str, Any]) -> DesignTarget:
    data = dict(payload.get("targets", payload))
    return DesignTarget(
        gain_db=data.get("gain_db"),
        bandwidth_hz=data.get("bandwidth_hz", data.get("gbw_hz")),
        phase_margin_deg=data.get("phase_margin_deg"),
        power_w=data.get("power_w"),
        load_cap_f=data.get("load_cap_f"),
        slew_rate_v_per_s=data.get("slew_rate_v_per_s"),
        settling_time_s=data.get("settling_time_s"),
        vref_v=data.get("vref_v"),
        vref_tolerance_v=data.get("vref_tolerance_v") or 10e-3,
        tempco_ppm_per_c=data.get("tempco_ppm_per_c"),
        vref_temp_nonlinearity_v=data.get("vref_temp_nonlinearity_v"),
        psrr_db=data.get("psrr_db"),
        line_regulation_v_per_v=data.get("line_regulation_v_per_v"),
        startup_time_s=data.get("startup_time_s"),
        topology_hint=str(payload.get("topology_hint", data.get("topology_hint", ""))),
        custom_specs=dict(payload.get("custom_specs", data.get("custom_specs", {}))),
        metric_goals=parse_metric_goals(
            payload.get("metric_goals", data.get("metric_goals"))
        ),
    )


def _write_frontend_manifest(
    project: FrontendProject,
    command: Sequence[str],
    *,
    dry_run: bool,
) -> None:
    flow = project.output_dir / "flow"
    flow.mkdir(parents=True, exist_ok=True)
    (flow / "frontend_manifest.json").write_text(
        json.dumps(
            {
                "schema": "auto_agent_design.frontend_run/v1",
                "project": project.project_name,
                "topology": project.topology,
                "original_requirement": project.original_requirement,
                "input_dir": str(project.input_dir),
                "requirements": str(project.requirements),
                "netlist": str(project.netlist),
                "testbenches": [str(path) for path in project.testbenches],
                "bo_command": list(command),
                "dry_run": dry_run,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
