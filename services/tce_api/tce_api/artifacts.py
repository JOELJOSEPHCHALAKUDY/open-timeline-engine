from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from pathlib import Path


class ArtifactStore(ABC):
    @abstractmethod
    def put_bytes(self, content: bytes, namespace: str, filename: str) -> tuple[str, str]:
        raise NotImplementedError


class LocalFilesystemArtifactStore(ArtifactStore):
    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(self, content: bytes, namespace: str, filename: str) -> tuple[str, str]:
        digest = hashlib.sha256(content).hexdigest()
        out_dir = self.root / namespace
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / filename
        path.write_bytes(content)
        return str(path), digest
