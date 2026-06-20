import json
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

CATALOG_URL = "https://raw.githubusercontent.com/rahul007-bit/syncit/main/syncit/registry/catalog.json"
LOCAL_CATALOG = Path(__file__).parent / "catalog.json"

def get_catalog() -> dict[str, Any]:
    """
    Fetch the catalog. Try remote GitHub first (3 s timeout), fallback to local JSON.
    Merge order: remote → local bundled copy.
    """
    try:
        req = urllib.request.Request(CATALOG_URL, headers={"User-Agent": "syncit"})
        with urllib.request.urlopen(req, timeout=3) as response:
            if response.status == 200:
                return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError):
        pass

    # Fallback to local bundled copy
    if LOCAL_CATALOG.exists():
        with open(LOCAL_CATALOG, "r", encoding="utf-8") as f:
            return json.load(f)

    return {}


def resolve_template(
    template: dict[str, Any],
    version: str,
    codename: str = "",
) -> dict[str, Any]:
    """
    Recursively replace {version}, {major_minor}, {codename} inside a template dict.
    Returns a new deep-copied dict with all placeholders resolved.
    """
    parts = version.split(".")
    major_minor = f"{parts[0]}.{parts[1]}" if len(parts) >= 2 else version

    replacements = {
        "{version}": version,
        "{major_minor}": major_minor,
        "{codename}": codename,
    }

    def _replace(obj: Any) -> Any:
        if isinstance(obj, str):
            res = obj
            for k, v in replacements.items():
                res = res.replace(k, str(v))
            return res
        elif isinstance(obj, list):
            return [_replace(i) for i in obj]
        elif isinstance(obj, dict):
            return {k: _replace(v) for k, v in obj.items()}
        return obj

    return _replace(template)


def resolve_subtask(
    subtask: dict[str, Any],
    plugin_type: str,
    version: str,
    codename: str = "",
) -> dict[str, Any] | None:
    """
    Resolve a single subtask definition to a concrete task dict.

    Picks ``templates[plugin_type]`` first; falls back to ``templates["any"]``
    for plugin-agnostic subtasks (OCI images, file downloads, etc.).
    Returns None if no matching template exists for this plugin type.
    """
    templates = subtask.get("templates", {})
    template = templates.get(plugin_type) or templates.get("any")
    if not template:
        return None
    return resolve_template(template, version, codename)
