import os
import re
import logging
import asyncio
from datetime import datetime, timedelta, timezone

import requests
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import mlb_api
import storage

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))
VIDEO_POLL_SECONDS = int(os.getenv("VIDEO_POLL_SECONDS", "60"))
VIDEO_MAX_ATTEMPTS = int(os.getenv("VIDEO_MAX_ATTEMPTS", "10"))  # ~10 min window at 60s poll
MAX_SEND_FAILURES = int(os.getenv("MAX_SEND_FAILURES", "5"))  # retries before giving up on an event

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("errors_bot")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ---- nonce dedup detection (July 18 outage armor, corrected) ----
# LESSON LEARNED: enforce_nonce is a PYCORD (fork) parameter -- it does
# not exist in discord.py at any version. Passing it made EVERY send
# raise TypeError, which silently killed all alerts for 6 hours on July
# 17 (events were marked-before-send, so failures were swallowed).
# The real discord.py behavior: since v2.5 the library AUTOMATICALLY
# enforces nonces on message creation ("Enforce and create random nonces
# when creating messages throughout the library" -- v2.5 changelog), so
# server-side duplicate protection needs no kwargs at all. We pass our
# own deterministic nonce so a crash-between-send-and-mark retry gets
# deduped by Discord too.
HAS_SERVER_NONCE_DEDUP = discord.version_info >= (2, 5)

SAVANT_CLIP_PAGE = "https://baseballsavant.mlb.com/sporty-videos?playId={play_id}"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
# Direct video-file URL embedded in the sporty-videos page HTML, e.g.
# https://sporty-clips.mlb.com/XXXX.mp4 -- the same pattern the working
# open-source Savant scrapers extract. Posting the .mp4 directly gives
# Discord a file it can always unfurl into an inline player.
SPORTY_CLIP_MP4_RE = re.compile(r'https://sporty-clips\.mlb\.com/[^"\'\s>]+\.mp4')


def et_date_str(offset_days: int = 0) -> str:
    et = datetime.now(timezone.utc) - timedelta(hours=4)
    et += timedelta(days=offset_days)
    return et.strftime("%Y-%m-%d")


TEAM_ABBR = {
    "Arizona Diamondbacks": "ARI", "Atlanta Braves": "ATL", "Baltimore Orioles": "BAL",
    "Boston Red Sox": "BOS", "Chicago Cubs": "CHC", "Chicago White Sox": "CWS",
    "Cincinnati Reds": "CIN", "Cleveland Guardians": "CLE", "Colorado Rockies": "COL",
    "Detroit Tigers": "DET", "Houston Astros": "HOU", "Kansas City Royals": "KC",
    "Los Angeles Angels": "LAA", "Los Angeles Dodgers": "LAD", "Miami Marlins": "MIA",
    "Milwaukee Brewers": "MIL", "Minnesota Twins": "MIN", "New York Mets": "NYM",
    "New York Yankees": "NYY", "Athletics": "ATH", "Oakland Athletics": "ATH",
    "Philadelphia Phillies": "PHI", "Pittsburgh Pirates": "PIT", "San Diego Padres": "SD",
    "San Francisco Giants": "SF", "Seattle Mariners": "SEA", "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TB", "Texas Rangers": "TEX", "Toronto Blue Jays": "TOR",
    "Washington Nationals": "WSH",
}


def team_abbr(name: str) -> str:
    return TEAM_ABBR.get(name, name[:3].upper())


def build_embed(game: dict, event: dict) -> discord.Embed:
    if event["type"] == "ruling_pending":
        title = "📋 Scorer ruling pending"
        if event.get("batter"):
            title += f" — {event['batter']}"
        color = discord.Color.light_grey()
    else:
        title = "⚠️ Error"
        if event.get("fielder_name"):
            pos = f"{event['fielder_position']} " if event.get("fielder_position") else ""
            title = f"⚠️ Error — {pos}{event['fielder_name']}"
        color = discord.Color.gold()

    embed = discord.Embed(title=title[:256], color=color)
    embed.add_field(name="Description", value=f"```{event['description'][:1000]}```", inline=False)
    embed.add_field(
        name="Game",
        value=f"{team_abbr(game['away_team'])} @ {team_abbr(game['home_team'])}",
        inline=False,
    )
    embed.add_field(name="Inning", value=f"{event['half']} {event['inning']}", inline=False)
    if event.get("batter"):
        embed.add_field(name="Batter", value=event["batter"], inline=False)
    embed.add_field(
        name="Run scored",
        value="✅ Yes" if event.get("run_scored") else "❌ No",
        inline=False,
    )
    if event.get("exit_velocity") is not None:
        embed.add_field(name="Exit velocity", value=f"{event['exit_velocity']:.1f} mph", inline=False)
    if event.get("launch_angle") is not None:
        embed.add_field(name="Launch angle", value=f"{event['launch_angle']:.1f}°", inline=False)
    if event.get("hit_distance") is not None:
        embed.add_field(name="Hit distance", value=f"{event['hit_distance']:.0f} ft", inline=False)

    embed.set_footer(text="MLB Error Bot • statsapi.mlb.com")

    if event.get("end_time"):
        try:
            embed.timestamp = datetime.fromisoformat(event["end_time"].replace("Z", "+00:00"))
        except Exception:
            embed.timestamp = datetime.now(timezone.utc)
    else:
        embed.timestamp = datetime.now(timezone.utc)

    return embed


# ---------------- Savant per-play clips ----------------
# Every pitch/play has a Statcast playId; Savant serves that play's
# broadcast clip at sporty-videos?playId=... -- ALL plays, not just the
# hand-picked editorial highlights the old lookup searched. The page gets
# its video ~1-5 minutes after the play; Discord unfurls it into an
# inline player when the URL is posted as a plain message.


def _last_play_uuid(play: dict) -> str | None:
    for pe in reversed(play.get("playEvents") or []):
        pid = pe.get("playId")
        if pid and UUID_RE.match(str(pid)):
            return str(pid)
    return None


def _resolve_play_uuid(game_pk: int, play_id, description: str,
                       end_time: str | None = None,
                       batter: str | None = None) -> str | None:
    """The stored play_id may already be the Statcast UUID -- or (per
    extract_events) the play's atBatIndex. If it isn't UUID-shaped,
    refetch the live feed and find the play to get the real one.

    Matching order (July 18 fix -- clips were being missed):
      0. atBatIndex direct lookup, VERIFIED by endTime or batter name --
         the feed can re-issue a play under a new atBatIndex after an
         internal reprocess (the reason storage has content-hash dedup),
         so an index hit is only trusted when a second field agrees
      1. about.endTime -- stable play key, survives description edits
      2. exact description
      3. unique first-sentence prefix -- official scorers AMEND play
         descriptions after the fact (seen live: dedup logging showed
         by_content=False on an already-alerted play), which made the
         old exact-only match fail all 10 attempts while the clip sat
         ready on Savant the whole time."""
    if play_id and UUID_RE.match(str(play_id)):
        return str(play_id)
    try:
        feed = mlb_api.get_live_feed(game_pk)
    except Exception as e:
        log.error("UUID resolve: feed fetch failed for %s: %s", game_pk, e)
        return None
    plays = (((feed.get("liveData") or {}).get("plays") or {}).get("allPlays")) or []

    # 0) atBatIndex: direct key from extract_events, trusted only when a
    #    second field confirms the play wasn't reindexed underneath us
    try:
        idx = int(play_id)
    except (TypeError, ValueError):
        idx = None
    if idx is not None:
        for play in plays:
            if ((play.get("about") or {}).get("atBatIndex")) != idx:
                continue
            confirmed_by = None
            if end_time and ((play.get("about") or {}).get("endTime")) == end_time:
                confirmed_by = "endTime"
            elif batter and (((play.get("matchup") or {}).get("batter") or {}).get("fullName")) == batter:
                confirmed_by = "batter"
            if confirmed_by:
                uuid = _last_play_uuid(play)
                if uuid:
                    log.info("Resolved play UUID via atBatIndex (confirmed by %s) for game %s",
                             confirmed_by, game_pk)
                    return uuid
            else:
                log.warning("atBatIndex %s matched a play but verification failed for game %s "
                            "(feed reindexed?) -- trying other strategies", idx, game_pk)
            break

    # 1) endTime: same-source stable identifier
    if end_time:
        for play in plays:
            if ((play.get("about") or {}).get("endTime")) == end_time:
                uuid = _last_play_uuid(play)
                if uuid:
                    log.info("Resolved play UUID via endTime for game %s", game_pk)
                    return uuid

    want = (description or "").strip().lower()
    if not want:
        return None

    # 2) exact description
    for play in plays:
        desc = (((play.get("result") or {}).get("description")) or "").strip().lower()
        if desc == want:
            uuid = _last_play_uuid(play)
            if uuid:
                log.info("Resolved play UUID via exact description for game %s", game_pk)
                return uuid

    # 3) first-sentence prefix, only if it identifies exactly ONE play
    first_sentence = want.split(".")[0].strip()
    if len(first_sentence) >= 20:
        matches = []
        for play in plays:
            desc = (((play.get("result") or {}).get("description")) or "").strip().lower()
            if desc.startswith(first_sentence):
                matches.append(play)
        if len(matches) == 1:
            uuid = _last_play_uuid(matches[0])
            if uuid:
                log.info("Resolved play UUID via first-sentence prefix for game %s "
                         "(description was amended by scorer)", game_pk)
                return uuid
        elif len(matches) > 1:
            log.warning("Prefix matched %d plays for game %s -- refusing ambiguous pick",
                        len(matches), game_pk)
    return None


def _savant_clip_ready(play_uuid: str) -> str | None:
    """Returns a postable clip URL once MLB has processed the play's video;
    None while it's still cooking.

    July 18 rework: extract the DIRECT sporty-clips .mp4 URL from the page
    HTML (the same mechanism the working open-source Savant scrapers use)
    and post that, instead of posting the page URL and hoping Discord's
    unfurler picks the video up. The direct .mp4 always unfurls into an
    inline player. If the page shows signs of the clip existing but the
    .mp4 regex doesn't hit (markup change), fall back to the page URL so
    behavior degrades to exactly what it was before."""
    url = SAVANT_CLIP_PAGE.format(play_id=play_uuid)
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return None
        m = SPORTY_CLIP_MP4_RE.search(resp.text)
        if m:
            return m.group(0)
        if "sporty-clips" in resp.text:
            # Clip exists but URL pattern didn't match -- degrade to the
            # old behavior (post the page link) rather than miss the clip.
            log.warning("Clip exists for %s but .mp4 URL not extracted -- posting page URL", play_uuid)
            return url
    except Exception as e:
        log.warning("Savant clip check failed for %s: %s", play_uuid, e)
    return None


async def _send_alert(channel, embed: discord.Embed, nonce: str) -> discord.Message:
    """Plain send with a deterministic nonce. On discord.py >= 2.5 the
    library automatically enforces nonces server-side, so duplicate
    protection comes free -- never pass enforce_nonce, it is a Pycord
    parameter that does not exist here and raises TypeError."""
    return await channel.send(embed=embed, nonce=nonce)


# Per-event send-failure counts, in memory. If a send keeps failing (e.g.
# missing permissions), give up after MAX_SEND_FAILURES instead of
# retrying every poll forever. Restart clears it -- acceptable, since a
# restart is exactly when a stuck event deserves one more shot.
_send_failures: dict[tuple, int] = {}


@tasks.loop(seconds=POLL_SECONDS)
async def poll_games():
    channel_id = storage.get_config("announce_channel_id")
    if not channel_id:
        return

    channel = bot.get_channel(int(channel_id))
    if channel is None:
        log.warning("Configured channel %s not found/visible to bot", channel_id)
        return

    date_str = et_date_str(0)
    try:
        games = mlb_api.get_live_games(date_str)
    except Exception as e:
        log.error("Failed to fetch schedule: %s", e)
        return

    live_games = [g for g in games if g["abstract_state"] == "Live"]

    for game in live_games:
        try:
            feed = mlb_api.get_live_feed(game["game_pk"])
            events = mlb_api.extract_events(feed)
        except Exception as e:
            log.error("Failed to fetch/parse feed for game %s: %s", game["game_pk"], e)
            continue

        for event in events:
            already_by_id = storage.already_alerted(game["game_pk"], event["play_id"], event["type"])
            already_by_content = storage.already_alerted_by_content(game["game_pk"], event["description"])
            if already_by_id or already_by_content:
                log.info(
                    "Skipping already-alerted event: game=%s play_id=%s type=%s by_id=%s by_content=%s",
                    game["game_pk"], event["play_id"], event["type"], already_by_id, already_by_content,
                )
                continue

            nonce = f"err-{game['game_pk']}-{str(event['play_id'])[:12]}"[:25]

            if not HAS_SERVER_NONCE_DEDUP:
                # Degraded mode (discord.py < 2.5): keep the original
                # mark-BEFORE-send ordering, because without automatic
                # server-side nonce dedup a mark-after retry could
                # double-post on an HTTP-retry duplicate (the original
                # July bug).
                storage.mark_alerted(game["game_pk"], event["play_id"], event["type"])
                storage.mark_alerted_by_content(game["game_pk"], event["description"])
                log.info(
                    "Marking alerted BEFORE send (degraded, discord.py<2.5): game=%s play_id=%s type=%s",
                    game["game_pk"], event["play_id"], event["type"],
                )

            try:
                sent_message = await _send_alert(channel, build_embed(game, event), nonce)
            except Exception as e:
                key = (game["game_pk"], str(event["play_id"]), event["type"])
                _send_failures[key] = _send_failures.get(key, 0) + 1
                log.error(
                    "Failed to send alert for game %s (attempt %d/%d): %s",
                    game["game_pk"], _send_failures[key], MAX_SEND_FAILURES, e,
                )
                if HAS_SERVER_NONCE_DEDUP and _send_failures[key] >= MAX_SEND_FAILURES:
                    # Stop retrying a permanently-broken send, but say so
                    # LOUDLY -- this event will never post.
                    storage.mark_alerted(game["game_pk"], event["play_id"], event["type"])
                    storage.mark_alerted_by_content(game["game_pk"], event["description"])
                    log.error(
                        "GIVING UP on event after %d failed sends: game=%s play_id=%s type=%s",
                        MAX_SEND_FAILURES, game["game_pk"], event["play_id"], event["type"],
                    )
                # Not marked (normal mode, under the cap) -> event retries
                # next poll. Degraded mode keeps old swallow behavior.
                continue

            # Send SUCCEEDED -- mark now (normal mode). Discord's
            # enforce_nonce dedup covers the gap if we crash between send
            # and mark: the re-send next poll reuses the same nonce and
            # gets deduped server-side.
            if HAS_SERVER_NONCE_DEDUP:
                storage.mark_alerted(game["game_pk"], event["play_id"], event["type"])
                storage.mark_alerted_by_content(game["game_pk"], event["description"])
            _send_failures.pop((game["game_pk"], str(event["play_id"]), event["type"]), None)
            log.info("Alerted %s in game %s", event["type"], game["game_pk"])

            # Only errors get a video-clip follow-up for now.
            if event["type"] == "error":
                try:
                    storage.add_pending_video_lookup(
                        game["game_pk"], event["play_id"], sent_message.id, channel.id,
                        event["description"], event.get("end_time"), event.get("batter"),
                    )
                except Exception as e:
                    log.error("Failed to queue video lookup for game %s: %s", game["game_pk"], e)


# Rows where the UUID resolved at least once but the clip never processed;
# lets the give-up note distinguish "couldn't identify the play" (resolver
# problem) from "clip never appeared" (broadcast/processing problem).
_uuid_resolved_rows: set = set()


async def _give_up_on_video(row: dict):
    """Instead of silently doing nothing when no clip is ever found, post
    an honest note -- e.g. if it's a national broadcast, MLB.tv is dark for
    that game and clips through this pipeline generally aren't available."""
    channel = bot.get_channel(row["channel_id"])
    if channel is None:
        return
    try:
        message = await channel.fetch_message(row["message_id"])
    except Exception as e:
        log.error("Couldn't fetch message %s to add no-clip note: %s", row["message_id"], e)
        return

    try:
        is_national = await asyncio.to_thread(mlb_api.is_national_broadcast, row["game_pk"])
    except Exception as e:
        log.error("Failed to check national broadcast status for game %s: %s", row["game_pk"], e)
        is_national = False

    had_uuid = row["id"] in _uuid_resolved_rows
    _uuid_resolved_rows.discard(row["id"])
    if is_national:
        note = "*(no clip — national TV exclusive; dark for MLB.tv)*"
    elif not had_uuid:
        note = "*(no clip — couldn't match this play in the live feed, likely a scorer amendment)*"
    else:
        note = "*(no clip found for this play)*"
    try:
        await channel.send(note, reference=message)
    except Exception as e:
        log.error("Failed to send no-clip note for game %s: %s", row["game_pk"], e)


@tasks.loop(seconds=VIDEO_POLL_SECONDS)
async def poll_video_followups():
    for row in storage.get_pending_video_lookups():
        if row["attempts"] >= VIDEO_MAX_ATTEMPTS:
            await _give_up_on_video(row)
            storage.delete_pending_video_lookup(row["id"])
            continue

        # 1) Resolve the Statcast play UUID (may already be stored)
        try:
            play_uuid = await asyncio.to_thread(
                _resolve_play_uuid, row["game_pk"], row["play_id"],
                row["description"], row.get("play_end_time"), row.get("batter"),
            )
        except Exception as e:
            log.error("UUID resolution failed for game %s: %s", row["game_pk"], e)
            storage.increment_video_attempts(row["id"])
            continue
        if not play_uuid:
            log.info("No play UUID yet for game %s play %s (attempt %d)",
                     row["game_pk"], row["play_id"], row["attempts"] + 1)
            storage.increment_video_attempts(row["id"])
            continue
        _uuid_resolved_rows.add(row["id"])

        # 2) Is the Savant clip processed yet?
        clip_url = await asyncio.to_thread(_savant_clip_ready, play_uuid)
        if not clip_url:
            log.info("Clip not ready for game %s play %s (attempt %d)",
                     row["game_pk"], play_uuid, row["attempts"] + 1)
            storage.increment_video_attempts(row["id"])
            continue

        channel = bot.get_channel(row["channel_id"])
        if channel is None:
            storage.delete_pending_video_lookup(row["id"])
            continue

        try:
            message = await channel.fetch_message(row["message_id"])
            # Raw URL as its OWN plain message -> Discord unfurls it into an
            # inline video player (links buried in embed fields never do).
            await channel.send(clip_url, reference=message)
            log.info("Attached Savant clip to message %s (game %s)", row["message_id"], row["game_pk"])
            _uuid_resolved_rows.discard(row["id"])
        except Exception as e:
            log.error(
                "Failed to attach video for game %s (channel_id=%s, message_id=%s): %s",
                row["game_pk"], row["channel_id"], row["message_id"], e,
            )
        finally:
            storage.delete_pending_video_lookup(row["id"])


@poll_video_followups.before_loop
async def before_video_poll():
    await bot.wait_until_ready()


@poll_games.before_loop
async def before_poll():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    try:
        storage.init_db()
    except Exception as e:
        log.error("Failed to init database at %s: %s -- falling back to local storage", storage.DB_PATH, e)
        storage.DB_PATH = "errors_bot_fallback.db"
        storage.init_db()
    try:
        synced = await bot.tree.sync()
        log.info("Synced %d slash commands", len(synced))
    except Exception as e:
        log.error("Slash command sync failed: %s", e)
    if HAS_SERVER_NONCE_DEDUP:
        log.info(
            "discord.py %s: automatic server-side nonce dedup ACTIVE (mark-after-send, failed sends retry)",
            discord.__version__,
        )
    else:
        log.warning(
            "discord.py %s is older than 2.5 -- running DEGRADED (no automatic nonce dedup, "
            "mark-before-send). Pin discord.py>=2.5.0 in requirements.txt and redeploy.",
            discord.__version__,
        )
    if not poll_games.is_running():
        poll_games.start()
    if not poll_video_followups.is_running():
        poll_video_followups.start()
    log.info("Logged in as %s", bot.user)


@bot.tree.command(name="setchannel", description="Set this channel to receive error alerts")
@app_commands.checks.has_permissions(manage_guild=True)
async def setchannel(interaction: discord.Interaction):
    storage.set_config("announce_channel_id", str(interaction.channel_id))
    await interaction.response.send_message(
        f"✅ Error alerts will post in {interaction.channel.mention}."
    )


@bot.tree.command(name="lasterror", description="Show the most recent error from today's games")
async def lasterror(interaction: discord.Interaction):
    await interaction.response.defer()
    date_str = et_date_str(0)
    try:
        games = mlb_api.get_live_games(date_str)
    except Exception as e:
        await interaction.followup.send(f"Couldn't reach the MLB API right now: {e}")
        return

    checkable = [g for g in games if g["abstract_state"] in ("Live", "Final")]

    best_game = None
    best_event = None
    best_time = None

    for game in checkable:
        try:
            feed = mlb_api.get_live_feed(game["game_pk"])
            events = mlb_api.extract_events(feed)
        except Exception:
            continue

        for event in events:
            if event["type"] != "error":
                continue
            end_time = event.get("end_time")
            if end_time and (best_time is None or end_time > best_time):
                best_time = end_time
                best_event = event
                best_game = game

    if not best_event:
        await interaction.followup.send("No errors found in today's games yet.")
        return

    await interaction.followup.send(embed=build_embed(best_game, best_event))


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file (see .env.example).")
    bot.run(TOKEN)
