from syncit.plugins.base import OfflinePlugin


class PluginRegistry:
    def __init__(self):
        self._plugins: dict[str, OfflinePlugin] = {}

    def register(self, plugin: OfflinePlugin):
        self._plugins[plugin.name] = plugin

    def get(self, name: str) -> OfflinePlugin:
        if name not in self._plugins:
            raise KeyError(f"Plugin {name} is not registered")
        return self._plugins[name]

    def list_plugins(self) -> list[str]:
        return list(self._plugins.keys())


# Global registry instance
registry = PluginRegistry()
