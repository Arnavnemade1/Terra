"""Configuration contains paths and capability flags, never public secrets."""
from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class Settings:
    root: Path
    data_dir: Path
    temporal_address: str
    temporal_namespace: str
    task_queue: str
    compute_mode: str
    compute_timeout: int
    docker_image: str

    @property
    def database(self) -> Path:
        return self.data_dir / "terra.db"

    @property
    def inputs(self) -> Path:
        return self.data_dir / "inputs"

    @property
    def artifacts(self) -> Path:
        return self.data_dir / "artifacts"

    def ensure_directories(self) -> None:
        for path in (self.data_dir, self.inputs, self.artifacts):
            path.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    root = Path(__file__).resolve().parents[2]
    return Settings(
        root=root,
        data_dir=Path(os.getenv("TERRA_DATA_DIR", str(root / ".data"))).resolve(),
        temporal_address=os.getenv("TEMPORAL_ADDRESS", "127.0.0.1:7233"),
        temporal_namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
        task_queue=os.getenv("TERRA_TASK_QUEUE", "terra-science"),
        compute_mode=os.getenv("TERRA_COMPUTE_MODE", "subprocess"),
        compute_timeout=int(os.getenv("TERRA_COMPUTE_TIMEOUT", "600")),
        docker_image=os.getenv("TERRA_COMPUTE_IMAGE", "terra-compute:local"),
    )
