"""Circuit Design Agent - Main entry point.

Interactive terminal dialogue for automated circuit optimization using LLM + BO.
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
from models import DesignTarget, NetlistTemplate, OptimizationState, SimResult
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

    # Phase 1: Collect user requirements
    targets = conduct_dialogue(llm)
    if not targets:
        console.print("[yellow]No targets specified. Exiting.[/yellow]")
        return

    # Phase 2-4: Run pipeline
    run_pipeline(targets, llm, sim, optimizer, config)


def conduct_dialogue(llm: LLMClient) -> DesignTarget | None:
    """Interactive dialogue to collect circuit requirements from user."""
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
        return None

    console.print("\n[dim]Parsing requirements...[/dim]")

    try:
        targets = llm.parse_user_requirements(user_input)
    except Exception as e:
        console.print(f"[red]Failed to parse requirements: {e}[/red]")
        console.print("Please try again with more specific numbers.")
        return None

    # Display parsed targets for confirmation
    _display_targets(targets)

    confirm = console.input("\n[bold]Proceed with these targets? (yes/no/modify) > [/bold]")
    if confirm.strip().lower() in ("n", "no"):
        return None
    if confirm.strip().lower() in ("m", "modify"):
        console.print("[yellow]Please re-enter your requirements:[/yellow]")
        user_input = console.input("[bold green]Your requirements > [/bold green]")
        if user_input.strip():
            try:
                targets = llm.parse_user_requirements(user_input)
                _display_targets(targets)
            except Exception:
                return None

    return targets


def run_pipeline(
    targets: DesignTarget,
    llm: LLMClient,
    sim: Simulator,
    optimizer: HybridOptimizer,
    config: Settings,
) -> None:
    """Execute the full optimization pipeline."""

    # --- Phase 1: Generate initial netlist ---
    console.print("\n[bold blue][Phase 1][/bold blue] Generating initial SPICE netlist...")

    try:
        template, param_space = llm.generate_initial_netlist(targets)
    except Exception as e:
        console.print(f"[red]Failed to generate initial netlist: {e}[/red]")
        return

    console.print(f"[green]✓[/green] Netlist generated with {len(param_space.params)} optimizable parameters:")
    for p in param_space.params:
        console.print(f"  {p.name}: [{_eng_fmt(p.low)} ~ {_eng_fmt(p.high)}]")

    # Save initial template
    workspace = config.get_workspace_path()
    template_path = workspace / "circuit_template.sp"
    template_path.write_text(template.template_content, encoding="utf-8")

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
    netlist_path = run_dir / "circuit_init.spi"
    sim.render_netlist(template, initial_params, netlist_path)

    success, log_content, error_msg = sim.run_spectre(netlist_path, run_dir)

    if not success:
        console.print(f"[yellow]Initial simulation failed: {error_msg[:100]}[/yellow]")
        console.print("[dim]Attempting LLM repair before optimization...[/dim]")
        # Try repair
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
        console.print(f"[green]✓[/green] Simulation converged")
        _display_results_table(initial_result, targets, "Initial Results")

        # Check if already meeting targets
        all_met, _ = targets.is_satisfied(initial_result)
        if all_met:
            console.print("\n[bold green]All targets already met! No optimization needed.[/bold green]")
            _save_final_output(template, initial_params, initial_result, config)
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
        _save_final_output(template, best.params, best.result, config)
    else:
        console.print("[red]Optimization produced no valid results.[/red]")


# --- Display helpers ---


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
):
    """Save final netlist and report to outputs directory."""
    outputs = config.get_outputs_path()

    # Save final rendered netlist
    final_netlist = template.render(params)
    final_path = outputs / "final_netlist.spi"
    final_path.write_text(final_netlist, encoding="utf-8")

    # Save summary report
    report_lines = [
        "=" * 60,
        "Circuit Optimization Report",
        "=" * 60,
        "",
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

    report_lines.append("")
    report_lines.append("=" * 60)

    report_path = outputs / "summary_report.txt"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")

    # Copy history if exists
    history_src = config.get_workspace_path() / "history.json"
    if history_src.exists():
        shutil.copy2(history_src, outputs / "optimization_log.json")

    console.print(f"\n[bold]Files saved:[/bold]")
    console.print(f"  • {final_path}")
    console.print(f"  • {report_path}")
    if history_src.exists():
        console.print(f"  • {outputs / 'optimization_log.json'}")


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
