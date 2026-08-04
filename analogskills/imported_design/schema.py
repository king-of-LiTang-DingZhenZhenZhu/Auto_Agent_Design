from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any, Mapping


HANDOFF_SCHEMA = "analogskills.imported_design_handoff/v1"


@dataclass(frozen=True)
class HandoffDevice:
    name: str
    kind: str
    model: str
    terminals: tuple[str, ...]
    nodes: tuple[str, ...]
    parameters: dict[str, float | int | str]
    role: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "HandoffDevice":
        return cls(
            name=str(data["name"]),
            kind=str(data["kind"]),
            model=str(data["model"]),
            terminals=tuple(str(item) for item in data["terminals"]),
            nodes=tuple(str(item) for item in data["nodes"]),
            parameters=dict(data.get("parameters", {})),
            role=str(data.get("role", "unknown")),
        )


@dataclass(frozen=True)
class ImportedDesignHandoff:
    project: str
    topology: str
    subckt_name: str
    pdk: str
    ports: tuple[str, ...]
    devices: tuple[HandoffDevice, ...]
    net_roles: dict[str, str]
    matched_groups: tuple[dict[str, Any], ...] = ()
    symmetry_groups: tuple[tuple[str, ...], ...] = ()
    routing_constraints: tuple[dict[str, Any], ...] = ()
    critical_nets: tuple[str, ...] = ()
    final_netlist: str = ""
    final_netlist_sha256: str = ""
    final_source: str = ""
    pvt_results: str = ""
    pvt_results_sha256: str = ""
    pvt_summary: dict[str, Any] = field(default_factory=dict)
    instance_mapping: dict[str, dict[str, Any]] = field(default_factory=dict)
    schema: str = HANDOFF_SCHEMA

    def validate(self) -> None:
        if self.schema != HANDOFF_SCHEMA:
            raise ValueError(f"unsupported handoff schema: {self.schema}")
        if self.pdk != "crn28hpcp":
            raise ValueError(f"unsupported physical PDK: {self.pdk}")
        if not self.ports:
            raise ValueError("handoff has no top-level ports")
        names = [device.name for device in self.devices]
        if len(names) != len(set(names)):
            raise ValueError("handoff contains duplicate instance names")
        if set(self.instance_mapping) != set(names):
            raise ValueError("instance_mapping must cover every frontend instance")
        for device in self.devices:
            if len(device.terminals) != len(device.nodes):
                raise ValueError(f"terminal/node count mismatch for {device.name}")
            if device.kind == "mos":
                for key in ("W", "L"):
                    value = device.parameters.get(key)
                    if not isinstance(value, (int, float)) or float(value) <= 0:
                        raise ValueError(f"{device.name} is missing positive {key}")
                for key in ("nf", "m"):
                    value = device.parameters.get(key, 1)
                    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                        raise ValueError(f"{device.name} has invalid integer {key}")
            if device.kind in {"res", "cap"}:
                key = "R" if device.kind == "res" else "C"
                value = device.parameters.get(key)
                if not isinstance(value, (int, float)) or float(value) <= 0:
                    raise ValueError(f"{device.name} is missing positive {key}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def write_json(self, path: str | Path) -> Path:
        self.validate()
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return target

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ImportedDesignHandoff":
        handoff = cls(
            project=str(data["project"]),
            topology=str(data["topology"]),
            subckt_name=str(data["subckt_name"]),
            pdk=str(data["pdk"]),
            ports=tuple(str(item) for item in data["ports"]),
            devices=tuple(HandoffDevice.from_dict(item) for item in data["devices"]),
            net_roles={str(k): str(v) for k, v in dict(data.get("net_roles", {})).items()},
            matched_groups=tuple(dict(item) for item in data.get("matched_groups", ())),
            symmetry_groups=tuple(tuple(str(v) for v in item) for item in data.get("symmetry_groups", ())),
            routing_constraints=tuple(dict(item) for item in data.get("routing_constraints", ())),
            critical_nets=tuple(str(item) for item in data.get("critical_nets", ())),
            final_netlist=str(data.get("final_netlist", "")),
            final_netlist_sha256=str(data.get("final_netlist_sha256", "")),
            final_source=str(data.get("final_source", "")),
            pvt_results=str(data.get("pvt_results", "")),
            pvt_results_sha256=str(data.get("pvt_results_sha256", "")),
            pvt_summary=dict(data.get("pvt_summary", {})),
            instance_mapping={str(k): dict(v) for k, v in dict(data.get("instance_mapping", {})).items()},
            schema=str(data.get("schema", "")),
        )
        handoff.validate()
        return handoff

    @classmethod
    def read_json(cls, path: str | Path) -> "ImportedDesignHandoff":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
