from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_summary(output: Path, summary: dict[str, Any]) -> None:
    path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output, prefix=".summary-", delete=False
        ) as temporary:
            path = temporary.name
            temporary.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        os.replace(path, output / "summary.json")
    finally:
        if path and os.path.exists(path):
            os.unlink(path)
