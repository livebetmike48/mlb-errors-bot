import os
import logging
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
    if event["type"] == "error":
        title = f"⚠️ Error"
        if event.get("fielder_name"):
            pos = f"{event['fielder_position']} " if event.get("fielder_position") else ""
            title = f"⚠️ Error — {pos}{event['fielder_name']}"
 
        embed = discord.Embed(title=title[:256], color=discord.Color.gold())
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
 
    else:
        matchup = f"{game['away_team']} @ {game['home_team']}"
        loc = f"{event['half']} {event['inning']}"
 
        if event["type"] == "review_pending":
            embed = discord.Embed(
                title=f"🔍 Replay review in progress ({event.get('review_type', 'Play')})",
                description=f"**{matchup}** — {loc}\n{event['description']}",
                color=discord.Color.purple(),
            )
        else:  # review_result
            outcome = "OVERTURNED" if event.get("overturned") else "Call stands / confirmed"
            embed = discord.Embed(
                title=f"✅ Replay review complete: {outcome}",
                description=f"**{matchup}** — {loc}\n{event['description']}",
                color=discord.Color.orange() if event.get("overturned") else discord.Color.green(),
            )
        embed.set_footer(text="Data: MLB Stats API")
 
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
 
            storage.mark_alerted(game["game_pk"], event["play_id"], event["type"])
            try:
                sent_message = await channel.send(embed=build_embed(game, event))
                log.info("Alerted %s in game %s", event["type"], game["game_pk"])
 
                # Only errors get a video-clip follow-up for now.
                if event["type"] == "error":
                    storage.add_pending_video_lookup(
                        game["game_pk"], event["play_id"], sent_message.id, channel.id,
                        event["description"], event.get("end_time"),
                    )
            except Exception as e:
                log.error("Failed to send alert for game %s: %s", game["game_pk"], e)
 
 
@tasks.loop(seconds=VIDEO_POLL_SECONDS)
async def poll_video_followups():
    for row in storage.get_pending_video_lookups():
        if row["attempts"] >= VIDEO_MAX_ATTEMPTS:
            storage.delete_pending_video_lookup(row["id"])
            continue
 
        try:
            content = mlb_api.get_game_content(row["game_pk"])
            match = mlb_api.find_highlight_for_play(content, row["description"], row["play_end_time"])
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
            embed = message.embeds[0]
            embed.add_field(name="📹 Highlight", value=f"[Watch clip]({match['video_url']})", inline=False)
            await message.edit(embed=embed)
            log.info("Attached video to message %s (game %s)", row["message_id"], row["game_pk"])
        except Exception as e:
            log.error("Failed to attach video for game %s: %s", row["game_pk"], e)
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
 
 
@bot.tree.command(name="setchannel", description="Set this channel to receive error/review alerts")
@app_commands.checks.has_permissions(manage_guild=True)
async def setchannel(interaction: discord.Interaction):
    storage.set_config("announce_channel_id", str(interaction.channel_id))
    await interaction.response.send_message(
        f"✅ Error and replay-review alerts will post in {interaction.channel.mention}."
    )
 
 
@bot.tree.command(name="pending", description="Check right now for any active replay reviews")
async def pending(interaction: discord.Interaction):
    await interaction.response.defer()
    date_str = et_date_str(0)
    try:
        games = mlb_api.get_live_games(date_str)
    except Exception as e:
        await interaction.followup.send(f"Couldn't reach the MLB API right now: {e}")
        return
 
    hits = []
    for game in games:
        if game["abstract_state"] != "Live":
            continue
        try:
            feed = mlb_api.get_live_feed(game["game_pk"])
            events = mlb_api.extract_events(feed)
        except Exception:
            continue
        for event in events:
            if event["type"] == "review_pending":
                hits.append((game, event))
 
    if not hits:
        await interaction.followup.send("No active replay reviews right now.")
        return
 
    embed = discord.Embed(title="Active replay reviews", color=discord.Color.purple())
    for game, event in hits:
        embed.add_field(
            name=f"{game['away_team']} @ {game['home_team']}",
            value=f"{event['half']} {event['inning']}: {event['description']}",
            inline=False,
        )
    await interaction.followup.send(embed=embed)
 
 
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file (see .env.example).")
    bot.run(TOKEN)
