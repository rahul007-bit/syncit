import json
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

CATALOG_URL = "https://raw.githubusercontent.com/rahul007-bit/syncit/main/syncit/registry/catalog.json"
LOCAL_CATALOG = Path(__file__).parent / "catalog.json"

USER_CATALOG = Path.home() / ".config" / "syncit" / "catalog.json"
PROJECT_CATALOG = Path("syncit-catalog.json")


def _uses_subtask_schema(catalog: dict) -> bool:
    """Return True if the catalog uses the current subtask-based schema."""
    return any("subtasks" in entry for entry in catalog.values())


def _load_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def get_catalog() -> dict[str, Any]:
    """
    Return the merged catalog.

    Priority (highest wins):
      project catalog  (./syncit-catalog.json)
      > user catalog   (~/.config/syncit/catalog.json)
      > remote catalog (GitHub raw, 3 s timeout, only if subtask schema matches)
      > bundled local  (syncit/registry/catalog.json)
    """
    # Baseline: bundled local copy
    catalog: dict = _load_json(LOCAL_CATALOG)

    # Try remote — only replace if schema-compatible
    try:
        req = urllib.request.Request(CATALOG_URL, headers={"User-Agent": "syncit"})
        with urllib.request.urlopen(req, timeout=3) as response:
            if response.status == 200:
                remote = json.loads(response.read().decode("utf-8"))
                if _uses_subtask_schema(remote):
                    catalog = remote
    except (urllib.error.URLError, json.JSONDecodeError):
        pass

    # Merge user catalog (~/.config/syncit/catalog.json)
    user = _load_json(USER_CATALOG)
    if user:
        catalog = {**catalog, **user}

    # Merge project catalog (./syncit-catalog.json) — highest priority
    project = _load_json(PROJECT_CATALOG)
    if project:
        catalog = {**catalog, **project}

    return catalog


def save_to_user_catalog(entry_id: str, entry: dict[str, Any]) -> Path:
    """Persist a new entry into the user-level catalog file."""
    USER_CATALOG.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_json(USER_CATALOG)
    existing[entry_id] = entry
    USER_CATALOG.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    return USER_CATALOG


def save_to_project_catalog(entry_id: str, entry: dict[str, Any]) -> Path:
    """Persist a new entry into the project-level catalog file."""
    existing = _load_json(PROJECT_CATALOG)
    existing[entry_id] = entry
    PROJECT_CATALOG.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    return PROJECT_CATALOG



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
        "{releasever}": codename,
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
    catalog: dict[str, Any] | None = None,
    distro: str = "",
) -> dict[str, Any] | None:
    """
    Resolve a single subtask definition to a concrete task dict.

    If a ``ref`` key is present, it looks up the referenced path in the catalog
    (e.g., ``kubernetes.subtasks.packages``) and resolves it recursively.
    Picks ``templates[plugin_type-{distro}]`` first, then ``templates[plugin_type]``; 
    falls back to ``templates["any"]`` for plugin-agnostic subtasks (OCI images, file downloads, etc.).
    Returns None if no matching template exists for this plugin type.
    """
    if "ref" in subtask:
        if not catalog:
            catalog = get_catalog()
        
        ref_parts = subtask["ref"].split(".")
        curr: Any = catalog
        for part in ref_parts:
            if isinstance(curr, dict) and part in curr:
                curr = curr[part]
            else:
                return None
                
        if isinstance(curr, dict):
            return resolve_subtask(curr, plugin_type, version, codename, catalog, distro)
        return None

    templates = subtask.get("templates", {})
    
    distro_normalized = distro.lower().replace(" ", "")
    
    template = (
        templates.get(f"{plugin_type}-{distro_normalized}") or
        templates.get(f"{plugin_type}-{distro.lower()}") or
        templates.get(plugin_type) or 
        templates.get("any")
    )
    
    if not template:
        return None
    return resolve_template(template, version, codename)
