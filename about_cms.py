"""Persisted content and Cloudinary asset helpers for the About page."""

import json
from pathlib import Path
from typing import Any

import cloudinary.uploader
import cloudinary.utils
from werkzeug.datastructures import FileStorage

from models import AboutPage, Setting, db


ABOUT_CONTENT_KEY = "about_page_content_v2"
ABOUT_ASSETS_KEY = "about_page_assets_v2"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_MB = MAX_IMAGE_BYTES // (1024 * 1024)
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}

ASSET_SPECS: dict[str, dict[str, str]] = {
    "story_image": {"label": "Our Story image", "guidance": "Best fit: 1254 × 1254 px", "ratio": "1:1"},
    "founder_portrait": {"label": "Founder portrait", "guidance": "Best fit: 800 × 1200 px PNG/WebP", "ratio": "2:3"},
    "hero_background": {"label": "Hero background", "guidance": "Best fit: 2048 × 768 px", "ratio": "8:3"},
    "founder_background": {"label": "Founder background", "guidance": "Best fit: 2048 × 768 px", "ratio": "8:3"},
    "purpose_background": {"label": "Purpose / CTA background", "guidance": "Best fit: 2048 × 768 px", "ratio": "8:3"},
}

LOCAL_ASSET_FILENAMES: dict[str, str] = {
    "hero_background": "1st_bg.png",
    "story_image": "A_journey.png",
    "founder_background": "2nd_bg.png",
    "founder_portrait": "founder_after_bg_remove.png",
    "purpose_background": "3rd_bg.png",
}


def _read_setting(key: str) -> dict[str, Any]:
    setting = Setting.query.filter_by(key=key).first()
    if not setting or not setting.value:
        return {}
    try:
        value = json.loads(setting.value)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _write_setting(key: str, value: dict[str, Any]) -> None:
    setting = Setting.query.filter_by(key=key).first()
    if setting is None:
        setting = Setting(key=key)
        db.session.add(setting)
    setting.value = json.dumps(value, ensure_ascii=False)


def default_content(about: AboutPage | None) -> dict[str, Any]:
    """Build the first editable CMS document from the legacy About record."""
    values: list[str] = []
    if about and about.values_items:
        try:
            loaded_values = json.loads(about.values_items)
            values = loaded_values if isinstance(loaded_values, list) else []
        except json.JSONDecodeError:
            values = []

    value_titles = [str(value) for value in values[:4]]
    fallback_titles = [
        "Thoughtful composition and lasting quality",
        "Honest details and a seamless customer experience",
        "Timeless design over passing trends",
        "A fragrance wardrobe that feels personal",
    ]
    value_descriptions = [
        "Every blend is selected for balance, depth, and a memorable finish.",
        "Clear details and considered service from discovery to delivery.",
        "Refined fragrances created to outlast passing trends.",
        "Build a personal scent wardrobe for every mood and moment.",
    ]
    value_icons = ["♧", "✦", "♡", "✧"]

    return {
        "seo_title": about.page_title if about and about.page_title else "About Zuhraan",
        "hero": {
            "eyebrow": "About Zuhraan",
            "heading": "Fragrance\nThat Feels Personal",
            "description": about.intro_text if about and about.intro_text else "",
            "button_label": "Our Story",
            "button_href": "#our-story",
        },
        "story": {
            "eyebrow": "Our Story",
            "heading": "A Journey From Simple Appreciation to Meaningful Creations",
            "description": about.story_content if about and about.story_content else "",
            "pillars": [
                {"icon": "✦", "label": "Inspired by\nreal moments"},
                {"icon": "✿", "label": "Premium\ningredients"},
                {"icon": "◷", "label": "Long-lasting\nformulas"},
                {"icon": "⌁", "label": "Fragrance\nfor every mood"},
            ],
        },
        "founder": {
            "eyebrow": "Meet Our Founder",
            "heading": "The Vision Behind Zuhraan",
            "description": "Driven by a deep passion for fragrances and a belief that scent is a powerful form of self-expression, our founder set out to create a brand that blends modern sophistication with timeless elegance.",
            "quote": "At Zuhraan, we do not just create perfumes, we create memories that stay with you.",
            "attribution": "Founder, Zuhraan",
            "script": "Crafting\nScents for\na Brighter\nYou",
            "portrait_alt": "Founder of Zuhraan",
        },
        "values": {
            "eyebrow": "Why Choose Zuhraan",
            "heading": "More Than Just a Fragrance",
            "description": about.commitment_content if about and about.commitment_content else "",
            "items": [
                {"icon": value_icons[index], "title": value_titles[index] if index < len(value_titles) else fallback_titles[index], "description": value_descriptions[index]}
                for index in range(4)
            ],
        },
        "purpose": {
            "eyebrow": "Our Purpose",
            "heading": about.legacy_heading if about and about.legacy_heading else "The Zuhraan Promise",
            "description": about.legacy_content if about and about.legacy_content else "",
            "button_label": "Explore Our Collection",
            "button_href": "/shop",
        },
    }


def _is_valid_content_document(content: dict[str, Any]) -> bool:
    required_sections = ("hero", "story", "founder", "values", "purpose")
    if not isinstance(content.get("seo_title"), str):
        return False
    if any(not isinstance(content.get(section), dict) for section in required_sections):
        return False
    return (
        isinstance(content["story"].get("pillars"), list)
        and len(content["story"]["pillars"]) == 4
        and isinstance(content["values"].get("items"), list)
        and len(content["values"]["items"]) == 4
    )


def get_content() -> dict[str, Any]:
    content = _read_setting(ABOUT_CONTENT_KEY)
    if _is_valid_content_document(content):
        return content
    content = default_content(AboutPage.query.first())
    _write_setting(ABOUT_CONTENT_KEY, content)
    db.session.commit()
    return content


def save_content(content: dict[str, Any]) -> None:
    _write_setting(ABOUT_CONTENT_KEY, content)
    db.session.commit()


def get_assets() -> dict[str, dict[str, str]]:
    return _read_setting(ABOUT_ASSETS_KEY)


def save_assets(assets: dict[str, dict[str, str]]) -> None:
    _write_setting(ABOUT_ASSETS_KEY, assets)
    db.session.commit()


def validate_image(file: FileStorage) -> None:
    filename = file.filename or ""
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("Upload a JPG, JPEG, PNG, or WebP image.")
    file.stream.seek(0, 2)
    size = file.stream.tell()
    file.stream.seek(0)
    if size == 0:
        raise ValueError("The selected image is empty.")
    if size > MAX_IMAGE_BYTES:
        raise ValueError("Images must be 10 MB or smaller.")


def upload_asset(slot: str, file: FileStorage) -> dict[str, str]:
    if slot not in ASSET_SPECS:
        raise ValueError("Unknown About-page image slot.")
    validate_image(file)
    result = cloudinary.uploader.upload(
        file,
        folder="zuhraan/about",
        resource_type="image",
        use_filename=True,
        unique_filename=True,
        overwrite=False,
    )
    public_id = result.get("public_id")
    if not public_id:
        raise RuntimeError("Cloudinary did not return an image public ID.")
    optimized_url, _ = cloudinary.utils.cloudinary_url(
        public_id,
        secure=True,
        fetch_format="auto",
        quality="auto:good",
    )
    return {"url": optimized_url, "public_id": public_id}


def delete_asset_from_cloudinary(public_id: str) -> None:
    if not public_id:
        return
    result = cloudinary.uploader.destroy(public_id, resource_type="image")
    if result.get("result") not in {"ok", "not found"}:
        raise RuntimeError("Cloudinary could not remove the image.")


def local_asset_path(slot: str) -> Path:
    if slot not in LOCAL_ASSET_FILENAMES:
        raise ValueError("Unknown About-page image slot.")
    return Path(__file__).parent / "static" / "images" / "About" / LOCAL_ASSET_FILENAMES[slot]
