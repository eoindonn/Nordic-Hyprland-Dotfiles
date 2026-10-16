from fabric.system_tray.widgets import SystemTray

# StatusNotifierItem Ids of apps the bar already shows in its own way
# (Nextcloud: sync status comes from system/nextcloud.py)
HIDDEN_ITEMS = {"Nextcloud"}


class FilteredSystemTray(SystemTray):
    """Fabric's tray, minus the items in HIDDEN_ITEMS."""

    def on_item_added(self, watcher, item_identifier: str):
        item = self._watcher.items.get(item_identifier)
        if item is not None and item.id in HIDDEN_ITEMS:
            return
        return super().on_item_added(watcher, item_identifier)
