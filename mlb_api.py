"""
Thin client for the free public MLB Stats API. No key required.
"""
import requests
from datetime import datetime

BASE = "https://statsapi.mlb.com/api/v1"
BASE_V1_1 = "https://statsapi.mlb.com/api/v1.1"


def get_live_games(date_str: str) -> list[dict]:
    """Lightweight schedule call to find which games are currently Live."""
    resp = requests.get(
        f"{BASE}/schedule",
        params={"sportId": 1, "date": date_str},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    games = []
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            games.append({
                "game_pk": g["gamePk"],
                "status": g["status"]["detailedState"],
                "abstract_state": g["status"].get("abstractGameState"),
                "home_team": g["teams"]["home"]["team"]["name"],
                "away_team": g["teams"]["away"]["team"]["name"],
            })
    return games


def get_live_feed(game_pk: int) -> dict:
    """Full play-by-play feed for a game -- this is where error/review details live."""
    resp = requests.get(f"{BASE_V1_1}/game/{game_pk}/feed/live", timeout=15)
    resp.raise_for_status()
    return resp.json()


def extract_events(feed_json: dict) -> list[dict]:
    """
    Scans every play in the live feed for two things:
      - a play whose result involved a fielding/throwing error
      - a play under active replay review, or one whose review just concluded

    Returns a list of event dicts, each tagged with a stable `play_id`
    (atBatIndex) so the caller can dedupe against previously-alerted events.

    NOTE: field names for review data (`reviewDetails`, `inProgress`,
    `isOverturned`) are based on the MLB Stats API's documented structure.
    I couldn't hit the live API from this sandbox to verify against a real
    in-progress review, so if a review event doesn't fire correctly against
    a real game, the fix is almost certainly just adjusting these key names.
    """
    events = []
    all_plays = feed_json.get("liveData", {}).get("plays", {}).get("allPlays", [])

    for play in all_plays:
        about = play.get("about", {}) or {}
        play_id = about.get("atBatIndex")
        inning = about.get("inning")
        half = "Top" if about.get("isTopInning") else "Bottom"

        result = play.get("result", {}) or {}
        event_name = (result.get("event") or "")
        description = result.get("description") or ""

        # --- Error detection ---
        if "error" in event_name.lower() or " error " in description.lower():
            events.append({
                "type": "error",
                "play_id": play_id,
                "inning": inning,
                "half": half,
                "description": description,
                "end_time": about.get("endTime"),
            })

        # --- Replay review detection ---
        review = play.get("reviewDetails") or {}
        if review:
            in_progress = review.get("inProgress")
            review_type = review.get("reviewType", "Play")

            if in_progress:
                events.append({
                    "type": "review_pending",
                    "play_id": play_id,
                    "inning": inning,
                    "half": half,
                    "description": description,
                    "review_type": review_type,
                })
            elif review.get("isOverturned") is not None:
                events.append({
                    "type": "review_result",
                    "play_id": play_id,
                    "inning": inning,
                    "half": half,
                    "description": description,
                    "overturned": review.get("isOverturned"),
                    "review_type": review_type,
                })

    return events


def get_game_content(game_pk: int) -> dict:
    """Highlight clips ('Film Room') live here, separate from the play-by-play feed."""
    resp = requests.get(f"{BASE}/game/{game_pk}/content", timeout=15)
    resp.raise_for_status()
    return resp.json()


def find_highlight_for_play(content_json: dict, play_description: str, play_end_time: str | None,
                             window_minutes: int = 6) -> dict | None:
    """
    Best-effort match of a highlight clip to a specific play.

    IMPORTANT CAVEAT: MLB's content API doesn't cleanly expose a documented
    "this clip belongs to atBatIndex N" field in the public docs, so this
    uses a heuristic instead: it looks for clips published close in time to
    the play, whose headline/blurb text overlaps with words from the play's
    description (player names, "error", etc). This works well in practice
    for distinctive plays but isn't guaranteed. I couldn't verify this
    against a live game from this sandbox -- if matches come back wrong or
    missing, send me a game_pk while a real error highlight is up and I can
    tune the matching logic against real content-endpoint data.
    """
    items = ((content_json.get("highlights") or {}).get("live") or {}).get("items") or []
    if not items:
        return None

    play_dt = None
    if play_end_time:
        try:
            play_dt = datetime.fromisoformat(play_end_time.replace("Z", "+00:00"))
        except Exception:
            play_dt = None

    desc_words = {w.strip(".,").lower() for w in play_description.split() if len(w) > 3}

    candidates = []
    for item in items:
        text = f"{item.get('headline', '')} {item.get('blurb', '')}".lower()
        overlap = sum(1 for w in desc_words if w in text)
        if overlap < 2:
            continue

        item_date = item.get("date")
        if play_dt and item_date:
            try:
                item_dt = datetime.fromisoformat(item_date.replace("Z", "+00:00"))
                if abs((item_dt - play_dt).total_seconds()) > window_minutes * 60:
                    continue
            except Exception:
                pass  # don't over-filter on a parse failure

        playbacks = item.get("playbacks") or []
        video_url = None
        by_name = {pb.get("name"): pb.get("url") for pb in playbacks}
        for preferred in ("mp4Avc", "highBit", "FLASH_2500K_960X540"):
            if by_name.get(preferred):
                video_url = by_name[preferred]
                break
        if not video_url and playbacks:
            video_url = playbacks[0].get("url")

        if video_url:
            candidates.append({
                "headline": item.get("headline") or "Highlight",
                "video_url": video_url,
                "overlap": overlap,
            })

    if not candidates:
        return None
    candidates.sort(key=lambda c: -c["overlap"])
    return candidates[0]
