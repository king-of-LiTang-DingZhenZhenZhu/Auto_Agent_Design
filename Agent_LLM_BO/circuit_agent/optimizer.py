"""LLM + Bayesian Optimization hybrid optimization engine."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import optuna
from optuna.samplers import TPESampler

from config import Settings
from llm_client import LLMClient
from models import (
    CircuitFiles,
    DesignTarget,
    IterationRecord,
    NetlistTemplate,
    OptimizationState,
    ParamSpace,
    SimResult,
)
from simulator import Simulator

logger = logging.getLogger(__name__)

# Suppress Optuna's verbose logging
optuna.logging.set_verbosity(optuna.logging.WARNING)


class HybridOptimizer:
    """LLM + Bayesian Optimization hybrid optimization engine.

    BO handles continuous parameter search efficiently.
    LLM validates physical feasibility, repairs errors, and suggests topology changes.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        simulator: Simulator,
        config: Settings,
    ):
        self.llm = llm_client
        self.sim = simulator
        self.config = config

        # Reward weights
        self.weights = {
            "gain_db": 1.0,
            "bandwidth_hz": 1.0,
            "phase_margin_deg": 1.5,
            "power_w": 0.8,
        }

    def run_optimization_loop(
        self,
        template: NetlistTemplate,
        param_space: ParamSpace,
        targets: DesignTarget,
        circuit_files: CircuitFiles | None = None,
        max_iterations: int | None = None,
        on_iteration: callable = None,
    ) -> OptimizationState:
        """Run the full LLM+BO optimization loop.

        Args:
            template: Initial parametrized SPICE netlist template (circuit DUT only)
            param_space: Search space definition
            targets: Performance targets
            circuit_files: Optional split circuit/testbench files. If provided,
                          renders circuit.cir + tb.sp per iteration.
            max_iterations: Override max iterations
            on_iteration: Callback(iteration, params, result, reward) for progress display

        Returns:
            OptimizationState with full history and best result
        """
        max_iter = max_iterations or self.config.max_iterations
        state = OptimizationState(targets=targets, param_space=param_space)

        # Create Optuna study
        sampler = TPESampler(seed=42, n_startup_trials=5)
        study = optuna.create_study(direction="maximize", sampler=sampler)

        current_template = template
        current_testbench = circuit_files.testbench if circuit_files else None
        topology_changes = 0

        logger.info(f"Starting optimization loop (max {max_iter} iterations)")

        for iteration in range(max_iter):
            # Create a trial
            trial = study.ask()

            # Step 1: BO suggests parameters
            proposed_params = param_space.suggest_from_trial(trial)

            # Step 2: LLM validates (every N iterations)
            if (iteration + 1) % self.config.llm_validation_frequency == 0:
                logger.info(f"[Iter {iteration+1}] LLM parameter validation")
                last_result = (
                    state.history[-1].result if state.history else None
                )
                try:
                    proposed_params = self.llm.validate_and_adjust_params(
                        proposed_params, last_result, param_space, targets,
                        circuit_template=current_template.template_content,
                    )
                except Exception as e:
                    logger.warning(f"LLM validation failed, using BO params: {e}")

            # Step 3: Render netlist
            run_dir = self.config.get_run_dir(iteration)
            if current_testbench is not None:
                netlist_path = self.sim.render_circuit_and_testbench(
                    current_template, current_testbench,
                    proposed_params, run_dir, param_space=param_space,
                    w_l_grid_step=self.config.w_l_grid_step,
                )
            else:
                netlist_path = run_dir / "circuit.spi"
                self.sim.render_netlist(
                    current_template, proposed_params, netlist_path, param_space=param_space,
                    w_l_grid_step=self.config.w_l_grid_step,
                )

            # Step 4: Run simulation (with error repair)
            sim_result = self._run_with_repair(
                netlist_path, run_dir, current_template, proposed_params,
                testbench_content=current_testbench,
            )

            # Step 5: Compute reward
            reward = self.compute_reward(sim_result, targets)

            # Report to Optuna
            study.tell(trial, reward)

            # Record iteration
            record = IterationRecord(
                iteration=iteration,
                params=proposed_params,
                result=sim_result,
                reward=reward,
            )
            state.update(record)

            # Callback for progress display
            if on_iteration:
                on_iteration(iteration, proposed_params, sim_result, reward)

            # Step 6: Check termination - all targets met
            all_met, _ = targets.is_satisfied(sim_result)
            if all_met and sim_result.converged:
                logger.info(f"All targets met at iteration {iteration + 1}!")
                break

            # Step 7: Check stagnation and trigger topology change
            if self._detect_stagnation(state):
                if topology_changes < self.config.max_topology_changes:
                    logger.info("Optimization stagnant, requesting LLM topology change")
                    new_topology = self._request_topology_change(
                        state, targets, current_template
                    )
                    if new_topology:
                        new_circuit_files, param_space = new_topology
                        current_template = NetlistTemplate.from_netlist(new_circuit_files.circuit_netlist)
                        current_testbench = new_circuit_files.testbench
                        state.param_space = param_space
                        state.topology_changes += 1
                        topology_changes += 1
                        # Recreate study with new param space
                        study = optuna.create_study(
                            direction="maximize", sampler=TPESampler(seed=42)
                        )
                        logger.info("Topology changed, restarting BO search")
                else:
                    logger.warning(
                        f"Max topology changes ({self.config.max_topology_changes}) reached"
                    )

        # Save history
        self._save_history(state)
        return state

    def compute_reward(self, result: SimResult, targets: DesignTarget) -> float:
        """Compute a scalar reward from simulation results vs targets.

        Strategy:
        - Each satisfied metric: +10
        - Each unsatisfied metric: penalty proportional to normalized gap
        - Simulation failure: -1000
        - All targets met: +100 bonus
        """
        if not result.converged:
            return -1000.0

        reward = 0.0
        all_met = True

        # Gain
        if targets.gain_db is not None and result.gain_db is not None:
            if result.gain_db >= targets.gain_db:
                reward += 10.0 * self.weights["gain_db"]
            else:
                gap = (targets.gain_db - result.gain_db) / max(abs(targets.gain_db), 1.0)
                reward -= gap * 50.0 * self.weights["gain_db"]
                all_met = False
        elif targets.gain_db is not None:
            reward -= 50.0  # Missing measurement
            all_met = False

        # Bandwidth
        if targets.bandwidth_hz is not None and result.bandwidth_hz is not None:
            if result.bandwidth_hz >= targets.bandwidth_hz:
                reward += 10.0 * self.weights["bandwidth_hz"]
            else:
                gap = (targets.bandwidth_hz - result.bandwidth_hz) / max(
                    targets.bandwidth_hz, 1.0
                )
                reward -= gap * 50.0 * self.weights["bandwidth_hz"]
                all_met = False
        elif targets.bandwidth_hz is not None:
            reward -= 50.0
            all_met = False

        # Phase margin
        if targets.phase_margin_deg is not None and result.phase_margin_deg is not None:
            if result.phase_margin_deg >= targets.phase_margin_deg:
                reward += 10.0 * self.weights["phase_margin_deg"]
            else:
                gap = (targets.phase_margin_deg - result.phase_margin_deg) / max(
                    targets.phase_margin_deg, 1.0
                )
                reward -= gap * 50.0 * self.weights["phase_margin_deg"]
                all_met = False
        elif targets.phase_margin_deg is not None:
            reward -= 50.0
            all_met = False

        # Power (lower is better)
        if targets.power_w is not None and result.power_w is not None:
            if result.power_w <= targets.power_w:
                reward += 10.0 * self.weights["power_w"]
            else:
                gap = (result.power_w - targets.power_w) / max(targets.power_w, 1e-9)
                reward -= gap * 50.0 * self.weights["power_w"]
                all_met = False
        elif targets.power_w is not None:
            reward -= 50.0
            all_met = False

        # Bonus for meeting all targets
        if all_met:
            reward += 100.0

        return reward

    def _run_with_repair(
        self,
        netlist_path: Path,
        run_dir: Path,
        template: NetlistTemplate,
        params: dict[str, float],
        testbench_content: str | None = None,
    ) -> SimResult:
        """Run simulation with automatic error repair via LLM.

        If simulation fails with syntax/node errors, asks LLM to fix and retries.
        When testbench_content is provided, repairs both circuit and testbench.
        """
        for attempt in range(self.config.max_repair_attempts + 1):
            success, log_content, error_msg = self.sim.run_spectre(
                netlist_path, run_dir
            )

            if success:
                return self.sim.parse_simulation_log(log_content)

            # Classify the error
            error_type = self.sim.classify_error(log_content or error_msg)
            logger.warning(
                f"Simulation failed (attempt {attempt+1}): {error_type} - {error_msg[:100]}"
            )

            # Only repair syntax and node_floating errors via LLM
            if error_type in ("syntax", "node_floating") and attempt < self.config.max_repair_attempts:
                try:
                    if testbench_content is not None:
                        # Split mode: repair circuit.cir + tb.sp separately
                        circuit_content = (run_dir / "circuit.cir").read_text(encoding="utf-8")
                        tb_content = netlist_path.read_text(encoding="utf-8")
                        repaired_circuit, repaired_tb = self.llm.repair_netlist(
                            circuit_content, log_content or error_msg, attempt + 1,
                            testbench=tb_content,
                        )
                        (run_dir / "circuit.cir").write_text(repaired_circuit, encoding="utf-8")
                        (run_dir / "tb.sp").write_text(repaired_tb, encoding="utf-8")
                        logger.info(f"LLM repaired circuit + testbench (attempt {attempt + 1})")
                    else:
                        current_netlist = netlist_path.read_text(encoding="utf-8")
                        repaired = self.llm.repair_netlist(
                            current_netlist, log_content or error_msg, attempt + 1
                        )
                        netlist_path.write_text(repaired, encoding="utf-8")
                        logger.info(f"LLM repaired netlist (attempt {attempt + 1})")
                    continue
                except Exception as e:
                    logger.error(f"LLM repair failed: {e}")

            # For convergence errors or exhausted repair attempts, return failed result
            return SimResult(
                converged=False,
                error_message=f"{error_type}: {error_msg[:200]}",
            )

        return SimResult(converged=False, error_message="Max repair attempts exhausted")

    def _detect_stagnation(self, state: OptimizationState) -> bool:
        """Check if optimization has stagnated (no improvement in N iterations)."""
        window = self.config.stagnation_window
        if len(state.history) < window:
            return False

        recent = state.history[-window:]
        best_recent = max(r.reward for r in recent)

        # Check if the global best was achieved before the recent window
        if state.best_iteration < len(state.history) - window:
            return True

        # Also check if variance is too low (all similar results)
        rewards = [r.reward for r in recent]
        if len(set(f"{r:.1f}" for r in rewards)) <= 2:
            return True

        return False

    def _request_topology_change(
        self,
        state: OptimizationState,
        targets: DesignTarget,
        current_template: NetlistTemplate,
    ) -> tuple[CircuitFiles, ParamSpace] | None:
        """Ask LLM to suggest a topology change."""
        # Build history summary
        history_summary = self._build_history_summary(state)

        best_record = state.best_record
        if not best_record:
            return None

        try:
            result = self.llm.suggest_topology_change(
                best_record.result, targets, history_summary
            )
            return result
        except Exception as e:
            logger.error(f"LLM topology change request failed: {e}")
            return None

    def _build_history_summary(self, state: OptimizationState) -> str:
        """Build a concise summary of optimization history for LLM."""
        if not state.history:
            return "No optimization history yet."

        lines = [
            f"Total iterations: {state.total_iterations}",
            f"Best reward: {state.best_reward:.1f} (iteration {state.best_iteration})",
            f"Topology changes so far: {state.topology_changes}",
            "",
            "Recent 5 iterations:",
        ]

        for record in state.history[-5:]:
            lines.append(
                f"  Iter {record.iteration}: reward={record.reward:.1f} | "
                f"{record.result.to_summary_str()}"
            )

        if state.best_record:
            lines.append(f"\nBest params: {_format_params(state.best_record.params)}")

        return "\n".join(lines)

    def _save_history(self, state: OptimizationState) -> None:
        """Save optimization history to JSON."""
        output_path = self.config.get_workspace_path() / "history.json"

        history_data = {
            "total_iterations": state.total_iterations,
            "best_iteration": state.best_iteration,
            "best_reward": state.best_reward,
            "topology_changes": state.topology_changes,
            "targets": {
                "gain_db": state.targets.gain_db,
                "bandwidth_hz": state.targets.bandwidth_hz,
                "phase_margin_deg": state.targets.phase_margin_deg,
                "power_w": state.targets.power_w,
            },
            "history": [
                {
                    "iteration": r.iteration,
                    "params": r.params,
                    "reward": r.reward,
                    "result": {
                        "gain_db": r.result.gain_db,
                        "bandwidth_hz": r.result.bandwidth_hz,
                        "phase_margin_deg": r.result.phase_margin_deg,
                        "power_w": r.result.power_w,
                        "converged": r.result.converged,
                    },
                }
                for r in state.history
            ],
        }

        output_path.write_text(
            json.dumps(history_data, indent=2, default=str), encoding="utf-8"
        )
        logger.info(f"History saved to {output_path}")


def _format_params(params: dict[str, float]) -> str:
    """Format parameters for display."""
    parts = []
    for name, value in params.items():
        abs_val = abs(value)
        if abs_val >= 1e-3:
            parts.append(f"{name}={value:.4g}")
        elif abs_val >= 1e-6:
            parts.append(f"{name}={value*1e6:.2f}u")
        elif abs_val >= 1e-9:
            parts.append(f"{name}={value*1e9:.1f}n")
        elif abs_val >= 1e-12:
            parts.append(f"{name}={value*1e12:.2f}p")
        else:
            parts.append(f"{name}={value:.2e}")
    return ", ".join(parts)
