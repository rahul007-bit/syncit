"""Field/search history for the create wizard.

Persists recently-used values to ~/.config/syncit/history.json so prompts
become "type-or-pick": recent entries are offered as select choices with a
"Enter a new value…" option.

Schema:
    {
      "fields":   {"bundle_name": [...], "version": [...], ...},
      "searches": {"apt": [...], "dnf": [...], "pypi": [...], "docker": [...]},
      "repos":    {"dnf:9": [...], "apt:noble": [...]}
    }

Every key holds at most MAX_HISTORY entries, most-recent first, deduped.
A missing or corrupt file is treated as empty history and never crashes the
wizard — history is a convenience, never a requirement.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import questionary

MAX_HISTORY = 10
NEW_VALUE = "Enter a new value…"

DEFAULT_PATH = Path(os.environ.get("SYNCIT_HISTORY", "~/.config/syncit/history.json")).expanduser()

_store: dict | None = None


def _history_path() -> Path:
    return DEFAULT_PATH


def _load() -> dict:
    global _store
    if _store is not None:
        return _store
    path = _history_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _store = data
            return _store
    except Exception:
        pass
    _store = {}
    return _store


def _save() -> None:
    assert _store is not None
    path = _history_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(_store, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass  # history must never break the wizard


def reset_cache() -> None:
    """Forget the in-memory store (mainly for tests)."""
    global _store
    _store = None


def _bucket(store: dict, section: str, key: str) -> list:
    sec = store.setdefault(section, {})
    if not isinstance(sec, dict):
        sec = store[section] = {}
    bucket = sec.setdefault(key, [])
    if not isinstance(bucket, list):
        bucket = sec[key] = []
    return bucket


def _record(store: dict, section: str, key: str, value: str) -> None:
    bucket = _bucket(store, section, key)
    value = value.strip()
    if not value:
        return
    if value in bucket:
        bucket.remove(value)
    bucket.insert(0, value)
    del bucket[MAX_HISTORY:]


def record_value(field: str, value: str) -> None:
    """Remember a generic field value (bundle name, version, paths…)."""
    if not value or not value.strip():
        return
    store = _load()
    _record(store, "fields", field, value)
    _save()


def recent_values(field: str) -> list[str]:
    """Most-recent-first list of previously used values for a field."""
    bucket = _bucket(_load(), "fields", field)
    return [v for v in bucket if isinstance(v, str)]


def record_search(browser: str, term: str) -> None:
    """Remember a search term for a browser (apt/dnf/pypi/docker)."""
    if not term or not term.strip():
        return
    store = _load()
    _record(store, "searches", browser, term)
    _save()


def recent_searches(browser: str) -> list[str]:
    bucket = _bucket(_load(), "searches", browser)
    return [v for v in bucket if isinstance(v, str)]


def record_repos(plugin: str, codename: str, repos: list[str]) -> None:
    """Remember an upstream-repo selection, keyed by plugin:codename."""
    if not repos:
        return
    store = _load()
    key = f"{plugin}:{codename}" if codename else plugin
    for repo in repos:
        _record(store, "repos", key, repo if isinstance(repo, str) else str(repo))
    _save()


def recent_repos(plugin: str, codename: str) -> list[str]:
    key = f"{plugin}:{codename}" if codename else plugin
    bucket = _bucket(_load(), "repos", key)
    return [v for v in bucket if isinstance(v, str)]


# ── History-aware prompts ────────────────────────────────────────────────


def prompt_with_history(
    field: str, message: str, default: str = "", history: list[str] | None = None
) -> str | None:
    """
    Select from recent values (with current default surfaced) or type a new
    one. Returns the chosen string, or None on Ctrl+C/blank-new.
    """
    recents = history if history is not None else recent_values(field)
    recents = [r for r in recents if r]
    # Surface the default (e.g. value currently in the manifest) at the top
    ordered = list(recents)
    if default and default not in ordered:
        ordered.insert(0, default)
    elif default and default in ordered:
        ordered.remove(default)
        ordered.insert(0, default)

    if not ordered:
        value = questionary.text(message, default=default).ask()
        if value is not None and value.strip():
            record_value(field, value)
        return value

    choices = [
        questionary.Choice(title=f"{v}  [recent]", value=v) if v != default else v
        for v in ordered[:MAX_HISTORY]
    ]
    choices.append(questionary.Choice(title=NEW_VALUE, value=NEW_VALUE))
    picked = questionary.select(message, choices=choices).ask()
    if picked is None:
        return None
    if picked == NEW_VALUE:
        value = questionary.text(message, default=default).ask()
        if value is not None and value.strip():
            record_value(field, value)
        return value
    return picked


def prompt_search(browser: str, message: str, history: list[str] | None = None) -> str | None:
    """
    Search prompt with per-browser history: pick a previous term or enter a
    new search. Returns the term, or None on Ctrl+C/blank-new.
    """
    recents = history if history is not None else recent_searches(browser)
    recents = [r for r in recents if r]
    if not recents:
        term = questionary.text(message).ask()
        if term is not None and term.strip():
            record_search(browser, term)
        return term
    choices = [questionary.Choice(title=f"{r}  [recent]", value=r) for r in recents[:MAX_HISTORY]]
    choices.append(questionary.Choice(title="New search…", value="__new_search__"))
    picked = questionary.select(message, choices=choices, use_jk_keys=False).ask()
    if picked is None:
        return None
    if picked == "__new_search__":
        term = questionary.text(message).ask()
        if term is not None and term.strip():
            record_search(browser, term)
        return term
    return picked


def clear_history(field: str | None = None) -> bool:
    """
    Clear one field's history (or everything when field is None).
    Returns True if anything was removed.
    """
    store = _load()
    if field is None:
        had = any(
            isinstance(v, list) and v
            for sec in store.values()
            if isinstance(sec, dict)
            for v in sec.values()
        )
        store.clear()
        _save()
        return had
    removed = False
    for section in ("fields", "searches", "repos"):
        sec = store.get(section)
        if isinstance(sec, dict) and field in sec:
            sec.pop(field, None)
            removed = True
    if removed:
        _save()
    return removed
