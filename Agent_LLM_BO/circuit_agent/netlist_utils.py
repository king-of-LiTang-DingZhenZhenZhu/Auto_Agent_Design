"""Utilities for normalizing legacy monolithic circuit netlists."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def split_monolithic_netlist(content: str) -> tuple[str, str]:
    """Split a monolithic HSPICE or Spectre netlist into DUT and testbench."""
    subckt_match = re.search(
        r"(^\s*\.?subckt\s+\w+.*?^\s*\.?ends\s*\w*)",
        content,
        re.DOTALL | re.IGNORECASE | re.MULTILINE,
    )
    if subckt_match:
        subckt_end = subckt_match.end()
        circuit = content[:subckt_end].strip()
        testbench = content[subckt_end:].strip()
        return circuit, testbench

    logger.warning("No subckt found in monolithic netlist, auto-wrapping")
    return wrap_monolithic_netlist(content)


def wrap_monolithic_netlist(content: str) -> tuple[str, str]:
    """Wrap a flat netlist in a DUT subcircuit and retain its testbench."""
    lib_lines: list[str] = []
    param_lines: list[str] = []
    device_lines: list[str] = []
    testbench_lines: list[str] = []
    in_testbench = False

    for line in content.splitlines():
        stripped = line.strip()
        if in_testbench:
            testbench_lines.append(line)
        elif stripped.startswith((".lib", ".include")):
            lib_lines.append(line)
        elif stripped.startswith(".param"):
            param_lines.append(line)
        elif re.match(r"^[MVIRCLX]", stripped, re.IGNORECASE):
            device_lines.append(line)
        elif any(
            stripped.lower().startswith(keyword)
            for keyword in (
                ".op", ".ac", ".dc", ".tran", ".meas", ".end",
                "vdd", "vss", "v", "i",
            )
        ):
            in_testbench = True
            testbench_lines.append(line)
        else:
            device_lines.append(line)

    circuit = "\n".join(lib_lines + param_lines)
    circuit += "\n.subckt dut vip vin vout vdd vss\n"
    circuit += "\n".join(device_lines)
    circuit += "\n.ends dut\n"

    testbench = "\n".join(testbench_lines) if testbench_lines else (
        '.include "circuit.cir"\n'
        "VDD vdd 0 DC 0.9\n"
        "VSS vss 0 DC 0\n"
        ".end\n"
    )
    return circuit, testbench
