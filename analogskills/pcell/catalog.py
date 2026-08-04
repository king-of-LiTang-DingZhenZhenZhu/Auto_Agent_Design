"""Node-neutral PCell realization catalog and calibration entry point."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from analogskills.pdk import PCellTemplate, PdkConfig, PdkProfile, ProcessNode, resolve_pdk_profile

from .calibration_run import PCellCalibrationManifest, build_default_pcell_calibration_manifest
from .unit_library import PCellUnitLibrary, build_pcell_unit_library


class PCellRealizationReadiness(str, Enum):
    TEMPLATE_ONLY = "template_only"
    CALIBRATION_READY = "calibration_ready"
    CALIBRATED = "calibrated"
    SIGNOFF_READY = "signoff_ready"


@dataclass(frozen=True)
class PCellFamilyInterface:
    logical_name: str
    lib_name: str
    cell_name: str
    view_name: str
    aliases: tuple[str, ...]
    terminals: tuple[str, ...]
    logical_parameters: tuple[str, ...]
    layout_parameters: tuple[str, ...]
    terminal_access_configured: bool
    realization_candidate_count: int
    clean_candidate_count: int
    calibration_target_count: int
    readiness: PCellRealizationReadiness
    realization_policy: Mapping[str, Any] = field(default_factory=dict)
    issues: tuple[str, ...] = ()
    template: PCellTemplate = field(repr=False, compare=False, default=None)  # type: ignore[assignment]

    @property
    def oa_cellview(self) -> str:
        return f"{self.lib_name}/{self.cell_name}/{self.view_name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_name": self.logical_name,
            "oa_cellview": self.oa_cellview,
            "aliases": list(self.aliases),
            "terminals": list(self.terminals),
            "logical_parameters": list(self.logical_parameters),
            "layout_parameters": list(self.layout_parameters),
            "terminal_access_configured": self.terminal_access_configured,
            "realization_candidate_count": self.realization_candidate_count,
            "clean_candidate_count": self.clean_candidate_count,
            "calibration_target_count": self.calibration_target_count,
            "readiness": self.readiness.value,
            "realization_policy": dict(self.realization_policy),
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class PCellRealizationCatalog:
    profile: PdkProfile
    families: Mapping[str, PCellFamilyInterface]
    unit_library: PCellUnitLibrary
    calibration_manifest: PCellCalibrationManifest | None = None

    def family(self, logical_name: str) -> PCellFamilyInterface:
        logical = self.profile.config.resolve_pcell_logical_name(logical_name)
        try:
            return self.families[logical]
        except KeyError as exc:
            raise KeyError(f"unknown PCell family {logical_name!r} for {self.profile.key}") from exc

    def validate(self, *, require_signoff: bool = False) -> tuple[str, ...]:
        issues: list[str] = []
        for family in self.families.values():
            issues.extend(f"{family.logical_name}: {issue}" for issue in family.issues)
            if require_signoff and family.readiness is not PCellRealizationReadiness.SIGNOFF_READY:
                issues.append(f"{family.logical_name}: no signoff-ready realization set")
        return tuple(issues)

    def summary(self) -> dict[str, Any]:
        return {
            "pdk": self.profile.key,
            "node": self.profile.node.value,
            "family_count": len(self.families),
            "candidate_count": len(self.unit_library.candidates),
            "clean_candidate_count": sum(1 for candidate in self.unit_library.candidates if candidate.clean),
            "calibration_target_count": len(self.calibration_manifest.targets) if self.calibration_manifest else 0,
            "families": {name: family.to_dict() for name, family in sorted(self.families.items())},
        }

    def to_dict(self) -> dict[str, Any]:
        return self.summary()


class PCellRealizationService:
    """Factory shared by placement/SMT, OA generation and calibration flows."""

    def catalog_for(self, pdk: PdkProfile | PdkConfig | ProcessNode | str | Path, *, include_manifest: bool = True) -> PCellRealizationCatalog:
        return build_pcell_realization_catalog(pdk, include_manifest=include_manifest)

    def calibration_manifest_for(
        self,
        pdk: PdkProfile | PdkConfig | ProcessNode | str | Path,
        *,
        logical_names: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> PCellCalibrationManifest:
        profile = resolve_pdk_profile(pdk)
        return build_default_pcell_calibration_manifest(profile.config, logical_names=logical_names, **kwargs)

    def unit_library_for(self, pdk: PdkProfile | PdkConfig | ProcessNode | str | Path, *, clean_only: bool = True) -> PCellUnitLibrary:
        profile = resolve_pdk_profile(pdk)
        return build_pcell_unit_library(profile.config, logical_names=None, clean_only=clean_only)


def build_pcell_realization_catalog(
    pdk: PdkProfile | PdkConfig | ProcessNode | str | Path,
    *,
    include_manifest: bool = True,
) -> PCellRealizationCatalog:
    profile = resolve_pdk_profile(pdk)
    unit_library = build_pcell_unit_library(profile.config, logical_names=None, clean_only=False)
    manifest = build_default_pcell_calibration_manifest(profile.config) if include_manifest else None
    aliases_by_family: dict[str, list[str]] = {name: [] for name in profile.config.pcell_templates}
    for alias, target in profile.config.pcell_aliases.items():
        if target in aliases_by_family:
            aliases_by_family[target].append(alias)
    families: dict[str, PCellFamilyInterface] = {}
    realization_root = _mapping(profile.config.metadata.get("pcell_realization"))
    for logical_name, template in profile.config.pcell_templates.items():
        candidates = unit_library.candidates_for(logical_name, clean_only=False)
        clean = tuple(candidate for candidate in candidates if candidate.clean)
        calibration_targets = tuple(
            target for target in (manifest.targets if manifest else ()) if target.logical_name == logical_name
        )
        access_configured = bool(template.terminal_access) or any(candidate.terminal_access for candidate in candidates)
        issues: list[str] = []
        if not access_configured:
            issues.append("terminal access still requires live PCell introspection/calibration")
        if clean:
            readiness = PCellRealizationReadiness.SIGNOFF_READY
        elif candidates:
            readiness = PCellRealizationReadiness.CALIBRATED
        elif calibration_targets:
            readiness = PCellRealizationReadiness.CALIBRATION_READY
        else:
            readiness = PCellRealizationReadiness.TEMPLATE_ONLY
        families[logical_name] = PCellFamilyInterface(
            logical_name=logical_name,
            lib_name=template.layout_lib_name or template.lib_name,
            cell_name=template.layout_cell_name or template.cell_name,
            view_name=template.layout_view_name or template.view_name,
            aliases=tuple(sorted(aliases_by_family[logical_name])),
            terminals=_terminals(logical_name, template, candidates),
            logical_parameters=tuple(sorted(template.parameter_ranges)),
            layout_parameters=tuple(sorted(set(template.layout_parameter_map.values()) or set(template.parameter_map.values()))),
            terminal_access_configured=access_configured,
            realization_candidate_count=len(candidates),
            clean_candidate_count=len(clean),
            calibration_target_count=len(calibration_targets),
            readiness=readiness,
            realization_policy=dict(_mapping(realization_root.get("mos" if logical_name in {"nmos", "pmos"} else logical_name))),
            issues=tuple(issues),
            template=template,
        )
    return PCellRealizationCatalog(profile, families, unit_library, manifest)


def _terminals(logical_name: str, template: PCellTemplate, candidates: Sequence[Any]) -> tuple[str, ...]:
    configured = set(template.terminal_access)
    for candidate in candidates:
        configured.update(candidate.terminals)
        configured.update(candidate.terminal_access)
    canonical = {
        "nmos": ("D", "G", "S", "B"),
        "pmos": ("D", "G", "S", "B"),
        "bjt": ("C", "B", "E"),
        "resistor": ("PLUS", "MINUS"),
        "capacitor": ("PLUS", "MINUS"),
    }.get(logical_name, ())
    if not configured:
        return canonical
    ordered = tuple(item for item in canonical if item in configured)
    extras = tuple(sorted(str(item) for item in configured.difference(canonical)))
    return ordered + extras


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
