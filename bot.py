import os
import logging
import io
from datetime import datetime, timedelta, timezone, time as dtime

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import mlb_api
import stats_hitting as sh
import storage
import card

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
ROSTER_REFRESH_HOURS = float(os.getenv("ROSTER_REFRESH_HOURS", "6"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("hitters_bot")

intents = discord.Intents.default()


def et_date_str(offset_days: int = 0) -> str:
    et = datetime.now(timezone.utc) - timedelta(hours=4)
    et += timedelta(days=offset_days)
    return et.strftime("%Y-%m-%d")


def build_batter_embed(name: str, team: str, splits: list[dict], platoon: dict | None = None,
                        since_date: str | None = None) -> discord.Embed:
    if not splits:
        return discord.Embed(title=name, description="No game log found for this season yet.",
                              color=discord.Color.light_grey())

    last = splits[-1]
    last10 = sh.summarize_batting(splits, 10)
    tag = sh.hot_cold_tag(last10)

    title = f"{name} ({team})"
    if tag:
        title += f"  {tag}"

    embed = discord.Embed(
        title=title,
        description=(
            f"{last['date']} vs {last['opponent']}\n"
            f"{last['hits']}-for-{last['ab']}, {last['hr']} HR, {last['rbi']} RBI, {last['bb']} BB, {last['so']} K"
        ),
        color=discord.Color.blue(),
    )

    streaks = sh.get_active_streaks(splits)
    notable = sh.notable_streak_labels(streaks)
    if notable:
        embed.add_field(name="Active streaks", value="\n".join(notable), inline=False)

    for n, label in ((5, "Last 5"), (10, "Last 10"), (20, "Last 20")):
        summary = sh.summarize_batting(splits, n)
        if summary:
            embed.add_field(
                name=label,
                value=(
                    f"AVG **{summary['avg']}** / OBP {summary['obp']} / SLG {summary['slg']} / OPS {summary['ops']}\n"
                    f"{summary['hr']} HR, {summary['rbi']} RBI over {summary['count']} games"
                ),
                inline=False,
            )

    season = sh.summarize_batting(splits, len(splits))
    if season:
        embed.add_field(
            name="Season",
            value=(
                f"AVG **{season['avg']}** / OBP {season['obp']} / SLG {season['slg']} / OPS {season['ops']}\n"
                f"{season['hr']} HR, {season['rbi']} RBI over {season['count']} games"
            ),
            inline=False,
        )

    if since_date:
        since_splits = [s for s in splits if s["date"] and s["date"] >= since_date]
        since_summary = sh.summarize_batting(since_splits, len(since_splits)) if since_splits else None
        if since_summary:
            embed.add_field(
                name=f"Since {since_date}",
                value=(
                    f"AVG **{since_summary['avg']}** / OBP {since_summary['obp']} / "
                    f"SLG {since_summary['slg']} / OPS {since_summary['ops']}\n"
                    f"{since_summary['hr']} HR, {since_summary['rbi']} RBI over {since_summary['count']} games"
                ),
                inline=False,
            )
        else:
            embed.add_field(name=f"Since {since_date}", value="No games found in this range.", inline=False)

    if platoon:
        for key, label in (("vs_lhp", "vs LHP (season)"), ("vs_rhp", "vs RHP (season)")):
            p = platoon.get(key)
            if p and p.get("ab", 0) > 0:
                embed.add_field(
                    name=label,
                    value=f"AVG **{p['avg']}** / OBP {p['obp']} / SLG {p['slg']} / OPS {p['ops']}  ({p['ab']} AB, {p['hr']} HR)",
                    inline=True,
                )

    embed.set_footer(text="Data: MLB Stats API")
    return embed


class HittersBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.teams: list[dict] = []
        self.player_directory: list[dict] = []  # [{"id":, "name":, "team":}]

    async def setup_hook(self):
        storage.init_db()
        try:
            self.teams = mlb_api.get_all_teams()
        except Exception as e:
            log.error("Failed to fetch teams at startup: %s", e)
            self.teams = []
        await self.refresh_player_directory()

        batter_cmd = app_commands.Command(
            name="batter",
            description="Recent stats, streaks, hot/cold status for any hitter (optional: since YYYY-MM-DD)",
            callback=self._batter_callback,
        )
        self.tree.add_command(batter_cmd)
        batter_cmd.autocomplete("name")(self._name_autocomplete)

        hittercard_cmd = app_commands.Command(
            name="hittercard",
            description="Visual stat card: AVG/OBP/SLG/OPS/HR/RBI, hot/cold, streaks",
            callback=self._hittercard_callback,
        )
        self.tree.add_command(hittercard_cmd)
        hittercard_cmd.autocomplete("name")(self._name_autocomplete)

        hot_cmd = app_commands.Command(
            name="hothitters",
            description="League-wide scan: who's hot right now (last 10 games). Takes a few minutes.",
            callback=self._hothitters_callback,
        )
        self.tree.add_command(hot_cmd)

        cold_cmd = app_commands.Command(
            name="coldhitters",
            description="League-wide scan: who's cold right now (last 10 games). Takes a few minutes.",
            callback=self._coldhitters_callback,
        )
        self.tree.add_command(cold_cmd)

        streaks_cmd = app_commands.Command(
            name="streaks",
            description="League-wide scan: active hit/walk/HR streaks. Takes a few minutes.",
            callback=self._streaks_callback,
        )
        self.tree.add_command(streaks_cmd)

        hotvlhp_cmd = app_commands.Command(
            name="hotvslhp",
            description="League-wide scan: who's mashing lefties this season. Takes a few minutes.",
            callback=self._hotvslhp_callback,
        )
        self.tree.add_command(hotvlhp_cmd)

        coldvlhp_cmd = app_commands.Command(
            name="coldvslhp",
            description="League-wide scan: who's struggling vs lefties this season. Takes a few minutes.",
            callback=self._coldvslhp_callback,
        )
        self.tree.add_command(coldvlhp_cmd)

        hotvrhp_cmd = app_commands.Command(
            name="hotvsrhp",
            description="League-wide scan: who's mashing righties this season. Takes a few minutes.",
            callback=self._hotvsrhp_callback,
        )
        self.tree.add_command(hotvrhp_cmd)

        coldvrhp_cmd = app_commands.Command(
            name="coldvsrhp",
            description="League-wide scan: who's struggling vs righties this season. Takes a few minutes.",
            callback=self._coldvsrhp_callback,
        )
        self.tree.add_command(coldvrhp_cmd)

        setchannel_cmd = app_commands.Command(
            name="setchannel",
            description="Set this channel for this bot's output",
            callback=self._setchannel_callback,
        )
        self.tree.add_command(setchannel_cmd)

        try:
            synced = await self.tree.sync()
            log.info("Synced %d slash commands", len(synced))
        except Exception as e:
            log.error("Slash command sync failed: %s", e)

    async def refresh_player_directory(self):
        directory = []
        for team in self.teams:
            try:
                hitters = mlb_api.get_active_roster_hitters(team["id"])
            except Exception as e:
                log.error("Failed to fetch roster for team %s: %s", team["id"], e)
                continue
            for p in hitters:
                directory.append({"id": p["id"], "name": p["name"], "team": team["abbreviation"]})

        self.player_directory = directory
        log.info("Player directory refreshed: %d hitters", len(directory))

    async def _name_autocomplete(self, interaction: discord.Interaction, current: str):
        current_lower = current.lower()
        matches = [p for p in self.player_directory if current_lower in p["name"].lower()][:25]
        return [app_commands.Choice(name=f"{p['name']} ({p['team']})", value=str(p["id"])) for p in matches]

    def _resolve_player(self, name: str):
        try:
            pid = int(name)
            match = next((p for p in self.player_directory if p["id"] == pid), None)
            return (pid, match) if match else (pid, None)
        except ValueError:
            pass
        match = next((p for p in self.player_directory if name.lower() in p["name"].lower()), None)
        return (match["id"], match) if match else (None, None)

    async def _batter_callback(self, interaction: discord.Interaction, name: str, since: str | None = None):
        await interaction.response.defer()
        person_id, match = self._resolve_player(name)
        if person_id is None:
            await interaction.followup.send(f"Couldn't find a hitter matching '{name}'.")
            return

        try:
            splits = mlb_api.get_batting_game_log(person_id)
        except Exception as e:
            await interaction.followup.send(f"Couldn't reach the MLB API right now: {e}")
            return
        try:
            platoon = mlb_api.get_platoon_splits(person_id)
        except Exception as e:
            log.error("Platoon split lookup failed for %s: %s", name, e)
            platoon = None
        display_name = match["name"] if match else name
        team = match["team"] if match else "?"
        await interaction.followup.send(embed=build_batter_embed(display_name, team, splits, platoon, since))

    async def _hittercard_callback(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer()
        person_id, match = self._resolve_player(name)
        if person_id is None:
            await interaction.followup.send(f"Couldn't find a hitter matching '{name}'.")
            return

        try:
            splits = mlb_api.get_batting_game_log(person_id)
        except Exception as e:
            await interaction.followup.send(f"Couldn't reach the MLB API right now: {e}")
            return

        if not splits:
            await interaction.followup.send("No game log found for this hitter this season yet.")
            return

        display_name = match["name"] if match else name
        team = match["team"] if match else "?"

        season = sh.summarize_batting(splits, len(splits))
        tag = sh.hot_cold_tag(sh.summarize_batting(splits, 10))
        streaks = sh.get_active_streaks(splits)
        notable = sh.notable_streak_labels(streaks)

        try:
            png_bytes = card.build_hitter_card(display_name, team, season, tag, notable, player_id=person_id)
        except Exception as e:
            log.error("Card generation failed for %s: %s", display_name, e)
            await interaction.followup.send(f"Couldn't generate the card: {e}")
            return

        file = discord.File(io.BytesIO(png_bytes), filename=f"{display_name.replace(' ', '_')}_card.png")
        await interaction.followup.send(file=file)

    async def _scan_all_hitters(self, interaction: discord.Interaction):
        """Shared scan used by /hothitters, /coldhitters, /streaks -- one pass over every
        active roster hitter's game log, since all three need the same underlying data."""
        await interaction.followup.send(
            f"Scanning {len(self.player_directory)} active hitters league-wide, this'll take a few minutes..."
        )
        results = []
        for p in self.player_directory:
            try:
                splits = mlb_api.get_batting_game_log(p["id"])
            except Exception as e:
                log.error("Batting log lookup failed for %s: %s", p["name"], e)
                continue
            if not splits:
                continue
            last10 = sh.summarize_batting(splits, 10)
            tag = sh.hot_cold_tag(last10)
            streaks = sh.get_active_streaks(splits)
            notable = sh.notable_streak_labels(streaks)
            results.append({"player": p, "last10": last10, "tag": tag, "notable": notable})
        return results

    async def _hothitters_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        results = await self._scan_all_hitters(interaction)
        lines = [
            f"**{r['player']['name']}** ({r['player']['team']}) — {r['last10']['ops']} OPS last {r['last10']['count']} games\n"
            for r in results if r["tag"] == "🔥 Hot"
        ]
        await self._send_chunked(interaction, "__**🔥 Hot Hitters (last 10 games)**__\n\n", lines)

    async def _coldhitters_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        results = await self._scan_all_hitters(interaction)
        lines = [
            f"**{r['player']['name']}** ({r['player']['team']}) — {r['last10']['ops']} OPS last {r['last10']['count']} games\n"
            for r in results if r["tag"] == "🥶 Cold"
        ]
        await self._send_chunked(interaction, "__**🥶 Cold Hitters (last 10 games)**__\n\n", lines)

    async def _streaks_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        results = await self._scan_all_hitters(interaction)
        lines = []
        for r in results:
            if not r["notable"]:
                continue
            lines.append(f"**{r['player']['name']}** ({r['player']['team']}): {', '.join(r['notable'])}\n")
        await self._send_chunked(interaction, "__**Active Notable Streaks**__\n\n", lines)

    async def _scan_platoon(self, interaction: discord.Interaction, side_key: str):
        side_label = "LHP" if side_key == "vs_lhp" else "RHP"
        await interaction.followup.send(
            f"Scanning {len(self.player_directory)} hitters' season vs {side_label} splits, this'll take a few minutes..."
        )
        results = []
        for p in self.player_directory:
            try:
                platoon = mlb_api.get_platoon_splits(p["id"])
            except Exception as e:
                log.error("Platoon lookup failed for %s: %s", p["name"], e)
                continue
            split = platoon.get(side_key)
            if not split or not split.get("ab") or split["ab"] < 20:
                continue  # too small a sample to mean anything
            try:
                ops = float(split["ops"]) if split.get("ops") not in (None, "-", "") else None
            except (TypeError, ValueError):
                ops = None
            if ops is None:
                continue
            results.append({"player": p, "split": split, "ops": ops})
        return results

    async def _platoon_scan(self, interaction: discord.Interaction, side_key: str, want_hot: bool):
        await interaction.response.defer()
        results = await self._scan_platoon(interaction, side_key)
        threshold_hits = [
            r for r in results
            if (r["ops"] >= sh.HOT_OPS_THRESHOLD if want_hot else r["ops"] <= sh.COLD_OPS_THRESHOLD)
        ]
        side_label = "LHP" if side_key == "vs_lhp" else "RHP"
        label = "Hot" if want_hot else "Cold"
        emoji = "🔥" if want_hot else "🥶"
        lines = [
            f"**{r['player']['name']}** ({r['player']['team']}) — {r['split']['ops']} OPS ({r['split']['ab']} AB) vs {side_label}\n"
            for r in threshold_hits
        ]
        header = f"__**{emoji} {label} vs {side_label} (season)**__\n\n"
        await self._send_chunked(interaction, header, lines)

    async def _hotvslhp_callback(self, interaction: discord.Interaction):
        await self._platoon_scan(interaction, "vs_lhp", want_hot=True)

    async def _coldvslhp_callback(self, interaction: discord.Interaction):
        await self._platoon_scan(interaction, "vs_lhp", want_hot=False)

    async def _hotvsrhp_callback(self, interaction: discord.Interaction):
        await self._platoon_scan(interaction, "vs_rhp", want_hot=True)

    async def _coldvsrhp_callback(self, interaction: discord.Interaction):
        await self._platoon_scan(interaction, "vs_rhp", want_hot=False)

    async def _send_chunked(self, interaction: discord.Interaction, header: str, lines: list[str], limit: int = 1900):
        if not lines:
            await interaction.channel.send(header + "Nobody qualifies right now.")
            return
        chunk = header
        for line in lines:
            if len(chunk) + len(line) > limit:
                await interaction.channel.send(chunk)
                chunk = ""
            chunk += line
        if chunk.strip():
            await interaction.channel.send(chunk)

    async def _setchannel_callback(self, interaction: discord.Interaction):
        storage.set_config("announce_channel_id", str(interaction.channel_id))
        await interaction.response.send_message(f"✅ Set to post in {interaction.channel.mention}.")

    async def on_ready(self):
        log.info("Logged in as %s", self.user)
        if not refresh_directory_loop.is_running():
            refresh_directory_loop.start(self)
        if not daily_streaks_post.is_running():
            daily_streaks_post.start(self)


client = HittersBot()


async def send_chunked_to_channel(channel, header: str, lines: list[str], limit: int = 1900):
    """Same chunking logic as _send_chunked, but for scheduled posts that aren't triggered by a command."""
    if not lines:
        await channel.send(header + "Nobody qualifies right now.")
        return
    chunk = header
    for line in lines:
        if len(chunk) + len(line) > limit:
            await channel.send(chunk)
            chunk = ""
        chunk += line
    if chunk.strip():
        await channel.send(chunk)


@tasks.loop(hours=ROSTER_REFRESH_HOURS)
async def refresh_directory_loop(bot: HittersBot):
    await bot.refresh_player_directory()


@refresh_directory_loop.before_loop
async def before_refresh():
    await client.wait_until_ready()


# 11:30 AM ET, approximated as UTC-4 (matches the rest of this bot's ET handling).
# NOTE: like everywhere else in this bot, this doesn't auto-adjust for EST in the
# off-season -- it'll run at 10:30 AM ET instead of 11:30 during standard time.
@tasks.loop(time=dtime(hour=15, minute=30))
async def daily_streaks_post(bot: HittersBot):
    channel_id = storage.get_config("announce_channel_id")
    if not channel_id:
        return
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        return

    log.info("Running daily streaks scan for %d hitters...", len(bot.player_directory))
    lines = []
    for p in bot.player_directory:
        try:
            splits = mlb_api.get_batting_game_log(p["id"])
        except Exception as e:
            log.error("Daily streak scan failed for %s: %s", p["name"], e)
            continue
        if not splits:
            continue
        streaks = sh.get_active_streaks(splits)
        notable = sh.notable_streak_labels(streaks)
        if notable:
            lines.append(f"**{p['name']}** ({p['team']}): {', '.join(notable)}\n")

    await send_chunked_to_channel(channel, "__**Daily Streak Report**__\n\n", lines)
    log.info("Daily streaks post complete: %d players with notable streaks", len(lines))


@daily_streaks_post.before_loop
async def before_daily_streaks():
    await client.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file (see .env.example).")
    client.run(TOKEN)
