import json
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

CATALOG_URL = "https://raw.githubusercontent.com/rahul007-bit/syncit/main/syncit/registry/catalog.json"
LOCAL_CATALOG = Path(__file__).parent / "catalog.json"

def get_catalog() -> dict[str, Any]:
    """
    Fetch the catalog. Try remote GitHub first, fallback to local JSON.
    """
    try:
        req = urllib.request.Request(CATALOG_URL, headers={"User-Agent": "syncit"})
        with urllib.request.urlopen(req, timeout=3) as response:
            if response.status == 200:
                return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError):
        pass

    # Fallback to local
    if LOCAL_CATALOG.exists():
        with open(LOCAL_CATALOG, "r", encoding="utf-8") as f:
            return json.load(f)
            
    return {}

def resolve_template(
    template: dict[str, Any], 
    version: str, 
    codename: str = ""
) -> dict[str, Any]:
    """
    Resolves variables like {version}, {major_minor}, {codename} in the template.
    Returns a new dict (deep copy with replaced strings).
    """
    # Calculate major_minor from version
    # e.g., '1.35.5' -> '1.35'
    parts = version.split('.')
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
