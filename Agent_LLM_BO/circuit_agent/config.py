"""Configuration management for Circuit Agent."""

import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings
from pdk_profiles import get_pdk_profile, validate_pdk_profile

# Load .env from the circuit_agent directory
_ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(_ENV_PATH)

_DEFAULT_PDK = get_pdk_profile()


class Settings(BaseSettings):
    # LLM
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-pro"

    # PDK — HSPICE format (.cir / .sp)
    pdk_profile: str = _DEFAULT_PDK.name
    pdk_hspice_path: str = _DEFAULT_PDK.hspice_model_path
    pdk_hspice_section: str = _DEFAULT_PDK.hspice_section

    # PDK — Spectre format (.scs)
    pdk_spectre_path: str = _DEFAULT_PDK.spectre_model_path
    pdk_spectre_section: str = _DEFAULT_PDK.spectre_section

    vdd: float = _DEFAULT_PDK.vdd
    vdd_min: float = _DEFAULT_PDK.vdd_min
    vdd_max: float = _DEFAULT_PDK.vdd_max
    min_l: float = _DEFAULT_PDK.min_l
    max_width_per_finger: float = _DEFAULT_PDK.max_width_per_finger
    min_width_per_finger: float = _DEFAULT_PDK.min_width_per_finger
    w_l_grid_step: float = 1e-8  # W/L 参数网格步长 (10nm)，输出网表时自动取整
    nmos_model: str = _DEFAULT_PDK.nmos_model
    pmos_model: str = _DEFAULT_PDK.pmos_model
    nmos_lvt_model: str = _DEFAULT_PDK.nmos_lvt_model
    pmos_lvt_model: str = _DEFAULT_PDK.pmos_lvt_model
    virtuoso_tech_lib: str = _DEFAULT_PDK.virtuoso_tech_lib
    virtuoso_pdk_lib_path: str = _DEFAULT_PDK.virtuoso_pdk_lib_path

    # gm/Id lookup table path
    gmid_table_path: str = _DEFAULT_PDK.gmid_table_path
    spectre_options: tuple[str, ...] = _DEFAULT_PDK.spectre_options

    # Spectre
    spectre_timeout: int = 300
    spectre_cmd_template: str = (
        "spectre {netlist_path} +aps -raw {raw_dir} 2>&1 | tee {log_path}"
    )

    # Optimization
    max_iterations: int = 50
    llm_validation_frequency: int = 5
    stagnation_window: int = 10
    severe_deviation_patience: int = 5
    severe_gain_gap_db: float = 40.0
    severe_bandwidth_ratio: float = 0.01
    enable_topology_escalation: bool = False
    max_topology_changes: int = 3
    max_repair_attempts: int = 3

    # Paths
    workspace_dir: str = "./workspace"
    knowledge_base_dir: str = "./Opamp_knowledge_base"
    outputs_dir: str = "./outputs"

    # Mode
    dry_run: bool = False  # Mock mode for testing without Spectre

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    def get_workspace_path(self) -> Path:
        base = Path(__file__).parent
        p = base / self.workspace_dir
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_outputs_path(self) -> Path:
        base = Path(__file__).parent
        p = base / self.outputs_dir
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_knowledge_base_path(self) -> Path:
        base = Path(__file__).parent
        return base / self.knowledge_base_dir

    def get_run_dir(self, iteration: int = 0) -> Path:
        run_dir = self.get_workspace_path() / f"run_{iteration:03d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    # --- Project output paths ---

    def get_project_path(self, project_name: str) -> Path:
        """Root output directory for a named project."""
        base = Path(__file__).parent
        p = base / self.outputs_dir / project_name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_project_netlist_path(self, project_name: str) -> Path:
        p = self.get_project_path(project_name) / "netlist"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_project_simulation_path(self, project_name: str) -> Path:
        p = self.get_project_path(project_name) / "simulation"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_project_data_path(self, project_name: str) -> Path:
        p = self.get_project_path(project_name) / "data"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @staticmethod
    def sanitize_project_name(raw: str) -> str:
        """Sanitize a raw string into a filesystem-safe project name."""
        if not raw or not raw.strip():
            return f"circuit_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        # Replace spaces and separators with underscore
        name = re.sub(r'[\s\-/\\.,;:]+', '_', raw.strip())
        # Remove non-alphanumeric except underscore
        name = re.sub(r'[^\w]', '', name)
        # Collapse multiple underscores
        name = re.sub(r'_+', '_', name)
        # Trim to 64 chars
        name = name[:64].strip('_')
        if not name:
            return f"circuit_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        return name

    def validate_required(self) -> None:
        """Validate that required settings are present."""
        errors = []
        if not self.dry_run and not self.deepseek_api_key:
            errors.append("DEEPSEEK_API_KEY is not set")
        if not self.pdk_hspice_path:
            errors.append("PDK_HSPICE_PATH is not set")
        if not self.pdk_spectre_path:
            errors.append("PDK_SPECTRE_PATH is not set")
        pdk_errors = validate_pdk_profile(get_pdk_profile())
        errors.extend(f"PDK profile: {error}" for error in pdk_errors)
        if errors:
            raise ValueError(
                "Missing required configuration:\n" + "\n".join(f"  - {e}" for e in errors)
            )


settings = Settings()
