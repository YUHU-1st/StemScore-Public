from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
from typing import Any

from .constants import PROJECT_FILE, STAGES


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class Artifact:
    role: str
    path: str
    sha256: str
    size: int
    media_type: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_file(
        cls,
        path: Path,
        role: str,
        media_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> "Artifact":
        resolved = path.resolve()
        return cls(
            role=role,
            path=str(resolved),
            sha256=sha256_file(resolved),
            size=resolved.stat().st_size,
            media_type=media_type,
            metadata=metadata or {},
        )

    def verify(self) -> tuple[bool, str]:
        path = Path(self.path)
        if not path.is_file():
            return False, f"文件不存在：{path}"
        if path.stat().st_size != self.size:
            return False, f"文件大小已变化：{path.name}"
        if sha256_file(path) != self.sha256:
            return False, f"文件哈希已变化：{path.name}"
        return True, ""


@dataclass
class StageRecord:
    number: int
    name: str
    status: str = "locked"
    started_at: str | None = None
    completed_at: str | None = None
    reviewed_at: str | None = None
    artifacts: list[Artifact] = field(default_factory=list)
    commands: list[list[str]] = field(default_factory=list)
    models: list[dict[str, Any]] = field(default_factory=list)
    explanation: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class ProjectManifest:
    schema_version: int
    project_id: str
    title: str
    root: str
    source_original: str
    created_at: str
    updated_at: str
    hardware: dict[str, Any]
    settings: dict[str, Any]
    stages: list[StageRecord]

    @classmethod
    def create(
        cls,
        project_id: str,
        title: str,
        root: Path,
        source_original: Path,
        settings: dict[str, Any],
    ) -> "ProjectManifest":
        stages = [StageRecord(number=n, name=name) for n, name, _ in STAGES]
        stages[0].status = "ready"
        now = utc_now()
        return cls(
            schema_version=1,
            project_id=project_id,
            title=title,
            root=str(root.resolve()),
            source_original=str(source_original.resolve()),
            created_at=now,
            updated_at=now,
            hardware=collect_hardware(),
            settings=settings,
            stages=stages,
        )

    @property
    def path(self) -> Path:
        return Path(self.root) / PROJECT_FILE

    def stage(self, number: int) -> StageRecord:
        return self.stages[number - 1]

    def save(self) -> None:
        self.updated_at = utc_now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    @classmethod
    def load(cls, path: Path) -> "ProjectManifest":
        data = json.loads(path.read_text(encoding="utf-8"))
        data["stages"] = [
            StageRecord(
                **{
                    **stage,
                    "artifacts": [Artifact(**artifact) for artifact in stage.get("artifacts", [])],
                }
            )
            for stage in data["stages"]
        ]
        return cls(**data)


def collect_hardware() -> dict[str, Any]:
    gpu = "unknown"
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        gpu = result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "processor": platform.processor(),
        "gpu": gpu,
    }

