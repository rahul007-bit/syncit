import textwrap
from pathlib import Path

import pytest

from syncit.inventory.loader import load_inventory, resolve_targets
from syncit.inventory.schema import Inventory


def test_load_inventory_valid(tmp_path: Path) -> None:
    inv_file = tmp_path / "valid.yaml"
    inv_file.write_text(
        textwrap.dedent("""
    hosts:
      app-server:
        host: 192.168.1.10
        user: deploy
        ssh_key: ~/.ssh/id_rsa
      db-server:
        host: 192.168.1.51
        user: root
        bundle_dest: /tmp/syncit
    groups:
      all-servers:
        - app-server
        - db-server
    """)
    )

    inv = load_inventory(inv_file)
    assert len(inv.hosts) == 2
    assert inv.hosts["app-server"].user == "deploy"
    assert inv.hosts["db-server"].bundle_dest == "/tmp/syncit"
    assert "all-servers" in inv.groups
    assert len(inv.groups["all-servers"]) == 2


def test_load_inventory_invalid_yaml(tmp_path: Path) -> None:
    inv_file = tmp_path / "invalid.yaml"
    inv_file.write_text("hosts: [")  # Syntax error
    with pytest.raises(ValueError, match="Invalid YAML syntax"):
        load_inventory(inv_file)


def test_load_inventory_missing_host_in_group(tmp_path: Path) -> None:
    inv_file = tmp_path / "missing.yaml"
    inv_file.write_text(
        textwrap.dedent("""
    hosts:
      valid-server:
        host: 192.168.1.10
        user: deploy
    groups:
      g1:
        - valid-server
        - missing-server
    """)
    )
    with pytest.raises(ValueError, match="defines undefined host 'missing-server'"):
        load_inventory(inv_file)


def test_resolve_targets_single_host() -> None:
    inv = Inventory(hosts={"app1": {"host": "10.0.0.1", "user": "admin"}})
    targets = resolve_targets(inv, "app1")
    assert len(targets) == 1
    assert targets[0][0] == "app1"
    assert targets[0][1].host == "10.0.0.1"


def test_resolve_targets_group() -> None:
    inv = Inventory(
        hosts={"h1": {"host": "1", "user": "u"}, "h2": {"host": "2", "user": "u"}},
        groups={"my-group": ["h1", "h2"]},
    )
    targets = resolve_targets(inv, "my-group")
    assert len(targets) == 2
    names = [t[0] for t in targets]
    assert "h1" in names and "h2" in names


def test_resolve_targets_unknown() -> None:
    inv = Inventory(hosts={"h1": {"host": "1", "user": "u"}})
    with pytest.raises(ValueError, match="Unknown target 'unknown'"):
        resolve_targets(inv, "unknown")
