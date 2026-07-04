"""Circuit Design Agent - batch optimization entry point.

The agent/topology library is responsible for understanding user requirements
and generating DUT/testbench/requirements files.  This script consumes those
files and runs simulation plus BO optimization.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from config import Settings, settings
from llm_client import LLMClient
from models import (
    CircuitFiles,
    DesignTarget,
    NetlistTemplate,
    ParamSpace,
    SimResult,
)
from optimizer import HybridOptimizer
from pdk_profiles import get_pdk_profile, validate_pdk_profile
from simulator import Simulator
from utils import ensure_directories, setup_logging

console = Console()


def main():
    """Main entry point."""
    args = parse_args()

    # Setup
    config = settings
    if args.dry_run:
        config.dry_run = True
    if args.max_iter:
        config.max_iterations = args.max_iter

    setup_logging(level="DEBUG" if args.verbose else "INFO")
    ensure_directories(config)

    if not args.netlist:
        _print_header(config)
        console.print("[red]No netlist provided.[/red]")
        console.print(
            "Generate a project with the topology library first, then run "
            "main.py in file mode, for example:"
        )
        console.print(
            "[dim]python3 main.py --netlist <project>/<name>.cir "
            "--testbench <project>/tb_<name>_ac.scs "
            "--requirements <project>/requirements.json[/dim]"
        )
        sys.exit(2)

    # Validate config
    try:
        config.validate_required()
    except ValueError as e:
        console.print(f"[red]Configuration Error:[/red] {e}")
        console.print("Please copy .env.example to .env and fill in required values.")
        sys.exit(1)

    # Display header
    _print_header(config)

    # Initialize components
    if config.dry_run:
        config.deepseek_api_key = "dry-run"  # placeholder, LLM won't be called
    llm = LLMClient(config)
    sim = Simulator(config)
    optimizer = HybridOptimizer(llm, sim, config)

    run_from_file(args, llm, sim, optimizer, config)


def run_from_file(
    args: argparse.Namespace,
    llm: LLMClient,
    sim: Simulator,
    optimizer: HybridOptimizer,
    config: Settings,
) -> None:
    """Run optimization from a pre-made netlist file.

    This mode is designed for integration with external agents (e.g., Claude Code)
    that generate the initial SPICE netlist themselves.

    Usage:
        python main.py --netlist circuit.sp --params params.json --requirements requirements.json
        python main.py --netlist circuit.sp --params params.json --targets-json targets.json
        python main.py --netlist circuit.sp --params params.json --gain 40 --pm 60
    """
    netlist_path = Path(args.netlist)
    if not netlist_path.exists():
        console.print(f"[red]Netlist file not found: {netlist_path}[/red]")
        sys.exit(1)

    # Load DUT and testbenches before constructing the parameter space so
    # testbench-owned optimization parameters such as VBIAS are visible.
    netlist_content = netlist_path.read_text(encoding="utf-8")
    if args.testbench:
        testbench_contents = []
        for tb_str in args.testbench:
            tb_path = Path(tb_str)
            if not tb_path.exists():
                console.print(f"[red]Testbench file not found: {tb_path}[/red]")
                sys.exit(1)
            testbench_contents.append(tb_path.read_text(encoding="utf-8"))
            console.print(f"[green]✓[/green] Loaded testbench: {tb_path}")
        circuit_files = CircuitFiles(
            circuit_netlist=netlist_content,
            testbenches=testbench_contents,
            circuit_name=CircuitFiles.extract_subckt_name(netlist_content),
        )
    else:
        circuit_files = _build_circuit_files(netlist_content)

    # --- Load parameter search space ---
    if args.params:
        param_space_from_args = True
        params_path = Path(args.params)
        if not params_path.exists():
            console.print(f"[red]Params file not found: {params_path}[/red]")
            sys.exit(1)
        with open(params_path) as f:
            param_data = json.load(f)
        param_space = ParamSpace.from_dict(param_data)
        console.print(f"[green]✓[/green] Loaded {len(param_space.params)} parameters from params file")
    else:
        param_space_from_args = False
        # Auto-extract from netlist .param/parameters declarations
        try:
            netlist_content = netlist_path.read_text(encoding="utf-8")
            param_space = ParamSpace.from_netlist(
                netlist_content, max_per_finger=config.max_width_per_finger
            )
            console.print(f"[green]✓[/green] Auto-extracted {len(param_space.params)} parameters from netlist:")
            for p in param_space.params:
                extra = f" [max_per_finger={p.max_per_finger}]" if p.max_per_finger else ""
                console.print(f"     {p.name}: [{_eng_fmt(p.low)} ~ {_eng_fmt(p.high)}]{extra}")
        except ValueError as e:
            console.print(f"[red]Cannot auto-extract parameter space: {e}[/red]")
            console.print("[dim]Provide a --params JSON file or add parameters declarations to the netlist.[/dim]")
            sys.exit(1)

    # --- Build design targets ---
    original_requirement_text = ""
    if args.requirements:
        # Load from requirements.json (includes original text + targets)
        req_path = Path(args.requirements)
        if not req_path.exists():
            console.print(f"[red]Requirements file not found: {req_path}[/red]")
            sys.exit(1)
        with open(req_path) as f:
            req_data = json.load(f)
        original_requirement_text = req_data.get("original_requirement", "")
        t = req_data.get("targets", {})
        targets = DesignTarget(
            gain_db=t.get("gain_db"),
            bandwidth_hz=t.get("gbw_hz", t.get("bandwidth_hz")),
            phase_margin_deg=t.get("phase_margin_deg"),
            power_w=t.get("power_w"),
            load_cap_f=t.get("load_cap_f"),
            slew_rate_v_per_s=t.get("slew_rate_v_per_s"),
            settling_time_s=t.get("settling_time_s"),
            topology_hint=req_data.get("topology_hint", ""),
        )
    elif args.targets_json:
        targets_path = Path(args.targets_json)
        if not targets_path.exists():
            console.print(f"[red]Targets file not found: {targets_path}[/red]")
            sys.exit(1)
        with open(targets_path) as f:
            t = json.load(f)
        targets = DesignTarget(
            gain_db=t.get("gain_db"),
            bandwidth_hz=t.get("gbw_hz", t.get("bandwidth_hz")),
            phase_margin_deg=t.get("phase_margin_deg"),
            power_w=t.get("power_w"),
            load_cap_f=t.get("load_cap_f"),
            slew_rate_v_per_s=t.get("slew_rate_v_per_s"),
            settling_time_s=t.get("settling_time_s"),
            topology_hint=t.get("topology_hint", ""),
        )
    elif any([args.gain, args.bw, args.pm, args.power, args.sr, args.settling_time]):
        targets = DesignTarget(
            gain_db=args.gain,
            bandwidth_hz=args.bw,
            phase_margin_deg=args.pm,
            power_w=args.power,
            load_cap_f=args.load_cap,
            slew_rate_v_per_s=args.sr,
            settling_time_s=args.settling_time,
        )
    else:
        console.print(
            "[red]No targets specified. Use --targets-json or "
            "--gain/--gbw/--pm/--power/--sr/--settling-time.[/red]"
        )
        sys.exit(1)

    console.print(f"[green]✓[/green] Loaded netlist: {netlist_path}")
    console.print(f"[green]✓[/green] Loaded {len(param_space.params)} parameters")
    _display_targets(targets)

    # --- Derive project name ---
    if args.project:
        project_name = settings.sanitize_project_name(args.project)
    else:
        project_name = settings.sanitize_project_name(netlist_path.stem)

    _prepare_workspace_for_new_optimization(config)

    # --- Detect gm/Id mode ---
    gmid_sizer = None
    topo = None
    topology_name_val = ""
    if args.requirements:
        try:
            with open(args.requirements) as f:
                _req_data = json.load(f)
            topology_name_val = _req_data.get("topology_name", "")
        except Exception:
            pass

    if topology_name_val:
        try:
            from topologies import get_topology
            topo = get_topology(topology_name_val)
            pdk_errors = validate_pdk_profile(
                get_pdk_profile(),
                required_model_roles=topo.required_model_roles(),
            )
            if pdk_errors:
                raise ValueError(
                    "Active PDK profile is incompatible with "
                    f"'{topology_name_val}': " + "; ".join(pdk_errors)
                )
            gmid_spec = topo.get_gmid_spec(targets)
            if gmid_spec is not None:
                from gmid_lookup import get_lookup, GmidSizer
                lu = get_lookup()
                gmid_sizer = GmidSizer(gmid_spec, lu)
                # Use gm/Id param space instead of physical W/L
                param_space = gmid_spec.build_param_space()
                console.print(
                    f"[green]✓[/green] gm/Id mode enabled for "
                    f"'{topology_name_val}' ({len(param_space.params)} params)"
                )
            elif not param_space_from_args:
                param_space = topo.get_param_space()
                console.print(
                    f"[green]✓[/green] Using topology param space for "
                    f"'{topology_name_val}' ({len(param_space.params)} params)"
                )
        except Exception as e:
            console.print(f"[yellow]gm/Id mode unavailable: {e}[/yellow]")
            if not param_space_from_args:
                try:
                    from topologies import get_topology
                    topo = get_topology(topology_name_val)
                    param_space = topo.get_param_space()
                    console.print(
                        f"[green]✓[/green] Falling back to topology param "
                        f"space for '{topology_name_val}' "
                        f"({len(param_space.params)} params)"
                    )
                except Exception as topo_error:
                    console.print(
                        f"[yellow]Topology param space unavailable: "
                        f"{topo_error}[/yellow]"
                    )

    # Save requirements to workspace for traceability
    _save_requirements(targets, original_text=original_requirement_text, config=config, project_name=project_name)

    # Template must be DUT-only so circuit.cir doesn't include the testbench
    # (which includes circuit.cir, creating a self-inclusion loop)
    if circuit_files:
        template = NetlistTemplate.from_netlist(circuit_files.circuit_netlist)
    else:
        template = NetlistTemplate.from_netlist(netlist_content)

    # Save template to workspace
    workspace = config.get_workspace_path()
    template_path = workspace / "circuit_template.cir"
    template_path.write_text(template.template_content, encoding="utf-8")
    if circuit_files:
        for i, tb in enumerate(circuit_files.testbenches):
            suffix = "" if i == 0 else f"_{i}"
            (workspace / f"tb_template{suffix}.scs").write_text(tb, encoding="utf-8")

    if gmid_sizer is not None and topo is not None:
        _run_default_param_baseline(
            sim=sim,
            template=template,
            circuit_files=circuit_files,
            topology=topo,
            config=config,
            targets=targets,
        )

    # --- Run initial simulation ---
    console.print("\n[bold blue][Phase 1][/bold blue] Running initial Spectre simulation...")

    if gmid_sizer is not None:
        # gm/Id mode: initial params are gm_id/L/current, convert to W/L
        netlist_for_init = _combined_parameter_source(netlist_content, circuit_files)
        gmid_init = gmid_sizer.get_initial_gmid_params(netlist_for_init)
        initial_params = gmid_sizer.size(gmid_init)
    else:
        initial_params = param_space.get_initial_params(
            _combined_parameter_source(netlist_content, circuit_files)
        )

    run_dir = config.get_run_dir(0)
    if circuit_files and circuit_files.testbenches:
        tb_paths = sim.render_circuit_and_testbench(
            template, circuit_files.testbenches,
            initial_params, run_dir, param_space=param_space,
            w_l_grid_step=config.w_l_grid_step,
        )
    else:
        tb_paths = [run_dir / "circuit_init.scs"]
        sim.render_netlist(template, initial_params, tb_paths[0], param_space=param_space,
                           w_l_grid_step=config.w_l_grid_step)

    # Run primary simulation (with LLM repair on failure)
    success, log_content, error_msg = sim.run_spectre(tb_paths[0], run_dir)

    if not success:
        console.print(f"[yellow]Initial simulation failed: {error_msg[:100]}[/yellow]")
        console.print("[dim]Proceeding to optimization — BO may find better parameters.[/dim]")
        initial_result = SimResult(
            converged=False,
            error_message=error_msg[:500],
        )
    else:
        initial_result = sim.parse_simulation_results(
            log_content, run_dir, tb_paths[0]
        )
        # Run extra testbenches and merge results
        for tb_path in tb_paths[1:]:
            ok, log, _ = sim.run_spectre(tb_path, run_dir)
            if ok:
                extra = sim.parse_simulation_results(log, run_dir, tb_path)
                initial_result = SimResult.merge(initial_result, extra)
        console.print("[green]✓[/green] Simulation converged")
        _display_results_table(initial_result, targets, "Initial Results")

    if gmid_sizer is not None:
        _persist_initial_run(
            source_dir=run_dir,
            dest_dir=workspace / "initial_gmid",
            params=initial_params,
            result=initial_result,
            targets=targets,
            title="GM/Id Initial Simulation",
        )

    if success:
        all_met, _ = targets.is_satisfied(initial_result)
        if all_met:
            console.print("\n[bold green]All targets already met! No optimization needed.[/bold green]")
            _save_final_output(template, initial_params, initial_result, config,
                              circuit_files=circuit_files, param_space=param_space,
                              targets=targets, project_name=project_name,
                              original_requirement=original_requirement_text)
            return

    # --- Optimization loop ---
    console.print(
        f"\n[bold blue][Phase 2][/bold blue] Starting BO optimization "
        f"(max {config.max_iterations} iterations)...\n"
    )

    iteration_count = [0]

    def on_iteration(iteration, params, result, reward):
        iteration_count[0] = iteration + 1
        status = "[green]✓[/green]" if result.converged else "[red]✗[/red]"
        metrics = []
        if result.gain_db is not None:
            metrics.append(f"Gain={result.gain_db:.1f}dB")
        if result.bandwidth_hz is not None:
            metrics.append(f"GBW={_eng_fmt(result.bandwidth_hz)}Hz")
        if result.phase_margin_deg is not None:
            metrics.append(f"PM={result.phase_margin_deg:.1f}°")
        if result.power_w is not None:
            metrics.append(f"Power={_eng_fmt(result.power_w)}W")

        metric_str = " ".join(metrics) if metrics else "N/A"
        console.print(
            f"  Iter {iteration+1:3d}/{config.max_iterations}: "
            f"{status} {metric_str}  [dim][reward={reward:.1f}][/dim]"
        )

    state = optimizer.run_optimization_loop(
        template=template,
        param_space=param_space,
        targets=targets,
        circuit_files=circuit_files,
        on_iteration=on_iteration,
        topology_name=topology_name_val,
        gmid_sizer=gmid_sizer,
    )

    # --- Output results ---
    console.print("")
    best = state.best_record
    if best:
        best_physical_params = best.physical_params or best.params
        all_met, _ = targets.is_satisfied(best.result)
        if all_met:
            console.print(
                Panel(
                    f"[bold green]ALL TARGETS MET at iteration {best.iteration + 1}![/bold green]",
                    border_style="green",
                )
            )
        else:
            console.print(
                Panel(
                    f"[bold yellow]Optimization completed. Best result at iteration {best.iteration + 1}.[/bold yellow]",
                    border_style="yellow",
                )
            )

        _display_results_table(best.result, targets, "Final Results")
        _display_params(best_physical_params)
        _save_final_output(template, best_physical_params, best.result, config,
                          circuit_files=circuit_files, param_space=param_space,
                          targets=targets, project_name=project_name,
                          original_requirement=original_requirement_text)
    else:
        console.print("[red]Optimization produced no valid results.[/red]")


# --- Display helpers ---


def _save_requirements(targets: DesignTarget, original_text: str, config: Settings, project_name: str = "") -> None:
    """Save requirements.json to workspace for traceability."""
    workspace = config.get_workspace_path()
    req_path = workspace / "requirements.json"
    req_data = targets.to_requirements_dict(original_text=original_text)
    if project_name:
        req_data["project_name"] = project_name
    pdk = get_pdk_profile()
    req_data["pdk_profile"] = pdk.to_dict()
    req_path.write_text(json.dumps(req_data, indent=2, ensure_ascii=False), encoding="utf-8")
    (workspace / "pdk_profile_used.json").write_text(
        json.dumps(pdk.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    console.print(f"[green]✓[/green] Requirements saved to {req_path}")


def _prepare_workspace_for_new_optimization(config: Settings) -> None:
    """Remove stale per-run artifacts before starting a new optimization.

    Stable directories such as ``workspace/run_003`` are intentionally reused
    between invocations.  Cleaning them at startup prevents old raw PSF,
    diagnostics, and history files from being mistaken for results from the
    current run when iteration counts differ.
    """
    workspace = config.get_workspace_path()
    for run_dir in workspace.glob("run_[0-9][0-9][0-9]*"):
        if run_dir.is_dir():
            shutil.rmtree(run_dir, ignore_errors=True)
    for dirname in ("initial_default", "initial_gmid"):
        path = workspace / dirname
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    for file_name in (
        "history.json",
        "optimization_metrics.csv",
        "pdk_profile_used.json",
        "requirements.json",
        "circuit_template.cir",
    ):
        path = workspace / file_name
        if path.exists():
            path.unlink(missing_ok=True)
    for path in workspace.glob("tb_template*.scs"):
        path.unlink(missing_ok=True)


def _run_default_param_baseline(
    sim: Simulator,
    template: NetlistTemplate,
    circuit_files: CircuitFiles | None,
    topology,
    config: Settings,
    targets: DesignTarget,
) -> None:
    """Run and persist the topology DEFAULT_PARAMS baseline before gm/Id sizing."""
    if circuit_files is None or not circuit_files.testbenches:
        return
    if not hasattr(topology, "get_default_params"):
        return

    console.print(
        "\n[bold blue][Baseline][/bold blue] Running DEFAULT_PARAMS simulation..."
    )
    run_dir = config.get_workspace_path() / "initial_default"
    if run_dir.exists():
        shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True, exist_ok=True)

    default_params = topology.get_default_params()
    try:
        default_param_space = topology.get_param_space()
    except Exception:
        default_param_space = None

    tb_paths = sim.render_circuit_and_testbench(
        template,
        circuit_files.testbenches,
        default_params,
        run_dir,
        param_space=default_param_space,
        w_l_grid_step=config.w_l_grid_step,
    )

    success, log_content, error_msg = sim.run_spectre(tb_paths[0], run_dir)
    if success:
        baseline_result = sim.parse_simulation_results(
            log_content, run_dir, tb_paths[0]
        )
        for tb_path in tb_paths[1:]:
            ok, log, _ = sim.run_spectre(tb_path, run_dir)
            if ok:
                extra = sim.parse_simulation_results(log, run_dir, tb_path)
                baseline_result = SimResult.merge(baseline_result, extra)
        console.print("[green]✓[/green] DEFAULT_PARAMS baseline converged")
    else:
        baseline_result = SimResult(
            converged=False,
            error_message=error_msg[:500],
        )
        console.print(
            f"[yellow]DEFAULT_PARAMS baseline failed: {error_msg[:100]}[/yellow]"
        )

    (run_dir / "params.json").write_text(
        json.dumps(default_params, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    (run_dir / "result.json").write_text(
        json.dumps(
            baseline_result.to_result_dict(targets=targets, params=default_params),
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )
    (run_dir / "metrics_summary.txt").write_text(
        "\n".join([
            "DEFAULT_PARAMS Initial Simulation",
            "=" * 33,
            baseline_result.to_summary_str() or "No metrics parsed.",
        ]) + "\n",
        encoding="utf-8",
    )


def _persist_initial_run(
    source_dir: Path,
    dest_dir: Path,
    params: dict[str, float],
    result: SimResult,
    targets: DesignTarget,
    title: str,
) -> None:
    """Persist an initial simulation before BO reuses run_000."""
    if dest_dir.exists():
        shutil.rmtree(dest_dir, ignore_errors=True)
    shutil.copytree(source_dir, dest_dir, dirs_exist_ok=True)

    (dest_dir / "params.json").write_text(
        json.dumps(params, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    (dest_dir / "result.json").write_text(
        json.dumps(
            result.to_result_dict(targets=targets, params=params),
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )
    (dest_dir / "metrics_summary.txt").write_text(
        "\n".join([
            title,
            "=" * len(title),
            result.to_summary_str() or "No metrics parsed.",
        ]) + "\n",
        encoding="utf-8",
    )


def _print_header(config: Settings):
    """Print application header."""
    mode = "[DRY RUN]" if config.dry_run else ""
    pdk = get_pdk_profile()
    console.print(
        Panel(
            f"[bold]Circuit Design Agent[/bold] "
            f"({pdk.name} | DeepSeek + Optuna) {mode}",
            border_style="bright_blue",
        )
    )


def _display_targets(targets: DesignTarget):
    """Display parsed targets in a formatted panel."""
    table = Table(title="Parsed Design Targets", show_header=True)
    table.add_column("Specification", style="cyan")
    table.add_column("Value", style="white")

    if targets.topology_hint:
        table.add_row("Topology", targets.topology_hint)
    if targets.gain_db is not None:
        table.add_row("Gain", f">= {targets.gain_db} dB")
    if targets.bandwidth_hz is not None:
        table.add_row("GBW", f">= {_eng_fmt(targets.bandwidth_hz)}Hz")
    if targets.phase_margin_deg is not None:
        table.add_row("Phase Margin", f">= {targets.phase_margin_deg}°")
    if targets.power_w is not None:
        table.add_row("Power", f"<= {_eng_fmt(targets.power_w)}W")
    if targets.load_cap_f is not None:
        table.add_row("Load Cap", f"{_eng_fmt(targets.load_cap_f)}F")
    if targets.slew_rate_v_per_s is not None:
        table.add_row(
            "Slew Rate",
            f">= {_eng_fmt(targets.slew_rate_v_per_s)}V/s",
        )
    if targets.settling_time_s is not None:
        table.add_row(
            "Settling Time (0.1%)",
            f"<= {_eng_fmt(targets.settling_time_s)}s",
        )

    console.print(table)


def _display_results_table(result: SimResult, targets: DesignTarget, title: str):
    """Display simulation results vs targets in a rich table."""
    table = Table(title=title, show_header=True)
    table.add_column("Metric", style="cyan")
    table.add_column("Target", style="white")
    table.add_column("Actual", style="white")
    table.add_column("Status", justify="center")

    _, status = targets.is_satisfied(result)

    if targets.gain_db is not None:
        actual = f"{result.gain_db:.1f} dB" if result.gain_db is not None else "N/A"
        mark = "[green]✓[/green]" if status.get("gain_db") else "[red]✗[/red]"
        table.add_row("Gain", f">= {targets.gain_db} dB", actual, mark)

    if targets.bandwidth_hz is not None:
        actual = f"{_eng_fmt(result.bandwidth_hz)}Hz" if result.bandwidth_hz is not None else "N/A"
        mark = "[green]✓[/green]" if status.get("bandwidth_hz") else "[red]✗[/red]"
        table.add_row("GBW", f">= {_eng_fmt(targets.bandwidth_hz)}Hz", actual, mark)

    if targets.phase_margin_deg is not None:
        actual = f"{result.phase_margin_deg:.1f}°" if result.phase_margin_deg is not None else "N/A"
        mark = "[green]✓[/green]" if status.get("phase_margin_deg") else "[red]✗[/red]"
        table.add_row("Phase Margin", f">= {targets.phase_margin_deg}°", actual, mark)

    if targets.power_w is not None:
        actual = f"{_eng_fmt(result.power_w)}W" if result.power_w is not None else "N/A"
        mark = "[green]✓[/green]" if status.get("power_w") else "[red]✗[/red]"
        table.add_row("Power", f"<= {_eng_fmt(targets.power_w)}W", actual, mark)

    if targets.slew_rate_v_per_s is not None:
        actual = (
            f"{_eng_fmt(result.slew_rate_v_per_s)}V/s"
            if result.slew_rate_v_per_s is not None
            else "N/A"
        )
        if (
            result.slew_rate_positive_v_per_s is not None
            and result.slew_rate_negative_v_per_s is not None
        ):
            actual += (
                f" (SR+ {_eng_fmt(result.slew_rate_positive_v_per_s)}, "
                f"SR- {_eng_fmt(result.slew_rate_negative_v_per_s)})"
            )
        mark = (
            "[green]✓[/green]"
            if status.get("slew_rate_v_per_s")
            else "[red]✗[/red]"
        )
        table.add_row(
            "Slew Rate",
            f">= {_eng_fmt(targets.slew_rate_v_per_s)}V/s",
            actual,
            mark,
        )

    if targets.settling_time_s is not None:
        actual = (
            f"{_eng_fmt(result.settling_time_s)}s"
            if result.settling_time_s is not None
            else "N/A"
        )
        mark = (
            "[green]✓[/green]"
            if status.get("settling_time_s")
            else "[red]✗[/red]"
        )
        table.add_row(
            "Settling Time (0.1%)",
            f"<= {_eng_fmt(targets.settling_time_s)}s",
            actual,
            mark,
        )

    console.print(table)


def _display_params(params: dict[str, float]):
    """Display optimized parameters."""
    console.print("\n[bold]Optimized Parameters:[/bold]")
    parts = []
    for name, value in params.items():
        parts.append(f"{name}={_eng_fmt(value)}")
    console.print("  " + ", ".join(parts))


def _save_final_output(
    template: NetlistTemplate,
    params: dict[str, float],
    result: SimResult,
    config: Settings,
    circuit_files: CircuitFiles | None = None,
    param_space: ParamSpace | None = None,
    targets: DesignTarget | None = None,
    project_name: str = "",
    original_requirement: str = "",
):
    """Save final netlist, report, and structured JSON to project directory.

    Structure:
        outputs/<project_name>/
        ├── netlist/circuit.cir
        ├── simulation/tb_circuit_ac.scs
        ├── data/
        ├── results.json
        ├── summary_report.txt
        └── optimization_log.json
    """
    if not project_name:
        project_name = config.sanitize_project_name("circuit")

    project_root = config.get_project_path(project_name)
    netlist_dir = config.get_project_netlist_path(project_name)
    sim_dir = config.get_project_simulation_path(project_name)
    data_dir = config.get_project_data_path(project_name)
    pdk = get_pdk_profile()
    pdk_profile_path = project_root / "pdk_profile_used.json"
    pdk_profile_path.write_text(
        json.dumps(pdk.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # 1. Save final rendered circuit netlist
    final_circuit = template.render(
        params,
        param_space=param_space,
        max_width_per_finger=config.max_width_per_finger,
        w_l_grid_step=config.w_l_grid_step,
    )
    circuit_path = netlist_dir / "circuit.cir"
    circuit_path.write_text(final_circuit, encoding="utf-8")

    # 2. Save testbenches
    if circuit_files and circuit_files.testbenches:
        for i, tb in enumerate(circuit_files.testbenches):
            suffix = "" if i == 0 else f"_{i}"
            tb_path = sim_dir / f"tb_circuit{suffix}.scs"
            rendered_tb = NetlistTemplate.from_netlist(tb).render(params)
            tb_path.write_text(rendered_tb, encoding="utf-8")

    # 3. Copy simulation data from best run (look for the latest history)
    workspace = config.get_workspace_path()
    history_file = workspace / "history.json"
    best_iter = 0
    if history_file.exists():
        try:
            history_data = json.loads(history_file.read_text(encoding="utf-8"))
            best_iter = history_data.get("best_iteration", 0)
        except Exception:
            pass

    best_run_dir = config.get_run_dir(best_iter)
    diagnostics_dir = project_root / "diagnostics"
    diagnostics_paths: dict[str, str] = {}
    initial_default_dir = workspace / "initial_default"
    initial_default_paths: dict[str, str] = {}
    initial_gmid_dir = workspace / "initial_gmid"
    initial_gmid_paths: dict[str, str] = {}
    if best_run_dir.exists():
        sim_log = best_run_dir / "sim.log"
        if sim_log.exists():
            shutil.copy2(sim_log, data_dir / "sim.log")
        raw_dir = best_run_dir / "raw"
        if raw_dir.exists():
            if (data_dir / "raw").exists():
                shutil.rmtree(data_dir / "raw", ignore_errors=True)
            shutil.copytree(raw_dir, data_dir / "raw", dirs_exist_ok=True)
        run_diagnostics = best_run_dir / "diagnostics"
        if run_diagnostics.exists():
            if diagnostics_dir.exists():
                shutil.rmtree(diagnostics_dir, ignore_errors=True)
            shutil.copytree(run_diagnostics, diagnostics_dir, dirs_exist_ok=True)
            dc_path = diagnostics_dir / "dc_operating_points.csv"
            ac_path = diagnostics_dir / "ac_response.csv"
            summary_path = diagnostics_dir / "diagnostics_summary.txt"
            if dc_path.exists():
                diagnostics_paths["dc_operating_points"] = str(dc_path)
            if ac_path.exists():
                diagnostics_paths["ac_response"] = str(ac_path)
            if summary_path.exists():
                diagnostics_paths["summary"] = str(summary_path)

    if initial_default_dir.exists():
        project_initial_dir = project_root / "initial_default"
        if project_initial_dir.exists():
            shutil.rmtree(project_initial_dir, ignore_errors=True)
        shutil.copytree(initial_default_dir, project_initial_dir, dirs_exist_ok=True)
        for name in (
            "circuit.cir",
            "tb.scs",
            "tb_1.scs",
            "tb_2.scs",
            "sim.log",
            "params.json",
            "result.json",
            "metrics_summary.txt",
        ):
            path = project_initial_dir / name
            if path.exists():
                initial_default_paths[name] = str(path)
        diagnostics_summary = (
            project_initial_dir / "diagnostics" / "diagnostics_summary.txt"
        )
        if diagnostics_summary.exists():
            initial_default_paths["diagnostics_summary"] = str(diagnostics_summary)

    if initial_gmid_dir.exists():
        project_initial_gmid_dir = project_root / "initial_gmid"
        if project_initial_gmid_dir.exists():
            shutil.rmtree(project_initial_gmid_dir, ignore_errors=True)
        shutil.copytree(initial_gmid_dir, project_initial_gmid_dir, dirs_exist_ok=True)
        for name in (
            "circuit.cir",
            "tb.scs",
            "tb_1.scs",
            "tb_2.scs",
            "sim.log",
            "params.json",
            "result.json",
            "metrics_summary.txt",
        ):
            path = project_initial_gmid_dir / name
            if path.exists():
                initial_gmid_paths[name] = str(path)
        diagnostics_summary = (
            project_initial_gmid_dir / "diagnostics" / "diagnostics_summary.txt"
        )
        if diagnostics_summary.exists():
            initial_gmid_paths["diagnostics_summary"] = str(diagnostics_summary)

    # 4. Save structured JSON result
    result_data = result.to_result_dict(targets=targets, params=params)
    result_data["netlist_file"] = str(circuit_path)
    result_data["project_name"] = project_name
    result_data["pdk_profile"] = pdk.to_dict()
    result_data["pdk_profile_file"] = str(pdk_profile_path)
    if diagnostics_paths:
        result_data["diagnostics"] = diagnostics_paths
    if initial_default_paths:
        result_data["initial_default"] = initial_default_paths
    if initial_gmid_paths:
        result_data["initial_gmid"] = initial_gmid_paths
    if original_requirement:
        result_data["original_requirement"] = original_requirement
    result_path = project_root / "results.json"
    result_path.write_text(
        json.dumps(result_data, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    # 5. Export Virtuoso SKILL schematic script (best-effort; no Cadence required)
    virtuoso_report = None
    try:
        from virtuoso_export import export_netlist

        virtuoso_report = export_netlist(
            netlist_path=circuit_path,
            lib_name="BO_Designs",
            cell_name=f"{project_name}_opt",
            out_path=project_root / "virtuoso" / "import_schematic.il",
            results_path=result_path,
        )
        result_data["virtuoso_export"] = {
            "skill_file": virtuoso_report["skill_file"],
            "report_file": str(project_root / "virtuoso" / "export_report.json"),
            "target": (
                f"{virtuoso_report['target_lib']}/"
                f"{virtuoso_report['target_cell']}/schematic"
            ),
        }
        result_path.write_text(
            json.dumps(result_data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        console.print(f"[yellow]Virtuoso export skipped:[/yellow] {e}")

    # 6. Save summary report
    report_lines = [
        "=" * 60,
        "Circuit Optimization Report",
        "=" * 60,
        "",
        f"Project: {project_name}",
        f"PDK Profile: {pdk.name}",
        "",
    ]
    if original_requirement:
        report_lines.append(f"Original Requirement: {original_requirement}")
        report_lines.append("")
    report_lines += [
        "Final Performance:",
        f"  Gain:         {result.gain_db:.1f} dB" if result.gain_db else "",
        f"  GBW:          {_eng_fmt(result.bandwidth_hz)}Hz" if result.bandwidth_hz else "",
        f"  Phase Margin: {result.phase_margin_deg:.1f} deg" if result.phase_margin_deg else "",
        f"  Power:        {_eng_fmt(result.power_w)}W" if result.power_w else "",
        f"  Slew Rate:    {_eng_fmt(result.slew_rate_v_per_s)}V/s"
        if result.slew_rate_v_per_s else "",
        f"  SR+:          {_eng_fmt(result.slew_rate_positive_v_per_s)}V/s"
        if result.slew_rate_positive_v_per_s else "",
        f"  SR-:          {_eng_fmt(result.slew_rate_negative_v_per_s)}V/s"
        if result.slew_rate_negative_v_per_s else "",
        f"  Settling 0.1%:{_eng_fmt(result.settling_time_s)}s"
        if result.settling_time_s else "",
        "",
        "Optimized Parameters:",
    ]
    for name, value in params.items():
        report_lines.append(f"  {name} = {_eng_fmt(value)}")

    if targets:
        gap = targets.compute_gap(result)
        all_met, status = targets.is_satisfied(result)
        report_lines.append("")
        report_lines.append("Target Gap Analysis:")
        for metric, gap_val in gap.items():
            if gap_val is not None:
                met = status.get(metric, False)
                mark = "✓" if met else "✗"
                report_lines.append(f"  {mark} {metric}: gap={gap_val:+.2f}")
        report_lines.append(f"\nAll targets met: {'YES' if all_met else 'NO'}")

    if virtuoso_report:
        report_lines.append("")
        report_lines.append("Virtuoso Export:")
        report_lines.append(f"  SKILL: {virtuoso_report['skill_file']}")
        report_lines.append(
            "  Target: "
            f"{virtuoso_report['target_lib']}/{virtuoso_report['target_cell']}/schematic"
        )

    report_lines.append("")
    report_lines.append("=" * 60)

    report_path = project_root / "summary_report.txt"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")

    # 7. Copy optimization history
    if history_file.exists():
        shutil.copy2(history_file, project_root / "optimization_log.json")
    metrics_csv = workspace / "optimization_metrics.csv"
    if metrics_csv.exists():
        shutil.copy2(metrics_csv, project_root / "optimization_metrics.csv")

    # 8. Write .last_project marker
    last_project_file = config.get_outputs_path() / ".last_project"
    last_project_file.write_text(project_name, encoding="utf-8")

    # 9. Print summary
    console.print(f"\n[bold green]Project saved to:[/bold green] {project_root}")
    console.print(f"\n[bold]Files:[/bold]")
    console.print(f"  • {circuit_path}")
    if circuit_files and circuit_files.testbenches:
        for i, tb in enumerate(circuit_files.testbenches):
            suffix = "" if i == 0 else f"_{i}"
            console.print(f"  • {sim_dir / f'tb_circuit{suffix}.scs'}")
    console.print(f"  • {result_path}")
    console.print(f"  • {report_path}")
    console.print(f"  • {pdk_profile_path}")
    if virtuoso_report:
        console.print(f"  • {virtuoso_report['skill_file']}")
        console.print(f"  • {project_root / 'virtuoso' / 'export_report.json'}")
    if history_file.exists():
        console.print(f"  • {project_root / 'optimization_log.json'}")
    if metrics_csv.exists():
        console.print(f"  • {project_root / 'optimization_metrics.csv'}")
    if diagnostics_paths:
        console.print(f"  • {diagnostics_dir}")
    if initial_default_paths:
        console.print(f"  • {project_root / 'initial_default'}")
    if initial_gmid_paths:
        console.print(f"  • {project_root / 'initial_gmid'}")
    console.print(f"\n[dim]cd {project_root}[/dim]")


def _build_circuit_files(netlist_content: str) -> CircuitFiles | None:
    """Attempt to split a netlist into circuit + testbench.

    Returns None if the netlist can't be split (no subckt found).
    """
    try:
        circuit, testbench = LLMClient._split_monolithic_netlist(netlist_content)
        circuit_name = CircuitFiles.extract_subckt_name(circuit)
        return CircuitFiles(
            circuit_netlist=circuit,
            testbenches=[testbench],
            circuit_name=circuit_name,
        )
    except Exception:
        return None


def _combined_parameter_source(
    netlist_content: str,
    circuit_files: CircuitFiles | None,
) -> str:
    """Combine DUT and testbench declarations for initial parameter lookup."""
    if not circuit_files:
        return netlist_content
    return "\n".join([circuit_files.circuit_netlist, *circuit_files.testbenches])


def _eng_fmt(value: float | None) -> str:
    """Quick engineering format."""
    if value is None:
        return "N/A"
    from utils import format_engineering
    return format_engineering(value)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Circuit Agent batch optimizer for generated netlist projects"
    )

    # --- File mode arguments ---
    parser.add_argument(
        "--netlist", type=str, default=None,
        help="Path to a pre-made SPICE netlist file (.cir). Enables file mode."
    )
    parser.add_argument(
        "--testbench", type=str, nargs='*', default=None,
        help="One or more Spectre testbench .scs files. When provided, --netlist is treated "
             "as DUT-only .cir (no monolithic split). The first testbench is the "
             "primary (typically AC); additional ones (e.g. transient) are run "
             "sequentially and their results are merged."
    )
    parser.add_argument(
        "--project", type=str, default=None,
        help="Project name for output directory (file mode). Derived from netlist filename if not set."
    )
    parser.add_argument(
        "--params", type=str, default=None,
        help="Path to JSON file defining parameter search space. "
             "If omitted, parameters are auto-extracted from the netlist's "
             "parameters declarations with sensible default bounds."
    )
    parser.add_argument(
        "--requirements", type=str, default=None,
        help="Path to JSON file with original requirement text and design targets"
    )
    parser.add_argument(
        "--targets-json", type=str, default=None,
        help="Path to JSON file with design targets (legacy, prefer --requirements)"
    )

    # --- Quick target shortcuts (alternatives to --targets-json) ---
    parser.add_argument("--gain", type=float, default=None, help="Min gain in dB")
    parser.add_argument(
        "--bw", "--gbw", dest="bw", type=float, default=None,
        help="Minimum gain-bandwidth product / unity-gain frequency in Hz",
    )
    parser.add_argument("--pm", type=float, default=None, help="Min phase margin in degrees")
    parser.add_argument("--power", type=float, default=None, help="Max power in W")
    parser.add_argument("--load-cap", type=float, default=None, help="Load capacitance in F")
    parser.add_argument(
        "--sr", type=float, default=None,
        help="Minimum worst-case slew rate min(SR+, SR-) in V/s",
    )
    parser.add_argument(
        "--settling-time", type=float, default=None,
        help="Maximum 0.1%% settling time in seconds",
    )

    # --- General options ---
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run in mock mode without Spectre (for testing)"
    )
    parser.add_argument(
        "--max-iter", type=int, default=None,
        help="Override maximum optimization iterations"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable verbose debug logging"
    )
    return parser.parse_args()


if __name__ == "__main__":
    main()
