"""Append-only JSONL result store, one line per completed seed attack."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class Finding:
    run_id: str
    seed_id: str
    category: str
    goal: str
    violated: bool
    severity: int
    rationale: str
    turns_to_success: Optional[int]
    transcript: List[dict]
    timestamp: float = field(default_factory=time.time)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, finding: Finding) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps(asdict(finding)) + "\n")

    def read_all(self) -> List[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]
