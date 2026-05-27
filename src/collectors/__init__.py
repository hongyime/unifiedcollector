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
}

ALL_COLLECTORS = list(COLLECTORS.values())


def get_collector(source: str):
    cls = COLLECTORS.get(source.lower())
    if not cls:
        raise ValueError(f"Unknown source: {source}. Available: {', '.join(COLLECTORS)}")
    return cls()


def list_sources() -> list[str]:
    return list(COLLECTORS.keys())
