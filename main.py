import csv
import logging
import os
import sys
from dotenv import load_dotenv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import spotipy
from spotipy.oauth2 import SpotifyOAuth

logger = logging.getLogger(__name__)

SPOTIFY_SCOPE = (
    "playlist-modify-public "
    "user-read-private "
    "user-read-email "
    "user-top-read "
    "user-follow-read "
    "user-follow-modify"
)
TOP_ARTIST_LIMIT = 10
GROUP_TYPES = ("album", "single", "appears_on")


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    redirect_uri: str
    playlist_name: str = "HAWT NU MUZIC"
    new_music_days_threshold: int = 7
    output_dir: Path = Path("output")


def load_settings() -> Settings:
    """Load required runtime configuration from the .env file"""
    load_dotenv()

    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
    redirect_uri = os.environ.get("SPOTIFY_REDIRECT_URI", "").strip()

    missing = [
        name
        for name, value in {
            "SPOTIFY_CLIENT_ID": client_id,
            "SPOTIFY_CLIENT_SECRET": client_secret,
            "SPOTIFY_REDIRECT_URI": redirect_uri,
        }.items()
        if not value
    ]
    if missing:
        missing_vars = ", ".join(missing)
        raise ValueError(f"Missing required environment variables: {missing_vars}")

    playlist_name = os.environ.get("SPOTIFY_PLAYLIST_NAME", "HAWT NU MUZIC").strip() or "HAWT NU MUZIC"
    threshold_raw = os.environ.get("NEW_MUSIC_DAYS_THRESHOLD", "7").strip()
    try:
        threshold = int(threshold_raw)
    except ValueError as exc:
        raise ValueError("NEW_MUSIC_DAYS_THRESHOLD must be an integer") from exc

    return Settings(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        playlist_name=playlist_name,
        new_music_days_threshold=threshold,
    )


def configure_logging() -> None:
    """Configure readable console logging and reduce third-party noise."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logging.getLogger("spotipy").setLevel(logging.WARNING)


def authorize(settings: Settings) -> spotipy.Spotify:
    return spotipy.Spotify(
        auth_manager=SpotifyOAuth(
            client_id=settings.client_id,
            client_secret=settings.client_secret,
            redirect_uri=settings.redirect_uri,
            scope=SPOTIFY_SCOPE,
        )
    )


def get_recent_top_artists(sp: spotipy.Spotify) -> list[dict[str, str]]:
    logger.info("Retrieving top artists for the last 4 weeks")
    top_artists_raw = sp.current_user_top_artists(
        limit=TOP_ARTIST_LIMIT,
        offset=0,
        time_range="short_term",
    )
    return [
        {"name": artist["name"], "id": artist["id"]}
        for artist in top_artists_raw.get("items", [])
        if artist.get("id")
    ]


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Writing CSV: %s", path)
    with path.open("w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def read_csv_column(path: Path, column_name: str) -> list[str]:
    values: list[str] = []
    with path.open("r", newline="", encoding="utf-8") as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            value = (row.get(column_name) or "").strip()
            if value:
                values.append(value)
    return values


def list_artists_following(sp: spotipy.Spotify) -> list[dict[str, str]]:
    logger.info("Retrieving followed artists")
    followed_artists: list[dict[str, str]] = []
    cursor: Optional[str] = None

    while True:
        results = sp.current_user_followed_artists(limit=50, after=cursor)
        artists_batch = results.get("artists", {}).get("items", [])
        if not artists_batch:
            break

        for artist in artists_batch:
            artist_id = artist.get("id")
            if artist_id:
                followed_artists.append({"name": artist.get("name", ""), "id": artist_id})

        cursor = artists_batch[-1].get("id")
        if not cursor:
            break

    logger.info("User follows %d artists", len(followed_artists))
    return followed_artists


def parse_spotify_release_date(release_date: str, precision: str) -> date:
    """Parse Spotify release_date according to release_date_precision."""
    if precision == "day":
        return datetime.strptime(release_date, "%Y-%m-%d").date()
    if precision == "month":
        return datetime.strptime(f"{release_date}-01", "%Y-%m-%d").date()
    if precision == "year":
        return datetime.strptime(f"{release_date}-01-01", "%Y-%m-%d").date()
    raise ValueError(f"Unsupported release date precision: {precision}")


def is_recent_release(release_date: str, precision: str, threshold_days: int) -> bool:
    try:
        release = parse_spotify_release_date(release_date, precision)
    except ValueError:
        logger.warning("Skipping album with unparseable date: %s (%s)", release_date, precision)
        return False

    days_difference = (datetime.now().date() - release).days
    return 0 <= days_difference <= threshold_days


def get_latest_artist_release(
    sp: spotipy.Spotify,
    artist_id: str,
    group: str,
    threshold_days: int,
) -> Optional[dict[str, Any]]:
    album_batch = sp.artist_albums(artist_id, include_groups=group, limit=1)
    items = album_batch.get("items", [])
    if not items:
        return None

    album = items[0]
    release_date = album.get("release_date", "")
    precision = album.get("release_date_precision", "day")
    if is_recent_release(release_date, precision, threshold_days):
        logger.info(
            "New %s: %s by %s (%s)",
            group,
            album.get("name", "Unknown"),
            album.get("artists", [{}])[0].get("name", "Unknown"),
            release_date,
        )
        return album
    return None


def add_new_music_to_playlist(
    sp: spotipy.Spotify,
    playlist_id: str,
    followed_artist_ids: list[str],
    threshold_days: int,
) -> int:
    unique_track_ids: set[str] = set()

    for artist_id in followed_artist_ids:
        for group_type in GROUP_TYPES:
            latest_release = get_latest_artist_release(
                sp=sp,
                artist_id=artist_id,
                group=group_type,
                threshold_days=threshold_days,
            )
            if not latest_release:
                continue

            album_tracks = sp.album_tracks(latest_release["id"])
            for track in album_tracks.get("items", []):
                track_id = track.get("id")
                if track_id:
                    unique_track_ids.add(track_id)

    if not unique_track_ids:
        logger.info("No new tracks found to add")
        return 0

    # Spotify API supports adding up to 100 items per request.
    track_ids = list(unique_track_ids)
    for index in range(0, len(track_ids), 100):
        sp.playlist_add_items(playlist_id, track_ids[index : index + 100])

    return len(track_ids)


def main() -> None:
    ## Settings & Auth
    configure_logging()

    try:
        settings = load_settings()
    except ValueError as exc:
        logger.error("Configuration error: %s", exc)
        raise SystemExit(1) from exc

    sp = authorize(settings)
    current_date = date.today().isoformat()

    ## Top Artists
    top_artists_file = settings.output_dir / f"top_artists_{current_date}.csv"
    if not top_artists_file.exists():
        top_artists = get_recent_top_artists(sp)
        write_csv(top_artists_file, ["name", "id"], top_artists)
    else:
        logger.info("Using existing file: %s", top_artists_file)

    ## Follow
    top_artist_ids = read_csv_column(top_artists_file, "id")
    if top_artist_ids:
        sp.user_follow_artists(top_artist_ids)
        logger.info("Followed/ensured follow for %d top artists", len(top_artist_ids))

    following_artists_file = settings.output_dir / f"following_artists_{current_date}.csv"
    if not following_artists_file.exists():
        following_artists = list_artists_following(sp)
        write_csv(following_artists_file, ["name", "id"], following_artists)
    else:
        logger.info("Using existing file: %s", following_artists_file)

    ## Playlist
    followed_artist_ids = read_csv_column(following_artists_file, "id")
    playlist = sp.current_user_playlist_create(
        name=f"{settings.playlist_name} [{current_date}]",
        description="Created using SpotifyNewMusic.py script",
    )

    added_count = add_new_music_to_playlist(
        sp=sp,
        playlist_id=playlist["id"],
        followed_artist_ids=followed_artist_ids,
        threshold_days=settings.new_music_days_threshold,
    )
    logger.info("Added %d tracks to playlist %s", added_count, playlist["name"])


if __name__ == "__main__":
    main()