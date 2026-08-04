"""Runtime helpers for N7 ArrayAPI constraint-to-layout generation."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


DEFAULT_N7_ARRAYAPI_LOAD_PATH = (
    "/home/userone/tsmcn7/eDesigner/util/tsmcN7_DeviceArrayAPI/skill_API/load_tsmcArrayAPI.il"
)
DEFAULT_N7_ARRAYAPI_LIBINIT_PATH = (
    "/home/userone/tsmcn7/eDesigner/util/tsmcN7_DeviceArrayAPI/pdkLib/tsmcN7_ArrayAPILib/libInit.il"
)


@dataclass(frozen=True)
class NativeStdCellArrayApiGenerateRequest:
    lib_name: str
    cell_name: str
    report_path: str
    schematic_view_name: str = "schematic"
    layout_view_name: str = "layout_arrayapi"
    assistant_name: str = "TSMC PDK+"
    selected_instance_names: tuple[str, ...] = ()
    select_first_instance: bool = True
    delete_existing_constraints: bool = True
    delete_existing_layout_contents: bool = True
    create_pins: bool = False
    create_boundary: bool = False
    extract_after_generate_all: bool = True
    extract_schematic: bool = True
    arrayapi_load_path: str = DEFAULT_N7_ARRAYAPI_LOAD_PATH
    arrayapi_libinit_path: str = DEFAULT_N7_ARRAYAPI_LIBINIT_PATH


@dataclass(frozen=True)
class NativeStdCellArrayApiGenerateReport:
    assistant_status: str | None
    generate_status: str | None
    schematic_constraint_count: int | None
    layout_instance_count: int | None
    layout_shape_count: int | None
    layout_pin_count: int | None
    layout_terminal_count: int | None
    layout_figgroup_count: int | None
    layout_constraint_count: int | None
    layout_has_prboundary: bool | None
    values: dict[str, str]

    @property
    def constraints_materialized(self) -> bool:
        return (self.schematic_constraint_count or 0) > 0 or (self.layout_constraint_count or 0) > 0

    @property
    def false_positive_generate(self) -> bool:
        return (
            self.assistant_status == "ok"
            and self.generate_status == "ok"
            and not self.constraints_materialized
        )


def write_native_stdcell_arrayapi_generate_skill(
    request: NativeStdCellArrayApiGenerateRequest,
    path: str | Path,
) -> Path:
    resolved = Path(path)
    resolved.write_text(render_native_stdcell_arrayapi_generate_skill(request), encoding="utf-8")
    return resolved


def render_native_stdcell_arrayapi_generate_skill(
    request: NativeStdCellArrayApiGenerateRequest,
) -> str:
    selected_names = " ".join(_skill_string(name) for name in request.selected_instance_names)
    if request.selected_instance_names:
        selected_mode = "'named"
    elif request.select_first_instance:
        selected_mode = "'first"
    else:
        selected_mode = "nil"
    return "\n".join(
        (
            "procedure(skzTry(expr)",
            "  let((res)",
            "    res = errset(expr t)",
            '    if(res then list("ok" res) else list("error" nil))',
            "  )",
            ")",
            "",
            "procedure(skzWriteConstraint(port con)",
            "  when(con",
            '    fprintf(port "constraint_id=%L\\n" con)',
            '    fprintf(port "constraint_name=%L\\n" if(getd(\'ciConGetName) then ciConGetName(con) else nil))',
            '    fprintf(port "constraint_type=%L\\n" if(getd(\'ciConGetType) then ciConGetType(con) else nil))',
            '    fprintf(port "constraint_members=%L\\n" if(getd(\'ciConListMembers) then ciConListMembers(con) else nil))',
            '    fprintf(port "constraint_params=%L\\n" if(getd(\'ciConListParams) then ciConListParams(con) else nil))',
            '    fprintf(port "constraint_writable=%L\\n" if(getd(\'ciConIsWritable) then ciConIsWritable(con) else nil))',
            '    fprintf(port "constraint_out_of_context=%L\\n" if(getd(\'ciConIsOutOfContext) then ciConIsOutOfContext(con) else nil))',
            "  )",
            ")",
            "",
            "procedure(skzListInstances(cv)",
            "  let((items)",
            "    items = nil",
            "    when(cv",
            "      foreach(inst cv~>instances",
            "        items = append1(items list(inst~>name inst~>libName inst~>cellName inst~>viewName inst~>xy inst~>orient))",
            "      )",
            "    )",
            "    items",
            "  )",
            ")",
            "",
            "procedure(skzListFigGroups(cv)",
            "  let((items)",
            "    items = nil",
            "    when(cv",
            "      foreach(fg cv~>figGroups",
            "        items = append1(items list(fg~>name fg~>type length(fg~>members)))",
            "      )",
            "    )",
            "    items",
            "  )",
            ")",
            "",
            "procedure(skzFindTargetConstraint(cons)",
            "  let((targetCon)",
            "    targetCon = nil",
            "    when(cons",
            "      foreach(con cons",
            "        when(!targetCon && getd('ciConGetType)",
            "          when(ciConGetType(con) == 'modgen",
            "            targetCon = con",
            "          )",
            "        )",
            "      )",
            "    )",
            "    when(!targetCon && cons targetCon = car(cons))",
            "    targetCon",
            "  )",
            ")",
            "",
            "procedure(skzSelectInstances(cv names mode)",
            "  let((selected)",
            "    selected = nil",
            "    when(getd('geDeselectAll) geDeselectAll())",
            "    when(cv",
            "      foreach(inst cv~>instances",
            "        when(",
            "          (mode == 'named && member(inst~>name names)) ||",
            "          (mode == 'first && !selected)",
            "          when(getd('geSelectObject) geSelectObject(inst))",
            "          selected = t",
            "        )",
            "      )",
            "    )",
            "    selected",
            "  )",
            ")",
            "",
            "let((schWin schCv schCache schCons schConNames schTargetCon",
            "      viewObj layViewObj oldLayCv saveLayCv retAssist retGen layCv layCache layCons layConNames layTargetCon layTargetFg layTopo",
            "      port selectedNames selectedMode)",
            '  envSetVal("graphic" "packetDialogBoxes" \'boolean nil)',
            f'  envSetVal("layoutXL" "lxExtractAfterGenerateAll" \'boolean {_skill_bool(request.extract_after_generate_all)})',
            f'  envSetVal("layoutXL" "initCreatePins" \'boolean {_skill_bool(request.create_pins)})',
            f'  envSetVal("layoutXL" "initCreateBoundary" \'boolean {_skill_bool(request.create_boundary)})',
            f"  when(isFile({_skill_string(request.arrayapi_load_path)}) load({_skill_string(request.arrayapi_load_path)}))",
            f"  when(isFile({_skill_string(request.arrayapi_libinit_path)}) load({_skill_string(request.arrayapi_libinit_path)}))",
            f"  schWin = deOpenCellView({_skill_string(request.lib_name)} {_skill_string(request.cell_name)} {_skill_string(request.schematic_view_name)} {_skill_string(request.schematic_view_name)} nil \"a\")",
            "  when(schWin hiSetCurrentWindow(schWin))",
            "  schCv = if(getd('geGetEditCellView) then geGetEditCellView() else nil)",
            "  schCache = if(schCv && getd('ciCacheGet) then car(errset(ciCacheGet(schCv) t)) else nil)",
            f"  selectedNames = '({selected_names})",
            f"  selectedMode = {selected_mode}",
            "  when(schCv skzSelectInstances(schCv selectedNames selectedMode))",
            "  when(schCv && getd('schCheck) errset(schCheck(schCv) t))",
            "  when(schCv errset(dbSave(schCv) t))",
            "  retAssist = if(schCache && getd('ciRunFindersAndGenerators)",
            "    then skzTry(ciRunFindersAndGenerators(",
            f"      schCache {_skill_string(request.assistant_name)} ?runGenerators t ?deleteExisting {_skill_bool(request.delete_existing_constraints)} ?addHierNotes nil ?printFinderResults t",
            "    ))",
            '    else list("skip" nil)',
            "  )",
            "  schCons = if(schCache && getd('ciCacheListCon) then car(errset(ciCacheListCon(schCache) t)) else nil)",
            "  schConNames = nil",
            "  when(schCons",
            "    foreach(con schCons",
            "      schConNames = append1(schConNames if(getd('ciConGetName) then ciConGetName(con) else \"\"))",
            "    )",
            "  )",
            "  schTargetCon = skzFindTargetConstraint(schCons)",
            "  when(schCv errset(dbSave(schCv) t))",
            f"  viewObj = ddGetObj({_skill_string(request.lib_name)} {_skill_string(request.cell_name)} {_skill_string(request.layout_view_name)})",
            f"  when({_skill_bool(request.delete_existing_layout_contents)} && viewObj",
            f"    oldLayCv = dbOpenCellViewByType({_skill_string(request.lib_name)} {_skill_string(request.cell_name)} {_skill_string(request.layout_view_name)} \"maskLayout\" \"a\")",
            "    when(oldLayCv",
            "      foreach(i oldLayCv~>instances dbDeleteObject(i))",
            "      foreach(s oldLayCv~>shapes dbDeleteObject(s))",
            "      foreach(l oldLayCv~>labels dbDeleteObject(l))",
            "      foreach(p oldLayCv~>pins dbDeleteObject(p))",
            "      foreach(t oldLayCv~>terminals dbDeleteObject(t))",
            "      foreach(fg oldLayCv~>figGroups dbDeleteObject(fg))",
            "      when(oldLayCv~>prBoundary dbDeleteObject(oldLayCv~>prBoundary))",
            "      errset(dbSave(oldLayCv) t)",
            "      dbClose(oldLayCv)",
            "    )",
            "  )",
            "  retGen = if(schCv && getd('lxGenFromSource)",
            "    then skzTry(",
            "      lxGenFromSource(",
            f"        schCv ?layLibName {_skill_string(request.lib_name)} ?layCellName {_skill_string(request.cell_name)} ?layViewName {_skill_string(request.layout_view_name)}",
            "        ?initCreateInstances t",
            "        ?initDoStacking nil",
            "        ?initDoFolding nil",
            f"        ?initCreatePins {_skill_bool(request.create_pins)}",
            "        ?initGlobalNetPins nil",
            "        ?initCreatePadPins nil",
            f"        ?initCreateBoundary {_skill_bool(request.create_boundary)}",
            f"        ?extractAfterGenerateAll {_skill_bool(request.extract_after_generate_all)}",
            f"        ?extractSchematic {_skill_bool(request.extract_schematic)}",
            "      )",
            "    )",
            '    else list("skip" nil)',
            "  )",
            f"  layViewObj = ddGetObj({_skill_string(request.lib_name)} {_skill_string(request.cell_name)} {_skill_string(request.layout_view_name)})",
            f"  saveLayCv = if(layViewObj then dbOpenCellViewByType({_skill_string(request.lib_name)} {_skill_string(request.cell_name)} {_skill_string(request.layout_view_name)} \"maskLayout\" \"a\") else nil)",
            "  when(saveLayCv",
            "    errset(dbSave(saveLayCv) t)",
            "    dbClose(saveLayCv)",
            "  )",
            f"  layCv = if(layViewObj then dbOpenCellViewByType({_skill_string(request.lib_name)} {_skill_string(request.cell_name)} {_skill_string(request.layout_view_name)} \"maskLayout\" \"r\") else nil)",
            "  layCache = if(layCv && getd('ciCacheGet) then car(errset(ciCacheGet(layCv) t)) else nil)",
            "  layCons = if(layCache && getd('ciCacheListCon) then car(errset(ciCacheListCon(layCache) t)) else nil)",
            "  layConNames = nil",
            "  when(layCons",
            "    foreach(con layCons",
            "      layConNames = append1(layConNames if(getd('ciConGetName) then ciConGetName(con) else \"\"))",
            "    )",
            "  )",
            "  layTargetCon = skzFindTargetConstraint(layCons)",
            "  layTargetFg = if(layCv && layTargetCon && getd('mgGetModgenFGFromConstraint) && getd('ciConGetName)",
            "    then car(errset(mgGetModgenFGFromConstraint(layCv ciConGetName(layTargetCon)) t))",
            "    else nil",
            "  )",
            "  layTopo = if(layTargetFg && getd('mgGetTopologyFromModgen)",
            "    then car(errset(mgGetTopologyFromModgen(layTargetFg) t))",
            "    else nil",
            "  )",
            f"  port = outfile({_skill_string(request.report_path)} \"w\")",
            '  fprintf(port "[schematic]\\n")',
            '  fprintf(port "assistant_result=%L\\n" retAssist)',
            '  fprintf(port "schematic_constraint_names=%L\\n" schConNames)',
            '  fprintf(port "schematic_constraint_count=%L\\n" if(schCons then length(schCons) else 0))',
            "  when(schTargetCon",
            '    fprintf(port "\\n[schematic_target_constraint]\\n")',
            "    skzWriteConstraint(port schTargetCon)",
            "  )",
            '  fprintf(port "\\n[generate]\\n")',
            '  fprintf(port "generate_result=%L\\n" retGen)',
            '  fprintf(port "\\n[layout]\\n")',
            '  fprintf(port "layout_instance_count=%L\\n" if(layCv then length(layCv~>instances) else 0))',
            '  fprintf(port "layout_shape_count=%L\\n" if(layCv then length(layCv~>shapes) else 0))',
            '  fprintf(port "layout_pin_count=%L\\n" if(layCv then length(layCv~>pins) else 0))',
            '  fprintf(port "layout_terminal_count=%L\\n" if(layCv then length(layCv~>terminals) else 0))',
            '  fprintf(port "layout_figgroup_count=%L\\n" if(layCv then length(layCv~>figGroups) else 0))',
            '  fprintf(port "layout_instances=%L\\n" skzListInstances(layCv))',
            '  fprintf(port "layout_figgroups=%L\\n" skzListFigGroups(layCv))',
            '  fprintf(port "layout_constraint_names=%L\\n" layConNames)',
            '  fprintf(port "layout_constraint_count=%L\\n" if(layCons then length(layCons) else 0))',
            "  when(layTargetCon",
            '    fprintf(port "\\n[layout_target_constraint]\\n")',
            "    skzWriteConstraint(port layTargetCon)",
            "  )",
            '  fprintf(port "layout_target_fg=%L\\n" layTargetFg)',
            '  fprintf(port "layout_target_fg_type=%L\\n" if(layTargetFg then layTargetFg~>type else nil))',
            '  fprintf(port "layout_target_topology=%L\\n" layTopo)',
            '  fprintf(port "layout_modgen_has_topology=%L\\n" if(layTargetFg && getd(\'mgModgenHasTopology) then mgModgenHasTopology(layTargetFg) else nil))',
            '  fprintf(port "layout_has_prboundary=%L\\n" if(layCv && layCv~>prBoundary then t else nil))',
            "  close(port)",
            "  when(layCv dbClose(layCv))",
            "  when(schWin hiCloseWindow(schWin))",
            "  exit()",
            ")",
            "",
        )
    )


def parse_native_stdcell_arrayapi_generate_report(text: str) -> NativeStdCellArrayApiGenerateReport:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("[") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return NativeStdCellArrayApiGenerateReport(
        assistant_status=_extract_status(values.get("assistant_result")),
        generate_status=_extract_status(values.get("generate_result")),
        schematic_constraint_count=_extract_int(values.get("schematic_constraint_count")),
        layout_instance_count=_extract_int(values.get("layout_instance_count")),
        layout_shape_count=_extract_int(values.get("layout_shape_count")),
        layout_pin_count=_extract_int(values.get("layout_pin_count")),
        layout_terminal_count=_extract_int(values.get("layout_terminal_count")),
        layout_figgroup_count=_extract_int(values.get("layout_figgroup_count")),
        layout_constraint_count=_extract_int(values.get("layout_constraint_count")),
        layout_has_prboundary=_extract_bool(values.get("layout_has_prboundary")),
        values=values,
    )


def _extract_status(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith('("ok"'):
        return "ok"
    if value.startswith('("error"'):
        return "error"
    if value.startswith('("skip"'):
        return "skip"
    return None


def _extract_int(value: str | None) -> int | None:
    if value is None:
        return None
    stripped = value.strip()
    if stripped.isdigit():
        return int(stripped)
    return None


def _extract_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    stripped = value.strip().lower()
    if stripped == "t":
        return True
    if stripped in {"nil", "()"}:
        return False
    return None


def _skill_bool(value: bool) -> str:
    return "t" if value else "nil"


def _skill_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
