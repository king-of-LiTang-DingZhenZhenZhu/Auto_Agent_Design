"""Export compact diagnostics from Spectre PSF ASCII results."""

from __future__ import annotations

import argparse
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

    summary_path = write_diagnostics_summary(out_dir)
    if summary_path:
        exported["summary"] = str(summary_path)

    return exported


def write_diagnostics_summary(
    diagnostics_dir: Path,
    out_path: Path | None = None,
) -> Path | None:
    """Write a human-readable summary from exported diagnostics CSV files."""
    diagnostics_dir = Path(diagnostics_dir)
    ac_path = diagnostics_dir / "ac_response.csv"
    dc_path = diagnostics_dir / "dc_operating_points.csv"
    if not ac_path.exists() and not dc_path.exists():
        return None

    if out_path is None:
        out_path = diagnostics_dir / "diagnostics_summary.txt"

    lines: list[str] = ["Circuit Diagnostics Summary", "=" * 27, ""]
    if dc_path.exists():
        lines.extend(_format_dc_summary(dc_path))
    if ac_path.exists():
        if lines and lines[-1] != "":
            lines.append("")
        lines.extend(_format_ac_summary(ac_path))

    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return out_path


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


def _format_ac_summary(ac_path: Path) -> list[str]:
    with ac_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    data = [
        {
            "frequency_hz": _safe_float(row.get("frequency_hz")),
            "magnitude_db": _safe_float(row.get("magnitude_db")),
            "phase_deg": _safe_float(row.get("phase_deg")),
        }
        for row in rows
    ]
    data = [
        row for row in data
        if row["frequency_hz"] is not None
        and row["magnitude_db"] is not None
        and row["phase_deg"] is not None
    ]

    lines = ["AC Response", "-" * 11]
    if not data:
        lines.append("No readable AC rows found.")
        return lines

    first = data[0]
    ugf = _unity_gain_point(data)
    lines.append(f"DC/low-frequency gain: {_fmt(first['magnitude_db'], 2)} dB")
    if ugf:
        ugf_hz, phase_deg = ugf
        lines.append(f"Unity-gain frequency: {_fmt(ugf_hz * 1e-6, 2)} MHz")
        lines.append(f"Phase at UGF: {_fmt(phase_deg, 2)} deg")
        lines.append(f"Estimated phase margin: {_fmt(180.0 + phase_deg, 2)} deg")
    else:
        lines.append("Unity-gain crossing: not found in exported sweep.")

    lines.append("")
    lines.append("Representative AC points:")
    lines.append("frequency | gain | phase")
    lines.append("--- | --- | ---")
    for idx in _sample_indices(len(data), max_rows=12):
        row = data[idx]
        lines.append(
            f"{_eng(row['frequency_hz'], 'Hz')} | "
            f"{_fmt(row['magnitude_db'], 2)} dB | "
            f"{_fmt(row['phase_deg'], 2)} deg"
        )
    return lines


def _format_dc_summary(dc_path: Path) -> list[str]:
    with dc_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    lines = ["DC Operating Points", "-" * 19]
    if not rows:
        lines.append("No readable DC operating-point rows found.")
        return lines

    lines.append("instance | model | vd | vg | vs | id | gm | gds | gm/id | vds-vdsat | region")
    lines.append("--- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---")
    for row in rows:
        vds = _safe_float(row.get("vds"))
        vdsat = _safe_float(row.get("vdsat"))
        margin = None if vds is None or vdsat is None else vds - vdsat
        region = _region_label(margin)
        lines.append(
            f"{row.get('instance', '')} | "
            f"{row.get('model', '')} | "
            f"{_fmt_v(row.get('vd'))} | "
            f"{_fmt_v(row.get('vg'))} | "
            f"{_fmt_v(row.get('vs'))} | "
            f"{_fmt_scaled(row.get('id'), 1e6, 'uA', 3)} | "
            f"{_fmt_scaled(row.get('gm'), 1e3, 'mS', 3)} | "
            f"{_fmt_scaled(row.get('gds'), 1e6, 'uS', 3)} | "
            f"{_fmt_plain(row.get('gmoverid'), 2)} | "
            f"{_fmt_margin(margin)} | "
            f"{region}"
        )
    return lines


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


def _safe_float(value: str | float | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        value_float = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(value_float) or math.isinf(value_float):
        return None
    return value_float


def _unity_gain_point(rows: list[dict[str, float]]) -> tuple[float, float] | None:
    for prev, cur in zip(rows, rows[1:]):
        prev_gain = prev["magnitude_db"]
        cur_gain = cur["magnitude_db"]
        if prev_gain == 0:
            return prev["frequency_hz"], prev["phase_deg"]
        if (prev_gain > 0 >= cur_gain) or (prev_gain < 0 <= cur_gain):
            span = cur_gain - prev_gain
            frac = 0.0 if span == 0 else (0.0 - prev_gain) / span
            f1 = max(prev["frequency_hz"], 1e-30)
            f2 = max(cur["frequency_hz"], 1e-30)
            log_freq = math.log10(f1) + frac * (math.log10(f2) - math.log10(f1))
            phase = prev["phase_deg"] + frac * (cur["phase_deg"] - prev["phase_deg"])
            return 10 ** log_freq, phase
    return None


def _sample_indices(length: int, max_rows: int = 12) -> list[int]:
    if length <= max_rows:
        return list(range(length))
    return sorted({
        int(round(i * (length - 1) / (max_rows - 1)))
        for i in range(max_rows)
    })


def _region_label(vds_minus_vdsat: float | None) -> str:
    if vds_minus_vdsat is None:
        return "unknown"
    if vds_minus_vdsat < 0:
        return "linear/warning"
    if vds_minus_vdsat < 0.05:
        return "near-boundary"
    return "sat"


def _fmt(value: float | None, digits: int) -> str:
    if value is None:
        return ""
    return f"{value:.{digits}f}"


def _fmt_plain(value: str | None, digits: int) -> str:
    parsed = _safe_float(value)
    return "" if parsed is None else _fmt(parsed, digits)


def _fmt_v(value: str | None) -> str:
    parsed = _safe_float(value)
    return "" if parsed is None else f"{parsed:.4f} V"


def _fmt_scaled(
    value: str | None,
    scale: float,
    unit: str,
    digits: int,
) -> str:
    parsed = _safe_float(value)
    return "" if parsed is None else f"{parsed * scale:.{digits}f} {unit}"


def _fmt_margin(value: float | None) -> str:
    return "" if value is None else f"{value:.4f} V"


def _eng(value: float | None, unit: str) -> str:
    if value is None:
        return ""
    prefixes = [
        (1e9, "G"),
        (1e6, "M"),
        (1e3, "k"),
        (1.0, ""),
        (1e-3, "m"),
        (1e-6, "u"),
        (1e-9, "n"),
        (1e-12, "p"),
    ]
    abs_value = abs(value)
    for scale, prefix in prefixes:
        if abs_value >= scale or scale == 1e-12:
            return f"{value / scale:.3g} {prefix}{unit}"
    return f"{value:.3g} {unit}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a readable diagnostics_summary.txt from diagnostics CSV files."
    )
    parser.add_argument(
        "diagnostics_dir",
        type=Path,
        help="Directory containing dc_operating_points.csv and/or ac_response.csv",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output text path. Defaults to diagnostics_dir/diagnostics_summary.txt",
    )
    args = parser.parse_args()
    summary = write_diagnostics_summary(args.diagnostics_dir, args.out)
    if summary is None:
        raise SystemExit("No diagnostics CSV files found")
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
