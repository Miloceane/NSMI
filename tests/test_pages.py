"""Every element a script looks up must exist on its page."""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).parent.parent / "app" / "static"
PAGES = [("login.html", "login.js"), ("index.html", "app.js")]


@pytest.mark.parametrize("page,script", PAGES)
def test_script_ids_exist_on_page(page, script):
    html = (STATIC / page).read_text(encoding="utf-8")
    js = (STATIC / script).read_text(encoding="utf-8")

    ids = set(re.findall(r'id="([^"]+)"', html))
    # ids the script builds itself (the colour dropdowns)
    ids |= set(re.findall(r'id="([^"$]+)"', js))
    used = set(re.findall(r'(?:getElementById|\$)\("([\w-]+)"\)', js))
    built = {u for u in used if u.startswith("cfg_")}

    missing = used - ids - built
    assert not missing, f"{script} uses ids missing from {page}: {sorted(missing)}"
