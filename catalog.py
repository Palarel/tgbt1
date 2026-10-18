from pathlib import Path

from database import Database, MediaItem


PROJECT_DIR = Path(__file__).resolve().parent
MEDIA_DIR = PROJECT_DIR / "ggfiles"
SUPPORTED_EXTENSIONS = {
    ".jpg": ("photo", 10),
    ".jpeg": ("photo", 10),
    ".png": ("photo", 10),
    ".mp4": ("video", 20),
    ".mov": ("video", 20),
}


def scan_media() -> list[MediaItem]:
    """Index supported files recursively, including nested media folders."""
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    items: list[MediaItem] = []
    for file_path in sorted(MEDIA_DIR.rglob("*")):
        if not file_path.is_file():
            continue
        media_info = SUPPORTED_EXTENSIONS.get(file_path.suffix.lower())
        if media_info is None:
            continue
        media_type, price = media_info
        relative_path = file_path.relative_to(MEDIA_DIR).as_posix()
        items.append(
            MediaItem(
                id=Database.media_id_for(media_type, relative_path),
                file_path=str(file_path.resolve()),
                media_type=media_type,
                price=price,
            )
        )
    return items
