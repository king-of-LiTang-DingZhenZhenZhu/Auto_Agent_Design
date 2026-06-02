"""DeepSeek LLM client for circuit design tasks."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from openai import OpenAI

from config import Settings
from models import CircuitFiles, DesignTarget, NetlistTemplate, ParamSpace, SimResult

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
            "",
            "## Critical Design Rules",
            f"- Process: TSMC N28",
            f"- NMOS model: {self.config.nmos_model}",
            f"- PMOS model: {self.config.pmos_model}",
            f"- VDD = {self.config.vdd}V",
            f"- Minimum channel length L >= {self.config.min_l * 1e9:.0f}nm",
            f"- Maximum width per finger: {self.config.max_width_per_finger * 1e6:.0f}um (use nf multiplier for larger effective widths)",
            f"- Total effective width = W * nf * M",
            "- nf and M are system-managed: only W_total goes in .param and parameter space",
            f"- HSPICE PDK (.cir/.sp): .lib '{self.config.pdk_hspice_path}' {self.config.pdk_hspice_section}",
            f"- Spectre PDK (.scs): include \"{self.config.pdk_spectre_path}\" section={self.config.pdk_spectre_section}",
            "- NMOS bulk connects to gnd! (or VSS)",
            "- PMOS bulk connects to vdd! (or VDD)",
            "- Port order: M<name> <drain> <gate> <source> <bulk> <model> [params]",
            "- ALWAYS add nf=<N> on every transistor line (even if nf=1). The system updates nf automatically.",
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

    @staticmethod
    def _save_dialogue(
        dialogue_dir: str,
        iteration: int,
        tag: str,
        prompt: str,
        response: str,
    ) -> None:
        """Save an LLM prompt/response pair to a markdown log file."""
        from pathlib import Path
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

    def generate_initial_netlist(
        self, targets: DesignTarget
    ) -> tuple[CircuitFiles, ParamSpace]:
        """Generate parametrized SPICE netlist and parameter space from targets.

        Returns:
            (CircuitFiles, ParamSpace) - split circuit + testbench and optimization search space
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
8. CRITICAL - Finger splitting rules:
   - W parameters represent TOTAL effective width. The system will automatically split into fingers if W > {self.config.max_width_per_finger * 1e6:.0f}um.
   - Every transistor MUST have `nf=1` on its line (e.g., `M1 ... w='Wdp' l='Ldp' nf=1`). The system will update nf automatically.
   - W in .param is the total effective width, NOT the per-finger width.
   - DO NOT add nf or M as .param entries — the system manages them automatically.
   - DO NOT include nf or M in the parameter search space JSON — system manages them.
   - DO include ALL .param variables: transistor W/L, compensation capacitors (Cc), nulling resistors (Rz), bias currents (Ibias), etc.

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
Output THREE separate code blocks in this order:

1. Circuit netlist in a ```circuit code block:
   - .lib statement at the top
   - All .param statements for optimizable parameters
   - A single .subckt block containing all transistors
   - Signal ports in order: inputs, outputs, bias, vdd, vss
   - Every transistor MUST have `nf=1` on its line

2. Testbench in a ```testbench code block:
   - .include "circuit.cir" to bring in the DUT (relative path, same directory)
   - Power supplies: VDD (0.9V), VSS (0V)
   - Bias voltage source
   - Input sources with AC stimulus (DC offset + AC 1)
   - DUT instantiation: Xdut ... <subckt_name>
   - Load capacitance
   - .op and .ac dec 20 1 10G
   - EXACT .meas statements listed below
   - .end

3. Parameter search space in a ```json code block:
```json
[
  {{"name": "Wtail", "low": 0.5e-6, "high": 20e-6, "log_scale": true, "unit": "m", "max_per_finger": 3e-6}},
  {{"name": "Ltail", "low": 30e-9, "high": 500e-9, "log_scale": true, "unit": "m"}},
  {{"name": "Cc", "low": 0.1e-12, "high": 10e-12, "log_scale": true, "unit": "F"}}
]
```
- For width (W) parameters, always include `"max_per_finger": 3e-6`
- For length (L) parameters, do NOT include max_per_finger
- For C/R/I parameters (capacitors, resistors, currents), use `"log_scale": true`, no max_per_finger
- DO NOT include nf or M in the parameter space — system manages them
- Each parameter should have physically reasonable bounds for TSMC N28
- Include ALL .param variables you declared, not just transistor widths and lengths
"""

        response = self._call_llm(prompt)
        circuit_files, param_space = self._parse_split_netlist_response(response)
        return circuit_files, param_space

    def validate_and_adjust_params(
        self,
        proposed_params: dict[str, float],
        current_result: SimResult | None,
        param_space: ParamSpace,
        targets: DesignTarget,
        circuit_template: str | None = None,
        dialogue_dir: str | None = None,
        iteration: int = 0,
    ) -> dict[str, float]:
        """LLM reviews BO-proposed parameters for physical feasibility.

        Called every N iterations to ensure parameters make physical sense.
        circuit_template: optional DUT subcircuit netlist showing topology and
                          which transistor each parameter belongs to.
        dialogue_dir: if set, logs the full prompt and LLM response to a file.
        iteration: current optimization iteration (for log file naming).
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

        prompt = f"""Review the following circuit parameters proposed by Bayesian Optimization.
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
            # Clamp each value to its param_space bounds (LLM may hallucinate out-of-range values)
            clamped = {}
            for p in param_space.params:
                value = float(adjusted.get(p.name, proposed_params[p.name]))
                value = max(p.low, min(p.high, value))
                clamped[p.name] = value
            return clamped

        # If parsing fails, return proposed params unchanged
        logger.warning("Failed to parse LLM parameter validation response, using proposed params")
        return proposed_params

    def repair_netlist(
        self, netlist: str, error_log: str, attempt: int,
        testbench: str | None = None,
    ) -> str | tuple[str, str]:
        """Ask LLM to fix a SPICE netlist that caused Spectre errors.

        Args:
            netlist: The failed netlist content (circuit DUT if testbench provided)
            error_log: Spectre error output
            attempt: Repair attempt number (1-3), higher = more aggressive
            testbench: Optional testbench content. If provided, both files are repaired.

        Returns:
            Corrected netlist string, or (circuit_str, testbench_str) if testbench provided
        """
        aggressiveness = {
            1: "Make minimal changes to fix the specific error.",
            2: "Fix the error and review the entire netlist for other potential issues.",
            3: "Rewrite problematic sections if needed. Ensure all connections are valid.",
        }

        if testbench is not None:
            prompt = f"""The following SPICE circuit and testbench failed in Spectre simulation.
Fix the errors and return corrected files.

## Error Log
```
{error_log[-3000:]}
```

## Circuit Netlist
```circuit
{netlist}
```

## Testbench
```testbench
{testbench}
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

Output the corrected circuit in a ```circuit code block,
then the corrected testbench in a ```testbench code block.
"""
            response = self._call_llm(prompt)
            corrected_circuit = self._extract_code_block(response, "circuit")
            corrected_tb = self._extract_code_block(response, "testbench")
            if not corrected_circuit:
                logger.error("Failed to extract corrected circuit from LLM response")
                return netlist, testbench
            return corrected_circuit, corrected_tb or testbench
        else:
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
                return netlist
            return corrected

    def suggest_topology_change(
        self,
        current_result: SimResult,
        targets: DesignTarget,
        history_summary: str,
    ) -> tuple[CircuitFiles, ParamSpace] | None:
        """Ask LLM to suggest a topology change when optimization is stagnant.

        Returns:
            New (circuit_files, param_space) or None if LLM suggests staying with current topology.
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

If suggesting a new topology, output THREE code blocks:

1. New circuit netlist in ```circuit block:
   - .lib statement, .param declarations, .subckt block
   - Every transistor MUST have `nf=1` on its line
   - W parameters are total effective widths (system handles finger splitting)
   - DO NOT add nf or M as .param entries — system manages them

2. New testbench in ```testbench block:
   - .include "circuit.cir"
   - Power supplies, bias, input stimulus, DUT instantiation
   - .op, .ac dec 20 1 10G, .meas statements, .end

3. New parameter search space in ```json block:
   - Width (W) params must include `"max_per_finger": 3e-6`
   - DO NOT include nf or M in the parameter space

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
            circuit_files, param_space = self._parse_split_netlist_response(response)
            return circuit_files, param_space
        except Exception as e:
            logger.warning(f"Failed to parse topology change suggestion: {e}")
            return None

    def parse_user_requirements(self, user_input: str) -> tuple[DesignTarget, str]:
        """Use LLM to parse free-form user input into structured DesignTarget and project name.

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
  "topology_hint": "<string describing topology>",
  "project_name": "<short filesystem-safe name, e.g. 5T_OTA_G40dB_BW500M>"
}}
```

Rules:
- Convert all values to base SI units (Hz not MHz, W not mW, F not pF)
- gain_db stays in dB
- phase_margin_deg stays in degrees
- If user says "BW > 100MHz", bandwidth_hz = 100e6
- If user says "power < 2mW", power_w = 2e-3
- If user says "CL = 1pF", load_cap_f = 1e-12
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
            topology_hint=data.get("topology_hint", ""),
        ), project_name

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

    def _parse_split_netlist_response(
        self, response: str
    ) -> tuple[CircuitFiles, ParamSpace]:
        """Parse LLM response containing circuit, testbench, and param space JSON blocks.

        Falls back to monolithic SPICE parsing if circuit/testbench blocks aren't found.
        """
        circuit_content = self._extract_code_block(response, "circuit")
        testbench_content = self._extract_code_block(response, "testbench")

        if not circuit_content or not testbench_content:
            # Fallback: try to parse as monolithic SPICE and split on .subckt boundary
            logger.warning("No circuit/testbench blocks found, attempting fallback split")
            spice_content = self._extract_code_block(response, "spice")
            if spice_content:
                circuit_content, testbench_content = self._split_monolithic_netlist(spice_content)

        if not circuit_content:
            raise ValueError("No circuit code block found in LLM response")
        if not testbench_content:
            raise ValueError("No testbench code block found in LLM response")

        json_content = self._extract_code_block(response, "json")
        if not json_content:
            raise ValueError("No JSON code block found in LLM response")

        param_data = json.loads(json_content)

        if isinstance(param_data, dict) and "params" in param_data:
            param_data = param_data["params"]

        circuit_name = CircuitFiles.extract_subckt_name(circuit_content)
        param_space = ParamSpace.from_dict(param_data)

        return CircuitFiles(
            circuit_netlist=circuit_content,
            testbench=testbench_content,
            circuit_name=circuit_name,
        ), param_space

    @staticmethod
    def _split_monolithic_netlist(content: str) -> tuple[str, str]:
        """Split a monolithic SPICE netlist into circuit (.subckt) and testbench parts."""
        subckt_match = re.search(r'(\.subckt\s+\w+.*?\.ends\s*\w*)', content, re.DOTALL | re.IGNORECASE)
        if subckt_match:
            # Circuit: everything from .lib through .ends
            subckt_end = subckt_match.end()
            circuit = content[:subckt_end].strip()
            # Testbench: everything after .ends
            testbench = content[subckt_end:].strip()
            # Ensure testbench has .end
            if '.end' not in testbench:
                testbench += '\n.end\n'
            return circuit, testbench
        else:
            # No subckt found; wrap transistor lines in a generated subckt
            logger.warning("No .subckt found in monolithic netlist, auto-wrapping")
            return LLMClient._wrap_monolithic_netlist(content)

    @staticmethod
    def _wrap_monolithic_netlist(content: str) -> tuple[str, str]:
        """Wrap a flat netlist into a .subckt and extract the testbench portion."""
        lines = content.split('\n')
        # Find .lib / .include lines for circuit part
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
                elif any(stripped.lower().startswith(kw) for kw in ('.op', '.ac', '.dc', '.tran', '.meas', '.end', 'vdd', 'vss', 'v', 'i')):
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
