from ..services import SystemStats
from .backups import BackupStatus
from .bluetooth import BluetoothService
from .brightness import Brightness
from .calendar import CalendarService
from .clipboard import ClipboardStore
from .deck import DeckService
from .media import MediaService
from .network import NetworkService
from .nextcloud_account import NextcloudAccount
from .notifications import DoNotDisturb, NotificationCenter
from .nextcloud import NextcloudSync
from .tailscale import Tailscale
from .vpn import ProtonVpn


class Services:
    """Every backend the bars use, created once and shared across monitors."""

    def __init__(self):
        self.stats = SystemStats()
        self.network = NetworkService()
        self.vpn = ProtonVpn(self.network)
        self.bluetooth = BluetoothService()
        self.dnd = DoNotDisturb()
        self.notifications = NotificationCenter(self.dnd)
        self.media = MediaService()
        self.tailscale = Tailscale()
        self.backups = BackupStatus()
        self.brightness = Brightness()
        self.nextcloud = NextcloudSync()
        self.clipboard = ClipboardStore()
        self.nextcloud_account = NextcloudAccount()
        self.calendar = CalendarService(self.nextcloud_account)
        self.deck = DeckService(self.nextcloud_account)

    @property
    def audio(self):
        return self.stats.audio
