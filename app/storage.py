import json
import os
from pathlib import Path

def _default_data_dir():
    # In Docker, /data is the mounted ./data folder. Outside Docker (e.g.
    # running `python -m app.admin` on the server) use that same ./data
    # folder directly, never a stray /data on the host.
    if Path("/.dockerenv").exists():
        return Path("/data")
    return Path(__file__).resolve().parent.parent / "data"


DATA_DIR = Path(os.getenv("DATA_DIR") or _default_data_dir())


def write_json(name, data):
    """Atomic write: a crash can't leave a half-written file."""
    target = DATA_DIR / name
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(target)


def read_json(name):
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))
