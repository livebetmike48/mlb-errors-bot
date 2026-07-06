"""
Extracts video clip links from Baseball Savant, based on the mechanism used
by a real, working open-source tool (dylandru/BSav_Scraper_Vid and
JumpingKangaroo/baseball-savant-video-downloader): Savant's search results
page embeds relative "/sporty-videos/..." links directly in its HTML, which
can be regex-extracted without needing a headless browser.

This is NOT an official/documented API -- it's screen-scraping a real page,
confirmed working by an existing published tool, but it could break if
Savant changes their page structure. Treat failures here as expected
sometimes, not necessarily a bug.
"""
import re
import requests

SAVANT_BASE = "https://baseballsavant.mlb.com"

# Matches the exact pattern confirmed in the working reference tool:
# href="/sporty-videos/xxxxxxxx-xxxx-..." target="..."
SPORTY_LINK_PATTERN = re.compile(r'"(/sporty-videos/[^"]*)"\s*target')


def search_savant_player_date(player_id: int, game_date: str) -> str:
    """
    Fetches Savant's search results page for a specific player on a specific
    date. game_date format: YYYY-MM-DD.
    """
    params = {
        "hfGT": "R|",
        "player_type": "batter",
        "batters_lookup[]": str(player_id),
        "game_date_gt": game_date,
        "game_date_lt": game_date,
        "group_by": "name",
        "min_pitches": 0,
        "min_results": 0,
    }
    resp = requests.get(f"{SAVANT_BASE}/statcast_search", params=params, timeout=20)
    resp.raise_for_status()
    return resp.text


def extract_sporty_links(html: str) -> list[str]:
    """Returns the list of relative /sporty-videos/... paths found in the page."""
    return SPORTY_LINK_PATTERN.findall(html)


def get_video_urls_for_player_date(player_id: int, game_date: str) -> list[str]:
    """Convenience wrapper: full URLs ready to use, for a player on a given date."""
    html = search_savant_player_date(player_id, game_date)
    links = extract_sporty_links(html)
    return [f"{SAVANT_BASE}{link}" for link in links]
