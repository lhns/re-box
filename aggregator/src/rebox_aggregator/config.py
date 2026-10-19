"""Settings from the environment and the children from the TOML file."""

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field

NAME_RE = re.compile(r"^[a-z0-9-]+$")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ChildSpec:
    name: str
    prefix: str
    command: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Settings:
    token: str
    children: tuple[ChildSpec, ...]
    port: int = 8443
    bind: str = "0.0.0.0"
    tls_cert: str | None = None
    tls_key: str | None = None
    max_sessions: int = 4
    idle_timeout: float = 1800.0
    child_grace: float = 5.0


def load_children(path: str) -> tuple[ChildSpec, ...]:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read children config {path}: {e}") from e
    specs = []
    for i, c in enumerate(data.get("child", [])):
        name = c.get("name", "")
        if not NAME_RE.match(name):
            raise ConfigError(f"child #{i}: name {name!r} must match [a-z0-9-]+")
        command = c.get("command")
        if not (isinstance(command, list) and command and all(isinstance(a, str) for a in command)):
            raise ConfigError(f"child {name}: command must be a non-empty list of strings")
        if not os.path.isabs(command[0]):
            raise ConfigError(f"child {name}: command[0] must be an absolute path")
        env = c.get("env", {})
        if not all(isinstance(v, str) for v in env.values()):
            raise ConfigError(f"child {name}: env values must be strings")
        specs.append(ChildSpec(name, str(c.get("prefix", "")), tuple(command), dict(env)))
    if not specs:
        raise ConfigError(f"no [[child]] entries in {path}")
    if len({s.name for s in specs}) != len(specs):
        raise ConfigError("duplicate child names")
    return tuple(specs)


def _num(env: Mapping[str, str], key: str, default: float, cast=float):
    try:
        return cast(env.get(key, default))
    except ValueError:
        raise ConfigError(f"{key} must be a number, got {env[key]!r}") from None


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    token = env.get("REBOX_TOKEN", "")
    if not token:
        raise ConfigError("REBOX_TOKEN is required")
    cert, key = env.get("REBOX_TLS_CERT") or None, env.get("REBOX_TLS_KEY") or None
    if bool(cert) != bool(key):
        raise ConfigError("REBOX_TLS_CERT and REBOX_TLS_KEY must be set together")
    max_sessions = _num(env, "REBOX_MAX_SESSIONS", 4, int)
    if max_sessions < 1:
        raise ConfigError("REBOX_MAX_SESSIONS must be at least 1")
    return Settings(
        token=token,
        children=load_children(env.get("REBOX_CONFIG", "/etc/rebox/aggregator.toml")),
        port=_num(env, "REBOX_PORT", 8443, int),
        bind=env.get("REBOX_BIND", "0.0.0.0"),
        tls_cert=cert,
        tls_key=key,
        max_sessions=max_sessions,
        idle_timeout=_num(env, "REBOX_SESSION_IDLE_TIMEOUT", 1800),
        child_grace=_num(env, "REBOX_CHILD_GRACE", 5),
    )
