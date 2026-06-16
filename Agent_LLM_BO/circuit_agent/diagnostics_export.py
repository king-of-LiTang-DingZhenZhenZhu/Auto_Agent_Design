"""Export compact diagnostics from Spectre PSF ASCII results."""

from __future__ import annotations

import csv
import math
import re
from pathlib import Path


_MOS_RE = re.compile(
    r"^\s*(M\w+)\s*\(([^)]+)\)\s+(\S+)\b",
    re.IGNORECASE | re.MULTILINE,
)


def inject_diagnostic_saves(testbench: str, circuit_netlist: str) -> str:
    """Add explicit MOS D/G/S node saves to an AC testbench."""
    if "Diagnostic node saves" in testbench:
        return testbench
    if not re.search(r"(?m)^\s*\w+\s+ac\b", testbench):
        return testbench

    nodes = _mos_dgs_nodes(circuit_netlist)
    if not nodes:
        return testbench

    save_lines = ["// Diagnostic node saves"]
    for i in range(0, len(nodes), 6):
        chunk = " ".join(f"Xdut.{node}" for node in nodes[i:i + 6])
        save_lines.append(f"save {chunk}")
    block = "\n".join(save_lines)

    match = re.search(r"(?m)^save\s+", testbench)
    if match:
        return testbench[:match.start()] + block + "\n" + testbench[match.start():]
    return testbench.rstrip() + "\n\n" + block + "\n"


def export_diagnostics(raw_dir: Path, netlist_path: Path, out_dir: Path) -> dict[str, str]:
    """Export OP and AC diagnostics CSVs from a run directory."""
    exported: dict[str, str] = {}
    if not raw_dir.exists() or not netlist_path.exists():
        return exported

    out_dir.mkdir(parents=True, exist_ok=True)
    circuit_text = netlist_path.read_text(encoding="utf-8", errors="replace")

    ac_path = _first_existing(raw_dir, ("ac1.ac", "*.ac"))
    if ac_path:
        out_path = out_dir / "ac_response.csv"
        if _export_ac_response(ac_path, out_path):
            exported["ac_response"] = str(out_path)

    info_path = _first_existing(raw_dir, ("op1.info", "opInfo.info", "*.info"))
    dc_path = _first_existing(raw_dir, ("op1.dc", "*.dc"))
    if info_path:
        out_path = out_dir / "dc_operating_points.csv"
        if _export_operating_points(info_path, dc_path, circuit_text, out_path):
            exported["dc_operating_points"] = str(out_path)

    return exported


def _mos_dgs_nodes(circuit_netlist: str) -> list[str]:
    nodes: list[str] = []
    seen: set[str] = set()
    for _name, node_text, _model in _MOS_RE.findall(circuit_netlist):
        parts = node_text.split()
        if len(parts) < 3:
            continue
        for node in parts[:3]:
            if node == "0" or node in seen:
                continue
            seen.add(node)
            nodes.append(node)
    return nodes


def _mos_connections(circuit_netlist: str) -> dict[str, tuple[str, str, str]]:
    conns: dict[str, tuple[str, str, str]] = {}
    for name, node_text, _model in _MOS_RE.findall(circuit_netlist):
        parts = node_text.split()
        if len(parts) >= 3:
            conns[name] = (parts[0], parts[1], parts[2])
    return conns


def _first_existing(root: Path, patterns: tuple[str, ...]) -> Path | None:
    for pattern in patterns:
        if "*" not in pattern:
            path = root / pattern
            if path.exists():
                return path
            continue
        matches = sorted(root.glob(pattern))
        if matches:
            return matches[0]
    return None


def _export_ac_response(ac_path: Path, out_path: Path) -> bool:
    rows = []
    freq: float | None = None
    text = ac_path.read_text(encoding="utf-8", errors="replace")
    value_text = text.split("\nVALUE", 1)[-1]
    for line in value_text.splitlines():
        freq_match = re.match(r'\s*"freq"\s+([+-]?\S+)', line)
        if freq_match:
            freq = _to_float(freq_match.group(1))
            continue
        vout_match = re.match(
            r'\s*"vout"\s+\(([+-]?\S+)\s+([+-]?\S+)\)', line
        )
        if vout_match and freq is not None:
            real = _to_float(vout_match.group(1))
            imag = _to_float(vout_match.group(2))
            mag = math.hypot(real, imag)
            mag_db = 20.0 * math.log10(mag) if mag > 0 else float("-inf")
            phase = math.degrees(math.atan2(imag, real))
            rows.append((freq, real, imag, mag, mag_db, phase))

    if not rows:
        return False
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "frequency_hz",
            "vout_real",
            "vout_imag",
            "magnitude_v",
            "magnitude_db",
            "phase_deg",
        ])
        writer.writerows(rows)
    return True


def _export_operating_points(
    info_path: Path,
    dc_path: Path | None,
    circuit_netlist: str,
    out_path: Path,
) -> bool:
    text = info_path.read_text(encoding="utf-8", errors="replace")
    fields = _bsim4_fields(text)
    if not fields:
        return False
    conns = _mos_connections(circuit_netlist)
    node_voltages = _dc_node_voltages(dc_path) if dc_path else {}
    rows = []

    pattern = re.compile(
        r'"([^"]+)"\s+"bsim4"\s+\((.*?)\)\s+PROP\(\s*"model"\s+"([^"]+)"',
        re.DOTALL,
    )
    for match in pattern.finditer(text):
        instance, values_text, model = match.groups()
        values = [_to_float(v) for v in re.findall(r"^[ \t]*([+-]?(?:nan|inf|\d\S*))", values_text, re.MULTILINE)]
        if len(values) < len(fields):
            continue
        data = dict(zip(fields, values))
        local_name = instance.split(".")[-1]
        d_node, g_node, s_node = conns.get(local_name, ("", "", ""))
        rows.append({
            "instance": instance,
            "model": model,
            "vd": _node_voltage(node_voltages, d_node),
            "vg": _node_voltage(node_voltages, g_node),
            "vs": _node_voltage(node_voltages, s_node),
            "id": data.get("id"),
            "ids": data.get("ids"),
            "gm": data.get("gm"),
            "gds": data.get("gds"),
            "vgs": data.get("vgs"),
            "vds": data.get("vds"),
            "vth": data.get("vth"),
            "vdsat": data.get("vdsat"),
            "gmoverid": data.get("gmoverid"),
        })

    if not rows:
        return False
    fieldnames = [
        "instance",
        "model",
        "vd",
        "vg",
        "vs",
        "id",
        "ids",
        "gm",
        "gds",
        "vgs",
        "vds",
        "vth",
        "vdsat",
        "gmoverid",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return True


def _bsim4_fields(info_text: str) -> list[str]:
    start = info_text.find('"bsim4" STRUCT(')
    if start < 0:
        return []
    end = info_text.find('\n"capacitor" STRUCT(', start)
    if end < 0:
        end = info_text.find("\nVALUE", start)
    block = info_text[start:end]
    return re.findall(r'^"([^"]+)"\s+FLOAT DOUBLE PROP\(', block, re.MULTILINE)


def _dc_node_voltages(dc_path: Path) -> dict[str, float]:
    values: dict[str, float] = {"0": 0.0, "vss": 0.0, "Xdut.vss": 0.0}
    text = dc_path.read_text(encoding="utf-8", errors="replace")
    for name, kind, value in re.findall(
        r'"([^"]+)"\s+"([^"]+)"\s+([+-]?(?:nan|inf|\d\S*))', text
    ):
        if kind == "V":
            values[name] = _to_float(value)
    return values


def _node_voltage(values: dict[str, float], node: str) -> float | str:
    if not node:
        return ""
    for candidate in (f"Xdut.{node}", node):
        if candidate in values:
            return values[candidate]
    if node == "0" or node.lower() == "vss":
        return 0.0
    return ""


def _to_float(value: str) -> float:
    if value.lower() == "nan":
        return float("nan")
    if value.lower() in ("inf", "+inf"):
        return float("inf")
    if value.lower() == "-inf":
        return float("-inf")
    return float(value)
