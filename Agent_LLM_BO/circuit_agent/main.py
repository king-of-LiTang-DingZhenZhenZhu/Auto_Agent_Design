"""Circuit Design Agent - Main entry point.

Supports two modes:
1. Interactive mode (default): Dialogue to collect requirements, LLM generates netlist.
2. File mode (--netlist): Provide pre-made netlist + param space, skip LLM generation.
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
from models import CircuitFiles, DesignTarget, NetlistTemplate, OptimizationState, ParamSpace, SimResult
from optimizer import HybridOptimizer
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
    llm = LLMClient(config)
    sim = Simulator(config)
    optimizer = HybridOptimizer(llm, sim, config)

    if args.netlist:
        # --- File mode: user provides netlist directly ---
        run_from_file(args, llm, sim, optimizer, config)
    else:
        # --- Interactive mode: LLM generates netlist ---
        targets, original_text, project_name = conduct_dialogue(llm)
        if not targets:
            console.print("[yellow]No targets specified. Exiting.[/yellow]")
            return
        # Save requirements for traceability
        _save_requirements(targets, original_text=original_text, config=config, project_name=project_name)
        run_pipeline(targets, llm, sim, optimizer, config, project_name=project_name)


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

    # --- Load parameter search space ---
    if args.params:
        params_path = Path(args.params)
        if not params_path.exists():
            console.print(f"[red]Params file not found: {params_path}[/red]")
            sys.exit(1)
        with open(params_path) as f:
            param_data = json.load(f)
        param_space = ParamSpace.from_dict(param_data)
    else:
        console.print("[red]--params is required in file mode. Provide a JSON file defining the search space.[/red]")
        console.print('[dim]Example: [{"name": "W1", "low": 0.5e-6, "high": 20e-6, "log_scale": true}][/dim]')
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
            bandwidth_hz=t.get("bandwidth_hz"),
            phase_margin_deg=t.get("phase_margin_deg"),
            power_w=t.get("power_w"),
            load_cap_f=t.get("load_cap_f"),
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
            bandwidth_hz=t.get("bandwidth_hz"),
            phase_margin_deg=t.get("phase_margin_deg"),
            power_w=t.get("power_w"),
            load_cap_f=t.get("load_cap_f"),
            topology_hint=t.get("topology_hint", ""),
        )
    elif any([args.gain, args.bw, args.pm, args.power]):
        targets = DesignTarget(
            gain_db=args.gain,
            bandwidth_hz=args.bw,
            phase_margin_deg=args.pm,
            power_w=args.power,
            load_cap_f=args.load_cap,
        )
    else:
        console.print("[red]No targets specified. Use --targets-json or --gain/--bw/--pm/--power.[/red]")
        sys.exit(1)

    console.print(f"[green]✓[/green] Loaded netlist: {netlist_path}")
    console.print(f"[green]✓[/green] Loaded {len(param_space.params)} parameters")
    _display_targets(targets)

    # --- Derive project name ---
    if args.project:
        project_name = settings.sanitize_project_name(args.project)
    else:
        project_name = settings.sanitize_project_name(netlist_path.stem)

    # Save requirements to workspace for traceability
    _save_requirements(targets, original_text=original_requirement_text, config=config, project_name=project_name)

    # --- Build template from user's netlist ---
    netlist_content = netlist_path.read_text(encoding="utf-8")
    template = NetlistTemplate.from_netlist(netlist_content)

    # Try to split into circuit + testbench, or wrap as monolithic
    circuit_files = _build_circuit_files(netlist_content)

    # Save template to workspace
    workspace = config.get_workspace_path()
    template_path = workspace / "circuit_template.sp"
    template_path.write_text(template.template_content, encoding="utf-8")
    if circuit_files:
        (workspace / "tb_template.sp").write_text(circuit_files.testbench, encoding="utf-8")

    # --- Run initial simulation ---
    console.print("\n[bold blue][Phase 1][/bold blue] Running initial Spectre simulation...")

    initial_params = {}
    for p in param_space.params:
        if p.log_scale:
            import math
            initial_params[p.name] = math.exp(
                (math.log(p.low) + math.log(p.high)) / 2
            )
        else:
            initial_params[p.name] = (p.low + p.high) / 2

    run_dir = config.get_run_dir(0)
    if circuit_files and circuit_files.testbench:
        netlist_path = sim.render_circuit_and_testbench(
            template, circuit_files.testbench,
            initial_params, run_dir, param_space=param_space,
        )
    else:
        netlist_path = run_dir / "circuit_init.spi"
        sim.render_netlist(template, initial_params, netlist_path, param_space=param_space)

    success, log_content, error_msg = sim.run_spectre(netlist_path, run_dir)

    if not success:
        console.print(f"[yellow]Initial simulation failed: {error_msg[:100]}[/yellow]")
        console.print("[dim]Attempting LLM repair before optimization...[/dim]")
        netlist_content = netlist_path.read_text(encoding="utf-8")
        try:
            repaired = llm.repair_netlist(netlist_content, log_content or error_msg, 1)
            netlist_path.write_text(repaired, encoding="utf-8")
            template = NetlistTemplate.from_netlist(repaired)
            success, log_content, error_msg = sim.run_spectre(netlist_path, run_dir)
        except Exception:
            pass

    if success:
        initial_result = sim.parse_simulation_log(log_content)
        console.print("[green]✓[/green] Simulation converged")
        _display_results_table(initial_result, targets, "Initial Results")
        all_met, _ = targets.is_satisfied(initial_result)
        if all_met:
            console.print("\n[bold green]All targets already met! No optimization needed.[/bold green]")
            _save_final_output(template, initial_params, initial_result, config,
                              circuit_files=circuit_files, param_space=param_space,
                              targets=targets, project_name=project_name,
                              original_requirement=original_requirement_text)
            return
    else:
        console.print("[yellow]Initial simulation did not converge. Proceeding to optimization...[/yellow]")

    # --- Optimization loop ---
    console.print(
        f"\n[bold blue][Phase 2][/bold blue] Starting LLM+BO optimization "
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
            metrics.append(f"BW={_eng_fmt(result.bandwidth_hz)}Hz")
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
    )

    # --- Output results ---
    console.print("")
    best = state.best_record
    if best:
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
        _display_params(best.params)
        _save_final_output(template, best.params, best.result, config,
                          circuit_files=circuit_files, param_space=param_space,
                          targets=targets, project_name=project_name,
                          original_requirement=original_requirement_text)
    else:
        console.print("[red]Optimization produced no valid results.[/red]")


def conduct_dialogue(llm: LLMClient) -> tuple[DesignTarget | None, str, str]:
    """Interactive dialogue to collect circuit requirements from user.

    Returns:
        (targets, original_text, project_name)
    """
    console.print(
        "\n[bold cyan]Please describe the circuit you want to design.[/bold cyan]"
    )
    console.print(
        "Include: topology, performance targets (gain, BW, PM, power), "
        "and load conditions.\n"
    )
    console.print(
        "[dim]Example: Design a 5T OTA, gain > 40dB, BW > 500MHz, "
        "PM > 60 degrees, power < 1mW, load cap 500fF[/dim]\n"
    )

    user_input = console.input("[bold green]Your requirements > [/bold green]")

    if not user_input.strip():
        return None, "", ""

    console.print("\n[dim]Parsing requirements...[/dim]")

    project_name = ""
    try:
        targets, project_name = llm.parse_user_requirements(user_input)
    except Exception as e:
        console.print(f"[red]Failed to parse requirements: {e}[/red]")
        console.print("Please try again with more specific numbers.")
        return None, "", ""

    # Sanitize project name
    project_name = settings.sanitize_project_name(project_name)

    # Display parsed targets for confirmation
    _display_targets(targets)

    confirm = console.input("\n[bold]Proceed with these targets? (yes/no/modify) > [/bold]")
    if confirm.strip().lower() in ("n", "no"):
        return None, user_input, project_name
    if confirm.strip().lower() in ("m", "modify"):
        console.print("[yellow]Please re-enter your requirements:[/yellow]")
        user_input = console.input("[bold green]Your requirements > [/bold green]")
        if user_input.strip():
            try:
                targets, project_name = llm.parse_user_requirements(user_input)
                project_name = settings.sanitize_project_name(project_name)
                _display_targets(targets)
            except Exception:
                return None, user_input, project_name

    return targets, user_input, project_name


def run_pipeline(
    targets: DesignTarget,
    llm: LLMClient,
    sim: Simulator,
    optimizer: HybridOptimizer,
    config: Settings,
    project_name: str = "",
) -> None:
    """Execute the full optimization pipeline."""

    # --- Phase 1: Generate initial netlist ---
    console.print("\n[bold blue][Phase 1][/bold blue] Generating initial SPICE netlist...")

    try:
        circuit_files, param_space = llm.generate_initial_netlist(targets)
    except Exception as e:
        console.print(f"[red]Failed to generate initial netlist: {e}[/red]")
        return

    template = NetlistTemplate.from_netlist(circuit_files.circuit_netlist)

    console.print(f"[green]✓[/green] Netlist generated with {len(param_space.params)} optimizable parameters:")
    console.print(f"     Subcircuit: [cyan]{circuit_files.circuit_name}[/cyan]")
    for p in param_space.params:
        console.print(f"  {p.name}: [{_eng_fmt(p.low)} ~ {_eng_fmt(p.high)}]")

    # Save initial template and testbench
    workspace = config.get_workspace_path()
    template_path = workspace / "circuit_template.sp"
    template_path.write_text(circuit_files.circuit_netlist, encoding="utf-8")
    tb_path = workspace / "tb_template.sp"
    tb_path.write_text(circuit_files.testbench, encoding="utf-8")

    # --- Phase 2: Initial simulation ---
    console.print("\n[bold blue][Phase 2][/bold blue] Running initial Spectre simulation...")

    # Use midpoint of parameter ranges as initial values
    initial_params = {}
    for p in param_space.params:
        if p.log_scale:
            import math
            initial_params[p.name] = math.exp(
                (math.log(p.low) + math.log(p.high)) / 2
            )
        else:
            initial_params[p.name] = (p.low + p.high) / 2

    run_dir = config.get_run_dir(0)
    netlist_path = sim.render_circuit_and_testbench(
        template, circuit_files.testbench,
        initial_params, run_dir, param_space=param_space,
    )

    success, log_content, error_msg = sim.run_spectre(netlist_path, run_dir)

    if not success:
        console.print(f"[yellow]Initial simulation failed: {error_msg[:100]}[/yellow]")
        console.print("[dim]Attempting LLM repair before optimization...[/dim]")
        # Try repair of circuit + testbench
        circuit_content = (run_dir / "circuit.cir").read_text(encoding="utf-8")
        tb_content = (run_dir / "tb.sp").read_text(encoding="utf-8")
        try:
            repaired = llm.repair_netlist(circuit_content, log_content or error_msg, 1, testbench=tb_content)
            if isinstance(repaired, tuple):
                (run_dir / "circuit.cir").write_text(repaired[0], encoding="utf-8")
                (run_dir / "tb.sp").write_text(repaired[1], encoding="utf-8")
                template = NetlistTemplate.from_netlist(repaired[0])
                circuit_files = CircuitFiles(
                    circuit_netlist=repaired[0],
                    testbench=repaired[1],
                    circuit_name=CircuitFiles.extract_subckt_name(repaired[0]),
                )
            else:
                netlist_path.write_text(repaired, encoding="utf-8")
                template = NetlistTemplate.from_netlist(repaired)
            success, log_content, error_msg = sim.run_spectre(netlist_path, run_dir)
        except Exception:
            pass

    if success:
        initial_result = sim.parse_simulation_log(log_content)
        console.print(f"[green]✓[/green] Simulation converged")
        _display_results_table(initial_result, targets, "Initial Results")

        # Check if already meeting targets
        all_met, _ = targets.is_satisfied(initial_result)
        if all_met:
            console.print("\n[bold green]All targets already met! No optimization needed.[/bold green]")
            _save_final_output(template, initial_params, initial_result, config,
                              circuit_files=circuit_files, param_space=param_space,
                              targets=targets, project_name=project_name)
            return
    else:
        console.print("[yellow]Initial simulation did not converge. Proceeding to optimization...[/yellow]")

    # --- Phase 3: Optimization loop ---
    console.print(
        f"\n[bold blue][Phase 3][/bold blue] Starting LLM+BO optimization "
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
            metrics.append(f"BW={_eng_fmt(result.bandwidth_hz)}Hz")
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
    )

    # --- Phase 4: Output results ---
    console.print("")
    best = state.best_record
    if best:
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
        _display_params(best.params)
        _save_final_output(template, best.params, best.result, config,
                          circuit_files=circuit_files, param_space=param_space,
                          targets=targets, project_name=project_name)
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
    req_path.write_text(json.dumps(req_data, indent=2, ensure_ascii=False), encoding="utf-8")
    console.print(f"[green]✓[/green] Requirements saved to {req_path}")


def _print_header(config: Settings):
    """Print application header."""
    mode = "[DRY RUN]" if config.dry_run else ""
    console.print(
        Panel(
            f"[bold]Circuit Design Agent[/bold] (TSMC N28 | DeepSeek + Optuna) {mode}",
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
        table.add_row("Bandwidth", f">= {_eng_fmt(targets.bandwidth_hz)}Hz")
    if targets.phase_margin_deg is not None:
        table.add_row("Phase Margin", f">= {targets.phase_margin_deg}°")
    if targets.power_w is not None:
        table.add_row("Power", f"<= {_eng_fmt(targets.power_w)}W")
    if targets.load_cap_f is not None:
        table.add_row("Load Cap", f"{_eng_fmt(targets.load_cap_f)}F")

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
        table.add_row("Bandwidth", f">= {_eng_fmt(targets.bandwidth_hz)}Hz", actual, mark)

    if targets.phase_margin_deg is not None:
        actual = f"{result.phase_margin_deg:.1f}°" if result.phase_margin_deg is not None else "N/A"
        mark = "[green]✓[/green]" if status.get("phase_margin_deg") else "[red]✗[/red]"
        table.add_row("Phase Margin", f">= {targets.phase_margin_deg}°", actual, mark)

    if targets.power_w is not None:
        actual = f"{_eng_fmt(result.power_w)}W" if result.power_w is not None else "N/A"
        mark = "[green]✓[/green]" if status.get("power_w") else "[red]✗[/red]"
        table.add_row("Power", f"<= {_eng_fmt(targets.power_w)}W", actual, mark)

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
        ├── simulation/tb_circuit_ac.sp
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

    # 1. Save final rendered circuit netlist
    final_circuit = template.render(
        params,
        param_space=param_space,
        max_width_per_finger=config.max_width_per_finger,
    )
    circuit_path = netlist_dir / "circuit.cir"
    circuit_path.write_text(final_circuit, encoding="utf-8")

    # 2. Save testbench
    if circuit_files and circuit_files.testbench:
        tb_path = sim_dir / "tb_circuit_ac.sp"
        tb_path.write_text(circuit_files.testbench, encoding="utf-8")

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
    if best_run_dir.exists():
        sim_log = best_run_dir / "sim.log"
        if sim_log.exists():
            shutil.copy2(sim_log, data_dir / "sim.log")
        raw_dir = best_run_dir / "raw"
        if raw_dir.exists():
            if (data_dir / "raw").exists():
                shutil.rmtree(data_dir / "raw", ignore_errors=True)
            shutil.copytree(raw_dir, data_dir / "raw", dirs_exist_ok=True)

    # 4. Save structured JSON result
    result_data = result.to_result_dict(targets=targets, params=params)
    result_data["netlist_file"] = str(circuit_path)
    result_data["project_name"] = project_name
    if original_requirement:
        result_data["original_requirement"] = original_requirement
    result_path = project_root / "results.json"
    result_path.write_text(
        json.dumps(result_data, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    # 5. Save summary report
    report_lines = [
        "=" * 60,
        "Circuit Optimization Report",
        "=" * 60,
        "",
        f"Project: {project_name}",
        "",
    ]
    if original_requirement:
        report_lines.append(f"Original Requirement: {original_requirement}")
        report_lines.append("")
    report_lines += [
        "Final Performance:",
        f"  Gain:         {result.gain_db:.1f} dB" if result.gain_db else "",
        f"  Bandwidth:    {_eng_fmt(result.bandwidth_hz)}Hz" if result.bandwidth_hz else "",
        f"  Phase Margin: {result.phase_margin_deg:.1f} deg" if result.phase_margin_deg else "",
        f"  Power:        {_eng_fmt(result.power_w)}W" if result.power_w else "",
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

    report_lines.append("")
    report_lines.append("=" * 60)

    report_path = project_root / "summary_report.txt"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")

    # 6. Copy optimization history
    if history_file.exists():
        shutil.copy2(history_file, project_root / "optimization_log.json")

    # 7. Write .last_project marker
    last_project_file = config.get_outputs_path() / ".last_project"
    last_project_file.write_text(project_name, encoding="utf-8")

    # 8. Print summary
    console.print(f"\n[bold green]Project saved to:[/bold green] {project_root}")
    console.print(f"\n[bold]Files:[/bold]")
    console.print(f"  • {circuit_path}")
    if circuit_files and circuit_files.testbench:
        console.print(f"  • {tb_path}")
    console.print(f"  • {result_path}")
    console.print(f"  • {report_path}")
    if history_file.exists():
        console.print(f"  • {project_root / 'optimization_log.json'}")
    console.print(f"\n[dim]cd {project_root}[/dim]")


def _build_circuit_files(netlist_content: str) -> CircuitFiles | None:
    """Attempt to split a netlist into circuit + testbench.

    Returns None if the netlist can't be split (no .subckt found).
    """
    try:
        circuit, testbench = LLMClient._split_monolithic_netlist(netlist_content)
        circuit_name = CircuitFiles.extract_subckt_name(circuit)
        return CircuitFiles(
            circuit_netlist=circuit,
            testbench=testbench,
            circuit_name=circuit_name,
        )
    except Exception:
        return None


def _eng_fmt(value: float | None) -> str:
    """Quick engineering format."""
    if value is None:
        return "N/A"
    from utils import format_engineering
    return format_engineering(value)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Circuit Design Agent - LLM + BO Automated Optimization"
    )

    # --- File mode arguments ---
    parser.add_argument(
        "--netlist", type=str, default=None,
        help="Path to a pre-made SPICE netlist file (.sp/.spi). Enables file mode."
    )
    parser.add_argument(
        "--project", type=str, default=None,
        help="Project name for output directory (file mode). Derived from netlist filename if not set."
    )
    parser.add_argument(
        "--params", type=str, default=None,
        help="Path to JSON file defining parameter search space (required in file mode)"
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
    parser.add_argument("--bw", type=float, default=None, help="Min bandwidth in Hz")
    parser.add_argument("--pm", type=float, default=None, help="Min phase margin in degrees")
    parser.add_argument("--power", type=float, default=None, help="Max power in W")
    parser.add_argument("--load-cap", type=float, default=None, help="Load capacitance in F")

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
