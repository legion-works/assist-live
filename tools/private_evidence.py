import json
import os
from collections.abc import Iterable
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def external_output(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError("evidence output must be outside the repository")
    return resolved


def write_private(path: Path, chunks: Iterable[str]) -> None:
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    ) as temporary:
        staged = Path(temporary.name)
        try:
            os.fchmod(temporary.fileno(), 0o600)
            temporary.writelines(chunks)
            temporary.close()
            staged.replace(path)
        finally:
            staged.unlink(missing_ok=True)


def write_private_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    write_private(path, (json.dumps(row, allow_nan=False) + "\n" for row in rows))
