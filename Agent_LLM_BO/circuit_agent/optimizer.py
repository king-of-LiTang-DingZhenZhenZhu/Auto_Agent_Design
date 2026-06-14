"""Bayesian Optimization engine with optional LLM parameter validation.

Netlist generation is handled by the hard-constrained topology library.
LLM is only used for physical-feasibility checks during BO.
"""

from __future__ import annotations

import json
import logging
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
    """BO-driven optimization with LLM parameter validation.

    BO handles continuous parameter search.
    LLM validates physical feasibility every N iterations.
    Topology changes use predefined escalation paths (not LLM).
    Netlist repair is unnecessary — topologies are correct-by-construction.
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
            "slew_rate_v_per_s": 1.0,
            "settling_time_s": 0.8,
        }

    def run_optimization_loop(
        self,
        template: NetlistTemplate,
        param_space: ParamSpace,
        targets: DesignTarget,
        circuit_files: CircuitFiles | None = None,
        max_iterations: int | None = None,
        on_iteration: callable = None,
        topology_name: str = "",
        gmid_sizer=None,  # GmidSizer | None
    ) -> OptimizationState:
        """Run the full BO optimization loop.

        Args:
            template: Initial parametrized SPICE netlist template (circuit DUT only)
            param_space: Search space definition
            targets: Performance targets
            circuit_files: Optional split circuit/testbench files. If provided,
                          renders circuit.cir + tb.sp per iteration.
            max_iterations: Override max iterations
            on_iteration: Callback(iteration, params, result, reward) for progress display
            topology_name: Registry key for the current topology (for escalation)
            gmid_sizer: Optional :class:`GmidSizer` instance.  When provided,
                        BO searches over gm/Id-space parameters, and this sizer
                        converts them to physical W/L before netlist rendering.

        Returns:
            OptimizationState with full history and best result
        """
        max_iter = max_iterations or self.config.max_iterations
        state = OptimizationState(targets=targets, param_space=param_space)

        # Create Optuna study
        sampler = TPESampler(seed=42, n_startup_trials=5)
        study = optuna.create_study(direction="maximize", sampler=sampler)

        current_template = template
        current_testbenches = circuit_files.testbenches if circuit_files else []
        topology_changes = 0

        logger.info(f"Starting optimization loop (max {max_iter} iterations)")

        for iteration in range(max_iter):
            # Create a trial
            trial = study.ask()

            # Step 1: BO suggests parameters
            proposed_params = param_space.suggest_from_trial(trial)

            # Step 2a: If gm/Id mode, convert gm_id/L/I params to physical W/L
            physical_params = proposed_params  # default: same (physical W/L mode)
            if gmid_sizer is not None:
                physical_params = gmid_sizer.size(proposed_params)

            # Step 2b: LLM validates (every N iterations)
            if (iteration + 1) % self.config.llm_validation_frequency == 0:
                logger.info(f"[Iter {iteration+1}] LLM parameter validation")
                last_result = (
                    state.history[-1].result if state.history else None
                )
                try:
                    physical_params = self.llm.validate_and_adjust_params(
                        physical_params, last_result, param_space, targets,
                        circuit_template=current_template.template_content,
                        dialogue_dir=str(self.config.get_workspace_path() / "LLM_DIALOGUE"),
                        iteration=iteration,
                        topology_name=topology_name,
                    )
                except Exception as e:
                    logger.warning(f"LLM validation failed, using BO params: {e}")

            # Step 3: Render netlist (always uses physical W/L params)
            run_dir = self.config.get_run_dir(iteration)
            if current_testbenches:
                tb_paths = self.sim.render_circuit_and_testbench(
                    current_template, current_testbenches,
                    physical_params, run_dir, param_space=param_space,
                    w_l_grid_step=self.config.w_l_grid_step,
                )
            else:
                tb_paths = [run_dir / "circuit.spi"]
                self.sim.render_netlist(
                    current_template, physical_params, tb_paths[0],
                    param_space=param_space,
                    w_l_grid_step=self.config.w_l_grid_step,
                )

            # Step 4: Run simulation (no LLM repair — netlists are correct-by-construction)
            sim_result = self._run_simulation(tb_paths, run_dir)

            # Step 5: Compute reward
            reward = self.compute_reward(sim_result, targets)

            # Report to Optuna
            study.tell(trial, reward)

            # Record iteration — store gm/Id params as primary, physical as metadata
            if gmid_sizer is not None:
                record = IterationRecord(
                    iteration=iteration,
                    params=proposed_params,           # gm/Id-space
                    result=sim_result,
                    reward=reward,
                    physical_params=physical_params,   # resolved W/L
                )
            else:
                record = IterationRecord(
                    iteration=iteration,
                    params=physical_params,            # physical W/L (no gm/Id)
                    result=sim_result,
                    reward=reward,
                )
            state.update(record)

            # Callback for progress display (show physical params for readability)
            if on_iteration:
                on_iteration(iteration, physical_params, sim_result, reward)

            # Step 6: Check termination - all targets met
            all_met, _ = targets.is_satisfied(sim_result)
            if all_met and sim_result.converged:
                logger.info(f"All targets met at iteration {iteration + 1}!")
                break

            # Step 7: Check stagnation and attempt topology escalation
            if self._detect_stagnation(state):
                if topology_changes < self.config.max_topology_changes:
                    logger.info("Optimization stagnant, attempting topology escalation")
                    new_topology = self._request_topology_change(topology_name)
                    if new_topology:
                        new_circuit_files, new_param_space, next_name = new_topology
                        current_template = NetlistTemplate.from_netlist(
                            new_circuit_files.circuit_netlist
                        )
                        current_testbenches = new_circuit_files.testbenches
                        param_space = new_param_space
                        topology_name = next_name
                        state.param_space = new_param_space
                        state.topology_changes += 1
                        topology_changes += 1
                        # Recreate study with new param space
                        study = optuna.create_study(
                            direction="maximize", sampler=TPESampler(seed=42)
                        )
                        logger.info("Topology escalated to %s, restarting BO search", next_name)
                else:
                    logger.warning(
                        "Max topology changes (%d) reached", self.config.max_topology_changes
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
            elif result.gain_db < 0:
                # Dead circuit (negative gain): heavy penalty
                reward -= 200.0 * self.weights["gain_db"]
                all_met = False
            else:
                gap = (targets.gain_db - result.gain_db) / max(abs(targets.gain_db), 1.0)
                reward -= gap * 50.0 * self.weights["gain_db"]
                all_met = False
        elif targets.gain_db is not None:
            reward -= 200.0  # Missing measurement in dead circuit
            all_met = False

        # GBW / unity-gain frequency (legacy field name: bandwidth_hz)
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

        # Slew rate (higher is better)
        if targets.slew_rate_v_per_s is not None and result.slew_rate_v_per_s is not None:
            if result.slew_rate_v_per_s >= targets.slew_rate_v_per_s:
                reward += 10.0 * self.weights["slew_rate_v_per_s"]
            else:
                gap = (targets.slew_rate_v_per_s - result.slew_rate_v_per_s) / max(
                    targets.slew_rate_v_per_s, 1.0
                )
                reward -= gap * 50.0 * self.weights["slew_rate_v_per_s"]
                all_met = False
        elif targets.slew_rate_v_per_s is not None:
            reward -= 50.0
            all_met = False

        # Settling time (lower is better)
        if targets.settling_time_s is not None and result.settling_time_s is not None:
            if result.settling_time_s <= targets.settling_time_s:
                reward += 10.0 * self.weights["settling_time_s"]
            else:
                gap = (result.settling_time_s - targets.settling_time_s) / max(
                    targets.settling_time_s, 1e-12
                )
                reward -= gap * 50.0 * self.weights["settling_time_s"]
                all_met = False
        elif targets.settling_time_s is not None:
            reward -= 50.0
            all_met = False

        # Bonus for meeting all targets
        if all_met:
            reward += 100.0

        return reward

    def _run_simulation(
        self,
        tb_paths: list[Path],
        run_dir: Path,
    ) -> SimResult:
        """Run primary and extra testbenches, merge results.

        No LLM repair — netlists generated by the topology library are
        syntactically correct by construction.  Failures are typically
        convergence issues from extreme parameter values.
        """
        primary_path = tb_paths[0]

        success, log_content, error_msg = self.sim.run_spectre(primary_path, run_dir)

        if not success:
            error_type = self.sim.classify_error(log_content or error_msg)
            logger.warning(
                "Simulation failed: %s — %s", error_type, (error_msg or "")[:150]
            )
            return SimResult(
                converged=False, error_message=f"{error_type}: {(error_msg or '')[:200]}"
            )

        merged = self.sim.parse_simulation_results(
            log_content, run_dir, primary_path
        )

        # Extra testbenches (e.g., transient)
        for tb_path in tb_paths[1:]:
            logger.info("Running extra simulation: %s", tb_path.name)
            ok, log, _ = self.sim.run_spectre(tb_path, run_dir)
            if ok:
                extra = self.sim.parse_simulation_results(log, run_dir, tb_path)
                merged = SimResult.merge(merged, extra)

        return merged

    def _detect_stagnation(self, state: OptimizationState) -> bool:
        """Check if optimization has stagnated (no improvement in N iterations)."""
        window = self.config.stagnation_window
        if len(state.history) < window:
            return False

        recent = state.history[-window:]

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
        topology_name: str,
    ) -> tuple[CircuitFiles, ParamSpace, str] | None:
        """Attempt topology escalation via predefined path.

        Uses TopologyMeta.escalation to find the next topology.
        Returns (CircuitFiles, ParamSpace, new_topology_name) or None.
        """
        if not topology_name:
            logger.warning("No topology name set — cannot escalate")
            return None

        try:
            from topologies import get_topology

            current = get_topology(topology_name)
        except ValueError:
            logger.warning("Unknown topology '%s' — cannot escalate", topology_name)
            return None

        if not current.meta.escalation:
            logger.warning(
                "No escalation path defined for '%s' — recommend Claude agent intervention",
                topology_name,
            )
            return None

        next_name = current.meta.escalation
        logger.info("Escalating topology: %s → %s", topology_name, next_name)

        try:
            next_topo = get_topology(next_name)
            return (
                next_topo.get_circuit_files(),
                next_topo.get_param_space(),
                next_name,
            )
        except ValueError:
            logger.warning(
                "Escalation target '%s' not yet implemented", next_name
            )
            return None

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
