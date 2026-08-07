"""Staged child-to-parent optimization for hierarchical topologies."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from config import settings
from design_flow_graph import run_design_flow
from models import CircuitFiles, DesignTarget, parse_metric_goals
from pdk_profiles import PDKProfile, get_pdk_profile
from topologies import get_topology
from topologies.base import ExecutableChildSpec
from virtuoso_export.exporter import select_export_netlist


class HierarchicalFlowError(RuntimeError):
    """Raised when a child artifact cannot safely enter parent optimization."""


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
QualificationRunner = Callable[..., dict[str, Any]]


class HierarchicalFlow:
    """Optimize, verify, freeze, and embed a topology's child blocks."""

    def __init__(
        self,
        project: str | Path,
        *,
        simulate: bool = False,
        max_iter: int | None = None,
        force_child: bool = False,
        resume_qualification: bool = False,
        output_root: str | Path | None = None,
        command_runner: CommandRunner = subprocess.run,
        qualification_runner: QualificationRunner = run_design_flow,
    ) -> None:
        self.project = Path(project).resolve()
        self.simulate = simulate
        self.max_iter = max_iter
        self.force_child = force_child
        self.resume_qualification = resume_qualification
        self.agent_dir = Path(__file__).parent
        self.output_root = (
            Path(output_root).resolve()
            if output_root is not None
            else settings.get_outputs_path().resolve()
        )
        self.command_runner = command_runner
        self.qualification_runner = qualification_runner
        self.resumed_outputs: set[str] = set()

    def run(self) -> dict[str, Any]:
        requirements = self._load_json(self.project / "requirements.json")
        hierarchy = self._load_json(self.project / "hierarchy.json")
        topology_name = str(requirements.get("topology_name") or "")
        if topology_name != hierarchy.get("parent_topology"):
            raise HierarchicalFlowError(
                "requirements.json and hierarchy.json disagree on parent topology"
            )

        parent_profile = get_pdk_profile(
            voltage_domain=requirements.get("voltage_domain")
        )
        blocks = [
            ExecutableChildSpec.from_dict(data)
            for data in hierarchy.get("blocks", [])
        ]
        if not blocks:
            raise HierarchicalFlowError("No hierarchical blocks declared")

        child_artifacts: dict[str, dict[str, Path]] = {}
        child_states: dict[str, str] = {}
        for spec in blocks:
            if spec.sizing_policy != "frozen_macro":
                raise HierarchicalFlowError(
                    f"Unsupported child sizing policy: {spec.sizing_policy}"
                )
            if not spec.netlist_param or not spec.results_param:
                raise HierarchicalFlowError(
                    f"Child '{spec.block_id}' is missing parent artifact bindings"
                )
            artifact = self.project / "child_blocks" / spec.block_id / "artifact"
            if not self.force_child:
                try:
                    child_artifacts[spec.block_id] = self._validate_artifact(
                        artifact, spec, parent_profile
                    )
                    child_states[spec.block_id] = "reused"
                    continue
                except HierarchicalFlowError:
                    pass
            child_artifacts[spec.block_id] = self._optimize_child(
                spec, parent_profile
            )
            child_output_name = settings.sanitize_project_name(
                f"{self.project.name}__{spec.block_id}"
            )
            child_states[spec.block_id] = (
                "resumed"
                if child_output_name in self.resumed_outputs
                else "optimized"
            )

        self._regenerate_parent(requirements, topology_name, child_artifacts)
        parent_output = self._run_or_resume_main(
            self.project,
            topology_name,
            self.project.name,
        )
        parent_qualification = self._qualify_output(
            parent_output,
            parent_profile,
        )
        self._require_qualified(parent_qualification, "Parent")

        state = {
            "project": str(self.project),
            "simulate": self.simulate,
            "children": child_states,
            "parent_output": str(parent_output),
            "parent_bo_state": (
                "resumed"
                if settings.sanitize_project_name(self.project.name)
                in self.resumed_outputs
                else "optimized"
            ),
            "parent_audit_status": parent_qualification.get("audit_status"),
            "parent_pvt_pass": True,
            "parent_next_action": parent_qualification.get("next_action"),
        }
        (self.project / "hierarchical_flow.json").write_text(
            json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return state

    def _optimize_child(
        self,
        spec: ExecutableChildSpec,
        parent_profile: PDKProfile,
    ) -> dict[str, Path]:
        child_topology = get_topology(spec.topology_name)
        child_root = self.project / "child_blocks" / spec.block_id
        child_project = child_root / "project"
        child_params: dict[str, Any] = {
            "VOLTAGE_DOMAIN": parent_profile.active_voltage_domain,
        }
        child_topology.write_project(
            child_project,
            targets=spec.targets,
            params=child_params,
            original_requirement=(
                f"Hierarchical child '{spec.block_id}' for {self.project.name}"
            ),
        )
        child_output = self._run_or_resume_main(
            child_project,
            child_topology.meta.name,
            f"{self.project.name}__{spec.block_id}",
        )
        qualification = self._qualify_output(
            child_output,
            parent_profile,
            targets=spec.pvt_targets or spec.targets,
        )
        self._require_qualified(qualification, f"Child '{spec.block_id}'")
        return self._freeze_artifact(
            spec=spec,
            source_project=child_output,
            parent_profile=parent_profile,
            qualification=qualification,
        )

    def _run_main(
        self,
        project_dir: Path,
        topology_name: str,
        output_name: str,
    ) -> Path:
        netlist = project_dir / f"{topology_name}.cir"
        requirements = project_dir / "requirements.json"
        testbenches = sorted(project_dir.glob(f"tb_{topology_name}_*.scs"))
        if not netlist.exists() or not requirements.exists() or not testbenches:
            raise HierarchicalFlowError(
                f"Incomplete generated project for '{topology_name}': {project_dir}"
            )

        project_name = settings.sanitize_project_name(output_name)
        command = [
            sys.executable,
            str(self.agent_dir / "main.py"),
            "--netlist",
            str(netlist),
            "--testbench",
            *(str(path) for path in testbenches),
            "--requirements",
            str(requirements),
            "--project",
            project_name,
        ]
        if self.max_iter is not None:
            command.extend(["--max-iter", str(self.max_iter)])
        if not self.simulate:
            command.append("--dry-run")

        completed = self.command_runner(
            command,
            cwd=self.agent_dir,
            text=True,
            capture_output=True,
            check=False,
        )
        log_path = project_dir / "bo.log"
        log_path.write_text(
            (getattr(completed, "stdout", "") or "")
            + (getattr(completed, "stderr", "") or ""),
            encoding="utf-8",
        )
        if completed.returncode != 0:
            raise HierarchicalFlowError(
                f"BO command failed for '{topology_name}'; see {log_path}"
            )
        output = self.output_root / project_name
        if not (output / "results.json").exists():
            raise HierarchicalFlowError(
                f"BO command completed without results.json: {output}"
            )
        return output

    def _run_or_resume_main(
        self,
        project_dir: Path,
        topology_name: str,
        output_name: str,
    ) -> Path:
        output = self.output_root / settings.sanitize_project_name(output_name)
        if self.resume_qualification and (output / "results.json").exists():
            self.resumed_outputs.add(settings.sanitize_project_name(output_name))
            return output
        return self._run_main(project_dir, topology_name, output_name)

    def _qualify_output(
        self,
        output: Path,
        profile: PDKProfile,
        targets: DesignTarget | None = None,
    ) -> dict[str, Any]:
        return self.qualification_runner(
            project=output,
            run_pvt=True,
            simulate=self.simulate,
            pvt_targets=targets,
            pvt_profile=profile,
        )

    @staticmethod
    def _require_qualified(state: dict[str, Any], label: str) -> None:
        errors = state.get("errors") or []
        if errors:
            raise HierarchicalFlowError(
                f"{label} qualification failed: {'; '.join(str(item) for item in errors)}"
            )
        if not (state.get("nominal_pass") or state.get("review_pass")):
            raise HierarchicalFlowError(
                f"{label} did not meet its nominal targets; "
                f"next action: {state.get('next_action')}"
            )
        if state.get("audit_status") not in {"pass", "warn"}:
            raise HierarchicalFlowError(
                f"{label} Design Audit did not pass; "
                f"next action: {state.get('next_action')}"
            )
        if state.get("pvt_pass") is not True:
            raise HierarchicalFlowError(
                f"{label} PVT verification did not pass; "
                f"next action: {state.get('next_action')}"
            )

    def _freeze_artifact(
        self,
        *,
        spec: ExecutableChildSpec,
        source_project: Path,
        parent_profile: PDKProfile,
        qualification: dict[str, Any],
    ) -> dict[str, Path]:
        results_path = source_project / "results.json"
        results = self._load_json(results_path)
        netlist_path, _ = select_export_netlist(results_path, results)
        pdk_path = source_project / "pdk_profile_used.json"
        source_pdk = self._load_json(pdk_path)
        if source_pdk != parent_profile.to_dict():
            raise HierarchicalFlowError(
                f"Child '{spec.block_id}' PDK profile or voltage domain differs "
                "from its parent"
            )
        interface = self._validate_interface(netlist_path, spec)

        artifact = self.project / "child_blocks" / spec.block_id / "artifact"
        artifact.mkdir(parents=True, exist_ok=True)
        frozen_netlist = artifact / "circuit.cir"
        frozen_results = artifact / "results.json"
        frozen_pdk = artifact / "pdk_profile_used.json"
        shutil.copy2(netlist_path, frozen_netlist)
        shutil.copy2(results_path, frozen_results)
        shutil.copy2(pdk_path, frozen_pdk)
        self._copy_pvt_summary(source_project, artifact, qualification)

        manifest = {
            "schema_version": 3,
            "block_id": spec.block_id,
            "source_project": str(source_project),
            "targets": spec.targets.to_requirements_dict()["targets"],
            "qualification_targets": _qualification_targets(spec),
            "pdk_profile": source_pdk,
            "nominal_pass": True,
            "audit_pass": qualification.get("audit_status") in {"pass", "warn"},
            "audit_status": qualification.get("audit_status"),
            "pvt_pass": True,
            "qualification_next_action": qualification.get("next_action"),
            "interface": interface,
            "files": {
                "circuit.cir": _sha256(frozen_netlist),
                "results.json": _sha256(frozen_results),
                "pdk_profile_used.json": _sha256(frozen_pdk),
            },
        }
        (artifact / "artifact.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return self._validate_artifact(artifact, spec, parent_profile)

    def _copy_pvt_summary(
        self,
        source_project: Path,
        artifact: Path,
        qualification: dict[str, Any],
    ) -> None:
        pvt_artifact = artifact / "pvt"
        pvt_artifact.mkdir(parents=True, exist_ok=True)
        source_pvt = source_project / "pvt"
        copied = False
        for filename in ("pvt_results.json", "pvt_results.csv", "pvt_report.md"):
            source = source_pvt / filename
            if source.exists():
                shutil.copy2(source, pvt_artifact / filename)
                copied = True
        if not copied:
            (pvt_artifact / "pvt_results.json").write_text(
                json.dumps(qualification, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )

    def _validate_artifact(
        self,
        artifact: Path,
        spec: ExecutableChildSpec,
        parent_profile: PDKProfile,
    ) -> dict[str, Path]:
        paths = {
            "netlist": artifact / "circuit.cir",
            "results": artifact / "results.json",
            "pdk": artifact / "pdk_profile_used.json",
            "manifest": artifact / "artifact.json",
            "pvt": artifact / "pvt" / "pvt_results.json",
        }
        if not all(path.exists() for path in paths.values()):
            raise HierarchicalFlowError(f"Incomplete child artifact: {artifact}")
        manifest = self._load_json(paths["manifest"])
        if (
            not manifest.get("nominal_pass")
            or not manifest.get("audit_pass")
            or not manifest.get("pvt_pass")
        ):
            raise HierarchicalFlowError(f"Child artifact is not qualified: {artifact}")
        if manifest.get("qualification_targets") != _qualification_targets(spec):
            raise HierarchicalFlowError(
                f"Child artifact target contract mismatch: {artifact}"
            )
        if self._load_json(paths["pdk"]) != parent_profile.to_dict():
            raise HierarchicalFlowError(f"Child artifact PDK mismatch: {artifact}")
        for filename, digest in manifest.get("files", {}).items():
            source = artifact / filename
            if not source.exists() or _sha256(source) != digest:
                raise HierarchicalFlowError(f"Child artifact checksum mismatch: {source}")
        self._validate_interface(paths["netlist"], spec)
        return paths

    def _validate_interface(
        self,
        netlist_path: Path,
        spec: ExecutableChildSpec,
    ) -> dict[str, Any]:
        text = netlist_path.read_text(encoding="utf-8")
        actual_subckt = CircuitFiles.extract_subckt_name(text)
        pattern = re.compile(
            rf"(?mi)^\s*subckt\s+{re.escape(spec.expected_subckt)}\s*\(([^)]*)\)"
        )
        match = pattern.search(text)
        if actual_subckt != spec.expected_subckt or match is None:
            raise HierarchicalFlowError(
                f"Child '{spec.block_id}' subckt must be '{spec.expected_subckt}'"
            )
        ports = tuple(match.group(1).split())
        if ports != spec.ports:
            raise HierarchicalFlowError(
                f"Child '{spec.block_id}' port mismatch: expected "
                f"{list(spec.ports)}, got {list(ports)}"
            )
        return {"subckt": actual_subckt, "ports": list(ports)}

    def _regenerate_parent(
        self,
        requirements: dict[str, Any],
        topology_name: str,
        artifacts: dict[str, dict[str, Path]],
    ) -> None:
        topology = get_topology(topology_name)
        parent_params = dict(requirements.get("default_params", {}))
        voltage_domain = requirements.get("voltage_domain")
        if voltage_domain:
            parent_params["VOLTAGE_DOMAIN"] = voltage_domain
        hierarchy = self._load_json(self.project / "hierarchy.json")
        for data in hierarchy["blocks"]:
            spec = ExecutableChildSpec.from_dict(data)
            artifact = artifacts[spec.block_id]
            parent_params[spec.netlist_param] = str(artifact["netlist"])
            parent_params[spec.results_param] = str(artifact["results"])
        topology.write_project(
            self.project,
            targets=_target_from_requirements(requirements),
            params=parent_params,
            original_requirement=str(requirements.get("original_requirement", "")),
        )
        preserved = {
            key: requirements[key]
            for key in ("system_type", "system_architecture", "system_design")
            if key in requirements
        }
        if preserved:
            requirements_path = self.project / "requirements.json"
            regenerated = self._load_json(requirements_path)
            regenerated.update(preserved)
            requirements_path.write_text(
                json.dumps(regenerated, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        if not path.exists():
            raise HierarchicalFlowError(f"Missing required file: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HierarchicalFlowError(f"Invalid JSON: {path}") from exc
        if not isinstance(data, dict):
            raise HierarchicalFlowError(f"Expected JSON object: {path}")
        return data


def _target_from_requirements(requirements: dict[str, Any]) -> DesignTarget:
    targets = dict(requirements.get("targets", {}))
    return DesignTarget(
        gain_db=targets.get("gain_db"),
        bandwidth_hz=targets.get("bandwidth_hz", targets.get("gbw_hz")),
        phase_margin_deg=targets.get("phase_margin_deg"),
        power_w=targets.get("power_w"),
        load_cap_f=targets.get("load_cap_f"),
        slew_rate_v_per_s=targets.get("slew_rate_v_per_s"),
        settling_time_s=targets.get("settling_time_s"),
        vref_v=targets.get("vref_v"),
        vref_tolerance_v=targets.get("vref_tolerance_v") or 10e-3,
        tempco_ppm_per_c=targets.get("tempco_ppm_per_c"),
        vref_temp_nonlinearity_v=targets.get("vref_temp_nonlinearity_v"),
        psrr_db=targets.get("psrr_db"),
        line_regulation_v_per_v=targets.get("line_regulation_v_per_v"),
        startup_time_s=targets.get("startup_time_s"),
        topology_hint=str(requirements.get("topology_hint", "")),
        custom_specs=dict(requirements.get("custom_specs", {})),
        metric_goals=parse_metric_goals(
            requirements.get("metric_goals", targets.get("metric_goals"))
        ),
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _qualification_targets(spec: ExecutableChildSpec) -> dict[str, Any]:
    pvt_targets = spec.pvt_targets or spec.targets
    return {
        "nominal": {
            "targets": spec.targets.to_requirements_dict()["targets"],
            "metric_goals": {
                name: goal.to_dict()
                for name, goal in spec.targets.resolved_metric_goals().items()
            },
        },
        "pvt": {
            "targets": pvt_targets.to_requirements_dict()["targets"],
            "metric_goals": {
                name: goal.to_dict()
                for name, goal in pvt_targets.resolved_metric_goals().items()
            },
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run child BO and design-flow qualification before parent BO and "
            "qualification for a hierarchical project."
        )
    )
    parser.add_argument("--project", required=True, help="Generated top-level project directory")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Run real Spectre BO and PVT; default is dry-run mode.",
    )
    parser.add_argument("--max-iter", type=int, default=None)
    parser.add_argument("--force-child", action="store_true")
    parser.add_argument(
        "--resume-qualification",
        action="store_true",
        help=(
            "Reuse existing BO outputs after manual Review validation and "
            "continue Audit/PVT/freeze without rerunning BO."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        state = HierarchicalFlow(
            args.project,
            simulate=args.simulate,
            max_iter=args.max_iter,
            force_child=args.force_child,
            resume_qualification=args.resume_qualification,
        ).run()
    except HierarchicalFlowError as exc:
        raise SystemExit(f"Hierarchical flow stopped: {exc}") from exc
    print(json.dumps(state, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
