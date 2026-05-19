"""Utility functions for Circuit Agent."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from config import Settings


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    """Configure logging for the application."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


def ensure_directories(config: Settings) -> None:
    """Create all required directories if they don't exist."""
    config.get_workspace_path()
    config.get_outputs_path()

    kb_path = config.get_knowledge_base_path()
    (kb_path / "topology_examples").mkdir(parents=True, exist_ok=True)


def load_knowledge_base(config: Settings) -> dict[str, str]:
    """Load all knowledge base files into a dict."""
    kb_path = config.get_knowledge_base_path()
    content = {}

    if not kb_path.exists():
        return content

    for md_file in kb_path.glob("*.md"):
        content[md_file.stem] = md_file.read_text(encoding="utf-8")

    return content


def format_engineering(value: float, unit: str = "") -> str:
    """Format a float in engineering notation.

    Examples: 1.5e-12 -> "1.5p", 100e6 -> "100M"
    """
    if value == 0:
        return f"0{unit}"

    abs_val = abs(value)
    prefixes = [
        (1e12, "T"), (1e9, "G"), (1e6, "M"), (1e3, "k"),
        (1, ""), (1e-3, "m"), (1e-6, "u"), (1e-9, "n"),
        (1e-12, "p"), (1e-15, "f"),
    ]

    for threshold, prefix in prefixes:
        if abs_val >= threshold * 0.999:  # Small tolerance for floating point
            scaled = value / threshold
            if abs(scaled - round(scaled)) < 0.001:
                return f"{int(round(scaled))}{prefix}{unit}"
            return f"{scaled:.3g}{prefix}{unit}"

    return f"{value:.2e}{unit}"
