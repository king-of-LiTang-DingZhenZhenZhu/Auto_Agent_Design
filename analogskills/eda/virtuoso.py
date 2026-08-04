"""Virtuoso SKILL script emitters for topology and abstract layout data."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from analogskills.contracts import TopologyGraph
from analogskills.layout import Placement
from analogskills.repair import LayoutShape

from .oa import build_oa_layout_plan, build_oa_schematic_plan, write_oa_skill


@dataclass(frozen=True)
class LayoutStreamOutPlan:
    skill_path: str
    output_path: str
    source_lib: str
    source_cell: str
    source_view: str = "layout"
    format: str = "gds"
    layer_map_path: str = ""
    object_map_path: str = ""
    run_dir: str = ""
    command: object | None = None
    metadata: dict[str, object] = field(default_factory=dict)


def write_virtuoso_schematic_skill(graph: TopologyGraph, path: str | Path, *, lib: str, cell: str, view: str = "schematic") -> Path:
    plan = build_oa_schematic_plan(graph, lib=lib, cell=cell, view=view)
    return write_oa_skill(plan, path)


def write_virtuoso_layout_skill(shapes: Sequence[LayoutShape], placements: Sequence[Placement], path: str | Path, *, lib: str, cell: str, view: str = "layout") -> Path:
    plan = build_oa_layout_plan(shapes, placements, lib=lib, cell=cell, view=view)
    return write_oa_skill(plan, path)



def make_virtuoso_batch_command(skill_file: str | Path, *, binary: str = "virtuoso"):
    from .command import EdaCommand

    return EdaCommand([binary, "-nograph", "-replay", str(skill_file)])


def write_layout_streamout_skill(
    path: str | Path,
    *,
    lib: str,
    cell: str,
    view: str = "layout",
    output_path: str | Path,
    stream_format: str = "gds",
    layer_map: str | Path | None = None,
    object_map: str | Path | None = None,
) -> Path:
    """Emit a reviewable Virtuoso batch script that exports layout to GDS/OASIS."""

    stream = str(stream_format or "gds").strip().lower()
    if stream not in {"gds", "oasis"}:
        raise ValueError("stream_format must be 'gds' or 'oasis'")
    out = Path(path)
    layer_map_path = str(layer_map) if layer_map is not None else ""
    object_map_path = str(object_map) if object_map is not None else ""
    export_cmd = "xstOutDoTranslate" if stream == "gds" else "oasisOutDoTranslate"
    lines = [
        f'cv = dbOpenCellViewByType("{lib}" "{cell}" "{view}" "maskLayout" "r")',
        'unless(cv error("failed to open source layout cellview"))',
        f'streamFile = "{Path(output_path)}"',
        f'layerMapFile = "{layer_map_path}"',
        f'objectMapFile = "{object_map_path}"',
        f'when(boundp(\'xstSetField) xstSetField("library" "{lib}"))',
        f'when(boundp(\'xstSetField) xstSetField("topCell" "{cell}"))',
        f'when(boundp(\'xstSetField) xstSetField("viewName" "{view}"))',
        f'when(boundp(\'xstSetField) xstSetField("strmFile" streamFile))',
        f'when(boundp(\'xstSetField) xstSetField("layerMap" layerMapFile))',
        f'when(boundp(\'xstSetField) objectMapFile != "" xstSetField("objectMap" objectMapFile))',
        f'when(boundp(\'xstSetField) xstSetField("format" "{stream.upper()}"))',
        f'when(boundp(\'{export_cmd}) {export_cmd}())',
        'dbClose(cv)',
    ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def build_layout_streamout_plan(
    *,
    lib: str,
    cell: str,
    view: str = "layout",
    output_path: str | Path,
    skill_path: str | Path,
    stream_format: str = "gds",
    layer_map: str | Path | None = None,
    object_map: str | Path | None = None,
    binary: str = "virtuoso",
    cwd: str | Path | None = None,
    timeout_s: float = 120.0,
    env: dict[str, str] | None = None,
    metadata: dict[str, object] | None = None,
) -> LayoutStreamOutPlan:
    """Create a stream-out script plus the batch command used to run it."""

    from .command import EdaCommand

    script = write_layout_streamout_skill(
        skill_path,
        lib=lib,
        cell=cell,
        view=view,
        output_path=output_path,
        stream_format=stream_format,
        layer_map=layer_map,
        object_map=object_map,
    )
    command = EdaCommand([binary, "-nograph", "-replay", str(script)], cwd=cwd, timeout_s=timeout_s, env=env)
    return LayoutStreamOutPlan(
        skill_path=str(script),
        output_path=str(output_path),
        source_lib=str(lib),
        source_cell=str(cell),
        source_view=str(view),
        format=str(stream_format),
        layer_map_path=str(layer_map) if layer_map is not None else "",
        object_map_path=str(object_map) if object_map is not None else "",
        run_dir=str(cwd) if cwd is not None else "",
        command=command,
        metadata=dict(metadata or {}),
    )
