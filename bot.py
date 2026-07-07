import os
import logging
import asyncio
from datetime import datetime, timedelta, timezone

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
VIDEO_MAX_ATTEMPTS = int(os.getenv("VIDEO_MAX_ATTEMPTS", "6"))  # ~6 min window at 60s poll

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("errors_bot")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


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

    # Timestamp shown by Discord next to the footer, matching the play time when known
    if event.get("end_time"):
        try:
            embed.timestamp = datetime.fromisoformat(event["end_time"].replace("Z", "+00:00"))
        except Exception:
            embed.timestamp = datetime.now(timezone.utc)
    else:
        embed.timestamp = datetime.now(timezone.utc)

    return embed


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
            if storage.already_alerted(game["game_pk"], event["play_id"], event["type"]):
                continue
            if storage.already_alerted_by_content(game["game_pk"], event["description"]):
                continue

            storage.mark_alerted(game["game_pk"], event["play_id"], event["type"])
            storage.mark_alerted_by_content(game["game_pk"], event["description"])
            try:
                sent_message = await channel.send(embed=build_embed(game, event))
                log.info("Alerted %s in game %s", event["type"], game["game_pk"])

                # Only errors get a video-clip follow-up for now.
                if event["type"] == "error":
                    storage.add_pending_video_lookup(
                        game["game_pk"], event["play_id"], sent_message.id, channel.id,
                        event["description"], event.get("end_time"), event.get("batter"),
                    )
            except Exception as e:
                log.error("Failed to send alert for game %s: %s", game["game_pk"], e)


async def _give_up_on_video(row: dict):
    """Instead of silently doing nothing when no clip is ever found, post
    an honest note -- e.g. if it's a national broadcast, MLB.tv is dark for
    that game and clips through this pipeline generally aren't available,
    same distinction a similar bot in the community already makes."""
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

    note = (
        "*(no clip — national TV exclusive; dark for MLB.tv)*" if is_national
        else "*(no clip found for this play)*"
    )
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

        try:
            content = mlb_api.get_game_content(row["game_pk"])
            items_count = len((((content.get("highlights") or {}).get("highlights") or {}).get("items")) or [])
            match = mlb_api.find_highlight_for_play(content, row["description"], row["play_end_time"], row.get("batter"))
            log.info(
                "Video lookup game %s play %s: %d highlight items available, match=%s",
                row["game_pk"], row["play_id"], items_count, bool(match),
            )
            if items_count == 0 and row["attempts"] == 0:
                # Diagnostic only, fires once per play on the first attempt --
                # this tells us the REAL shape of the response instead of
                # continuing to guess at the JSON path. Once we see this in
                # the logs we can fix the parsing with certainty.
                top_keys = list(content.keys())
                highlights_val = content.get("highlights")
                log.info(
                    "DIAGNOSTIC game %s: top-level content keys=%s | 'highlights' key type=%s value_preview=%s",
                    row["game_pk"], top_keys, type(highlights_val).__name__,
                    str(highlights_val)[:500] if highlights_val else "None/empty",
                )
        except Exception as e:
            log.error("Video lookup failed for game %s: %s", row["game_pk"], e)
            storage.increment_video_attempts(row["id"])
            continue

        if not match:
            storage.increment_video_attempts(row["id"])
            continue

        channel = bot.get_channel(row["channel_id"])
        if channel is None:
            storage.delete_pending_video_lookup(row["id"])
            continue

        try:
            message = await channel.fetch_message(row["message_id"])
            # Sending the raw URL as its OWN plain message (not inside the
            # embed as a markdown link) is what makes Discord auto-render it
            # as an inline playable video -- a link buried in an embed field
            # never gets that treatment, which is why it showed as plain
            # clickable text before instead of a real video player.
            await channel.send(match["video_url"], reference=message)
            log.info("Attached video to message %s (game %s)", row["message_id"], row["game_pk"])
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

    # Check games that have started (Live or Final) -- skip ones that haven't begun yet
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
