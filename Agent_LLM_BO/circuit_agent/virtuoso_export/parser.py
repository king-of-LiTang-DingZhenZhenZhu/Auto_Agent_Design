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

    for raw_line in lines:
        line = _strip_inline_comment(raw_line).strip()
        if not line:
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
        inst = _parse_instance(line)
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


def _parse_instance(line: str) -> Instance | None:
    spectre_match = _SPECTRE_INSTANCE_RE.match(line)
    if spectre_match:
        return _parse_spectre_instance(spectre_match)

    prefix = line[0].upper()
    if prefix == "M":
        return _parse_mos(line)
    if prefix == "R":
        return _parse_two_terminal(line, kind="res", param_name="R")
    if prefix == "C":
        return _parse_two_terminal(line, kind="cap", param_name="C")
    return None


def _parse_spectre_instance(match: re.Match[str]) -> Instance | None:
    name = match.group(1)
    nodes = match.group(2).split()
    primitive = match.group(3)
    params = _parse_params(_split_tokens(match.group(4) or ""))
    primitive_lower = primitive.lower()

    if primitive_lower == "resistor":
        return Instance(name=name, kind="res", model="res", nodes=nodes, params=_normalize_params(params))
    if primitive_lower == "capacitor":
        return Instance(name=name, kind="cap", model="cap", nodes=nodes, params=_normalize_params(params))
    if len(nodes) == 4 and name.upper().startswith("M"):
        return Instance(
            name=name,
            kind="mos",
            model=primitive,
            nodes=nodes,
            params=_normalize_params(params),
        )
    return None


def _parse_mos(line: str) -> Instance:
    tokens = _split_tokens(line)
    if len(tokens) < 6:
        raise ValueError(f"Invalid MOS instance line: {line}")
    name = tokens[0]
    nodes = tokens[1:5]
    model = tokens[5]
    params = _parse_params(tokens[6:])
    return Instance(name=name, kind="mos", model=model, nodes=nodes, params=params)


def _parse_two_terminal(line: str, kind: str, param_name: str) -> Instance:
    tokens = _split_tokens(line)
    if len(tokens) < 3:
        raise ValueError(f"Invalid {kind} instance line: {line}")
    name = tokens[0]
    nodes = tokens[1:3]
    params = _parse_params(tokens[3:])

    if param_name not in params and len(tokens) >= 4 and "=" not in tokens[3]:
        params[param_name] = _clean_value(tokens[3])

    return Instance(name=name, kind=kind, model=kind, nodes=nodes, params=params)


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


def _normalize_params(params: dict[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for name, value in params.items():
        if name.lower() == "w":
            normalized["W"] = value
        elif name.lower() == "l":
            normalized["L"] = value
        elif name.lower() == "r":
            normalized["R"] = value
        elif name.lower() == "c":
            normalized["C"] = value
        elif name.lower() == "nf":
            normalized["nf"] = value
        else:
            normalized[name] = value
    return normalized


def _clean_value(value: str) -> str:
    return value.strip().strip("'\"")


def _net_sort_key(name: str) -> tuple[int, str]:
    lower = name.lower()
    if lower in {"vdd", "vdd!"}:
        return (0, lower)
    if lower in {"vss", "vss!", "gnd", "gnd!", "0"}:
        return (1, lower)
    return (2, lower)
