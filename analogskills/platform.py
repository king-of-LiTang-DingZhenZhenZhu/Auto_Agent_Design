"""Single entry point binding technology, EDA adapters and native PCells."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .eda.backend import EdaStage, EdaToolchain
from .pcell.catalog import PCellRealizationCatalog, build_pcell_realization_catalog
from .pdk.config import PdkConfig
from .pdk.profile import PdkProfile, ProcessNode, resolve_pdk_profile


@dataclass(frozen=True)
class AnalogPlatform:
    pdk: PdkProfile
    eda: EdaToolchain
    pcells: PCellRealizationCatalog

    def validate_core(self) -> tuple[str, ...]:
        """Validate offline contracts needed before licensed EDA execution."""

        issues = list(self.pdk.validate_interface())
        required_stages = (EdaStage.SCHEMATIC, EdaStage.LAYOUT, EdaStage.PCELL_INTROSPECTION, EdaStage.STREAMOUT)
        report = self.eda.preflight(required_stages)
        issues.extend(issue.message for issue in report.issues if issue.blocking)
        return tuple(issues)

    def to_dict(self) -> dict[str, Any]:
        signoff = self.eda.preflight((EdaStage.SIMULATION, EdaStage.DRC, EdaStage.LVS, EdaStage.PEX))
        return {
            "pdk": self.pdk.to_dict(),
            "pcells": self.pcells.summary(),
            "core_ready": not self.validate_core(),
            "core_issues": list(self.validate_core()),
            "signoff_preflight": signoff.to_dict(),
        }


def load_platform(
    pdk: PdkProfile | PdkConfig | ProcessNode | str | Path | None = None,
    *,
    binaries: Mapping[str, str] | None = None,
    include_pcell_manifest: bool = True,
) -> AnalogPlatform:
    profile = resolve_pdk_profile(pdk)
    return AnalogPlatform(
        pdk=profile,
        eda=EdaToolchain.for_pdk(profile, binaries=binaries),
        pcells=build_pcell_realization_catalog(profile, include_manifest=include_pcell_manifest),
    )
