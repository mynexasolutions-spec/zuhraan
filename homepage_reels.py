"""Persistence helpers for the homepage reel slots."""

import json
from typing import TypedDict

from models import Setting, db


HOMEPAGE_REELS_KEY = "homepage_reels"
MAX_HOMEPAGE_REELS_KEY = "max_homepage_reels"
DEFAULT_MAX_HOMEPAGE_REELS = 5
REEL_TITLES = (
    "Signature Notes",
    "The Art of Oud",
    "Made to Linger",
    "A Lasting Impression",
    "Your Scent Story",
)


def _get_max_reels() -> int:
    """Read the max reel count from settings, defaulting to 5."""
    setting = Setting.query.filter_by(key=MAX_HOMEPAGE_REELS_KEY).first()
    if setting is None or not setting.value:
        return DEFAULT_MAX_HOMEPAGE_REELS
    try:
        value = int(setting.value)
        return max(1, min(value, 20))
    except (ValueError, TypeError):
        return DEFAULT_MAX_HOMEPAGE_REELS


def set_max_reels(count: int) -> None:
    """Persist the max reel count to settings."""
    count = max(1, min(int(count), 20))
    setting = Setting.query.filter_by(key=MAX_HOMEPAGE_REELS_KEY).first()
    if setting is None:
        setting = Setting(key=MAX_HOMEPAGE_REELS_KEY, value=str(count))
        db.session.add(setting)
    else:
        setting.value = str(count)


class HomepageReel(TypedDict):
    """Stored metadata for one homepage reel."""

    url: str
    public_id: str
    title: str


def default_homepage_reels(default_url: str) -> list[HomepageReel | None]:
    """Return the initial slots backed by the bundled reel video."""
    max_reels = _get_max_reels()
    return [
        {"url": default_url, "public_id": "", "title": REEL_TITLES[index % len(REEL_TITLES)]}
        for index in range(max_reels)
    ]


def get_homepage_reels(default_url: str) -> list[HomepageReel | None]:
    """Read and normalize the persisted reel slots."""
    max_reels = _get_max_reels()
    setting = Setting.query.filter_by(key=HOMEPAGE_REELS_KEY).first()
    if setting is None or not setting.value:
        return default_homepage_reels(default_url)

    try:
        stored_slots = json.loads(setting.value)
    except json.JSONDecodeError as error:
        raise ValueError('Stored homepage reels contain invalid JSON.') from error
    if not isinstance(stored_slots, list):
        raise ValueError('Stored homepage reels must be a JSON array.')

    normalized_slots: list[HomepageReel | None] = []
    for index in range(max_reels):
        stored_reel = stored_slots[index] if index < len(stored_slots) else None
        if not isinstance(stored_reel, dict) or not str(stored_reel.get("url") or "").strip():
            normalized_slots.append(None)
            continue
        stored_title = str(stored_reel.get("title") or "").strip()
        normalized_slots.append({
            "url": str(stored_reel["url"]).strip(),
            "public_id": str(stored_reel.get("public_id") or "").strip(),
            "title": stored_title or REEL_TITLES[index % len(REEL_TITLES)],
        })
    return normalized_slots


def save_homepage_reels(reels: list[HomepageReel | None]) -> None:
    """Stage reel slots in the existing Supabase-backed setting table."""
    max_reels = _get_max_reels()
    if len(reels) != max_reels:
        raise ValueError(f"Homepage reels must contain exactly {max_reels} slots.")
    setting = Setting.query.filter_by(key=HOMEPAGE_REELS_KEY).first()
    if setting is None:
        setting = Setting(key=HOMEPAGE_REELS_KEY)
        db.session.add(setting)
    setting.value = json.dumps(reels, ensure_ascii=False, separators=(",", ":"))
