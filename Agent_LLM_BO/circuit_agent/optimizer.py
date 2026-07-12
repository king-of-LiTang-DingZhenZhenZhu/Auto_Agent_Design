"""Bayesian Optimization engine with optional external LLM validation.

Netlist generation is handled by the hard-constrained topology library.
The main loop is driven by BO, Spectre parsing, gm/Id sizing, diagnostics,
and operating-point penalties.  External LLM validation is disabled by default
and is intended only as an experimental physical-feasibility check.
"""

from __future__ import annotations

import csv
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
from summarize_metrics import build_report_from_sim_result
from operating_point import OperatingPointStatus, evaluate_dc_operating_points

logger = logging.getLogger(__name__)

# Suppress Optuna's verbose logging
optuna.logging.set_verbosity(optuna.logging.WARNING)


class HybridOptimizer:
    """BO-driven optimization with optional external LLM parameter validation.

    BO handles continuous/integer parameter search.  When explicitly enabled,
    an external LLM can review BO-proposed parameters every N iterations.
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
        initial_candidates: list[dict[str, float]] | None = None,
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
            initial_candidates: Known parameter sets evaluated before
                                sampler-generated startup trials.

        Returns:
            OptimizationState with full history and best result
        """
        max_iter = max_iterations or self.config.max_iterations
        state = OptimizationState(targets=targets, param_space=param_space)

        # Create Optuna study
        sampler = TPESampler(
            seed=42,
            n_startup_trials=self.config.bo_n_startup_trials,
        )
        study = optuna.create_study(direction="maximize", sampler=sampler)
        for candidate in initial_candidates or []:
            if set(candidate) == set(param_space.get_param_names()):
                study.enqueue_trial(candidate)
            else:
                logger.warning("Skipping incomplete BO initial candidate")

        current_template = template
        current_testbenches = circuit_files.testbenches if circuit_files else []
        topology_changes = 0
        critical_op_instances = self._critical_op_instances(topology_name)

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

            # Step 2b: Optional external LLM validation. Disabled by default;
            # local Agent Review after BO is the preferred analysis path.
            if (
                self.config.enable_llm_validation
                and self.config.llm_validation_frequency > 0
                and self.llm is not None
                and (iteration + 1) % self.config.llm_validation_frequency == 0
            ):
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

            # Step 4: Run simulation (no external repair — netlists are correct-by-construction)
            sim_result = self._run_simulation(tb_paths, run_dir)
            op_status = self._evaluate_operating_point(
                run_dir,
                critical_op_instances,
            )
            if op_status is not None:
                sim_result.operating_point_status = op_status.to_dict()

            # Step 5: Compute reward
            reward = self.compute_reward(sim_result, targets, op_status=op_status)
            self._write_iteration_summary(
                run_dir=run_dir,
                iteration=iteration,
                result=sim_result,
                reward=reward,
                tb_paths=tb_paths,
                op_status=op_status,
            )

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
            if self._can_stop_for_success(sim_result, targets, op_status):
                logger.info(f"All targets met at iteration {iteration + 1}!")
                break
            all_met, _ = targets.is_satisfied(sim_result)
            if (
                all_met
                and sim_result.converged
                and op_status is not None
                and op_status.critical_linear_count > 0
            ):
                logger.info(
                    "All performance targets met at iteration %d, but "
                    "critical MOS are linear (%s); continuing BO",
                    iteration + 1,
                    ", ".join(op_status.critical_linear),
                )

            # Step 6b: Stop early when the same topology is repeatedly far
            # from a usable operating point. This catches likely topology,
            # bias, or testbench mistakes instead of wasting BO iterations.
            if self._detect_repeated_severe_deviation(state, targets):
                state.stop_reason = (
                    "Stopped after "
                    f"{self.config.severe_deviation_patience} consecutive "
                    "severe simulation deviations"
                )
                logger.warning(state.stop_reason)
                break

            # Step 7: Optional topology escalation. Disabled by default so one
            # optimization run remains on the topology selected at startup.
            if (
                self.config.enable_topology_escalation
                and self._detect_stagnation(state)
            ):
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
                        critical_op_instances = self._critical_op_instances(
                            topology_name
                        )
                        state.param_space = new_param_space
                        state.topology_changes += 1
                        topology_changes += 1
                        # Recreate study with new param space
                        study = optuna.create_study(
                            direction="maximize",
                            sampler=TPESampler(
                                seed=42,
                                n_startup_trials=self.config.bo_n_startup_trials,
                            ),
                        )
                        logger.info("Topology escalated to %s, restarting BO search", next_name)
                else:
                    logger.warning(
                        "Max topology changes (%d) reached", self.config.max_topology_changes
                    )

        # Save history
        self._save_history(state)
        self._save_metrics_csv(state)
        return state

    def compute_reward(
        self,
        result: SimResult,
        targets: DesignTarget,
        op_status: OperatingPointStatus | None = None,
    ) -> float:
        """Compute a feasibility-first scalar reward.

        Feasible designs always outrank infeasible designs. Among infeasible
        designs, reduce the largest normalized violation first, then the total
        violation. Feasible designs are ranked by margin quality and OP health.
        """
        if not result.converged:
            return -1_000_000.0

        violations: list[float] = []
        utility = 0.0

        def lower_bound(value, target, weight, missing=1.0):
            nonlocal utility
            if target is None:
                return
            if value is None:
                violations.append(missing)
            elif value < target:
                violations.append(
                    (target - value) / max(abs(target), 1e-30) * weight
                )
            else:
                utility += 10.0 * weight

        def upper_bound(value, target, weight):
            nonlocal utility
            if target is None:
                return
            if value is None:
                violations.append(1.0)
            elif value > target:
                violations.append(
                    (value - target) / max(abs(target), 1e-30) * weight
                )
            else:
                utility += 10.0 * weight

        lower_bound(result.gain_db, targets.gain_db, self.weights["gain_db"], 2.0)
        lower_bound(
            result.bandwidth_hz,
            targets.bandwidth_hz,
            self.weights["bandwidth_hz"],
        )
        lower_bound(
            result.phase_margin_deg,
            targets.phase_margin_deg,
            self.weights["phase_margin_deg"],
        )
        upper_bound(result.power_w, targets.power_w, self.weights["power_w"])
        lower_bound(
            result.slew_rate_v_per_s,
            targets.slew_rate_v_per_s,
            self.weights["slew_rate_v_per_s"],
        )
        upper_bound(
            result.settling_time_s,
            targets.settling_time_s,
            self.weights["settling_time_s"],
        )

        if (
            targets.phase_margin_deg is not None
            and result.phase_margin_deg is not None
            and result.phase_margin_deg > 75.0
        ):
            utility -= (
                (result.phase_margin_deg - 75.0)
                / 75.0
                * 30.0
                * self.weights["phase_margin_deg"]
            )

        if op_status is not None:
            utility += op_status.penalty
            if op_status.critical_linear_count:
                violations.append(float(op_status.critical_linear_count))

        if violations:
            return -1000.0 - 1000.0 * max(violations) - 100.0 * sum(violations)
        return 1000.0 + utility

    def _evaluate_operating_point(
        self,
        run_dir: Path,
        critical_instances: set[str],
    ) -> OperatingPointStatus | None:
        """Evaluate DC OP diagnostics if this run produced them."""
        dc_path = run_dir / "diagnostics" / "dc_operating_points.csv"
        if not dc_path.exists():
            return None
        return evaluate_dc_operating_points(
            dc_path,
            critical_instances=critical_instances,
        )

    def _critical_op_instances(self, topology_name: str) -> set[str]:
        if not topology_name:
            return set()
        try:
            from topologies import get_topology

            return get_topology(topology_name).critical_operating_point_instances()
        except Exception as exc:
            logger.debug(
                "Could not load critical OP instances for %s: %s",
                topology_name,
                exc,
            )
            return set()

    def _run_simulation(
        self,
        tb_paths: list[Path],
        run_dir: Path,
    ) -> SimResult:
        """Run primary and extra testbenches, merge results.

        No external repair — netlists generated by the topology library are
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

    def _write_iteration_summary(
        self,
        run_dir: Path,
        iteration: int,
        result: SimResult,
        reward: float,
        tb_paths: list[Path],
        op_status: OperatingPointStatus | None = None,
    ) -> None:
        """Write this iteration's parsed metrics into its run directory."""
        report = build_report_from_sim_result(
            result,
            source=run_dir,
            testbenches=tb_paths,
        )
        lines = report.rstrip().splitlines()
        insert_at = 4
        lines[insert_at:insert_at] = [
            f"Iteration: {iteration + 1}",
            f"Reward: {reward:.6g}",
        ]
        if op_status is not None:
            lines.append("")
            lines.extend(op_status.summary_lines())
        (run_dir / "metrics_summary.txt").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

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

    def _detect_repeated_severe_deviation(
        self,
        state: OptimizationState,
        targets: DesignTarget,
    ) -> bool:
        """Return True when recent results are all severe failures."""
        patience = self.config.severe_deviation_patience
        if patience <= 0 or len(state.history) < patience:
            return False
        recent = state.history[-patience:]
        return all(
            self._is_severe_deviation(record.result, targets)
            for record in recent
        )

    def _can_stop_for_success(
        self,
        result: SimResult,
        targets: DesignTarget,
        op_status: OperatingPointStatus | None = None,
    ) -> bool:
        """Only stop when specs pass and no critical MOS is in linear region."""
        all_met, _ = targets.is_satisfied(result)
        if not (all_met and result.converged):
            return False
        if op_status is not None and op_status.critical_linear_count > 0:
            return False
        return True

    def _is_severe_deviation(
        self,
        result: SimResult,
        targets: DesignTarget,
    ) -> bool:
        """Detect circuit-level failure, not ordinary unmet specs."""
        if not result.converged:
            return True

        if targets.gain_db is not None:
            if result.gain_db is None:
                return True
            if result.gain_db < 0:
                return True
            if result.gain_db < targets.gain_db - self.config.severe_gain_gap_db:
                return True

        if targets.bandwidth_hz is not None:
            if result.bandwidth_hz is None:
                return True
            if result.bandwidth_hz < (
                targets.bandwidth_hz * self.config.severe_bandwidth_ratio
            ):
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
            "stop_reason": state.stop_reason,
            "targets": {
                "gain_db": state.targets.gain_db,
                "bandwidth_hz": state.targets.bandwidth_hz,
                "phase_margin_deg": state.targets.phase_margin_deg,
                "power_w": state.targets.power_w,
                "load_cap_f": state.targets.load_cap_f,
                "slew_rate_v_per_s": state.targets.slew_rate_v_per_s,
                "settling_time_s": state.targets.settling_time_s,
            },
            "history": [
                {
                    "iteration": r.iteration,
                    "params": r.params,
                    "reward": r.reward,
                    "result": {
                        "gain_db": r.result.gain_db,
                        "bandwidth_hz": r.result.bandwidth_hz,
                        "gbw_hz": r.result.bandwidth_hz,
                        "unity_gain_freq_hz": r.result.unity_gain_freq_hz,
                        "phase_margin_deg": r.result.phase_margin_deg,
                        "power_w": r.result.power_w,
                        "slew_rate_v_per_s": r.result.slew_rate_v_per_s,
                        "slew_rate_positive_v_per_s": (
                            r.result.slew_rate_positive_v_per_s
                        ),
                        "slew_rate_negative_v_per_s": (
                            r.result.slew_rate_negative_v_per_s
                        ),
                        "settling_time_s": r.result.settling_time_s,
                        "converged": r.result.converged,
                        "error_message": r.result.error_message,
                        "operating_point_status": r.result.operating_point_status,
                    },
                    "op_penalty": (
                        r.result.operating_point_status or {}
                    ).get("penalty"),
                    "op_critical_linear": (
                        r.result.operating_point_status or {}
                    ).get("critical_linear", []),
                    "op_critical_near_edge": (
                        r.result.operating_point_status or {}
                    ).get("critical_near_edge", []),
                }
                for r in state.history
            ],
        }

        output_path.write_text(
            json.dumps(history_data, indent=2, default=str), encoding="utf-8"
        )
        logger.info(f"History saved to {output_path}")

    def _save_metrics_csv(self, state: OptimizationState) -> None:
        """Save one row per BO iteration with the main parsed metrics."""
        output_path = self.config.get_workspace_path() / "optimization_metrics.csv"
        fieldnames = [
            "iteration",
            "reward",
            "gain_db(dB)",
            "gbw_hz(MHz)",
            "phase_margin_deg(deg)",
            "power_w(mW)",
            "slew_rate_v_per_s(V/us)",
            "settling_time_s(ns)",
            "op_linear_count",
            "op_near_edge_count",
            "op_min_margin_mv",
            "error_message",
        ]
        with output_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for record in state.history:
                result = record.result
                writer.writerow(
                    {
                        "iteration": record.iteration + 1,
                        "reward": self._fmt_csv_value(record.reward, 6),
                        "gain_db(dB)": self._fmt_csv_value(result.gain_db, 2),
                        "gbw_hz(MHz)": self._fmt_csv_value(
                            result.bandwidth_hz, 2, scale=1e-6
                        ),
                        "phase_margin_deg(deg)": self._fmt_csv_value(
                            result.phase_margin_deg, 2
                        ),
                        "power_w(mW)": self._fmt_csv_value(
                            result.power_w, 3, scale=1e3
                        ),
                        "slew_rate_v_per_s(V/us)": self._fmt_csv_value(
                            result.slew_rate_v_per_s, 2, scale=1e-6
                        ),
                        "settling_time_s(ns)": self._fmt_csv_value(
                            result.settling_time_s, 2, scale=1e9
                        ),
                        "op_linear_count": self._op_field(
                            result, "linear_count", ""
                        ),
                        "op_near_edge_count": self._op_field(
                            result, "near_edge_count", ""
                        ),
                        "op_min_margin_mv": self._fmt_csv_value(
                            self._op_field(result, "min_margin_v", None),
                            2,
                            scale=1e3,
                        ),
                        "error_message": result.error_message,
                    }
                )
        logger.info("Metrics CSV saved to %s", output_path)

    @staticmethod
    def _fmt_csv_value(
        value: float | None,
        digits: int,
        scale: float = 1.0,
    ) -> str:
        """Format display values for optimization_metrics.csv."""
        if value is None:
            return ""
        return f"{value * scale:.{digits}f}"

    @staticmethod
    def _op_field(
        result: SimResult,
        name: str,
        default,
    ):
        status = result.operating_point_status or {}
        return status.get(name, default)
