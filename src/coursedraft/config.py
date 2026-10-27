from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(f"COURSEDRAFT_{name}", default)


@dataclass(frozen=True)
class Settings:
    """Runtime settings, read from COURSEDRAFT_* variables.

    The prefix keeps them clear of anything else the shell exports.
    """

    database_url: str = "postgresql://postgres:postgres@localhost:5452/coursedraft"
    data_dir: Path = Path("data")
    template_dir: Path = Path("templates")
    # Which adapter handles each sensitivity route. "offline" is the deterministic drafter
    # used by the tests and the demo stack; "anthropic", "openai" and "ollama" call real models.
    hosted_adapter: str = "offline"
    local_adapter: str = "offline"
    hosted_model: str = "claude-opus-5-5"
    local_model: str = "qwen3:14b"
    openai_base_url: str = "https://api.openai.com/v1"
    ollama_url: str = "http://localhost:11434/v1"
    embed_dim: int = 256
    # The offline drafter can reproduce the production failure (a near-miss standards
    # paragraph number) on roughly one slide in N, so the validator and redraft path run
    # without a paid model. 0 turns it off.
    fault_every: int = 15

    @classmethod
    def from_env(cls) -> Settings:
        base = cls()
        return cls(
            database_url=_env("DATABASE_URL", base.database_url),
            data_dir=Path(_env("DATA_DIR", str(base.data_dir))),
            template_dir=Path(_env("TEMPLATE_DIR", str(base.template_dir))),
            hosted_adapter=_env("HOSTED_ADAPTER", base.hosted_adapter),
            local_adapter=_env("LOCAL_ADAPTER", base.local_adapter),
            hosted_model=_env("HOSTED_MODEL", base.hosted_model),
            local_model=_env("LOCAL_MODEL", base.local_model),
            openai_base_url=_env("OPENAI_BASE_URL", base.openai_base_url),
            ollama_url=_env("OLLAMA_URL", base.ollama_url),
            embed_dim=int(_env("EMBED_DIM", str(base.embed_dim))),
            fault_every=int(_env("FAULT_EVERY", str(base.fault_every))),
        )

    def with_(self, **changes) -> Settings:
        return replace(self, **changes)

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def sources_dir(self) -> Path:
        return self.data_dir / "sources"
