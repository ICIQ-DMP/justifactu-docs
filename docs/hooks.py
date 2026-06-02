"""MkDocs hook: inject dynamically discovered package roots into mkdocstrings.

Runs on_config (before any module resolution happens) and adds the same roots
that gen_ref_pages.py uses to both sys.path and the mkdocstrings handler paths,
so griffe can import modules regardless of how the source tree is laid out.
"""

import sys
from pathlib import Path


def _find_src_roots(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    package_dirs = {p.parent for p in base.rglob("__init__.py")}
    if not package_dirs:
        return [base]
    return sorted({d.parent for d in package_dirs if d.parent not in package_dirs})


def on_config(config):
    base = Path("source")
    search_root = (base / "src") if (base / "src").is_dir() else base
    roots = _find_src_roots(search_root) or [search_root]
    str_roots = [str(r.resolve()) for r in roots]

    for p in str_roots:
        if p not in sys.path:
            sys.path.insert(0, p)

    plugin = config.get("plugins", {}).get("mkdocstrings")
    if plugin:
        try:
            plugin.config["handlers"]["python"]["paths"] = str_roots
        except (KeyError, TypeError, AttributeError):
            pass

    return config
