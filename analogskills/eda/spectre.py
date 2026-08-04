"""Spectre command-spec builders."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .command import EdaCommand, EdaRunResult, run_eda_command
from .netlist import export_spice_netlist
from .reports import PostLayoutRunRecord, PostLayoutRunSummary, build_post_layout_run_record, build_post_layout_scorecard, parse_pex_report, summarize_post_layout_runs


@dataclass(frozen=True)
class PostLayoutSpectreRunPlan:
    command: EdaCommand
    run_id: str
    netlist: str
    extracted_netlist: str = ""
    corner: str = ""
    voltage_v: float | None = None
    temperature_c: float | None = None
    monte_carlo_seed: int | None = None
    output_dir: str = ""
    measurement_file: str = ""
    variables: dict[str, float | int | str] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class PostLayoutSimulationBundle:
    plan: PostLayoutSpectreRunPlan
    run_result: EdaRunResult
    record: PostLayoutRunRecord
    summary: tuple[str, ...] = ()


@dataclass(frozen=True)
class PostLayoutSpectreSweepPoint:
    run_id: str
    netlist: str = ""
    measurement_file: str = ""
    cwd: str = ""
    corner: str = ""
    voltage_v: float | None = None
    temperature_c: float | None = None
    monte_carlo_seed: int | None = None
    variables: dict[str, float | int | str] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class PostLayoutSpectreSweepPlan:
    runs: tuple[PostLayoutSpectreRunPlan, ...]
    metric_targets: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)
    metric_objectives: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PostLayoutSpectreSweepResult:
    plan: PostLayoutSpectreSweepPlan
    simulations: tuple[PostLayoutSimulationBundle, ...]
    run_summary: PostLayoutRunSummary
    summary: tuple[str, ...] = ()


@dataclass(frozen=True)
class SpectreIncludeDirective:
    path: str
    section: str = ""


@dataclass(frozen=True)
class SpectreTestbenchTemplate:
    title: str
    body_template: str
    model_includes: tuple[str | SpectreIncludeDirective, ...] = ()
    save_lines: tuple[str, ...] = ()
    measure_lines: tuple[str, ...] = ()
    setup_lines: tuple[str, ...] = ()
    statistics_lines: tuple[str, ...] = ()
    monte_carlo_mode: str = ""
    monte_carlo_name: str = "mc1"
    monte_carlo_numruns: int = 1
    monte_carlo_options: tuple[str, ...] = ()
    monte_carlo_statement_template: str = ""
    context: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SpectreEvaluatorPlan:
    graph: object
    simulation_netlist: str
    source_netlist_path: str
    sizing: dict[str, dict[str, Any]] = field(default_factory=dict)
    param_mapping: dict[str, str] = field(default_factory=dict)
    output_dir: str = ""
    measurement_file: str = ""
    variables: dict[str, float | int | str] = field(default_factory=dict)
    corner: str = ""
    required_metrics: tuple[str, ...] = ()
    binary: str = "spectre"
    cwd: str = ""
    timeout_s: float = 120.0
    env: dict[str, str] = field(default_factory=dict)


def make_spectre_command(netlist: str | Path, *, output_dir: str | Path | None = None, variables: Mapping[str, float | int | str] | None = None, corner: str | None = None, binary: str = "spectre") -> EdaCommand:
    cmd = [binary, str(netlist)]
    if output_dir is not None:
        cmd.extend(["+escchars", f"-raw={output_dir}"])
    for key, value in (variables or {}).items():
        cmd.append(f"{key}={value}")
    if corner:
        cmd.append(f"corner={corner}")
    return EdaCommand(cmd)


def run_spectre_measurements(spec: EdaCommand) -> EdaRunResult:
    return run_eda_command(spec)


def run_spectre_evaluation(
    plan: SpectreEvaluatorPlan,
    *,
    params: Mapping[str, Any] | None = None,
    sizing_updates: Mapping[str, Mapping[str, Any]] | None = None,
    check: bool = True,
) -> EdaRunResult:
    """Export a sized SPICE source netlist, run Spectre, and parse metrics."""

    effective_sizing = _merge_sizing_updates(
        plan.sizing,
        sizing_updates=sizing_updates,
        param_updates=_map_optimizer_params_to_sizing_updates(params or {}, mapping=plan.param_mapping),
    )
    export_spice_netlist(
        plan.graph,
        _resolve_eval_path(plan.source_netlist_path, cwd=plan.cwd or None),
        sizing=effective_sizing,
    )
    command = make_spectre_command(
        plan.simulation_netlist,
        output_dir=plan.output_dir or None,
        variables=plan.variables,
        corner=plan.corner or None,
        binary=plan.binary,
    )
    command = EdaCommand(
        command.command,
        cwd=plan.cwd or None,
        timeout_s=plan.timeout_s,
        env=plan.env,
        measurement_file=plan.measurement_file or None,
    )
    result = run_spectre_measurements(command)
    missing_metrics = tuple(metric for metric in plan.required_metrics if metric not in result.metrics)
    if check:
        if not result.ok:
            raise RuntimeError(
                "Spectre evaluation failed "
                f"rc={result.returncode} timed_out={result.timed_out}: {' '.join(result.command)}"
            )
        if missing_metrics:
            raise RuntimeError(
                "Spectre evaluation missing required metrics: "
                + ", ".join(missing_metrics)
            )
    return result


def make_spectre_evaluator(
    plan: SpectreEvaluatorPlan,
    *,
    check: bool = True,
) -> Callable[[Mapping[str, Any]], dict[str, float]]:
    """Build an ``optimize_blackbox``-compatible Spectre metric evaluator."""

    def evaluator(params: Mapping[str, Any]) -> dict[str, float]:
        return dict(run_spectre_evaluation(plan, params=params, check=check).metrics)

    return evaluator


def build_post_layout_spectre_plan(
    run_id: str,
    netlist: str | Path,
    *,
    extracted_netlist: str | Path | None = None,
    output_dir: str | Path | None = None,
    measurement_file: str | Path | None = None,
    variables: Mapping[str, float | int | str] | None = None,
    corner: str | None = None,
    voltage_v: float | None = None,
    temperature_c: float | None = None,
    monte_carlo_seed: int | None = None,
    binary: str = "spectre",
    cwd: str | Path | None = None,
    timeout_s: float = 120.0,
    env: Mapping[str, str] | None = None,
    artifacts: Mapping[str, str | Path] | None = None,
    tags: tuple[str, ...] | list[str] = (),
) -> PostLayoutSpectreRunPlan:
    target_netlist = extracted_netlist if extracted_netlist is not None else netlist
    command = make_spectre_command(
        target_netlist,
        output_dir=output_dir,
        variables=variables,
        corner=corner,
        binary=binary,
    )
    command = EdaCommand(
        command.command,
        cwd=cwd,
        timeout_s=timeout_s,
        env=env,
        measurement_file=measurement_file,
    )
    artifact_map = {str(key): str(value) for key, value in dict(artifacts or {}).items()}
    if extracted_netlist is not None:
        artifact_map.setdefault("extracted_netlist", str(extracted_netlist))
    artifact_map.setdefault("schematic_or_base_netlist", str(netlist))
    if output_dir is not None:
        artifact_map.setdefault("raw_output_dir", str(output_dir))
    if measurement_file is not None:
        artifact_map.setdefault("measurement_file", str(measurement_file))
    return PostLayoutSpectreRunPlan(
        command=command,
        run_id=str(run_id),
        netlist=str(netlist),
        extracted_netlist=str(extracted_netlist) if extracted_netlist is not None else "",
        corner=str(corner or ""),
        voltage_v=voltage_v,
        temperature_c=temperature_c,
        monte_carlo_seed=monte_carlo_seed,
        output_dir=str(output_dir) if output_dir is not None else "",
        measurement_file=str(measurement_file) if measurement_file is not None else "",
        variables={str(key): value for key, value in dict(variables or {}).items()},
        artifacts=artifact_map,
        tags=tuple(str(tag) for tag in tags),
    )


def build_post_layout_spectre_plan_from_pex(
    run_id: str,
    pex_report: str | Path | object,
    *,
    netlist: str | Path,
    output_dir: str | Path | None = None,
    measurement_file: str | Path | None = None,
    variables: Mapping[str, float | int | str] | None = None,
    corner: str | None = None,
    voltage_v: float | None = None,
    temperature_c: float | None = None,
    monte_carlo_seed: int | None = None,
    binary: str = "spectre",
    cwd: str | Path | None = None,
    timeout_s: float = 120.0,
    env: Mapping[str, str] | None = None,
    artifacts: Mapping[str, str | Path] | None = None,
    tags: tuple[str, ...] | list[str] = (),
) -> PostLayoutSpectreRunPlan:
    parsed = pex_report if hasattr(pex_report, "extracted_netlist") else parse_pex_report(pex_report)
    extracted_netlist = str(getattr(parsed, "extracted_netlist", "") or "").strip()
    if not extracted_netlist:
        raise ValueError("PEX report does not provide an extracted netlist path")
    merged_artifacts = dict(artifacts or {})
    merged_artifacts.setdefault("pex_report", str(pex_report) if isinstance(pex_report, (str, Path)) else "")
    return build_post_layout_spectre_plan(
        run_id,
        netlist,
        extracted_netlist=extracted_netlist,
        output_dir=output_dir,
        measurement_file=measurement_file,
        variables=variables,
        corner=corner,
        voltage_v=voltage_v,
        temperature_c=temperature_c,
        monte_carlo_seed=monte_carlo_seed,
        binary=binary,
        cwd=cwd,
        timeout_s=timeout_s,
        env=env,
        artifacts=merged_artifacts,
        tags=tags,
    )


def build_post_layout_spectre_sweep_plan(
    run_id: str,
    netlist: str | Path,
    sweep: tuple[PostLayoutSpectreSweepPoint, ...] | list[PostLayoutSpectreSweepPoint],
    *,
    extracted_netlist: str | Path | None = None,
    output_dir: str | Path | None = None,
    measurement_file: str | Path | None = None,
    variables: Mapping[str, float | int | str] | None = None,
    corner: str | None = None,
    voltage_v: float | None = None,
    temperature_c: float | None = None,
    binary: str = "spectre",
    cwd: str | Path | None = None,
    timeout_s: float = 120.0,
    env: Mapping[str, str] | None = None,
    artifacts: Mapping[str, str | Path] | None = None,
    tags: tuple[str, ...] | list[str] = (),
    metric_targets: Mapping[str, tuple[float | None, float | None]] | None = None,
    metric_objectives: Mapping[str, str] | None = None,
    metadata: Mapping[str, object] | None = None,
    voltage_var: str | None = None,
    temperature_var: str | None = None,
    monte_carlo_seed_var: str | None = None,
) -> PostLayoutSpectreSweepPlan:
    runs: list[PostLayoutSpectreRunPlan] = []
    base_tags = tuple(str(tag) for tag in tags)
    base_artifacts = {str(key): str(value) for key, value in dict(artifacts or {}).items()}
    base_variables = {str(key): value for key, value in dict(variables or {}).items()}
    default_corner = str(corner or "")
    for idx, point in enumerate(tuple(sweep)):
        point_run_id = str(point.run_id or f"{run_id}_{idx}")
        merged_variables = dict(base_variables)
        merged_variables.update(dict(point.variables))
        resolved_voltage = point.voltage_v if point.voltage_v is not None else voltage_v
        resolved_temp = point.temperature_c if point.temperature_c is not None else temperature_c
        if voltage_var and resolved_voltage is not None:
            merged_variables[str(voltage_var)] = resolved_voltage
        if temperature_var and resolved_temp is not None:
            merged_variables[str(temperature_var)] = resolved_temp
        if monte_carlo_seed_var and point.monte_carlo_seed is not None:
            merged_variables[str(monte_carlo_seed_var)] = point.monte_carlo_seed
        merged_artifacts = dict(base_artifacts)
        merged_artifacts.update({str(key): str(value) for key, value in dict(point.artifacts).items()})
        merged_tags = (*base_tags, *tuple(str(tag) for tag in point.tags))
        runs.append(
            build_post_layout_spectre_plan(
                point_run_id,
                point.netlist or netlist,
                extracted_netlist=extracted_netlist,
                output_dir=output_dir,
                measurement_file=point.measurement_file or measurement_file,
                variables=merged_variables,
                corner=point.corner or default_corner,
                voltage_v=resolved_voltage,
                temperature_c=resolved_temp,
                monte_carlo_seed=point.monte_carlo_seed,
                binary=binary,
                cwd=point.cwd or cwd,
                timeout_s=timeout_s,
                env=env,
                artifacts=merged_artifacts,
                tags=merged_tags,
            )
        )
    return PostLayoutSpectreSweepPlan(
        runs=tuple(runs),
        metric_targets={str(name): tuple(bounds) for name, bounds in dict(metric_targets or {}).items()},
        metric_objectives={str(name): str(objective) for name, objective in dict(metric_objectives or {}).items()},
        metadata={str(key): value for key, value in dict(metadata or {}).items()},
    )


def render_spectre_testbench(
    template: SpectreTestbenchTemplate,
    *,
    dut_include_path: str | Path,
    run_id: str = "",
    corner: str = "",
    voltage_v: float | None = None,
    temperature_c: float | None = None,
    monte_carlo_seed: int | None = None,
    measurement_file: str | Path | None = None,
    extra_context: Mapping[str, object] | None = None,
) -> str:
    """Render one reusable Spectre signoff testbench from a template."""

    model_include_block = "\n".join(_render_include_directive(entry) for entry in template.model_includes)
    dut_include_block = f'include "{dut_include_path}"'
    save_block = "\n".join(template.save_lines)
    measure_block = "\n".join(template.measure_lines)
    setup_block = "\n".join(template.setup_lines)
    statistics_block = _render_statistics_block(template.statistics_lines)
    context = {
        "title": template.title,
        "run_id": run_id,
        "corner": corner,
        "voltage_v": "" if voltage_v is None else voltage_v,
        "temperature_c": "" if temperature_c is None else temperature_c,
        "monte_carlo_seed": "" if monte_carlo_seed is None else monte_carlo_seed,
        "measurement_file": "" if measurement_file is None else str(measurement_file),
        "model_include_block": model_include_block,
        "dut_include_block": dut_include_block,
        "save_block": save_block,
        "measure_block": measure_block,
        "setup_block": setup_block,
        "statistics_block": statistics_block,
        **dict(template.context),
        **{str(key): value for key, value in dict(extra_context or {}).items()},
    }
    body = template.body_template.format(**context).strip()
    if template.monte_carlo_mode:
        body = _wrap_monte_carlo_block(
            body,
            seed=None if monte_carlo_seed is None else int(monte_carlo_seed),
            mode=template.monte_carlo_mode,
            name=template.monte_carlo_name,
            numruns=max(1, int(template.monte_carlo_numruns)),
            options=template.monte_carlo_options,
            statement_template=template.monte_carlo_statement_template,
        )
    lines = [
        "simulator lang=spectre",
        f"// {template.title}",
        *(line for line in (model_include_block, dut_include_block, setup_block, statistics_block, body, save_block, measure_block) if line),
    ]
    return "\n".join(lines) + "\n"


def write_spectre_testbench(
    template: SpectreTestbenchTemplate,
    path: str | Path,
    *,
    dut_include_path: str | Path,
    run_id: str = "",
    corner: str = "",
    voltage_v: float | None = None,
    temperature_c: float | None = None,
    monte_carlo_seed: int | None = None,
    measurement_file: str | Path | None = None,
    extra_context: Mapping[str, object] | None = None,
) -> Path:
    target = Path(path)
    target.write_text(
        render_spectre_testbench(
            template,
            dut_include_path=dut_include_path,
            run_id=run_id,
            corner=corner,
            voltage_v=voltage_v,
            temperature_c=temperature_c,
            monte_carlo_seed=monte_carlo_seed,
            measurement_file=measurement_file,
            extra_context=extra_context,
        ),
        encoding="utf-8",
    )
    return target


def build_post_layout_spectre_sweep_plan_from_template(
    run_id: str,
    template: SpectreTestbenchTemplate,
    *,
    dut_include_path: str | Path,
    sweep: tuple[PostLayoutSpectreSweepPoint, ...] | list[PostLayoutSpectreSweepPoint],
    generated_dir: str | Path,
    testbench_suffix: str = ".scs",
    measurement_file_name: str = "meas.txt",
    binary: str = "spectre",
    timeout_s: float = 120.0,
    env: Mapping[str, str] | None = None,
    tags: tuple[str, ...] | list[str] = (),
    metric_targets: Mapping[str, tuple[float | None, float | None]] | None = None,
    metric_objectives: Mapping[str, str] | None = None,
    metadata: Mapping[str, object] | None = None,
    extra_context: Mapping[str, object] | None = None,
) -> PostLayoutSpectreSweepPlan:
    generated_root = Path(generated_dir)
    generated_root.mkdir(parents=True, exist_ok=True)
    generated_points: list[PostLayoutSpectreSweepPoint] = []
    for idx, point in enumerate(tuple(sweep)):
        point_run_id = str(point.run_id or f"{run_id}_{idx}")
        run_dir = generated_root / point_run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        measurement_file = point.measurement_file or measurement_file_name
        measurement_path = Path(measurement_file)
        testbench_path = run_dir / f"{point_run_id}{testbench_suffix}"
        write_spectre_testbench(
            template,
            testbench_path,
            dut_include_path=dut_include_path,
            run_id=point_run_id,
            corner=point.corner,
            voltage_v=point.voltage_v,
            temperature_c=point.temperature_c,
            monte_carlo_seed=point.monte_carlo_seed,
            measurement_file=measurement_path.name if not measurement_path.is_absolute() else measurement_path,
            extra_context=extra_context,
        )
        artifacts = dict(point.artifacts)
        artifacts.setdefault("generated_testbench", str(testbench_path))
        generated_points.append(
            PostLayoutSpectreSweepPoint(
                run_id=point_run_id,
                netlist=str(testbench_path),
                measurement_file=str(measurement_path if measurement_path.is_absolute() else measurement_path.name),
                cwd=str(run_dir),
                corner=point.corner,
                voltage_v=point.voltage_v,
                temperature_c=point.temperature_c,
                monte_carlo_seed=point.monte_carlo_seed,
                variables=dict(point.variables),
                artifacts=artifacts,
                tags=point.tags,
            )
        )
    return build_post_layout_spectre_sweep_plan(
        run_id,
        netlist=str(dut_include_path),
        sweep=tuple(generated_points),
        binary=binary,
        cwd=str(generated_root),
        timeout_s=timeout_s,
        env=env,
        tags=tags,
        metric_targets=metric_targets,
        metric_objectives=metric_objectives,
        metadata={str(key): value for key, value in dict(metadata or {}).items()},
    )


def build_pdk_spectre_testbench_template(
    pdk: object,
    preset_name: str,
    *,
    corner: str = "",
    extra_context: Mapping[str, object] | None = None,
    save_lines: tuple[str, ...] | list[str] = (),
    measure_lines: tuple[str, ...] | list[str] = (),
    setup_lines: tuple[str, ...] | list[str] = (),
    statistics_lines: tuple[str, ...] | list[str] = (),
) -> SpectreTestbenchTemplate:
    preset = getattr(pdk, "signoff_preset")(preset_name)
    monte_carlo = getattr(preset, "monte_carlo")
    resolved_corner = str(corner or "")
    model_includes = tuple(
        SpectreIncludeDirective(
            path=str(library.path),
            section=str(library.resolve_section(resolved_corner)),
        )
        for library in tuple(getattr(preset, "model_libraries", ()) or ())
    )
    context = dict(getattr(preset, "context", {}))
    context.update({str(key): value for key, value in dict(extra_context or {}).items()})
    return SpectreTestbenchTemplate(
        title=str(getattr(preset, "title", preset_name) or preset_name),
        body_template=str(getattr(preset, "body_template", "")),
        model_includes=model_includes,
        save_lines=tuple(getattr(preset, "save_lines", ())) + tuple(str(item) for item in save_lines),
        measure_lines=tuple(getattr(preset, "measure_lines", ())) + tuple(str(item) for item in measure_lines),
        setup_lines=tuple(getattr(preset, "setup_lines", ())) + tuple(str(item) for item in setup_lines),
        statistics_lines=(
            *tuple(getattr(preset, "statistics_lines", ())),
            *tuple(getattr(monte_carlo, "statistics_lines", ())),
            *tuple(str(item) for item in statistics_lines),
        ),
        monte_carlo_mode=str(getattr(monte_carlo, "mode", "")),
        monte_carlo_name=str(getattr(monte_carlo, "name", "mc1")),
        monte_carlo_numruns=max(1, int(getattr(monte_carlo, "numruns", 1) or 1)),
        monte_carlo_options=tuple(str(item) for item in tuple(getattr(monte_carlo, "options", ()) or ()) if str(item)),
        monte_carlo_statement_template=str(getattr(monte_carlo, "statement_template", "")),
        context=context,
    )


def build_post_layout_spectre_sweep_plan_from_pdk(
    run_id: str,
    pdk: object,
    preset_name: str,
    *,
    dut_include_path: str | Path,
    sweep: tuple[PostLayoutSpectreSweepPoint, ...] | list[PostLayoutSpectreSweepPoint],
    generated_dir: str | Path,
    default_corner: str = "",
    default_voltage_v: float | None = None,
    measurement_file_name: str | None = None,
    testbench_suffix: str = ".scs",
    binary: str = "spectre",
    timeout_s: float = 120.0,
    env: Mapping[str, str] | None = None,
    tags: tuple[str, ...] | list[str] = (),
    metric_targets: Mapping[str, tuple[float | None, float | None]] | None = None,
    metric_objectives: Mapping[str, str] | None = None,
    metadata: Mapping[str, object] | None = None,
    extra_context: Mapping[str, object] | None = None,
) -> PostLayoutSpectreSweepPlan:
    preset = getattr(pdk, "signoff_preset")(preset_name)
    generated_root = Path(generated_dir)
    generated_root.mkdir(parents=True, exist_ok=True)
    generated_points: list[PostLayoutSpectreSweepPoint] = []
    base_measurement_name = (
        str(measurement_file_name)
        if measurement_file_name is not None
        else str(getattr(preset, "default_measurement_file_name", "meas.txt"))
    )
    base_variables = {str(key): value for key, value in dict(getattr(preset, "variables", {})).items()}
    for idx, point in enumerate(tuple(sweep)):
        point_run_id = str(point.run_id or f"{run_id}_{idx}")
        resolved_corner = str(point.corner or default_corner)
        resolved_temp = point.temperature_c
        if resolved_temp is None and resolved_corner:
            extraction_corner = getattr(pdk, "extraction_corners", {}).get(resolved_corner)
            resolved_temp = getattr(extraction_corner, "temperature_c", None)
        resolved_voltage = point.voltage_v if point.voltage_v is not None else default_voltage_v
        run_dir = generated_root / point_run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        measurement_file = point.measurement_file or base_measurement_name
        measurement_path = Path(measurement_file)
        testbench_path = run_dir / f"{point_run_id}{testbench_suffix}"
        template = build_pdk_spectre_testbench_template(
            pdk,
            preset_name,
            corner=resolved_corner,
            extra_context=extra_context,
        )
        write_spectre_testbench(
            template,
            testbench_path,
            dut_include_path=dut_include_path,
            run_id=point_run_id,
            corner=resolved_corner,
            voltage_v=resolved_voltage,
            temperature_c=resolved_temp,
            monte_carlo_seed=point.monte_carlo_seed,
            measurement_file=measurement_path.name if not measurement_path.is_absolute() else measurement_path,
            extra_context=extra_context,
        )
        artifacts = dict(point.artifacts)
        artifacts.setdefault("generated_testbench", str(testbench_path))
        artifacts.setdefault("signoff_preset", preset_name)
        merged_variables = dict(base_variables)
        merged_variables.update(dict(point.variables))
        generated_points.append(
            PostLayoutSpectreSweepPoint(
                run_id=point_run_id,
                netlist=str(testbench_path),
                measurement_file=str(measurement_path if measurement_path.is_absolute() else measurement_path.name),
                cwd=str(run_dir),
                corner=resolved_corner,
                voltage_v=resolved_voltage,
                temperature_c=resolved_temp,
                monte_carlo_seed=point.monte_carlo_seed,
                variables=merged_variables,
                artifacts=artifacts,
                tags=point.tags,
            )
        )
    return build_post_layout_spectre_sweep_plan(
        run_id,
        netlist=str(dut_include_path),
        sweep=tuple(generated_points),
        binary=binary,
        cwd=str(generated_root),
        timeout_s=timeout_s,
        env=env,
        tags=tags,
        metric_targets=metric_targets,
        metric_objectives=metric_objectives,
        metadata={
            "signoff_preset": preset_name,
            **{str(key): value for key, value in dict(metadata or {}).items()},
        },
    )


def run_post_layout_spectre(plan: PostLayoutSpectreRunPlan, *, targets: Mapping[str, tuple[float | None, float | None]] | None = None):
    result = run_eda_command(plan.command)
    scorecard = build_post_layout_scorecard(result.metrics, targets=dict(targets or {}))
    return build_post_layout_run_record(
        plan.run_id,
        scorecard,
        corner=plan.corner,
        voltage_v=plan.voltage_v,
        temperature_c=plan.temperature_c,
        monte_carlo_seed=plan.monte_carlo_seed,
        artifacts=plan.artifacts,
        tags=plan.tags,
    )


def execute_post_layout_simulation(
    plan: PostLayoutSpectreRunPlan,
    *,
    targets: Mapping[str, tuple[float | None, float | None]] | None = None,
    check: bool = False,
) -> PostLayoutSimulationBundle:
    run_result = run_eda_command(plan.command)
    if check and not run_result.ok:
        raise RuntimeError(
            "post-layout Spectre execution failed "
            f"rc={run_result.returncode} timed_out={run_result.timed_out}: {' '.join(run_result.command)}"
        )
    scorecard = build_post_layout_scorecard(run_result.metrics, targets=dict(targets or {}))
    record = build_post_layout_run_record(
        plan.run_id,
        scorecard,
        corner=plan.corner,
        voltage_v=plan.voltage_v,
        temperature_c=plan.temperature_c,
        monte_carlo_seed=plan.monte_carlo_seed,
        artifacts=plan.artifacts,
        tags=plan.tags,
    )
    return PostLayoutSimulationBundle(
        plan=plan,
        run_result=run_result,
        record=record,
        summary=(
            f"run_id={plan.run_id}",
            f"corner={plan.corner or 'default'}",
            f"metrics={len(record.scorecard.metrics)}",
            f"passed={record.scorecard.passed}",
        ),
    )


def execute_post_layout_spectre_sweep(
    plan: PostLayoutSpectreSweepPlan,
    *,
    check: bool = False,
) -> PostLayoutSpectreSweepResult:
    simulations = tuple(
        execute_post_layout_simulation(run, targets=plan.metric_targets, check=check)
        for run in plan.runs
    )
    run_summary = summarize_post_layout_runs(
        tuple(simulation.record for simulation in simulations),
        objectives=plan.metric_objectives,
    )
    return PostLayoutSpectreSweepResult(
        plan=plan,
        simulations=simulations,
        run_summary=run_summary,
        summary=(
            f"runs={run_summary.total_runs}",
            f"passing={run_summary.passing_runs}",
            f"failing={run_summary.failing_runs}",
        ),
    )


def _merge_sizing_updates(
    base: Mapping[str, Mapping[str, Any]],
    *,
    sizing_updates: Mapping[str, Mapping[str, Any]] | None = None,
    param_updates: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    merged = {str(name): dict(values) for name, values in dict(base).items()}
    for update_group in (dict(sizing_updates or {}), dict(param_updates or {})):
        for device, values in update_group.items():
            current = dict(merged.get(str(device), {}))
            current.update(dict(values))
            merged[str(device)] = current
    return merged


def _map_optimizer_params_to_sizing_updates(
    params: Mapping[str, Any],
    *,
    mapping: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    explicit = {str(name): str(target) for name, target in dict(mapping or {}).items()}
    updates: dict[str, dict[str, Any]] = {}
    for raw_name, value in dict(params).items():
        target = explicit.get(str(raw_name), str(raw_name))
        device, param = _decode_optimizer_param_target(target)
        if not device or not param:
            continue
        updates.setdefault(device, {})[param] = value
    return updates


def _decode_optimizer_param_target(target: str) -> tuple[str, str]:
    if "__" in target:
        device, param = target.split("__", 1)
        return device.strip(), param.strip()
    if "." in target:
        device, param = target.split(".", 1)
        return device.strip(), param.strip()
    return "", ""


def _resolve_eval_path(path: str | Path, *, cwd: str | Path | None) -> Path:
    resolved = Path(path)
    if resolved.is_absolute() or cwd is None:
        return resolved
    return Path(cwd) / resolved


def _render_statistics_block(lines: tuple[str, ...] | list[str]) -> str:
    if not lines:
        return ""
    return "\n".join(
        (
            "statistics {",
            _indent_block("\n".join(lines), "  "),
            "}",
        )
    )


def _render_include_directive(entry: str | SpectreIncludeDirective) -> str:
    if isinstance(entry, SpectreIncludeDirective):
        rendered = f'include "{entry.path}"'
        if entry.section:
            rendered += f" section={entry.section}"
        return rendered
    return f'include "{entry}"'


def _wrap_monte_carlo_block(
    body: str,
    *,
    seed: int | None,
    mode: str,
    name: str = "mc1",
    numruns: int = 1,
    options: tuple[str, ...] | list[str] = (),
    statement_template: str = "",
) -> str:
    inner = _indent_block(body.strip(), "  ")
    mc_context = {
        "name": str(name),
        "numruns": max(1, int(numruns)),
        "seed_clause": "" if seed is None else f" seed={seed}",
        "seed": "" if seed is None else seed,
        "mode": str(mode),
        "options_clause": "" if not options else " " + " ".join(str(option) for option in options),
    }
    if statement_template:
        header = statement_template.format(**mc_context).rstrip()
    else:
        header = (
            f"{mc_context['name']} montecarlo numruns={mc_context['numruns']}"
            f"{mc_context['seed_clause']} variations={mc_context['mode']}"
            f"{mc_context['options_clause']}"
        )
    if not header.endswith("{"):
        header = header + " {"
    return "\n".join(
        (
            header,
            inner,
            "}",
        )
    )


def _indent_block(text: str, prefix: str) -> str:
    return "\n".join(prefix + line if line else line for line in text.splitlines())
