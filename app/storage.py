import json
import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))


def write_json(name, data):
    """Atomic write: a crash can't leave a half-written file."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / name
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(target)


def read_json(name):
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))
