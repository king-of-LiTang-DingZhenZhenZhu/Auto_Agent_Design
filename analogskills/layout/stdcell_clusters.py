"""Reusable shared-diffusion and source/drain cluster extraction helpers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from analogskills.contracts import TopologyGraph


@dataclass(frozen=True)
class NativeStdCellSdCluster:
    net: str
    terminals: tuple[tuple[str, str], ...]
    contains_pmos: bool
    contains_nmos: bool

    @property
    def terminal_count(self) -> int:
        return len(self.terminals)


def extract_native_stdcell_sd_clusters(
    graph: TopologyGraph,
    *,
    available_pin_nets: Iterable[str] = (),
    rail_nets: Iterable[str] = ("VDD", "VSS"),
    device_models: Mapping[str, str] | None = None,
) -> tuple[NativeStdCellSdCluster, ...]:
    pin_nets = {str(net) for net in available_pin_nets}
    blocked_nets = pin_nets | {str(net) for net in rail_nets}
    models = {
        str(name): str(getattr(device, "model", "")).lower()
        for name, device in graph.devices.items()
    }
    if device_models:
        models.update({str(name): str(model).lower() for name, model in device_models.items()})

    clusters: list[NativeStdCellSdCluster] = []
    for net_name, net in graph.nets.items():
        if net_name in blocked_nets:
            continue
        sd_terms = tuple(
            (str(term.device), str(term.terminal))
            for term in net.terminals
            if term.device in graph.devices and term.terminal in {"S", "D"}
        )
        if not sd_terms:
            continue
        non_sd_terms = [
            term
            for term in net.terminals
            if term.device == "PIN" or term.device not in graph.devices or term.terminal not in {"S", "D"}
        ]
        if non_sd_terms:
            continue
        dev_models = {models.get(device, "") for device, _ in sd_terms}
        clusters.append(
            NativeStdCellSdCluster(
                net=str(net_name),
                terminals=sd_terms,
                contains_pmos=any("pmos" in model or model.startswith("pch") or model.startswith("mp") for model in dev_models),
                contains_nmos=any("nmos" in model or model.startswith("nch") or model.startswith("mn") for model in dev_models),
            )
        )
    return tuple(sorted(clusters, key=lambda item: (item.terminal_count, item.net)))


def find_native_stdcell_sd_cluster(
    graph: TopologyGraph,
    net_name: str,
    *,
    available_pin_nets: Iterable[str] = (),
    rail_nets: Iterable[str] = ("VDD", "VSS"),
    device_models: Mapping[str, str] | None = None,
) -> NativeStdCellSdCluster | None:
    for cluster in extract_native_stdcell_sd_clusters(
        graph,
        available_pin_nets=available_pin_nets,
        rail_nets=rail_nets,
        device_models=device_models,
    ):
        if cluster.net == str(net_name):
            return cluster
    return None
