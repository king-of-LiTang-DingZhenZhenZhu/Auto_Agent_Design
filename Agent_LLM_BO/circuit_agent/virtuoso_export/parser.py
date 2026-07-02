"""Parser for topology-library generated SPICE/HSPICE DUT netlists."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from .models import Instance, SchematicIR


_SUBCKT_RE = re.compile(r"^\.?subckt\s+(\S+)\s*(.*)$", re.IGNORECASE)
_ENDS_RE = re.compile(r"^\.?ends\b", re.IGNORECASE)
_SPECTRE_INSTANCE_RE = re.compile(
    r"^(\S+)\s+\(([^)]+)\)\s+(\S+)(?:\s+(.*))?$"
)


def parse_netlist(path_or_content: str | Path) -> SchematicIR:
    """Parse a final rendered DUT netlist into a schematic IR.

    The parser intentionally targets the regular output from this repository's
    topology library. It supports MOS, resistor, and capacitor instances inside
    the first subckt block.
    """
    content = _read_content(path_or_content)
    lines = _join_continuations(content.splitlines())

    subckt_name = ""
    ports: list[str] = []
    body: list[str] = []
    in_subckt = False
    param_values: dict[str, float] = {}

    for raw_line in lines:
        line = _strip_inline_comment(raw_line).strip()
        if not line:
            continue
        if _is_parameter_line(line):
            param_values.update(_parse_parameter_line(line, param_values))
            continue
        if not in_subckt:
            match = _SUBCKT_RE.match(line)
            if match:
                subckt_name = match.group(1)
                ports = match.group(2).strip().strip("()").split()
                in_subckt = True
            continue
        if _ENDS_RE.match(line):
            break
        if not line.startswith("."):
            body.append(line)

    if not subckt_name:
        raise ValueError("No subckt block found in netlist")

    instances: list[Instance] = []
    nets: set[str] = set(ports)
    for line in body:
        inst = _parse_instance(line, param_values)
        if not inst:
            continue
        instances.append(inst)
        nets.update(inst.nodes)

    return SchematicIR(
        subckt_name=subckt_name,
        ports=ports,
        instances=instances,
        nets=sorted(nets, key=_net_sort_key),
    )


def _read_content(path_or_content: str | Path) -> str:
    if isinstance(path_or_content, Path):
        return path_or_content.read_text(encoding="utf-8")
    candidate = Path(path_or_content)
    if "\n" not in path_or_content and candidate.exists():
        return candidate.read_text(encoding="utf-8")
    return path_or_content


def _join_continuations(lines: list[str]) -> list[str]:
    joined: list[str] = []
    current = ""
    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.lstrip()
        if stripped.startswith("+"):
            current += " " + stripped[1:].strip()
            continue
        if current:
            joined.append(current)
        current = line
    if current:
        joined.append(current)
    return joined


def _strip_inline_comment(line: str) -> str:
    stripped = line.lstrip()
    if stripped.startswith("*") or stripped.startswith("//"):
        return ""
    return line


def _parse_instance(line: str, param_values: dict[str, float] | None = None) -> Instance | None:
    spectre_match = _SPECTRE_INSTANCE_RE.match(line)
    if spectre_match:
        return _parse_spectre_instance(spectre_match, param_values or {})

    prefix = line[0].upper()
    if prefix == "M":
        return _parse_mos(line, param_values or {})
    if prefix == "R":
        return _parse_two_terminal(line, kind="res", param_name="R", param_values=param_values or {})
    if prefix == "C":
        return _parse_two_terminal(line, kind="cap", param_name="C", param_values=param_values or {})
    return None


def _parse_spectre_instance(
    match: re.Match[str],
    param_values: dict[str, float],
) -> Instance | None:
    name = match.group(1)
    nodes = match.group(2).split()
    primitive = match.group(3)
    params = _parse_params(_split_tokens(match.group(4) or ""))
    primitive_lower = primitive.lower()

    if primitive_lower == "resistor":
        return Instance(
            name=name,
            kind="res",
            model="res",
            nodes=nodes,
            params=_normalize_params(params, param_values),
        )
    if primitive_lower == "capacitor":
        return Instance(
            name=name,
            kind="cap",
            model="cap",
            nodes=nodes,
            params=_normalize_params(params, param_values),
        )
    if len(nodes) == 4 and name.upper().startswith("M"):
        return Instance(
            name=name,
            kind="mos",
            model=primitive,
            nodes=nodes,
            params=_normalize_params(params, param_values),
        )
    return None


def _parse_mos(line: str, param_values: dict[str, float]) -> Instance:
    tokens = _split_tokens(line)
    if len(tokens) < 6:
        raise ValueError(f"Invalid MOS instance line: {line}")
    name = tokens[0]
    nodes = tokens[1:5]
    model = tokens[5]
    params = _parse_params(tokens[6:])
    return Instance(
        name=name,
        kind="mos",
        model=model,
        nodes=nodes,
        params=_normalize_params(params, param_values),
    )


def _parse_two_terminal(
    line: str,
    kind: str,
    param_name: str,
    param_values: dict[str, float],
) -> Instance:
    tokens = _split_tokens(line)
    if len(tokens) < 3:
        raise ValueError(f"Invalid {kind} instance line: {line}")
    name = tokens[0]
    nodes = tokens[1:3]
    params = _parse_params(tokens[3:])

    if param_name not in params and len(tokens) >= 4 and "=" not in tokens[3]:
        params[param_name] = _clean_value(tokens[3])

    return Instance(
        name=name,
        kind=kind,
        model=kind,
        nodes=nodes,
        params=_normalize_params(params, param_values),
    )


def _split_tokens(line: str) -> list[str]:
    lexer = shlex.shlex(line, posix=False)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _parse_params(tokens: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            continue
        name, value = token.split("=", 1)
        params[name.strip()] = _clean_value(value)
    return params


def _normalize_params(
    params: dict[str, str],
    param_values: dict[str, float] | None = None,
) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for name, value in params.items():
        resolved_value = _resolve_param_value(value, param_values or {})
        if name.lower() == "w":
            normalized["W"] = _format_spice_value(resolved_value, integer=False)
        elif name.lower() == "l":
            normalized["L"] = _format_spice_value(resolved_value, integer=False)
        elif name.lower() == "r":
            normalized["R"] = _format_spice_value(resolved_value, integer=False)
        elif name.lower() == "c":
            normalized["C"] = _format_spice_value(resolved_value, integer=False)
        elif name.lower() == "nf":
            normalized["nf"] = _format_spice_value(resolved_value, integer=True)
        elif name.lower() == "m":
            normalized["m"] = _format_spice_value(resolved_value, integer=True)
        else:
            normalized[name] = _format_spice_value(resolved_value, integer=False)
    return normalized


def _clean_value(value: str) -> str:
    return value.strip().strip("'\"")


def _is_parameter_line(line: str) -> bool:
    stripped = line.strip().lower()
    return stripped.startswith("parameters ") or stripped.startswith(".param ")


def _parse_parameter_line(
    line: str,
    current_values: dict[str, float],
) -> dict[str, float]:
    body = re.sub(r"^\s*(?:parameters|\.param)\s+", "", line, flags=re.IGNORECASE)
    parsed: dict[str, float] = {}
    working = dict(current_values)
    for name, raw in re.findall(r"(\w+)\s*=\s*'?(.*?)'?(?=\s+\w+\s*=|\s*$)", body):
        try:
            value = _eval_spice_expr(raw, working)
        except ValueError:
            continue
        parsed[name] = value
        working[name] = value
    return parsed


def _resolve_param_value(value: str, param_values: dict[str, float]) -> str | float:
    try:
        return _eval_spice_expr(value, param_values)
    except ValueError:
        return value


def _eval_spice_expr(raw: str, param_values: dict[str, float]) -> float:
    expr = _clean_value(raw)
    if not expr:
        raise ValueError("empty expression")

    for name in sorted(param_values, key=len, reverse=True):
        expr = re.sub(
            rf"\b{re.escape(name)}\b",
            f"({param_values[name]:.17g})",
            expr,
        )

    unresolved = [
        token for token in re.findall(r"\b[A-Za-z_]\w*\b", expr)
        if token.lower() not in _SPICE_SUFFIXES
    ]
    if unresolved:
        raise ValueError(f"unresolved parameter(s): {', '.join(unresolved)}")

    expr = _replace_spice_numbers(expr)
    if re.search(r"[^0-9eE+\-*/().\s]", expr):
        raise ValueError(f"unsafe expression: {raw}")
    try:
        value = eval(expr, {"__builtins__": {}}, {})
    except Exception as exc:
        raise ValueError(f"cannot evaluate expression {raw!r}") from exc
    return float(value)


_SPICE_SUFFIXES = {
    "f": 1e-15,
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
    "k": 1e3,
    "meg": 1e6,
    "g": 1e9,
}


def _replace_spice_numbers(expr: str) -> str:
    suffixes = "|".join(sorted(_SPICE_SUFFIXES, key=len, reverse=True))
    pattern = re.compile(
        rf"(?<![\w.])([+-]?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)(?:({suffixes})\b)?",
        re.IGNORECASE,
    )

    def repl(match: re.Match[str]) -> str:
        number = float(match.group(1))
        suffix = (match.group(2) or "").lower()
        scale = _SPICE_SUFFIXES.get(suffix, 1.0)
        return f"({number * scale:.17g})"

    return pattern.sub(repl, expr)


def _format_spice_value(value: str | float, integer: bool = False) -> str:
    if isinstance(value, str):
        return value
    if integer:
        rounded = round(value)
        if abs(value - rounded) < 1e-9:
            return str(int(rounded))
    abs_v = abs(value)
    if abs_v >= 1e3:
        return f"{value / 1e3:.6g}k"
    if abs_v >= 1:
        return f"{value:.6g}"
    if abs_v >= 1e-3:
        return f"{value * 1e3:.6g}m"
    if abs_v >= 1e-6:
        return f"{value * 1e6:.6g}u"
    if abs_v >= 1e-9:
        return f"{value * 1e9:.6g}n"
    if abs_v >= 1e-12:
        return f"{value * 1e12:.6g}p"
    return f"{value * 1e15:.6g}f"


def _net_sort_key(name: str) -> tuple[int, str]:
    lower = name.lower()
    if lower in {"vdd", "vdd!"}:
        return (0, lower)
    if lower in {"vss", "vss!", "gnd", "gnd!", "0"}:
        return (1, lower)
    return (2, lower)
