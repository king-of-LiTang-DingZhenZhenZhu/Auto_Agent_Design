"""Tool-neutral EDA adapters bound to a selected PDK profile."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import shutil
from typing import Any, Iterable, Mapping

from analogskills.artifacts import ArtifactRef, ArtifactStatus, EvidenceLevel, RunManifest
from analogskills.pdk import PdkConfig, PdkProfile, ProcessNode, resolve_pdk_profile, resolve_tool_binary

from .calibre import make_calibre_drc_command, make_calibre_lvs_command, make_calibre_pex_command
from .command import EdaCommand, EdaRunResult
from .target import EdaExecutionTarget, ExecutionTargetKind
from .spectre import make_spectre_command
from .virtuoso import make_virtuoso_batch_command


class EdaStage(str, Enum):
    SCHEMATIC = "schematic"
    LAYOUT = "layout"
    PCELL_INTROSPECTION = "pcell_introspection"
    STREAMOUT = "streamout"
    SIMULATION = "simulation"
    DRC = "drc"
    LVS = "lvs"
    PEX = "pex"


@dataclass(frozen=True)
class EdaPreflightIssue:
    stage: EdaStage
    code: str
    message: str
    blocking: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "code": self.code,
            "message": self.message,
            "blocking": self.blocking,
        }


@dataclass(frozen=True)
class EdaPreflightReport:
    pdk_key: str
    requested_stages: tuple[EdaStage, ...]
    issues: tuple[EdaPreflightIssue, ...] = ()

    @property
    def ready(self) -> bool:
        return not any(issue.blocking for issue in self.issues)

    def issues_for(self, stage: EdaStage | str) -> tuple[EdaPreflightIssue, ...]:
        target = EdaStage(stage)
        return tuple(issue for issue in self.issues if issue.stage is target)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pdk_key": self.pdk_key,
            "requested_stages": [stage.value for stage in self.requested_stages],
            "ready": self.ready,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class EdaArtifactSpec:
    kind: str
    name: str
    path: str | Path
    provenance: EvidenceLevel | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EdaExecutionRecord:
    result: EdaRunResult
    manifest: RunManifest
    manifest_path: Path
    artifacts: tuple[ArtifactRef, ...]

    @property
    def ok(self) -> bool:
        return self.result.ok


@dataclass(frozen=True)
class VirtuosoBackend:
    profile: PdkProfile
    binary: str = "virtuoso"

    def batch_command(self, skill_file: str | Path) -> EdaCommand:
        return make_virtuoso_batch_command(skill_file, binary=self.binary)


@dataclass(frozen=True)
class SpectreBackend:
    profile: PdkProfile
    binary: str = "spectre"

    def simulation_command(
        self,
        netlist: str | Path,
        *,
        output_dir: str | Path | None = None,
        variables: Mapping[str, float | int | str] | None = None,
        corner: str | None = None,
    ) -> EdaCommand:
        return make_spectre_command(
            netlist,
            output_dir=output_dir,
            variables=variables,
            corner=corner,
            binary=self.binary,
        )


@dataclass(frozen=True)
class CalibreBackend:
    profile: PdkProfile
    binary: str = "calibre"

    def deck_for(self, stage: EdaStage | str, explicit: str | Path | None = None) -> str:
        if explicit is not None and str(explicit).strip():
            return str(explicit)
        target = EdaStage(stage)
        binding = self.profile.tool_binding(target.value)
        if not binding.configured:
            raise RuntimeError(f"PDK {self.profile.key!r} has no configured Calibre {target.value.upper()} deck")
        return binding.path

    def drc_command(self, *, layout: str | Path | None = None, rule_deck: str | Path | None = None) -> EdaCommand:
        return make_calibre_drc_command(self.deck_for(EdaStage.DRC, rule_deck), layout, binary=self.binary)

    def lvs_command(
        self,
        *,
        layout: str | Path | None = None,
        source: str | Path | None = None,
        rule_deck: str | Path | None = None,
    ) -> EdaCommand:
        return make_calibre_lvs_command(self.deck_for(EdaStage.LVS, rule_deck), layout, source, binary=self.binary)

    def pex_command(
        self,
        *,
        layout: str | Path | None = None,
        source: str | Path | None = None,
        rule_deck: str | Path | None = None,
        report: str | Path | None = None,
        extracted_netlist: str | Path | None = None,
        pex_format: str = "spf",
        corner: str | None = None,
        switches: Mapping[str, str | int | float | bool] | None = None,
    ) -> EdaCommand:
        return make_calibre_pex_command(
            self.deck_for(EdaStage.PEX, rule_deck),
            layout,
            source,
            report=report,
            extracted_netlist=extracted_netlist,
            pex_format=pex_format,
            corner=corner,
            switches=switches,
            binary=self.binary,
        )


@dataclass(frozen=True)
class EdaToolchain:
    profile: PdkProfile
    virtuoso: VirtuosoBackend
    spectre: SpectreBackend
    calibre: CalibreBackend
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def for_pdk(
        cls,
        pdk: PdkProfile | PdkConfig | ProcessNode | str | Path,
        *,
        binaries: Mapping[str, str] | None = None,
    ) -> "EdaToolchain":
        profile = resolve_pdk_profile(pdk)
        configured = dict(binaries or {})
        return cls(
            profile=profile,
            virtuoso=VirtuosoBackend(profile, configured.get("virtuoso", resolve_tool_binary("virtuoso"))),
            spectre=SpectreBackend(profile, configured.get("spectre", resolve_tool_binary("spectre"))),
            calibre=CalibreBackend(profile, configured.get("calibre", resolve_tool_binary("calibre"))),
        )

    def preflight(
        self,
        stages: Iterable[EdaStage | str] = tuple(EdaStage),
        *,
        check_executables: bool = False,
        check_paths: bool = False,
        execution_target: EdaExecutionTarget | None = None,
    ) -> EdaPreflightReport:
        requested = tuple(EdaStage(stage) for stage in stages)
        issues: list[EdaPreflightIssue] = []
        for stage in requested:
            tool, binary, binding_stage = self._stage_contract(stage)
            if binding_stage:
                binding = self.profile.tool_binding(binding_stage)
                if not binding.configured:
                    issues.append(EdaPreflightIssue(stage, "pdk_collateral_unconfigured", f"{self.profile.key}: {binding_stage} collateral is not configured"))
                elif check_paths and binding.path and not (
                    execution_target.path_readable(binding.path)
                    if execution_target is not None
                    else Path(binding.path).exists()
                ):
                    issues.append(EdaPreflightIssue(stage, "pdk_path_unavailable", f"configured path is unavailable: {binding.path}"))
            executable = (
                execution_target.resolve_executable(binary)
                if execution_target is not None
                else shutil.which(binary)
            ) if check_executables else binary
            if check_executables and not executable:
                issues.append(EdaPreflightIssue(stage, "executable_unavailable", f"{tool} executable is unavailable: {binary}"))
        return EdaPreflightReport(self.profile.key, requested, tuple(issues))

    def command_for(self, stage: EdaStage | str, **kwargs: Any) -> EdaCommand:
        target = EdaStage(stage)
        if target in {EdaStage.SCHEMATIC, EdaStage.LAYOUT, EdaStage.PCELL_INTROSPECTION, EdaStage.STREAMOUT}:
            return self.virtuoso.batch_command(kwargs["skill_file"])
        if target is EdaStage.SIMULATION:
            return self.spectre.simulation_command(**kwargs)
        if target is EdaStage.DRC:
            return self.calibre.drc_command(**kwargs)
        if target is EdaStage.LVS:
            return self.calibre.lvs_command(**kwargs)
        if target is EdaStage.PEX:
            return self.calibre.pex_command(**kwargs)
        raise AssertionError(target)

    def execute(
        self,
        stage: EdaStage | str,
        command: EdaCommand,
        *,
        run_dir: str | Path,
        inputs: Iterable[ArtifactRef] = (),
        outputs: Iterable[EdaArtifactSpec] = (),
        provenance: EvidenceLevel | None = None,
        tool_version: str = "",
        parameters: Mapping[str, Any] | None = None,
        environment_summary: Mapping[str, str] | None = None,
        execution_target: EdaExecutionTarget | None = None,
        check: bool = False,
    ) -> EdaExecutionRecord:
        """Execute a command and always persist logs plus a typed manifest."""

        target_stage = EdaStage(stage)
        target_dir = Path(run_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = execution_target or EdaExecutionTarget()
        effective_cwd = command.cwd
        if effective_cwd is None and target.kind is ExecutionTargetKind.LOCAL:
            effective_cwd = target_dir
        effective = EdaCommand(
            command.command,
            cwd=effective_cwd,
            timeout_s=command.timeout_s,
            env=command.env,
            measurement_file=command.measurement_file,
        )
        result = target.run(effective, check=False)
        stdout_path = target_dir / "stdout.log"
        stderr_path = target_dir / "stderr.log"
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")
        evidence = provenance or _default_evidence(target_stage)
        status = ArtifactStatus.PASSED if result.ok else ArtifactStatus.FAILED
        artifacts: list[ArtifactRef] = [
            ArtifactRef.from_path(
                stdout_path,
                kind="eda_stdout",
                name=f"{target_stage.value}_stdout",
                provenance=evidence,
                status=status,
                pdk_key=self.profile.key,
                tool=_tool_name(target_stage),
                tool_version=tool_version,
            ),
            ArtifactRef.from_path(
                stderr_path,
                kind="eda_stderr",
                name=f"{target_stage.value}_stderr",
                provenance=evidence,
                status=status,
                pdk_key=self.profile.key,
                tool=_tool_name(target_stage),
                tool_version=tool_version,
            ),
        ]
        binding = self.profile.tool_binding(_binding_stage(target_stage))
        for output in outputs:
            output_path = Path(output.path)
            if not output_path.is_absolute():
                output_path = target_dir / output_path
            artifacts.append(
                ArtifactRef.from_path(
                    output_path,
                    kind=output.kind,
                    name=output.name,
                    provenance=output.provenance or evidence,
                    status=status if output_path.exists() else ArtifactStatus.FAILED,
                    pdk_key=self.profile.key,
                    tool=_tool_name(target_stage),
                    tool_version=tool_version,
                    deck_path=binding.path,
                    metrics=result.metrics,
                    metadata=dict(output.metadata),
                )
            )
        manifest = RunManifest(
            stage=target_stage.value,
            pdk_key=self.profile.key,
            status=status,
            inputs=tuple(inputs),
            outputs=tuple(artifacts),
            command=result.command,
            tool=_tool_name(target_stage),
            tool_version=tool_version,
            deck_path=binding.path,
            parameters=dict(parameters or {}),
            metrics=result.metrics,
            environment={
                **dict(environment_summary or {}),
                "execution_target": target.kind.value,
                "execution_host": target.host,
            },
        )
        manifest_path = manifest.save_json(target_dir / "run_manifest.json")
        record = EdaExecutionRecord(result, manifest, manifest_path, tuple(artifacts))
        if check and not record.ok:
            raise RuntimeError(
                f"{target_stage.value} failed rc={result.returncode}; manifest={manifest_path}"
            )
        return record

    def _stage_contract(self, stage: EdaStage) -> tuple[str, str, str]:
        if stage in {EdaStage.SCHEMATIC, EdaStage.LAYOUT, EdaStage.PCELL_INTROSPECTION, EdaStage.STREAMOUT}:
            return "Virtuoso", self.virtuoso.binary, "virtuoso"
        if stage is EdaStage.SIMULATION:
            return "Spectre", self.spectre.binary, "spectre"
        return "Calibre", self.calibre.binary, stage.value


def load_eda_toolchain(
    pdk: PdkProfile | PdkConfig | ProcessNode | str | Path,
    *,
    binaries: Mapping[str, str] | None = None,
) -> EdaToolchain:
    return EdaToolchain.for_pdk(pdk, binaries=binaries)


def _binding_stage(stage: EdaStage) -> str:
    if stage in {EdaStage.SCHEMATIC, EdaStage.LAYOUT, EdaStage.PCELL_INTROSPECTION, EdaStage.STREAMOUT}:
        return "virtuoso"
    if stage is EdaStage.SIMULATION:
        return "spectre"
    return stage.value


def _tool_name(stage: EdaStage) -> str:
    if stage in {EdaStage.SCHEMATIC, EdaStage.LAYOUT, EdaStage.PCELL_INTROSPECTION, EdaStage.STREAMOUT}:
        return "virtuoso"
    if stage is EdaStage.SIMULATION:
        return "spectre"
    return "calibre"


def _default_evidence(stage: EdaStage) -> EvidenceLevel:
    if stage in {EdaStage.SCHEMATIC, EdaStage.LAYOUT, EdaStage.PCELL_INTROSPECTION, EdaStage.STREAMOUT}:
        return EvidenceLevel.VIRTUOSO
    if stage is EdaStage.SIMULATION:
        return EvidenceLevel.SPECTRE_PRE_LAYOUT
    if stage is EdaStage.DRC:
        return EvidenceLevel.CALIBRE_DRC
    if stage is EdaStage.LVS:
        return EvidenceLevel.CALIBRE_LVS
    return EvidenceLevel.CALIBRE_PEX
