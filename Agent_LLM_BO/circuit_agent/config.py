"""Configuration management for Circuit Agent."""

from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings

# Load .env from the circuit_agent directory
_ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(_ENV_PATH)


class Settings(BaseSettings):
    # LLM
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-pro"

    # PDK
    pdk_path: str = "/mnt/hgfs/Share/PDKS/TSMC28nm/models/spectre/toplevel.scs"
    pdk_section: str = "top_tt"
    vdd: float = 0.9
    min_l: float = 30e-9
    nmos_model: str = "nch_mac"
    pmos_model: str = "pch_mac"

    # Spectre
    spectre_timeout: int = 300
    spectre_cmd_template: str = (
        "spectre +lang spice {netlist_path} +aps -raw {raw_dir} 2>&1 | tee {log_path}"
    )

    # Optimization
    max_iterations: int = 50
    llm_validation_frequency: int = 5
    stagnation_window: int = 10
    max_topology_changes: int = 3
    max_repair_attempts: int = 3

    # Paths
    workspace_dir: str = "./workspace"
    knowledge_base_dir: str = "./knowledge_base"
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

    def validate_required(self) -> None:
        """Validate that required settings are present."""
        errors = []
        if not self.deepseek_api_key:
            errors.append("DEEPSEEK_API_KEY is not set")
        if not self.pdk_path:
            errors.append("PDK_PATH is not set")
        if errors:
            raise ValueError(
                "Missing required configuration:\n" + "\n".join(f"  - {e}" for e in errors)
            )


settings = Settings()
