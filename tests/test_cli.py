import yaml

from tests.conftest import diag
from ts.cli import main


def write(path, data):
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def test_lint_list_show(rules_dir, capsys):
    assert main(["--rules", str(rules_dir), "rule", "lint"]) == 0
    assert " 0 errors, " in capsys.readouterr().out  # rules a person accepted from a suggestion may carry warnings (test_shipped_rules_are_clean)
    assert main(["--rules", str(rules_dir), "rule", "list"]) == 0
    out = capsys.readouterr().out
    assert "interface-down" in out
    assert main(["--rules", str(rules_dir), "rule", "show", "interface-down"]) == 0
    assert "snmp_saw_down {device} {interface}" in capsys.readouterr().out


def test_install_forces_proposed_and_keeps_comments(rules_dir, tmp_path, capsys):
    data = diag("link-flap-check", event="interface.up", mode="active")
    src = tmp_path / "new.yaml"
    src.write_text("# written by a person after incident 42\n" + yaml.safe_dump(data, sort_keys=False))
    assert main(["--rules", str(rules_dir), "rule", "install", str(src)]) == 0
    dest = rules_dir / "diagnose" / "link-flap-check.yaml"
    text = dest.read_text()
    assert text.startswith("# written by a person")
    assert yaml.safe_load(text)["mode"] == "proposed"
    assert "mode set to proposed" in capsys.readouterr().out


def test_install_refuses_duplicates_and_old_versions(rules_dir, tmp_path, capsys):
    src = write(tmp_path / "r.yaml", diag("link-flap-check", event="interface.up"))
    assert main(["--rules", str(rules_dir), "rule", "install", str(src)]) == 0
    assert main(["--rules", str(rules_dir), "rule", "install", str(src)]) == 1
    assert "already installed" in capsys.readouterr().out
    assert main(["--rules", str(rules_dir), "rule", "install", "--replace", str(src)]) == 1
    assert "must be higher" in capsys.readouterr().out
    write(src, diag("link-flap-check", event="interface.up", version=2))
    assert main(["--rules", str(rules_dir), "rule", "install", "--replace", str(src)]) == 0


def test_install_refuses_new_errors(rules_dir, tmp_path, capsys):
    clash = write(tmp_path / "clash.yaml", diag("another-down-rule", priority=100))  # same match as interface-down
    assert main(["--rules", str(rules_dir), "rule", "install", str(clash)]) == 1
    out = capsys.readouterr().out
    assert "overlaps interface-down" in out and "not installed" in out
    assert not (rules_dir / "diagnose" / "another-down-rule.yaml").exists()
    r5 = write(tmp_path / "r5.yaml", diag("poke-the-isp", event="interface.up",
                                         steps=[{"check": "interface_status r5 Ethernet1"}]))
    assert main(["--rules", str(rules_dir), "rule", "install", str(r5)]) == 1
    assert "ISP router" in capsys.readouterr().out


def test_validate_does_not_write(rules_dir, tmp_path, capsys):
    src = write(tmp_path / "v.yaml", diag("validate-only", event="interface.up"))
    assert main(["--rules", str(rules_dir), "rule", "validate", str(src)]) == 0
    assert "ok" in capsys.readouterr().out
    assert not (rules_dir / "diagnose" / "validate-only.yaml").exists()


def test_misplaced_file_is_a_load_error(rules_dir, capsys):
    (rules_dir / "fix").mkdir(exist_ok=True)
    (rules_dir / "fix" / "interface-down.yaml").write_text((rules_dir / "diagnose" / "interface-down.yaml").read_text())
    assert main(["--rules", str(rules_dir), "rule", "lint"]) == 1
    assert "belongs in diagnose/" in capsys.readouterr().out
