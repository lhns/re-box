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
