"""Tool definitions and the registry the agent dispatches through.

A tool is the only way anything actually happens. Skills are prose; tools are
capability. Keeping that separation strict is what lets untrusted skill packs be
merely *unhelpful* rather than dangerous.

Tools are the only way anything happens, in any domain. Every tool carries
two flags the loop reads:

``destructive``   the call needs an affirmative confirmation callback
``requires``      capability names that must be present for the tool to load
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from .types import JSONSchema

#: A tool implementation receives the agent context plus validated kwargs.
ToolFn = Callable[..., str]


@dataclass
class ToolSpec:
    """A callable exposed to the model."""

    name: str
    description: str
    input_schema: JSONSchema
    fn: ToolFn
    destructive: bool = False
    requires: tuple[str, ...] = ()
    needs_context: bool = True
    #: True only for tools whose output this library authored. Everything
    #: else is fenced as untrusted data before the model sees it.
    trusted_output: bool = False

    def to_anthropic(self) -> dict[str, Any]:
        """Render as an Anthropic tool definition with strict validation on."""
        schema = dict(self.input_schema)
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        schema.setdefault("required", sorted(schema["properties"]))
        schema["additionalProperties"] = False
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": schema,
            "strict": True,
        }


@dataclass
class ToolRegistry:
    """An ordered, name-unique collection of tools."""

    tools: dict[str, ToolSpec] = field(default_factory=dict)

    def add(self, spec: ToolSpec) -> None:
        self.tools[spec.name] = spec

    def extend(self, specs: Iterable[ToolSpec]) -> None:
        for spec in specs:
            self.add(spec)

    def filter_by(self, capabilities: Iterable[str]) -> None:
        """Drop registered tools whose ``requires`` are not satisfied."""
        available = set(capabilities)
        for name, spec in list(self.tools.items()):
            if spec.requires and not available.issuperset(spec.requires):
                del self.tools[name]

    def remove(self, name: str) -> None:
        self.tools.pop(name, None)

    def get(self, name: str) -> ToolSpec | None:
        return self.tools.get(name)

    def names(self) -> list[str]:
        return sorted(self.tools)

    def to_anthropic(self) -> list[dict[str, Any]]:
        # Sorted so the rendered tool block is byte-stable across runs, which is
        # what makes the prompt cache actually hit.
        return [self.tools[name].to_anthropic() for name in sorted(self.tools)]

    def __len__(self) -> int:
        return len(self.tools)

    def __iter__(self) -> Iterator[ToolSpec]:
        return iter(self.tools.values())

    def __contains__(self, name: object) -> bool:
        return name in self.tools


def tool(
    name: str,
    description: str,
    schema: JSONSchema,
    *,
    destructive: bool = False,
    requires: Iterable[str] = (),
    trusted_output: bool = False,
) -> Callable[[ToolFn], ToolFn]:
    """Attach tool metadata to a function.

    The schema stays explicit rather than inferred from type hints: the
    description text a model reads is as much a part of the interface as the
    signature, and it deserves to be written by hand.
    """

    def decorator(fn: ToolFn) -> ToolFn:
        fn.__tool_spec__ = ToolSpec(  # type: ignore[attr-defined]
            name=name,
            description=description,
            input_schema=schema,
            fn=fn,
            destructive=destructive,
            requires=tuple(requires),
            trusted_output=trusted_output,
        )
        return fn

    return decorator


def collect(module: Any) -> list[ToolSpec]:
    """Return every decorated tool defined in ``module``."""
    specs: list[ToolSpec] = []
    for attr in vars(module).values():
        spec = getattr(attr, "__tool_spec__", None)
        if isinstance(spec, ToolSpec):
            specs.append(spec)
    return sorted(specs, key=lambda s: s.name)


def filter_specs(specs: Iterable[ToolSpec], capabilities: Iterable[str]) -> list[ToolSpec]:
    """Keep only tools whose ``requires`` are satisfied.

    Unsatisfied tools are left out entirely rather than registered-and-failing:
    an advertised tool that always errors wastes turns.
    """
    available = set(capabilities)
    return [s for s in specs if not s.requires or available.issuperset(s.requires)]


__all__ = ["ToolFn", "ToolRegistry", "ToolSpec", "collect", "filter_specs", "tool"]
