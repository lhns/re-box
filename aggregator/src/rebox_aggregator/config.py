"""Settings from the environment and the children from the TOML file."""

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NAME_RE = re.compile(r"^[a-z0-9-]+$")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ChildSpec:
    name: str
    prefix: str
    command: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    autosave: "Autosave | None" = None
    on_ready: "OnReady | None" = None
    deny: tuple[str, ...] = ()  # the child's own (unprefixed) tool names: hidden, and calls to them fail


@dataclass(frozen=True)
class Autosave:
    """A child tool that persists the child's work; called on session close and periodically."""

    tool: str  # the child's own (unprefixed) tool name
    arguments: Mapping[str, Any] = field(default_factory=dict)  # {state_dir} and {session} are substituted
    prune_empty: bool = False  # after a save, delete arguments.path when it is an evidence bundle with no content


@dataclass(frozen=True)
class OnReady:
    """A child tool called when the shared backend (the Ghidra server) becomes ready after the child booted."""

    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


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
    init_timeout: float = 120.0
    max_body: int = 16 * 1024 * 1024
    state_dir: str = ""  # empty: autosave disabled
    autosave_interval: float = 300.0  # 0: only on session close
    autosave_timeout: float = 20.0
    orphan_timeout: float = 300.0  # idle limit of a session with no open GET stream; 0: same as idle_timeout
    evict_min_idle: float = 120.0  # at the session cap, sessions idle at least this long may be evicted
    progress_keepalive: float = 30.0  # synthetic progress for quiet requests with a progressToken; 0: off


def _autosave(name: str, raw: Any) -> Autosave | None:
    if raw is None:
        return None
    tool = raw.get("tool") if isinstance(raw, dict) else None
    args = raw.get("arguments", {}) if isinstance(raw, dict) else None
    if not (isinstance(tool, str) and tool) or not isinstance(args, dict):
        raise ConfigError(f"child {name}: autosave needs a non-empty tool name and an arguments table")
    return Autosave(tool, dict(args), bool(raw.get("prune_empty", False)))


def _on_ready(name: str, raw: Any) -> OnReady | None:
    if raw is None:
        return None
    tool = raw.get("tool") if isinstance(raw, dict) else None
    args = raw.get("arguments", {}) if isinstance(raw, dict) else None
    if not (isinstance(tool, str) and tool) or not isinstance(args, dict):
        raise ConfigError(f"child {name}: on_ready needs a non-empty tool name and an arguments table")
    return OnReady(tool, dict(args))


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
        deny = c.get("deny", [])
        if not (isinstance(deny, list) and all(isinstance(t, str) and t for t in deny)):
            raise ConfigError(f"child {name}: deny must be a list of tool names")
        specs.append(ChildSpec(
            name, str(c.get("prefix", "")), tuple(command), dict(env), _autosave(name, c.get("autosave")),
            _on_ready(name, c.get("on_ready")), tuple(deny),
        ))
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


def read_token(env: Mapping[str, str]) -> str:
    """REBOX_TOKEN_FILE (preferred: env is readable via /proc by same-uid processes) or REBOX_TOKEN."""
    path = env.get("REBOX_TOKEN_FILE")
    if path:
        try:
            token = Path(path).read_text().strip()
        except OSError as e:
            raise ConfigError(f"cannot read REBOX_TOKEN_FILE {path}: {e.strerror}") from None
    else:
        token = env.get("REBOX_TOKEN", "")
    if not token:
        raise ConfigError("REBOX_TOKEN or REBOX_TOKEN_FILE is required (and must not be empty)")
    return token


def harden() -> None:
    """Make /proc/self/environ unreadable to other same-uid processes (children reset this on exec)."""
    try:
        import ctypes

        ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE
    except (OSError, AttributeError):
        pass


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    token = read_token(env)
    cert, key = env.get("REBOX_TLS_CERT") or None, env.get("REBOX_TLS_KEY") or None
    if bool(cert) != bool(key):
        raise ConfigError("REBOX_TLS_CERT and REBOX_TLS_KEY must be set together")
    max_sessions = _num(env, "REBOX_MAX_SESSIONS", 4, int)
    if max_sessions < 1:
        raise ConfigError("REBOX_MAX_SESSIONS must be at least 1")
    state_dir = env.get("REBOX_STATE_DIR", "")
    interval = _num(env, "REBOX_AUTOSAVE_INTERVAL", 300)
    timeout = _num(env, "REBOX_AUTOSAVE_TIMEOUT", 20)
    if interval < 0 or timeout <= 0:
        raise ConfigError("REBOX_AUTOSAVE_INTERVAL must be >= 0 and REBOX_AUTOSAVE_TIMEOUT > 0")
    orphan = _num(env, "REBOX_SESSION_ORPHAN_TIMEOUT", 300)
    evict = _num(env, "REBOX_SESSION_EVICT_MIN_IDLE", 120)
    keepalive = _num(env, "REBOX_PROGRESS_KEEPALIVE_INTERVAL", 30)
    if min(orphan, evict, keepalive) < 0:
        raise ConfigError(
            "REBOX_SESSION_ORPHAN_TIMEOUT, REBOX_SESSION_EVICT_MIN_IDLE and REBOX_PROGRESS_KEEPALIVE_INTERVAL must be >= 0"
        )
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
        init_timeout=_num(env, "REBOX_INIT_TIMEOUT", 120),
        max_body=_num(env, "REBOX_MAX_BODY_BYTES", 16 * 1024 * 1024, int),
        state_dir=state_dir,
        autosave_interval=interval,
        autosave_timeout=timeout,
        orphan_timeout=orphan,
        evict_min_idle=evict,
        progress_keepalive=keepalive,
    )
