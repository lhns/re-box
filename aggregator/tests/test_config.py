import pytest

from rebox_aggregator.app import instance_id
from rebox_aggregator.config import ConfigError, load_children, load_settings


def toml(tmp_path, body: str) -> str:
    p = tmp_path / "a.toml"
    p.write_text(body)
    return str(p)


GOOD = '[[child]]\nname = "rea"\nprefix = "rea_"\ncommand = ["/bin/true"]\n[child.env]\nX = "1"\n'


def test_load_children(tmp_path):
    (spec,) = load_children(toml(tmp_path, GOOD))
    assert (spec.name, spec.prefix, spec.command, dict(spec.env)) == ("rea", "rea_", ("/bin/true",), {"X": "1"})


@pytest.mark.parametrize("body", [
    '[[child]]\nname = "Bad"\ncommand = ["/bin/true"]\n',
    '[[child]]\nname = "x"\ncommand = ["true"]\n',
    '[[child]]\nname = "x"\ncommand = []\n',
    "",
])
def test_bad_children(tmp_path, body):
    with pytest.raises(ConfigError):
        load_children(toml(tmp_path, body))


def test_settings(tmp_path):
    cfg = toml(tmp_path, GOOD)
    s = load_settings({"REBOX_TOKEN": "t", "REBOX_CONFIG": cfg, "REBOX_MAX_SESSIONS": "2"})
    assert (s.port, s.max_sessions, s.idle_timeout, s.child_grace) == (8443, 2, 1800, 5)
    for bad in ({}, {"REBOX_TOKEN": "t", "REBOX_CONFIG": cfg, "REBOX_TLS_CERT": "/c"}):
        with pytest.raises(ConfigError):
            load_settings({"REBOX_CONFIG": cfg, **bad})


def test_instance_id_honours_env(monkeypatch):
    monkeypatch.setenv("REBOX_INSTANCE", "run-1")
    assert instance_id() == "run-1"
    monkeypatch.setenv("REBOX_INSTANCE", "bad id!")
    assert instance_id() != "bad id!"


AUTOSAVE = GOOD + '[child.autosave]\ntool = "export_evidence_bundle"\narguments = { path = "{state_dir}/rea/{session}.json", overwrite = true }\n'


def test_autosave_parsed(tmp_path):
    (spec,) = load_children(toml(tmp_path, AUTOSAVE))
    assert spec.autosave.tool == "export_evidence_bundle"
    assert dict(spec.autosave.arguments) == {"path": "{state_dir}/rea/{session}.json", "overwrite": True}
    assert load_children(toml(tmp_path, GOOD))[0].autosave is None


@pytest.mark.parametrize("bad", [
    '[child.autosave]\ntool = ""\n',
    '[child.autosave]\narguments = {}\n',
    '[child.autosave]\ntool = "x"\narguments = "no"\n',
])
def test_bad_autosave(tmp_path, bad):
    with pytest.raises(ConfigError, match="autosave"):
        load_children(toml(tmp_path, GOOD + bad))


def test_shipped_config_has_rea_autosave():
    from pathlib import Path

    repo = Path(__file__).parents[2] / "rootfs/etc/rebox/aggregator.toml"  # in the image: the installed copy
    specs = {s.name: s for s in load_children(str(repo if repo.exists() else "/etc/rebox/aggregator.toml"))}
    assert specs["rea"].autosave.tool == "export_evidence_bundle" and specs["ghidra"].autosave is None


def test_autosave_settings(tmp_path):
    cfg = toml(tmp_path, GOOD)
    s = load_settings({"REBOX_TOKEN": "t", "REBOX_CONFIG": cfg})
    assert (s.state_dir, s.autosave_interval, s.autosave_timeout) == ("", 300, 20)
    s = load_settings({"REBOX_TOKEN": "t", "REBOX_CONFIG": cfg, "REBOX_STATE_DIR": "/s", "REBOX_AUTOSAVE_INTERVAL": "0"})
    assert (s.state_dir, s.autosave_interval) == ("/s", 0)
    for bad in ("-1", "x"):
        with pytest.raises(ConfigError):
            load_settings({"REBOX_TOKEN": "t", "REBOX_CONFIG": cfg, "REBOX_AUTOSAVE_INTERVAL": bad})


CHILD = '[[child]]\nname = "rea"\nprefix = "rea_"\ncommand = ["/bin/true"]\n'
READY = '[child.on_ready]\ntool = "connect"\narguments = { project = "p" }\n'


def test_deny_and_on_ready_parsed(tmp_path):
    (spec,) = load_children(toml(tmp_path, CHILD.replace("\n[child", "\n[child") + 'deny = ["run_script"]\n' + READY))
    assert spec.deny == ("run_script",)
    assert (spec.on_ready.tool, dict(spec.on_ready.arguments)) == ("connect", {"project": "p"})
    (spec,) = load_children(toml(tmp_path, GOOD))
    assert spec.deny == () and spec.on_ready is None


@pytest.mark.parametrize("bad", ['deny = "x"\n', 'deny = [1]\n', 'deny = [""]\n'])
def test_bad_deny(tmp_path, bad):
    with pytest.raises(ConfigError, match="deny"):
        load_children(toml(tmp_path, CHILD + bad))


@pytest.mark.parametrize("bad", ['[child.on_ready]\ntool = ""\n', '[child.on_ready]\narguments = {}\n'])
def test_bad_on_ready(tmp_path, bad):
    with pytest.raises(ConfigError, match="on_ready"):
        load_children(toml(tmp_path, GOOD + bad))


def test_prune_empty_parsed(tmp_path):
    (spec,) = load_children(toml(tmp_path, AUTOSAVE + "prune_empty = true\n"))
    assert spec.autosave.prune_empty is True
    assert load_children(toml(tmp_path, AUTOSAVE))[0].autosave.prune_empty is False


def test_shipped_config_denies_scripts_and_connects_on_ready():
    from pathlib import Path

    repo = Path(__file__).parents[2] / "rootfs/etc/rebox/aggregator.toml"
    specs = {s.name: s for s in load_children(str(repo if repo.exists() else "/etc/rebox/aggregator.toml"))}
    assert specs["ghidra"].deny == ("run_ghidra_script", "run_script_inline")
    assert (specs["ghidra"].on_ready.tool, dict(specs["ghidra"].on_ready.arguments)) == ("connect_instance", {"project": "rebox"})
    assert specs["rea"].autosave.prune_empty and not specs["rea"].deny


def test_session_policy_settings(tmp_path):
    cfg = toml(tmp_path, GOOD)
    s = load_settings({"REBOX_TOKEN": "t", "REBOX_CONFIG": cfg})
    assert (s.orphan_timeout, s.evict_min_idle, s.progress_keepalive) == (300, 120, 30)
    s = load_settings({
        "REBOX_TOKEN": "t", "REBOX_CONFIG": cfg, "REBOX_SESSION_ORPHAN_TIMEOUT": "60",
        "REBOX_SESSION_EVICT_MIN_IDLE": "10", "REBOX_PROGRESS_KEEPALIVE_INTERVAL": "0",
    })
    assert (s.orphan_timeout, s.evict_min_idle, s.progress_keepalive) == (60, 10, 0)
    for key in ("REBOX_SESSION_ORPHAN_TIMEOUT", "REBOX_SESSION_EVICT_MIN_IDLE", "REBOX_PROGRESS_KEEPALIVE_INTERVAL"):
        for bad in ("-1", "x"):
            with pytest.raises(ConfigError):
                load_settings({"REBOX_TOKEN": "t", "REBOX_CONFIG": cfg, key: bad})
