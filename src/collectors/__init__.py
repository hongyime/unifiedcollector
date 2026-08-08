from .github import GithubCollector
from .website import WebsiteCollector
from .instagram import InstagramCollector
from .telegram import TelegramCollector
from .tiktok import TiktokCollector
from .youtube import YoutubeCollector
from .lemon8 import Lemon8Collector
from .strava import StravaCollector
from .whatsapp import WhatsappCollector
from .search import SearchCollector
from .beeper import BeeperCollector
from .instagram_dm import InstagramDmCollector

COLLECTORS = {
    "github": GithubCollector,
    "website": WebsiteCollector,
    "instagram": InstagramCollector,
    "telegram": TelegramCollector,
    "tiktok": TiktokCollector,
    "youtube": YoutubeCollector,
    "lemon8": Lemon8Collector,
    "strava": StravaCollector,
    "whatsapp": WhatsappCollector,
    "search": SearchCollector,
    "beeper": BeeperCollector,
    "instagram_dm": InstagramDmCollector,
}

ALL_COLLECTORS = list(COLLECTORS.values())


def get_collector(source: str):
    cls = COLLECTORS.get(source.lower())
    if not cls:
        raise ValueError(f"Unknown source: {source}. Available: {', '.join(COLLECTORS)}")
    return cls()


def list_sources() -> list[str]:
    """Active collector sources, minus any in COLLECTOR_DISABLED_SOURCES.

    COLLECTOR_DISABLED_SOURCES is a comma-separated kill-switch (operational
    control) for taking a misbehaving collector out of the shared worker without
    a code change — e.g. youtube, whose yt-dlp subprocess can wedge on lost
    SIGCHLD in WSL2/Docker and freeze the shared event loop.
    """
    import os
    disabled = {
        s.strip().lower()
        for s in os.getenv("COLLECTOR_DISABLED_SOURCES", "").split(",")
        if s.strip()
    }
    return [s for s in COLLECTORS.keys() if s.lower() not in disabled]
