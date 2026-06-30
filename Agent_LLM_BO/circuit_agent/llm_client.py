"""Lightweight LLM client for circuit design tasks.

Responsibilities (narrowly scoped):
  1. parse_user_requirements()  — free-text → structured DesignTarget
  2. validate_and_adjust_params() — physical-feasibility check during BO
  3. select_topology()           — LLM-assisted topology selection (optional)

Netlist generation is handled by the hard-constrained topology library
(topologies/), NOT by LLM.  Repair and topology-change suggestions are
handled by predefined escalation rules, also NOT by LLM.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from openai import OpenAI

from config import Settings
from models import DesignTarget, ParamSpace, SimResult

logger = logging.getLogger(__name__)


class LLMClient:
    """Wraps DeepSeek API (OpenAI-compatible) for circuit parameter validation.

    Only two core functions are retained:
    - parse natural-language requirements into DesignTarget
    - validate BO-proposed parameters for physical feasibility
    """

    def __init__(self, config: Settings):
        self.config = config
        self.client = OpenAI(
            api_key=config.deepseek_api_key,
            base_url=config.deepseek_base_url,
        )
        self.model = config.deepseek_model
        self.system_prompt = self._build_system_prompt()

    # ------------------------------------------------------------------
    # System prompt
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        """Build a concise system prompt with PDK constraints only.

        No netlist-generation instructions — those belong in the topology
        library, not here.
        """
        kb_path = self.config.get_knowledge_base_path()
        sections = [
            "You are an expert analog circuit designer specializing in TSMC N28 process.",
            "",
            "## Process & PDK",
            f"- Process: TSMC N28",
            f"- NMOS model: {self.config.nmos_model}",
            f"- PMOS model: {self.config.pmos_model}",
            f"- VDD = {self.config.vdd}V (core devices)",
            f"- Minimum channel length L >= {self.config.min_l * 1e9:.0f}nm",
            f"- Maximum width per finger: {self.config.max_width_per_finger * 1e6:.0f}um",
            "- Spectre MOS width semantics: effective width = W * M; "
            "nf only splits W into fingers",
            f"- NMOS bulk → gnd! (or VSS), PMOS bulk → vdd! (or VDD)",
            f"- Analog design recommendation: L >= 60nm for better output impedance",
            "",
        ]

        # Load knowledge base files if available
        constraints_file = kb_path / "pdk_constraints.md"
        if constraints_file.exists():
            sections.append("## PDK Constraints (full)")
            sections.append(constraints_file.read_text(encoding="utf-8"))
            sections.append("")

        return "\n".join(sections)

    # ------------------------------------------------------------------
    # Core API call
    # ------------------------------------------------------------------

    def _call_llm(self, user_prompt: str, max_tokens: int = 8192) -> str:
        """Call DeepSeek API with retry logic."""
        for attempt in range(3):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    messages=[
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                )
                return response.choices[0].message.content
            except Exception as e:
                logger.warning(f"LLM API call failed (attempt {attempt + 1}/3): {e}")
                if attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                else:
                    raise RuntimeError(f"LLM API failed after 3 attempts: {e}") from e

    # ------------------------------------------------------------------
    # 1. Parse user requirements  (retained)
    # ------------------------------------------------------------------

    def parse_user_requirements(self, user_input: str) -> tuple[DesignTarget, str]:
        """Use LLM to parse free-form user input into structured DesignTarget.

        Returns:
            (DesignTarget, project_name)
        """
        prompt = f"""Parse the following circuit design requirement into structured specifications.

User input: "{user_input}"

Extract and output a JSON object with these fields (use null if not specified):
```json
{{
  "gain_db": <number or null>,
  "bandwidth_hz": <number or null>,
  "phase_margin_deg": <number or null>,
  "power_w": <number or null>,
  "load_cap_f": <number or null>,
  "slew_rate_v_per_s": <number or null>,
  "settling_time_s": <number or null>,
  "topology_hint": "<string describing topology>",
  "project_name": "<short filesystem-safe name, e.g. 5T_OTA_G40dB_BW500M>"
}}
```

Rules:
- Convert all values to base SI units (Hz not MHz, W not mW, F not pF)
- gain_db stays in dB
- phase_margin_deg stays in degrees
- Treat "BW", "GBW", and "UGF" as the op-amp gain-bandwidth target
- If user says "GBW > 100MHz", bandwidth_hz = 100e6
- If user says "power < 2mW", power_w = 2e-3
- If user says "CL = 1pF", load_cap_f = 1e-12
- If user says "SR > 100V/us", slew_rate_v_per_s = 100e6
- If user says "0.1% settling time < 10ns", settling_time_s = 10e-9
- project_name: short, filesystem-safe, descriptive. Use underscores. Max 40 chars.
"""

        response = self._call_llm(prompt, max_tokens=1024)
        data = self._parse_json_from_response(response)

        if not data:
            raise ValueError("Failed to parse user requirements from LLM response")

        project_name = data.get("project_name", "")
        return DesignTarget(
            gain_db=data.get("gain_db"),
            bandwidth_hz=data.get("bandwidth_hz"),
            phase_margin_deg=data.get("phase_margin_deg"),
            power_w=data.get("power_w"),
            load_cap_f=data.get("load_cap_f"),
            slew_rate_v_per_s=data.get("slew_rate_v_per_s"),
            settling_time_s=data.get("settling_time_s"),
            topology_hint=data.get("topology_hint", ""),
        ), project_name

    # ------------------------------------------------------------------
    # 2. Validate BO-proposed parameters  (retained)
    # ------------------------------------------------------------------

    def validate_and_adjust_params(
        self,
        proposed_params: dict[str, float],
        current_result: SimResult | None,
        param_space: ParamSpace,
        targets: DesignTarget,
        circuit_template: str | None = None,
        dialogue_dir: str | None = None,
        iteration: int = 0,
        topology_name: str = "",
    ) -> dict[str, float]:
        """LLM reviews BO-proposed parameters for physical feasibility.

        Called every N iterations to ensure parameters make physical sense
        (saturation conditions, headroom, current density, etc.).

        circuit_template: optional DUT subcircuit netlist showing topology.
        topology_name: optional topology identifier for context.
        """
        result_context = ""
        if current_result:
            result_context = f"""
## Current Simulation Result
{current_result.to_summary_str()}

## Gap to Targets
{targets.to_prompt_str()}
"""

        topology_context = ""
        if circuit_template:
            topology_context = f"""
## Circuit Topology (DUT subcircuit)
```spice
{circuit_template.strip()}
```
"""

        param_str = "\n".join(
            f"  {k} = {_format_value_with_unit(v)}" for k, v in proposed_params.items()
        )

        topo_hint = f"\nTopology: {topology_name}" if topology_name else ""

        prompt = f"""Review the following circuit parameters proposed by Bayesian Optimization.{topo_hint}
{topology_context}
Check for physical feasibility in TSMC N28 (VDD=0.9 - 1.1V):
- All transistors should be able to enter saturation (Vds > Vgs - Vth, typical Vth ~ 0.4V for N28)
- Current must be reasonable for the given W/L ratios
- Headroom constraints under VDD=0.9 - 1.1V must be respected
- Refer to the circuit topology above to understand each transistor's role
{result_context}
## Proposed Parameters
{param_str}

## Instructions
If parameters are physically valid, output them unchanged.
If any parameter needs adjustment, modify it and explain why briefly.

Output the final parameters as a JSON object in a ```json code block:
```json
{{"W1": 5e-6, "L1": 60e-9, ...}}
```
"""

        response = self._call_llm(prompt, max_tokens=2048)

        # Log dialogue to file if requested
        if dialogue_dir:
            self._save_dialogue(
                dialogue_dir, iteration, "validate_params",
                prompt, response,
            )

        adjusted = self._parse_json_from_response(response)

        if adjusted and isinstance(adjusted, dict):
            # Ensure all required params are present
            for name in proposed_params:
                if name not in adjusted:
                    adjusted[name] = proposed_params[name]
            # Clamp each value to its param_space bounds (LLM may hallucinate)
            clamped = {}
            for p in param_space.params:
                value = float(adjusted.get(p.name, proposed_params[p.name]))
                value = max(p.low, min(p.high, value))
                clamped[p.name] = value
            return clamped

        logger.warning("Failed to parse LLM parameter validation response, using proposed params")
        return proposed_params

    # ------------------------------------------------------------------
    # 3. Topology selection  (new, optional)
    # ------------------------------------------------------------------

    def select_topology(
        self,
        user_requirements: str,
        available_topologies: list[dict],
    ) -> str | None:
        """LLM-assisted topology selection when rule-based heuristics are ambiguous.

        Args:
            user_requirements: free-text description of what the user wants.
            available_topologies: list of TopologyMeta dicts from list_topologies().

        Returns:
            Selected topology name, or None if LLM cannot decide.
        """
        topo_list = "\n".join(
            f"- **{t['name']}**: {t['display_name']} — {t['description']} "
            f"(gain {t['min_gain_db']}-{t['max_gain_db']} dB, "
            f"GBW {t.get('min_gbw_hz', t.get('min_bw_hz'))}-"
            f"{t.get('max_gbw_hz', t.get('max_bw_hz'))} Hz, "
            f"typical power {t['typical_power_w']} W)"
            for t in available_topologies
        )

        prompt = f"""Select the best circuit topology for the following requirements.

## User Requirements
{user_requirements}

## Available Topologies
{topo_list}

## Instructions
Pick the single best topology. Output only a JSON object:
```json
{{"topology": "<name>", "reason": "<one-sentence justification>"}}
```
"""

        try:
            response = self._call_llm(prompt, max_tokens=512)
            data = self._parse_json_from_response(response)
            if data and data.get("topology") in {t["name"] for t in available_topologies}:
                logger.info(
                    "LLM selected topology: %s — %s",
                    data["topology"], data.get("reason", "")
                )
                return data["topology"]
        except Exception as e:
            logger.warning("LLM topology selection failed: %s", e)

        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _save_dialogue(
        dialogue_dir: str,
        iteration: int,
        tag: str,
        prompt: str,
        response: str,
    ) -> None:
        """Save an LLM prompt/response pair to a markdown log file."""
        import datetime

        dp = Path(dialogue_dir)
        dp.mkdir(parents=True, exist_ok=True)
        filename = f"iter_{iteration:03d}_{tag}.md"
        filepath = dp / filename

        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        content = f"""# {tag} — Iteration {iteration}
**Time**: {timestamp}

## Prompt

{prompt}

---

## Response

{response}
"""
        filepath.write_text(content, encoding="utf-8")
        logger.debug(f"Saved dialogue to {filepath}")

    # --- Code-block extraction (retained — used by both methods) ---

    def _extract_code_block(self, text: str, lang: str) -> str | None:
        """Extract content from a fenced code block with given language."""
        pattern = rf"```{lang}\s*\n(.*?)```"
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()
        # Fallback: try alternate language tags
        if lang == "spice":
            pattern2 = r"```(?:spice|SPICE|sp)?\s*\n(.*?)```"
            match2 = re.search(pattern2, text, re.DOTALL)
            if match2:
                return match2.group(1).strip()
        if lang == "circuit":
            pattern2 = r"```(?:circuit|spice|SPICE|sp|cir)?\s*\n(.*?)```"
            match2 = re.search(pattern2, text, re.DOTALL)
            if match2:
                return match2.group(1).strip()
        if lang == "testbench":
            pattern2 = r"```(?:testbench|tb|spice|SPICE|sp)?\s*\n(.*?)```"
            match2 = re.search(pattern2, text, re.DOTALL)
            if match2:
                return match2.group(1).strip()
        return None

    def _parse_json_from_response(self, text: str) -> dict | list | None:
        """Extract and parse JSON from LLM response."""
        json_str = self._extract_code_block(text, "json")
        if json_str:
            try:
                return json.loads(json_str)
            except json.JSONDecodeError:
                pass

        # Try to find JSON directly in text
        json_patterns = [
            r'\{[^{}]*\}',  # Simple object
            r'\[[^\[\]]*\]',  # Simple array
        ]
        for pattern in json_patterns:
            matches = re.findall(pattern, text, re.DOTALL)
            for match in reversed(matches):
                try:
                    return json.loads(match)
                except json.JSONDecodeError:
                    continue
        return None

    # --- Static helpers (still used by main.py file mode) ---

    @staticmethod
    def _split_monolithic_netlist(content: str) -> tuple[str, str]:
        """Split a monolithic HSPICE or Spectre netlist into DUT and testbench."""
        subckt_match = re.search(
            r'(^\s*\.?subckt\s+\w+.*?^\s*\.?ends\s*\w*)',
            content,
            re.DOTALL | re.IGNORECASE | re.MULTILINE,
        )
        if subckt_match:
            subckt_end = subckt_match.end()
            circuit = content[:subckt_end].strip()
            testbench = content[subckt_end:].strip()
            return circuit, testbench
        else:
            logger.warning("No subckt found in monolithic netlist, auto-wrapping")
            return LLMClient._wrap_monolithic_netlist(content)

    @staticmethod
    def _wrap_monolithic_netlist(content: str) -> tuple[str, str]:
        """Wrap a flat netlist into a .subckt and extract the testbench portion."""
        lines = content.split('\n')
        lib_lines = []
        param_lines = []
        device_lines = []
        tb_lines = []
        in_tb = False

        for line in lines:
            stripped = line.strip()
            if not in_tb:
                if stripped.startswith('.lib') or stripped.startswith('.include'):
                    lib_lines.append(line)
                elif stripped.startswith('.param'):
                    param_lines.append(line)
                elif re.match(r'^[MVIRCLX]', stripped, re.IGNORECASE) and not in_tb:
                    device_lines.append(line)
                elif any(
                    stripped.lower().startswith(kw)
                    for kw in ('.op', '.ac', '.dc', '.tran', '.meas', '.end',
                               'vdd', 'vss', 'v', 'i')
                ):
                    in_tb = True
                    tb_lines.append(line)
                else:
                    device_lines.append(line)
            else:
                tb_lines.append(line)

        subckt_name = "dut"
        circuit = '\n'.join(lib_lines + param_lines)
        circuit += f'\n.subckt {subckt_name} vip vin vout vdd vss\n'
        circuit += '\n'.join(device_lines)
        circuit += f'\n.ends {subckt_name}\n'

        testbench = '\n'.join(tb_lines) if tb_lines else (
            ".include \"circuit.cir\"\n"
            "VDD vdd 0 DC 0.9\n"
            "VSS vss 0 DC 0\n"
            ".end\n"
        )

        return circuit, testbench


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _format_value_with_unit(value: float) -> str:
    """Format a parameter value with engineering unit for display."""
    abs_val = abs(value)
    if abs_val >= 1e-3:
        return f"{value:.4g}"
    elif abs_val >= 1e-6:
        return f"{value * 1e6:.3g}u"
    elif abs_val >= 1e-9:
        return f"{value * 1e9:.3g}n"
    elif abs_val >= 1e-12:
        return f"{value * 1e12:.3g}p"
    else:
        return f"{value:.3e}"
