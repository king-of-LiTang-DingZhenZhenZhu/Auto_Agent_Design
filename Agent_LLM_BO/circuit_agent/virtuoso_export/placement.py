"""Simple placement templates for generated schematics."""

from __future__ import annotations

from dataclasses import dataclass

from .models import Instance, SchematicIR


@dataclass(frozen=True)
class Placement:
    """Virtuoso placement for one instance."""

    x: int
    y: int
    orient: str = "R0"


_FIVE_T_PLACEMENT: dict[str, Placement] = {
    "Mtail": Placement(0, 600, "R0"),
    "Mdp1": Placement(-300, 300, "R0"),
    "Mdp2": Placement(300, 300, "R0"),
    "Mcm1": Placement(-300, -100, "R0"),
    "Mcm2": Placement(300, -100, "R0"),
}


_TWO_STAGE_PLACEMENT: dict[str, Placement] = {
    "M5": Placement(0, -300, "R0"),
    "M1": Placement(-400, 100, "R0"),
    "M2": Placement(0, 100, "R0"),
    "M3": Placement(-400, 500, "R0"),
    "M4": Placement(0, 500, "R0"),
    "M6": Placement(500, 500, "R0"),
    "M7": Placement(500, -100, "R0"),
    "Rz": Placement(760, 260, "R0"),
    "Cc": Placement(760, 80, "R0"),
}


_FOLDED_CASCODE_PLACEMENT: dict[str, Placement] = {
    "M0": Placement(-900, 650, "R0"),
    "M1": Placement(-700, 650, "R0"),
    "M2": Placement(-500, 650, "R0"),
    "M4": Placement(-300, 650, "R0"),
    "M3_1": Placement(-400, 850, "R0"),
    "M3_2": Placement(-200, 850, "R0"),
    "M3_3": Placement(0, 850, "R0"),
    "M3_4": Placement(200, 850, "R0"),
    "M3_5": Placement(400, 850, "R0"),
    "M3_6": Placement(600, 850, "R0"),
    "M7": Placement(100, 650, "R0"),
    "M8": Placement(100, -450, "R0"),
    "M9": Placement(300, -450, "R0"),
    "M10": Placement(500, -450, "R0"),
    "M11": Placement(700, -450, "R0"),
    "M12": Placement(900, -450, "R0"),
    "M13_1": Placement(100, -650, "R0"),
    "M13_2": Placement(300, -650, "R0"),
    "M13_3": Placement(500, -650, "R0"),
    "M13_4": Placement(700, -650, "R0"),
    "M13_5": Placement(900, -650, "R0"),
    "M13_6": Placement(1100, -650, "R0"),
    "Mtailp": Placement(0, 350, "R0"),
    "Mdiff1": Placement(-300, 100, "R0"),
    "Mdiff2": Placement(300, 100, "R0"),
    "Mfold1": Placement(-300, -250, "R0"),
    "Mfold2": Placement(300, -250, "R0"),
    "Mcasn1": Placement(-300, -50, "R0"),
    "Mcasn2": Placement(300, -50, "R0"),
    "Mmirr1": Placement(-300, 450, "R0"),
    "Mmirr2": Placement(300, 450, "R0"),
    "Mcasp1": Placement(-300, 250, "R0"),
    "Mcasp2": Placement(300, 250, "R0"),
    "Mcs": Placement(800, 250, "R0"),
    "Mload": Placement(800, -200, "R0"),
    "Rz": Placement(1050, 130, "R0"),
    "Cc": Placement(1050, -50, "R0"),
}


def get_placements(ir: SchematicIR) -> dict[str, Placement]:
    """Return deterministic placements for all instances in an IR."""
    named = _template_for(ir.subckt_name)
    placements: dict[str, Placement] = {}
    fallback_index = 0

    for inst in ir.instances:
        if inst.name in named:
            placements[inst.name] = named[inst.name]
            continue
        placements[inst.name] = _fallback_placement(inst, fallback_index)
        fallback_index += 1

    return placements


def _template_for(subckt_name: str) -> dict[str, Placement]:
    normalized = subckt_name.lower()
    if normalized in {"ota_5t", "5t_ota"}:
        return _FIVE_T_PLACEMENT
    if normalized in {"two_stage_ota", "twostage_ota"}:
        return _TWO_STAGE_PLACEMENT
    if normalized in {"folded_cascode", "folded_cascode_two_stage"}:
        return _FOLDED_CASCODE_PLACEMENT
    return {}


def _fallback_placement(inst: Instance, index: int) -> Placement:
    row = index // 6
    col = index % 6
    y_base = 300 if inst.kind == "mos" else -500
    return Placement(x=-750 + col * 300, y=y_base - row * 250, orient="R0")
