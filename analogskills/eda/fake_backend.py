"""Deterministic fake EDA backends for tests and adapter development."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SchematicInstance:
    name: str
    lib: str
    cell: str
    view: str = "symbol"
    terminals: dict[str, str] = field(default_factory=dict)


@dataclass
class FakeSchematicBackend:
    lib: str = "work"
    cell: str = "top"
    view: str = "schematic"
    pins: dict[str, str] = field(default_factory=dict)
    instances: dict[str, SchematicInstance] = field(default_factory=dict)

    def create_cellview(self, lib: str, cell: str, view: str = "schematic") -> None:
        self.lib = lib
        self.cell = cell
        self.view = view
        self.pins.clear()
        self.instances.clear()

    def add_pin(self, name: str, direction: str) -> None:
        self.pins[name] = direction

    def instantiate(self, name: str, lib: str, cell: str, view: str = "symbol") -> None:
        if name in self.instances:
            raise ValueError(f"duplicate instance {name!r}")
        self.instances[name] = SchematicInstance(name, lib, cell, view, {})

    def bind(self, instance: str, terminal: str, net: str) -> None:
        if instance not in self.instances:
            raise KeyError(f"unknown instance {instance!r}")
        inst = self.instances[instance]
        terminals = dict(inst.terminals)
        terminals[terminal] = net
        self.instances[instance] = SchematicInstance(inst.name, inst.lib, inst.cell, inst.view, terminals)

    def terminal_map(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for inst in self.instances.values():
            for terminal, net in inst.terminals.items():
                result[f"{inst.name}.{terminal}"] = net
        return result

    def compare_terminal_map(self, expected: dict[str, str]) -> dict[str, tuple[str | None, str | None]]:
        actual = self.terminal_map()
        mismatch: dict[str, tuple[str | None, str | None]] = {}
        for terminal in sorted(set(actual) | set(expected)):
            if actual.get(terminal) != expected.get(terminal):
                mismatch[terminal] = (expected.get(terminal), actual.get(terminal))
        return mismatch

    def export_cdl(self, path: str | Path) -> Path:
        path = Path(path)
        lines = [f"* fake CDL {self.lib}/{self.cell}/{self.view}"]
        if self.pins:
            lines.append(".SUBCKT " + self.cell + " " + " ".join(self.pins))
        for inst in self.instances.values():
            nets = [net for _, net in sorted(inst.terminals.items())]
            lines.append("X" + inst.name + " " + " ".join(nets) + f" {inst.cell}")
        if self.pins:
            lines.append(".ENDS " + self.cell)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path
