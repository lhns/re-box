"""Prefixing, name validation and the merged tools/prompts/resources catalog."""

import re
from dataclasses import dataclass, field

from .config import ChildSpec

NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class CatalogError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class ChildCatalog:
    tools: list[dict] = field(default_factory=list)
    prompts: list[dict] = field(default_factory=list)
    resources: list[dict] = field(default_factory=list)
    templates: list[dict] = field(default_factory=list)


def _prefixed(prefix: str, item: dict) -> dict:
    return {**item, "name": prefix + item.get("name", "")}


@dataclass(frozen=True)
class Catalog:
    """Immutable merged view. `tools`/`prompts` map prefixed name to (child name, original name)."""

    tools: dict[str, tuple[str, str]]
    prompts: dict[str, tuple[str, str]]
    resources: dict[str, str]  # uri -> child
    templates: tuple[tuple[str, str], ...]  # (static uri prefix, child), longest first
    lists: dict[str, list[dict]]  # method result key -> merged items
    denied: frozenset[str] = frozenset()  # prefixed names of tools hidden by a child's `deny`

    @classmethod
    def build(cls, specs: tuple[ChildSpec, ...], parts: dict[str, ChildCatalog]) -> "Catalog":
        problems: list[str] = []
        tools: dict[str, tuple[str, str]] = {}
        prompts: dict[str, tuple[str, str]] = {}
        resources: dict[str, str] = {}
        templates: list[tuple[str, str]] = []
        denied: set[str] = set()
        lists: dict[str, list[dict]] = {"tools": [], "prompts": [], "resources": [], "resourceTemplates": []}

        def claim(kind: str, table: dict, name: str, child: str, original: str) -> None:
            if not NAME_RE.fullmatch(name):
                problems.append(f"{kind} name {name!r} ({child}) must match [a-zA-Z0-9_-]{{1,64}}")
            if name in table:
                problems.append(f"{kind} name {name!r} is provided by both {table[name][0]} and {child}")
            else:
                table[name] = (child, original)

        for spec in specs:
            part = parts.get(spec.name)
            if part is None:
                continue
            for tool in part.tools:
                item = _prefixed(spec.prefix, tool)
                if tool.get("name") in spec.deny:
                    denied.add(item["name"])
                    continue
                claim("tool", tools, item["name"], spec.name, tool.get("name", ""))
                lists["tools"].append(item)
            for prompt in part.prompts:
                item = _prefixed(spec.prefix, prompt)
                claim("prompt", prompts, item["name"], spec.name, prompt.get("name", ""))
                lists["prompts"].append(item)
            for res in part.resources:
                uri = res.get("uri", "")
                if uri in resources:
                    problems.append(f"resource {uri!r} is provided by both {resources[uri]} and {spec.name}")
                resources[uri] = resources.get(uri, spec.name)
                lists["resources"].append(_prefixed(spec.prefix, res))
            for tpl in part.templates:
                templates.append((tpl.get("uriTemplate", "").split("{", 1)[0], spec.name))
                lists["resourceTemplates"].append(_prefixed(spec.prefix, tpl))
        if problems:
            raise CatalogError(problems)
        templates.sort(key=lambda t: -len(t[0]))
        return cls(tools, prompts, resources, tuple(templates), lists, frozenset(denied))

    def owner_of_uri(self, uri: str) -> str | None:
        if uri in self.resources:
            return self.resources[uri]
        return next((child for prefix, child in self.templates if uri.startswith(prefix)), None)
