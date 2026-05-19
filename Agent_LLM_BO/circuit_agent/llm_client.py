"""DeepSeek LLM client for circuit design tasks."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from openai import OpenAI

from config import Settings
from models import DesignTarget, NetlistTemplate, ParamSpace, SimResult

logger = logging.getLogger(__name__)


class LLMClient:
    """Wraps DeepSeek API (OpenAI-compatible) for circuit design tasks."""

    def __init__(self, config: Settings):
        self.config = config
        self.client = OpenAI(
            api_key=config.deepseek_api_key,
            base_url=config.deepseek_base_url,
        )
        self.model = config.deepseek_model
        self.system_prompt = self._build_system_prompt()

    def _build_system_prompt(self) -> str:
        """Build system prompt with knowledge base content."""
        kb_path = self.config.get_knowledge_base_path()
        sections = [
            "You are an expert analog circuit designer specializing in TSMC N28 process.",
            "You design circuits that are physically realizable and meet performance targets.",
            "",
            "## Critical Design Rules",
            f"- Process: TSMC N28",
            f"- NMOS model: {self.config.nmos_model}",
            f"- PMOS model: {self.config.pmos_model}",
            f"- VDD = {self.config.vdd}V",
            f"- Minimum channel length L >= {self.config.min_l * 1e9:.0f}nm",
            f"- PDK: .lib '{self.config.pdk_path}' {self.config.pdk_section}",
            "- NMOS bulk connects to gnd! (or VSS)",
            "- PMOS bulk connects to vdd! (or VDD)",
            "- Port order: M<name> <drain> <gate> <source> <bulk> <model> [params]",
            "",
        ]

        # Load knowledge base files
        guide_file = kb_path / "spice_llm_guide.md"
        if guide_file.exists():
            sections.append("## SPICE Writing Guide")
            sections.append(guide_file.read_text(encoding="utf-8"))
            sections.append("")

        constraints_file = kb_path / "pdk_constraints.md"
        if constraints_file.exists():
            sections.append("## PDK Constraints")
            sections.append(constraints_file.read_text(encoding="utf-8"))
            sections.append("")

        return "\n".join(sections)

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

    def generate_initial_netlist(
        self, targets: DesignTarget
    ) -> tuple[NetlistTemplate, ParamSpace]:
        """Generate parametrized SPICE netlist and parameter space from targets.

        Returns:
            (NetlistTemplate, ParamSpace) - the template and optimization search space
        """
        # Load topology examples if available
        examples_dir = self.config.get_knowledge_base_path() / "topology_examples"
        example_content = ""
        if examples_dir.exists():
            for sp_file in examples_dir.glob("*.sp"):
                example_content += f"\n### Example: {sp_file.stem}\n```spice\n"
                example_content += sp_file.read_text(encoding="utf-8")
                example_content += "\n```\n"

        prompt = f"""Generate a complete parametrized SPICE netlist for the following circuit requirements.

## Requirements
{targets.to_prompt_str()}

## Instructions
1. Write a complete SPICE netlist following TSMC N28 rules.
2. Use `.param` statements for ALL optimizable parameters (transistor widths W, lengths L, compensation components Cc/Rc, bias currents).
3. Encapsulate the core circuit in a `.subckt` block.
4. Include a testbench with appropriate biasing and AC/DC stimulus.
5. Include `.meas` statements to extract: gain (dB), unity-gain frequency, phase margin, and total power.
6. Use `.ac dec 20 1 10G` for AC analysis.
7. End with `.end`.

## .meas Statement Format (IMPORTANT)
Use these EXACT measurement names so the parser can extract them:
```
.meas ac gain_db MAX VDB(vout)
.meas ac ugf WHEN VDB(vout)=0 CROSS=1
.meas ac phase_margin FIND VP(vout) WHEN VDB(vout)=0 CROSS=1
.meas dc power_total PARAM='-I(Vdd)*0.9'
```
Adjust node names as needed but keep measurement names: gain_db, ugf, phase_margin, power_total.

{f"## Reference Examples{example_content}" if example_content else ""}

## Output Format
First output the complete SPICE netlist in a ```spice code block.
Then output the parameter search space as a JSON array in a ```json code block with this format:
```json
[
  {{"name": "W1", "low": 0.5e-6, "high": 20e-6, "log_scale": true, "unit": "m"}},
  {{"name": "L1", "low": 30e-9, "high": 500e-9, "log_scale": true, "unit": "m"}}
]
```
Each parameter should have physically reasonable bounds for TSMC N28.
"""

        response = self._call_llm(prompt)
        netlist, param_space = self._parse_netlist_response(response)
        return netlist, param_space

    def validate_and_adjust_params(
        self,
        proposed_params: dict[str, float],
        current_result: SimResult | None,
        param_space: ParamSpace,
        targets: DesignTarget,
    ) -> dict[str, float]:
        """LLM reviews BO-proposed parameters for physical feasibility.

        Called every N iterations to ensure parameters make physical sense.
        """
        result_context = ""
        if current_result:
            result_context = f"""
## Current Simulation Result
{current_result.to_summary_str()}

## Gap to Targets
{targets.to_prompt_str()}
"""

        param_str = "\n".join(
            f"  {k} = {_format_value_with_unit(v)}" for k, v in proposed_params.items()
        )

        prompt = f"""Review the following circuit parameters proposed by Bayesian Optimization.
Check for physical feasibility in TSMC N28 (VDD=0.9V):
- All transistors must be able to enter saturation (Vds > Vgs - Vth, typical Vth ~ 0.4V for N28)
- Matched pairs must have identical L
- Current must be reasonable for the given W/L ratios
- Headroom constraints under VDD=0.9V must be respected
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
        adjusted = self._parse_json_from_response(response)

        if adjusted and isinstance(adjusted, dict):
            # Ensure all required params are present
            for name in proposed_params:
                if name not in adjusted:
                    adjusted[name] = proposed_params[name]
            return adjusted

        # If parsing fails, return proposed params unchanged
        logger.warning("Failed to parse LLM parameter validation response, using proposed params")
        return proposed_params

    def repair_netlist(
        self, netlist: str, error_log: str, attempt: int
    ) -> str:
        """Ask LLM to fix a SPICE netlist that caused Spectre errors.

        Args:
            netlist: The failed netlist content
            error_log: Spectre error output
            attempt: Repair attempt number (1-3), higher = more aggressive

        Returns:
            Corrected netlist string
        """
        aggressiveness = {
            1: "Make minimal changes to fix the specific error.",
            2: "Fix the error and review the entire netlist for other potential issues.",
            3: "Rewrite problematic sections if needed. Ensure all connections are valid.",
        }

        prompt = f"""The following SPICE netlist failed in Spectre simulation.
Fix the errors and return a corrected netlist.

## Error Log
```
{error_log[-3000:]}
```

## Current Netlist
```spice
{netlist}
```

## Repair Instructions (Attempt {attempt}/3)
{aggressiveness.get(attempt, aggressiveness[3])}

CRITICAL RULES:
- NMOS model = nch_mac, PMOS model = pch_mac
- NMOS bulk -> gnd!, PMOS bulk -> vdd!
- VDD = 0.9V
- Min L = 30n
- Keep all .meas statements intact
- Keep the .param block intact

Output ONLY the corrected complete netlist in a ```spice code block.
"""

        response = self._call_llm(prompt)
        corrected = self._extract_code_block(response, "spice")
        if not corrected:
            logger.error("Failed to extract corrected netlist from LLM response")
            return netlist  # Return original if parsing fails
        return corrected

    def suggest_topology_change(
        self,
        current_result: SimResult,
        targets: DesignTarget,
        history_summary: str,
    ) -> tuple[NetlistTemplate, ParamSpace] | None:
        """Ask LLM to suggest a topology change when optimization is stagnant.

        Returns:
            New (template, param_space) or None if LLM suggests staying with current topology.
        """
        prompt = f"""The circuit optimization is stuck. After many iterations, performance has plateaued.

## Current Best Performance
{current_result.to_summary_str()}

## Targets (NOT MET)
{targets.to_prompt_str()}

## Optimization History Summary
{history_summary}

## Task
Analyze why the current topology cannot meet the targets. Then either:
1. Suggest a modified topology (e.g., add cascode, change compensation strategy, add gain-boosting)
2. If you believe the current topology CAN meet targets with different parameter ranges, say "KEEP_TOPOLOGY" and suggest new parameter bounds.

If suggesting a new topology, output:
1. A complete new SPICE netlist in ```spice block
2. New parameter search space in ```json block

If keeping current topology, output only:
```json
{{"action": "KEEP_TOPOLOGY", "reason": "...", "new_bounds": [...]}}
```
"""

        response = self._call_llm(prompt)

        # Check if LLM suggests keeping topology
        if "KEEP_TOPOLOGY" in response:
            json_data = self._parse_json_from_response(response)
            if json_data and json_data.get("action") == "KEEP_TOPOLOGY":
                logger.info(f"LLM suggests keeping topology: {json_data.get('reason', '')}")
                return None

        # Otherwise parse new topology
        try:
            netlist, param_space = self._parse_netlist_response(response)
            return netlist, param_space
        except Exception as e:
            logger.warning(f"Failed to parse topology change suggestion: {e}")
            return None

    def parse_user_requirements(self, user_input: str) -> DesignTarget:
        """Use LLM to parse free-form user input into structured DesignTarget."""
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
  "topology_hint": "<string describing topology>"
}}
```

Rules:
- Convert all values to base SI units (Hz not MHz, W not mW, F not pF)
- gain_db stays in dB
- phase_margin_deg stays in degrees
- If user says "BW > 100MHz", bandwidth_hz = 100e6
- If user says "power < 2mW", power_w = 2e-3
- If user says "CL = 1pF", load_cap_f = 1e-12
"""

        response = self._call_llm(prompt, max_tokens=1024)
        data = self._parse_json_from_response(response)

        if not data:
            raise ValueError("Failed to parse user requirements from LLM response")

        return DesignTarget(
            gain_db=data.get("gain_db"),
            bandwidth_hz=data.get("bandwidth_hz"),
            phase_margin_deg=data.get("phase_margin_deg"),
            power_w=data.get("power_w"),
            load_cap_f=data.get("load_cap_f"),
            topology_hint=data.get("topology_hint", ""),
        )

    # --- Private helper methods ---

    def _parse_netlist_response(
        self, response: str
    ) -> tuple[NetlistTemplate, ParamSpace]:
        """Parse LLM response containing SPICE netlist and param space JSON."""
        netlist_content = self._extract_code_block(response, "spice")
        if not netlist_content:
            raise ValueError("No SPICE code block found in LLM response")

        json_content = self._extract_code_block(response, "json")
        if not json_content:
            raise ValueError("No JSON code block found in LLM response")

        param_data = json.loads(json_content)

        # Handle both array format and object format
        if isinstance(param_data, dict) and "params" in param_data:
            param_data = param_data["params"]

        template = NetlistTemplate.from_netlist(netlist_content)
        param_space = ParamSpace.from_dict(param_data)

        return template, param_space

    def _extract_code_block(self, text: str, lang: str) -> str | None:
        """Extract content from a fenced code block with given language."""
        pattern = rf"```{lang}\s*\n(.*?)```"
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()
        # Try without language specifier if specific one not found
        if lang == "spice":
            # Also try ```spice or just the first large code block
            pattern2 = r"```(?:spice|SPICE|sp)?\s*\n(.*?)```"
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
            for match in reversed(matches):  # Try last match first
                try:
                    return json.loads(match)
                except json.JSONDecodeError:
                    continue
        return None


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
