"""Settings from the environment and the children from the TOML file."""

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
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
    catalog_cache: "CatalogCacheSpec | None" = None


_ENV_REF = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def fill_in(text: str, values: Mapping[str, str]) -> str:
    """{NAME} in `text` replaced by values[NAME]; an unknown name becomes empty."""
    return _ENV_REF.sub(lambda m: values.get(m.group(1), ""), text)


def child_env(spec: ChildSpec, environ: Mapping[str, str]) -> dict[str, str]:
    """The child's [child.env] with {NAME} filled in from `environ` (the supervisor's own environment,
    REBOX_* included)."""
    return {k: fill_in(v, environ) for k, v in spec.env.items()}


def service_env(environ: Mapping[str, str] = os.environ) -> dict[str, str]:
    """What the processes we start inherit: everything but REBOX_* (the token stays with us)."""
    return {k: v for k, v in environ.items() if not k.startswith("REBOX_")}


def child_environ(spec: ChildSpec, instance: str, session: str, environ: Mapping[str, str] = os.environ) -> dict[str, str]:
    """A child's whole environment: service_env, its [child.env], and the tags the leftover sweep finds it by."""
    return {**service_env(environ), **child_env(spec, environ), "REBOX_INSTANCE": instance, "REBOX_SESSION": session}


@dataclass(frozen=True)
class CatalogCacheSpec:
    """Remember the child's full catalog under REBOX_STATE_DIR so a session can advertise it while the
    shared backend is still starting. `key` names what invalidates it: {NAME} is an environment
    variable (the child's [child.env] wins), {file:/path} the stripped content of a file."""

    key: str


@dataclass(frozen=True)
class Autosave:
    """A child tool that persists the child's work; called on session close and periodically."""

    tool: str  # the child's own (unprefixed) tool name
    arguments: Mapping[str, Any] = field(default_factory=dict)  # {state_dir} and {session} are substituted
    prune_empty: bool = False  # after a save, delete arguments.path when it is an evidence bundle with no content
    before_tools: tuple[str, ...] = ()  # the child's tools that drop its work: save first (e.g. REA's close_binary)


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
    start_timeout: float = 300.0  # how long a call or initialize waits for the backend (REBOX_HEADLESS_START_TIMEOUT)
    orphan_timeout: float = 300.0  # idle limit of a session with no open GET stream; 0: same as idle_timeout
    evict_min_idle: float = 120.0  # at the session cap, sessions idle at least this long may be evicted
    progress_keepalive: float = 30.0  # synthetic progress for quiet requests with a progressToken; 0: off
    # [session] texts of the TOML config, see load_texts.
    instructions: str = ""
    backend_failed: str = "The backend is {state}: {reason}. Try again later."
    backend_starting: str = "The backend is still starting after {seconds} s. Try again later."


def _tool_call(name: str, table: str, raw: Any) -> tuple[str, dict]:
    tool = raw.get("tool") if isinstance(raw, dict) else None
    args = raw.get("arguments", {}) if isinstance(raw, dict) else None
    if not (isinstance(tool, str) and tool) or not isinstance(args, dict):
        raise ConfigError(f"child {name}: {table} needs a non-empty tool name and an arguments table")
    return tool, dict(args)


def _autosave(name: str, raw: Any) -> Autosave | None:
    if raw is None:
        return None
    tool, args = _tool_call(name, "autosave", raw)
    before = raw.get("before_tools", [])
    if not (isinstance(before, list) and all(isinstance(t, str) and t for t in before)):
        raise ConfigError(f"child {name}: autosave.before_tools must be a list of tool names")
    return Autosave(tool, args, bool(raw.get("prune_empty", False)), tuple(before))


def _catalog_cache(name: str, raw: Any) -> CatalogCacheSpec | None:
    if raw is None:
        return None
    key = raw.get("key") if isinstance(raw, dict) else None
    if not (isinstance(key, str) and key):
        raise ConfigError(f"child {name}: catalog_cache needs a non-empty key")
    return CatalogCacheSpec(key)


def _on_ready(name: str, raw: Any) -> OnReady | None:
    return None if raw is None else OnReady(*_tool_call(name, "on_ready", raw))


def _read(path: str) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read children config {path}: {e}") from e


TEXTS = ("instructions", "backend_failed", "backend_starting")


def load_texts(path: str, environ: Mapping[str, str] = os.environ) -> dict[str, str]:
    """The [session] table: `instructions` (initialize result; {NAME} filled in from `environ`), and
    `backend_failed` ({state}, {reason}) / `backend_starting` ({seconds}) for a call held for the backend."""
    raw = _read(path).get("session", {})
    if not isinstance(raw, dict) or not all(isinstance(raw.get(k, ""), str) for k in TEXTS) or set(raw) - set(TEXTS):
        raise ConfigError(f"[session] in {path} takes only the strings {', '.join(TEXTS)}")
    texts = {k: raw[k] for k in TEXTS if k in raw}
    if "instructions" in texts:
        texts["instructions"] = fill_in(texts["instructions"], environ).strip()
    return texts


def _env_deny(specs: list[ChildSpec], environ: Mapping[str, str]) -> list[ChildSpec]:
    """Add REBOX_DENY_TOOLS (comma-separated prefixed names) to the children's deny lists. Refuses an unknown
    prefix and a tool the child's autosave or on_ready needs, since hiding it would break them quietly."""
    extra: dict[str, list[str]] = {s.name: [] for s in specs}
    for name in (n.strip() for n in environ.get("REBOX_DENY_TOOLS", "").split(",")):
        if not name:
            continue
        owner = max((s for s in specs if name.startswith(s.prefix)), key=lambda s: len(s.prefix), default=None)
        if owner is None:
            raise ConfigError(f"REBOX_DENY_TOOLS: {name!r} starts with no child's prefix ({', '.join(s.prefix for s in specs)})")
        tool = name[len(owner.prefix):]
        needed = set(owner.autosave.before_tools if owner.autosave else ())
        needed |= {h.tool for h in (owner.autosave, owner.on_ready) if h}
        if tool in needed:
            raise ConfigError(f"REBOX_DENY_TOOLS: {name!r} is used by the autosave or on_ready hooks of child {owner.name}")
        extra[owner.name].append(tool)
    return [replace(s, deny=tuple(dict.fromkeys((*s.deny, *extra[s.name])))) for s in specs]


def load_children(path: str, environ: Mapping[str, str] = os.environ) -> tuple[ChildSpec, ...]:
    data = _read(path)
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
            _on_ready(name, c.get("on_ready")), tuple(deny), _catalog_cache(name, c.get("catalog_cache")),
        ))
    if not specs:
        raise ConfigError(f"no [[child]] entries in {path}")
    if len({s.name for s in specs}) != len(specs):
        raise ConfigError("duplicate child names")
    return tuple(_env_deny(specs, environ))


def number(env: Mapping[str, str], key: str, default: float, cast=float):
    """env[key] as a number (default when unset); ConfigError naming the key otherwise."""
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
    max_sessions = number(env, "REBOX_MAX_SESSIONS", 4, int)
    if max_sessions < 1:
        raise ConfigError("REBOX_MAX_SESSIONS must be at least 1")
    state_dir = env.get("REBOX_STATE_DIR", "")
    interval = number(env, "REBOX_AUTOSAVE_INTERVAL", 300)
    timeout = number(env, "REBOX_AUTOSAVE_TIMEOUT", 20)
    if interval < 0 or timeout <= 0:
        raise ConfigError("REBOX_AUTOSAVE_INTERVAL must be >= 0 and REBOX_AUTOSAVE_TIMEOUT > 0")
    orphan = number(env, "REBOX_SESSION_ORPHAN_TIMEOUT", 300)
    evict = number(env, "REBOX_SESSION_EVICT_MIN_IDLE", 120)
    keepalive = number(env, "REBOX_PROGRESS_KEEPALIVE_INTERVAL", 30)
    if min(orphan, evict, keepalive) < 0:
        raise ConfigError(
            "REBOX_SESSION_ORPHAN_TIMEOUT, REBOX_SESSION_EVICT_MIN_IDLE and REBOX_PROGRESS_KEEPALIVE_INTERVAL must be >= 0"
        )
    path = env.get("REBOX_CONFIG", "/etc/rebox/aggregator.toml")
    return Settings(
        token=token,
        children=load_children(path, env),
        **load_texts(path, env),
        port=number(env, "REBOX_PORT", 8443, int),
        bind=env.get("REBOX_BIND", "0.0.0.0"),
        tls_cert=cert,
        tls_key=key,
        max_sessions=max_sessions,
        idle_timeout=number(env, "REBOX_SESSION_IDLE_TIMEOUT", 1800),
        child_grace=number(env, "REBOX_CHILD_GRACE", 5),
        init_timeout=number(env, "REBOX_INIT_TIMEOUT", 120),
        max_body=number(env, "REBOX_MAX_BODY_BYTES", 16 * 1024 * 1024, int),
        state_dir=state_dir,
        autosave_interval=interval,
        autosave_timeout=timeout,
        orphan_timeout=orphan,
        evict_min_idle=evict,
        progress_keepalive=keepalive,
        start_timeout=number(env, "REBOX_HEADLESS_START_TIMEOUT", 300),
    )
