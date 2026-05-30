"""Spectre simulator invocation and result parsing."""

from __future__ import annotations

import logging
import math
import random
import re
import subprocess
from pathlib import Path

from config import Settings
from models import CircuitFiles, DesignTarget, NetlistTemplate, ParamSpace, SimResult

logger = logging.getLogger(__name__)


class Simulator:
    """Handles Spectre simulation execution and result extraction."""

    def __init__(self, config: Settings):
        self.config = config

    def render_netlist(
        self,
        template: NetlistTemplate,
        params: dict[str, float],
        output_path: Path,
        param_space: ParamSpace | None = None,
        w_l_grid_step: float | None = None,
    ) -> None:
        """Render parametrized template into a concrete SPICE netlist file.

        If param_space is provided, wide transistors are automatically split
        into multiple fingers respecting max_per_finger limits.
        """
        content = template.render(
            params,
            param_space=param_space,
            max_width_per_finger=self.config.max_width_per_finger,
            w_l_grid_step=w_l_grid_step,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
        logger.debug(f"Rendered netlist to {output_path}")

    def render_circuit_and_testbench(
        self,
        circuit_template: NetlistTemplate,
        testbench_contents: list[str],
        params: dict[str, float],
        run_dir: Path,
        param_space: ParamSpace | None = None,
        w_l_grid_step: float | None = None,
    ) -> list[Path]:
        """Render circuit and testbench files into run_dir.

        Writes circuit.cir (rendered DUT) once, then writes each testbench as
        tb.sp, tb_1.sp, tb_2.sp, ... (all .include "circuit.cir").
        Returns the list of testbench paths as entry points for Spectre.
        """
        run_dir.mkdir(parents=True, exist_ok=True)

        # Render circuit with parameter values
        circuit_content = circuit_template.render(
            params,
            param_space=param_space,
            max_width_per_finger=self.config.max_width_per_finger,
            w_l_grid_step=w_l_grid_step,
        )
        circuit_path = run_dir / "circuit.cir"
        circuit_path.write_text(circuit_content, encoding="utf-8")
        logger.debug(f"Rendered circuit to {circuit_path}")

        # Write each testbench
        tb_paths = []
        for i, tb_content in enumerate(testbench_contents):
            suffix = "" if i == 0 else f"_{i}"
            tb_path = run_dir / f"tb{suffix}.sp"
            tb_path.write_text(tb_content, encoding="utf-8")
            logger.debug(f"Wrote testbench to {tb_path}")
            tb_paths.append(tb_path)

        return tb_paths

    def run_spectre(
        self, netlist_path: Path, run_dir: Path, timeout: int | None = None
    ) -> tuple[bool, str, str]:
        """Run Spectre simulation.

        Args:
            netlist_path: Path to the .spi netlist file
            run_dir: Directory for simulation outputs
            timeout: Override timeout in seconds

        Returns:
            (success, log_content, error_message)
        """
        if self.config.dry_run:
            return self._mock_simulate(netlist_path, run_dir)

        timeout = timeout or self.config.spectre_timeout
        run_dir.mkdir(parents=True, exist_ok=True)
        raw_dir = run_dir / "raw"
        log_path = run_dir / "sim.log"

        cmd = self.config.spectre_cmd_template.format(
            netlist_path=str(netlist_path),
            raw_dir=str(raw_dir),
            log_path=str(log_path),
        )

        logger.info(f"Running Spectre: {cmd}")

        try:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=str(run_dir),
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            # Read log file if it exists
            log_content = ""
            if log_path.exists():
                log_content = log_path.read_text(encoding="utf-8", errors="replace")
            elif result.stdout:
                log_content = result.stdout

            # Spectre writes .meas results to a separate .measure file (e.g., tb.measure),
            # not to sim.log. Append its content so parse_simulation_log can find results.
            measure_files = sorted(run_dir.glob("*.measure"))
            if measure_files:
                measure_content = ""
                for mf in measure_files:
                    measure_content += f"\n--- {mf.name} ---\n"
                    measure_content += mf.read_text(encoding="utf-8", errors="replace")
                log_content = log_content + measure_content
                logger.debug(f"Appended {len(measure_files)} .measure file(s) to log")

            if result.returncode != 0:
                error_msg = result.stderr or "Spectre returned non-zero exit code"
                logger.warning(f"Spectre failed (rc={result.returncode}): {error_msg[:200]}")
                return False, log_content, error_msg

            return True, log_content, ""

        except subprocess.TimeoutExpired:
            logger.error(f"Spectre timed out after {timeout}s")
            return False, "", "Simulation timed out"
        except FileNotFoundError:
            logger.error("Spectre executable not found")
            return False, "", "Spectre executable not found. Is it installed and in PATH?"
        except Exception as e:
            logger.error(f"Spectre execution error: {e}")
            return False, "", str(e)

    def run_all_testbenches(
        self,
        tb_paths: list[Path],
        run_dir: Path,
        timeout: int | None = None,
    ) -> SimResult:
        """Run Spectre on the primary testbench, then on any extra ones.

        Each testbench's .measure results are parsed and merged into a single
        SimResult. The primary testbench (index 0) is typically AC; extras may
        be transient, DC, etc. If the primary fails, a failed SimResult is
        returned immediately.
        """
        if not tb_paths:
            return SimResult(converged=False, error_message="No testbench to run")

        # --- Primary simulation ---
        success, log_content, error_msg = self.run_spectre(tb_paths[0], run_dir, timeout)
        if not success:
            return SimResult(converged=False, error_message=error_msg)

        merged = self.parse_simulation_log(log_content)

        # --- Extra simulations ---
        for tb_path in tb_paths[1:]:
            logger.info(f"Running extra simulation: {tb_path.name}")
            ok, log, err = self.run_spectre(tb_path, run_dir, timeout)
            if ok:
                extra = self.parse_simulation_log(log)
                merged = SimResult.merge(merged, extra)
            else:
                logger.warning(f"Extra simulation {tb_path.name} failed: {err[:100]}")

        return merged

    def parse_simulation_log(self, log_content: str) -> SimResult:
        """Parse Spectre simulation log to extract performance metrics.

        Looks for .meas statement outputs in the log.
        """
        if not log_content:
            return SimResult(converged=False, error_message="Empty simulation log")

        result = SimResult()
        result.raw_metrics = {}

        # Check for convergence issues
        convergence_errors = [
            r"convergence\s+problem",
            r"did\s+not\s+converge",
            r"convergence\s+failure",
            r"no\s+convergence",
            r"ERROR.*convergence",
        ]
        for pattern in convergence_errors:
            if re.search(pattern, log_content, re.IGNORECASE):
                result.converged = False
                result.error_message = "Convergence failure"
                return result

        # Parse .meas results - multiple common Spectre output formats
        meas_patterns = [
            # Format: measure_name = value
            r"(\w+)\s*=\s*([+-]?\d+\.?\d*(?:[eE][+-]?\d+)?)",
            # Format: Measure "name" = value
            r'[Mm]easure\s+"?(\w+)"?\s*=\s*([+-]?\d+\.?\d*(?:[eE][+-]?\d+)?)',
            # Format: name: value (some Spectre versions)
            r"^(\w+):\s*([+-]?\d+\.?\d*(?:[eE][+-]?\d+)?)\s*$",
        ]

        for pattern in meas_patterns:
            for match in re.finditer(pattern, log_content, re.MULTILINE):
                name = match.group(1).lower()
                value_str = match.group(2)
                # Skip NaN / inf sentinel values from Spectre .measure files
                if value_str.lower() in ("nan", "inf", "-inf", "+inf"):
                    continue
                try:
                    value = float(value_str)
                    result.raw_metrics[name] = value
                except ValueError:
                    continue

        # Map extracted metrics to SimResult fields
        #
        # Canonical names (from testbench_sp_guide.md):
        #   AC:  gain_dc, phase_dc, gbw_hz, phase_at_ugf, power_total
        #   Transient: slew_rate, settling_time
        # Legacy names (backwards compatible):
        #   gain_db, gain, ugf, bw, bandwidth, phase_margin, pm, power

        # Gain (dB)
        if "gain_dc" in result.raw_metrics:
            result.gain_db = result.raw_metrics["gain_dc"]
        elif "gain_db" in result.raw_metrics:
            result.gain_db = result.raw_metrics["gain_db"]
        elif "gain" in result.raw_metrics:
            val = result.raw_metrics["gain"]
            # If gain is large (>10), it might be in V/V, convert to dB
            if val > 10:
                result.gain_db = 20 * math.log10(val) if val > 0 else None
            else:
                result.gain_db = val  # Assume already in dB

        # Unity gain frequency / bandwidth
        if "gbw_hz" in result.raw_metrics:
            result.unity_gain_freq_hz = result.raw_metrics["gbw_hz"]
            if result.bandwidth_hz is None:
                result.bandwidth_hz = result.raw_metrics["gbw_hz"]
        elif "ugf" in result.raw_metrics:
            result.unity_gain_freq_hz = result.raw_metrics["ugf"]
            if result.bandwidth_hz is None:
                result.bandwidth_hz = result.raw_metrics["ugf"]
        if "bw" in result.raw_metrics:
            result.bandwidth_hz = result.raw_metrics["bw"]
        if "bandwidth" in result.raw_metrics:
            result.bandwidth_hz = result.raw_metrics["bandwidth"]

        # Phase margin: PM = 180 - (phase_dc - phase_at_ugf)
        if "phase_dc" in result.raw_metrics and "phase_at_ugf" in result.raw_metrics:
            result.phase_margin_deg = 180.0 - (
                result.raw_metrics["phase_dc"] - result.raw_metrics["phase_at_ugf"]
            )
        elif "phase_margin" in result.raw_metrics:
            pm_val = result.raw_metrics["phase_margin"]
            if pm_val < 0:
                result.phase_margin_deg = 180.0 + pm_val
            else:
                result.phase_margin_deg = pm_val
        elif "pm" in result.raw_metrics:
            pm_val = result.raw_metrics["pm"]
            if pm_val < 0:
                result.phase_margin_deg = 180.0 + pm_val
            else:
                result.phase_margin_deg = pm_val

        # Power
        if "power_total" in result.raw_metrics:
            result.power_w = abs(result.raw_metrics["power_total"])
        elif "power" in result.raw_metrics:
            result.power_w = abs(result.raw_metrics["power"])

        # Slew rate (V/s) — transient measurement
        if "slew_rate" in result.raw_metrics:
            result.slew_rate_v_per_s = abs(result.raw_metrics["slew_rate"])

        # Settling time (s) — transient measurement
        if "settling_time" in result.raw_metrics:
            result.settling_time_s = result.raw_metrics["settling_time"]

        # Check if any .meas FAILED
        if re.search(r"FAILED|failed\s+to\s+find", log_content):
            failed_meas = re.findall(
                r"(\w+).*(?:FAILED|failed)", log_content, re.IGNORECASE
            )
            if failed_meas:
                logger.warning(f".meas statements failed: {failed_meas}")
                result.error_message = f"Measurement failed: {', '.join(failed_meas)}"

        result.converged = True
        return result

    def classify_error(self, log_content: str) -> str:
        """Classify the type of Spectre error for appropriate handling.

        Returns one of: "syntax", "convergence", "model_not_found",
                        "node_floating", "timeout", "unknown"
        """
        if not log_content:
            return "unknown"

        log_lower = log_content.lower()

        if any(kw in log_lower for kw in ["syntax error", "parse error", "unexpected token"]):
            return "syntax"

        if any(kw in log_lower for kw in ["convergence", "did not converge"]):
            return "convergence"

        if any(kw in log_lower for kw in ["model not found", "undefined model", "no such model"]):
            return "model_not_found"

        if any(kw in log_lower for kw in ["floating node", "unconnected", "dangling"]):
            return "node_floating"

        if "timeout" in log_lower or "timed out" in log_lower:
            return "timeout"

        return "unknown"

    # --- Mock simulation for development/testing ---

    def _mock_simulate(
        self, netlist_path: Path, run_dir: Path
    ) -> tuple[bool, str, str]:
        """Generate synthetic simulation results for testing without Spectre.

        Uses heuristics based on parameter values to produce plausible results.
        """
        logger.info("[DRY RUN] Generating mock simulation results")

        # Read netlist to extract param values
        netlist_content = netlist_path.read_text(encoding="utf-8")
        params = self._extract_params_from_netlist(netlist_content)

        # Generate plausible mock results based on parameters
        mock_results = self._compute_mock_results(params)

        # Format as a mock Spectre log
        log_lines = [
            "spectre (mock mode)",
            "Spectre (R) Circuit Simulator",
            "Date: mock simulation",
            "",
            "DC analysis `dcOp' converged.",
            "AC analysis `acSweep' completed.",
            "",
            "Measurement Results:",
        ]

        for name, value in mock_results.items():
            log_lines.append(f"  {name} = {value:.6e}")

        log_content = "\n".join(log_lines)

        # Write mock log
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "sim.log").write_text(log_content, encoding="utf-8")

        return True, log_content, ""

    def _extract_params_from_netlist(self, content: str) -> dict[str, float]:
        """Extract .param values from a netlist."""
        params = {}
        for match in re.finditer(
            r"\.param\s+(\w+)\s*=\s*(\S+)", content, re.IGNORECASE
        ):
            name = match.group(1)
            value_str = match.group(2)
            try:
                params[name] = _parse_spice_value(value_str)
            except ValueError:
                continue
        return params

    def _compute_mock_results(self, params: dict[str, float]) -> dict[str, float]:
        """Compute plausible mock performance from parameters.

        This is a simplified heuristic model, NOT accurate circuit simulation.
        Used only for testing the optimization loop logic.
        """
        # Default parameter values for heuristic calculation
        w1 = params.get("W1", 5e-6)
        l1 = params.get("L1", params.get("L", 60e-9))
        w3 = params.get("W3", 10e-6)
        w5 = params.get("W5", params.get("Wtail", 10e-6))
        cc = params.get("Cc", params.get("Cc_val", 1e-12))

        # Simplified heuristics (very rough approximations)
        # gm ~ sqrt(2 * mu * Cox * W/L * Id), simplified
        gm1 = 0.5e-3 * math.sqrt(w1 / l1) * (1 + 0.1 * random.gauss(0, 1))
        # ro ~ 1 / (lambda * Id)
        ro = 50e3 * (l1 / 30e-9) * (1 + 0.1 * random.gauss(0, 1))

        # Gain (dB) ~ 20*log10(gm * ro)
        gain_linear = gm1 * ro
        gain_db = 20 * math.log10(max(gain_linear, 1.0))

        # UGF ~ gm / (2*pi*Cc) for miller-compensated
        ugf = gm1 / (2 * math.pi * max(cc, 0.1e-12))

        # Phase margin (heuristic: depends on compensation)
        pm = 60.0 + 10.0 * (cc / 1e-12) - 5.0 * (ugf / 1e9)
        pm = max(20.0, min(90.0, pm + 5 * random.gauss(0, 1)))

        # Power ~ VDD * Itail, Itail ~ gm^2 / (2 * mu * Cox * W/L)
        power = 0.9 * gm1 * 2.0 / (w1 / l1 * 200e-6)
        power = max(50e-6, min(5e-3, power))

        return {
            "gain_db": gain_db,
            "ugf": ugf,
            "phase_margin": pm - 180.0,  # Store as phase at UGF (negative)
            "power_total": -power / 0.9,  # Negative current convention
        }


def _parse_spice_value(s: str) -> float:
    """Parse a SPICE value string with suffix to float.

    Examples: '5u' -> 5e-6, '180n' -> 180e-9, '10k' -> 10e3
    """
    s = s.strip().lower()
    suffixes = {
        "t": 1e12, "g": 1e9, "meg": 1e6, "k": 1e3,
        "m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15,
    }

    for suffix, multiplier in sorted(suffixes.items(), key=lambda x: -len(x[0])):
        if s.endswith(suffix):
            num_part = s[: -len(suffix)]
            return float(num_part) * multiplier

    return float(s)
