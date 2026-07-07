from swarmer.agent_tools import AgentToolStrategy

_REGISTRY: dict[str, AgentToolStrategy] = {}


def register(strategy: AgentToolStrategy) -> None:
    _REGISTRY[strategy.name] = strategy


_ALIASES: dict[str, str] = {
    "opencode-golang": "opencode",
    "opencode-python": "opencode",
}


def get(name: str) -> AgentToolStrategy:
    name = _ALIASES.get(name, name)
    if name not in _REGISTRY:
        raise ValueError(
            f"Unknown agent tool: {name!r}. Available: {list(_REGISTRY)}"
        )
    return _REGISTRY[name]


def all_tools() -> list[AgentToolStrategy]:
    return list(_REGISTRY.values())


def _init() -> None:
    from swarmer.agent_tools.opencode import OpenCodeStrategy  # noqa: F811
    register(OpenCodeStrategy())


_init()
