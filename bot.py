from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import aiohttp
import discord
from bs4 import BeautifulSoup, Tag
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("matchbot")

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0") or 0)
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0") or 0)
URLS_FILE = Path(os.getenv("MATCH_URLS_FILE", "data/kampliste.txt"))
if not URLS_FILE.is_absolute():
    URLS_FILE = ROOT / URLS_FILE
POLL_INTERVAL = max(25, int(os.getenv("POLL_INTERVAL_SECONDS", "25")))
SCHEDULE_REFRESH_HOURS = max(1, int(os.getenv("SCHEDULE_REFRESH_HOURS", "6")))
ACTIVE_WINDOW_BEFORE_MINUTES = max(0, int(os.getenv("ACTIVE_WINDOW_BEFORE_MINUTES", "120")))
ACTIVE_WINDOW_AFTER_HOURS = max(1, int(os.getenv("ACTIVE_WINDOW_AFTER_HOURS", "3")))
LOCAL_TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Copenhagen"))
DB_PATH = ROOT / "data" / "matchbot.sqlite3"
PLAYERS_FILE = Path(os.getenv("PLAYERS_FILE", "data/players.txt"))
if not PLAYERS_FILE.is_absolute():
    PLAYERS_FILE = ROOT / PLAYERS_FILE


@dataclass
class Team:
    name: str = "Unknown team"
    url: str | None = None


@dataclass
class MapInfo:
    key: str | None = None
    name: str = "Unknown map"
    status: str | None = None
    rounds: int | None = None
    score1: int | None = None
    score2: int | None = None
    winner: str | None = None


@dataclass
class PlayerRating:
    profile_url: str
    nickname: str
    esr: str


@dataclass
class Match:
    url: str
    match_id: str
    state: str = "unknown"
    team1: Team = field(default_factory=Team)
    team2: Team = field(default_factory=Team)
    format: str | None = None
    game: str | None = None
    starts_at: str | None = None
    series1: int | None = None
    series2: int | None = None
    winner: str | None = None
    maps: list[MapInfo] = field(default_factory=list)
    player_ratings: list[PlayerRating] = field(default_factory=list)
    mvp: str | None = None
    mvp_rating: str | None = None
    raw_status: str | None = None

    def serializable(self) -> dict[str, Any]:
        return asdict(self)


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def text(node: Tag | None, selector: str | None = None) -> str | None:
    if node is None:
        return None
    target = node.select_one(selector) if selector else node
    return clean(target.get_text(" ", strip=True)) if target else None


def as_int(value: str | None) -> int | None:
    if not value:
        return None
    found = re.search(r"-?\d+", value.replace("\u00a0", " "))
    return int(found.group()) if found else None


def as_float(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"-?\d+(?:[,.]\d+)?", value.replace("\u00a0", " "))
    if not match:
        return None
    try:
        return float(match.group().replace(",", "."))
    except ValueError:
        return None


def canonical_profile_url(value: str) -> str:
    absolute = urljoin("https://esportligaen.dk", value.strip())
    parsed = urlsplit(absolute)
    return f"{parsed.netloc.casefold()}{parsed.path.rstrip('/').casefold()}"


def score_pair(value: str | None) -> tuple[int | None, int | None]:
    numbers = [int(number) for number in re.findall(r"\d+", value or "")]
    return (numbers[0], numbers[1]) if len(numbers) >= 2 else (None, None)


def match_id_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1].split("?")[0]


def read_players() -> list[tuple[str, str]]:
    if not PLAYERS_FILE.exists():
        log.warning("Player list is missing: %s", PLAYERS_FILE)
        return []
    players: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in PLAYERS_FILE.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#") or "|" not in value:
            continue
        profile_url, display_name = (part.strip() for part in value.split("|", 1))
        if "/profil/" not in profile_url.casefold() or not display_name:
            continue
        key = canonical_profile_url(profile_url)
        if key not in seen:
            players.append((profile_url, display_name))
            seen.add(key)
    return players


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=LOCAL_TZ)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        # Esportligaen's not-started match page displays dates as DD-MM-YYYY HH:mm.
        try:
            parsed = datetime.strptime(value.strip(), "%d-%m-%Y %H:%M").replace(tzinfo=LOCAL_TZ)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return None


def _team_from_link(node: Tag | None) -> Team:
    if not node:
        return Team()
    link = node if node.name == "a" else node.select_one("a")
    return Team(
        name=clean(link.get("title") or link.get_text(" ", strip=True)) if link else clean(node.get_text(" ", strip=True)),
        url=link.get("href") if link else None,
    )


def _jsonld_datetime(soup: BeautifulSoup) -> str | None:
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if isinstance(item, dict):
                if item.get("startDate"):
                    return str(item["startDate"])
                graph = item.get("@graph")
                if isinstance(graph, list):
                    for subitem in graph:
                        if isinstance(subitem, dict) and subitem.get("startDate"):
                            return str(subitem["startDate"])
    return None


def parse_match(url: str, html: str) -> Match:
    soup = BeautifulSoup(html, "html.parser")
    mid = match_id_from_url(url)
    root = soup.select_one(".match")
    if root is None:
        return Match(url=url, match_id=mid)

    classes = set(root.get("class", []))
    if "finished" in classes:
        state = "finished"
    elif "veto" in classes:
        state = "veto"
    elif "ongoing" in classes:
        state = "live"
    elif "upcoming" in classes:
        state = "upcoming"
    else:
        state = "unknown"
    result = Match(url=url, match_id=mid, state=state)

    # Completed match template.
    hero = root.select_one(".match-hero")
    if hero:
        result.team1 = _team_from_link(hero.select_one(".match-hero__team.is-team1 .match-hero__name"))
        result.team2 = _team_from_link(hero.select_one(".match-hero__team.is-team2 .match-hero__name"))
        nums = hero.select(".match-hero__score .match-hero__num")
        if len(nums) >= 2:
            result.series1, result.series2 = as_int(nums[0].get_text()), as_int(nums[1].get_text())
        if hero.select_one(".match-hero__team.is-team1.is-winner"):
            result.winner = result.team1.name
        elif hero.select_one(".match-hero__team.is-team2.is-winner"):
            result.winner = result.team2.name
        meta_text = text(hero, ".match-hero__meta") or ""
        format_match = re.search(r"\bBO\s*\d+\b", meta_text, re.IGNORECASE)
        result.format = format_match.group().replace(" ", "").upper() if format_match else None
        result.game = text(hero, ".match-hero__game")
        time_el = hero.select_one("time[datetime]")
        result.starts_at = time_el.get("datetime") if time_el else None
        for card in root.select(".map-results__card"):
            side_score1, side_score2 = score_pair(text(card, ".map-results__score"))
            result.maps.append(MapInfo(
                name=text(card, ".map-results__map") or "Unknown map",
                score1=side_score1,
                score2=side_score2,
                status="finished",
                winner=text(card, ".map-results__winner .map-results__team"),
            ))
        # BO1 pages can omit map-results cards, while still rendering the map
        # score in the map-specific scoreboard panel.
        if not result.maps:
            panels = root.select(".matchroom-scoreboard__panel")
            map_panels = [
                panel for panel in panels
                if (text(panel, ".matchroom-scoreboard__meta-map") or "").casefold() not in {"alle maps", "all maps"}
            ] or panels
            for panel in map_panels:
                score1, score2 = score_pair(text(panel, ".matchroom-scoreboard__meta-score"))
                if score1 is None or score2 is None:
                    continue
                winner_team = panel.select_one(".matchroom-scoreboard__team.is-winner .matchroom-scoreboard__team-label")
                result.maps.append(MapInfo(
                    name=text(panel, ".matchroom-scoreboard__meta-map") or "Unknown map",
                    score1=score1,
                    score2=score2,
                    status="finished",
                    winner=text(winner_team),
                ))

        scoreboard_panels = root.select(".matchroom-scoreboard__panel")
        aggregate_panels = [
            panel for panel in scoreboard_panels
            if (text(panel, ".matchroom-scoreboard__meta-map") or "").casefold() in {"alle maps", "all maps"}
        ]
        rating_panels = aggregate_panels[:1] if aggregate_panels else scoreboard_panels
        for panel in rating_panels:
            for row in panel.select(".matchroom-scoreboard__table tbody tr"):
                name_link = row.select_one(".matchroom-scoreboard__name[href]")
                esr = text(row, ".col-esr")
                if name_link and esr:
                    result.player_ratings.append(PlayerRating(
                        profile_url=urljoin("https://esportligaen.dk", name_link["href"]),
                        nickname=clean(name_link.get_text(" ", strip=True)),
                        esr=esr,
                    ))
        result.mvp = text(root, ".matchroom-mvp__name")
        for stat in root.select(".matchroom-mvp__stat"):
            if (text(stat, "dt") or "").casefold() in {"esr-rating", "esr rating"}:
                result.mvp_rating = text(stat, "dd")
                break
        return result

    # In-progress/veto template teams.
    result.team1 = _team_from_link(root.select_one(".match_header .team-1-name"))
    result.team2 = _team_from_link(root.select_one(".match_header .team-2-name"))
    result.raw_status = text(root, ".match_header .status")
    result.format = text(root, ".estag_wrapper .estag.danger.type")
    result.game = "CS2" if "csgo" in classes else None
    date_node = root.select_one("time[datetime]") or soup.select_one("time[datetime]")
    result.starts_at = (date_node.get("datetime") if date_node else None) or _jsonld_datetime(soup)
    # The visible page date is the intended local kickoff. In the captured
    # upcoming HTML, attr-start encodes that same wall-clock value as if it were
    # UTC, so using it as a true UTC instant would shift Discord display by +2h.
    date_input = root.select_one("input.time-datepicker-match-start[value]")
    date_text = (date_input.get("value") if date_input else None) or result.raw_status
    parsed_start = parse_datetime(date_text)
    if parsed_start:
        result.starts_at = parsed_start.isoformat()
    else:
        roster = root.select_one(".roster_info[attr-start]")
        if roster and roster.get("attr-start"):
            try:
                timestamp_as_wall_clock = datetime.fromtimestamp(int(roster["attr-start"]), timezone.utc)
                result.starts_at = timestamp_as_wall_clock.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc).isoformat()
            except (ValueError, OSError, OverflowError):
                pass
    # The upcoming template stores format and game in labeled setting fields.
    for field_node in root.select(".match_settings .field"):
        label = (text(field_node, ".label") or "").casefold()
        value = text(field_node, ".value")
        if label == "type" and value:
            result.format = value
        elif label == "spil" and value:
            result.game = value

    for card in root.select(".single_match_map"):
        title = card.select_one(".map_title")
        body = card.select_one(".single_match_map_body")
        teams = card.select(".fighters > .team") if body else []
        score1 = as_int(text(teams[0], ".score")) if len(teams) > 0 else None
        score2 = as_int(text(teams[1], ".score")) if len(teams) > 1 else None
        status = text(card, ".ongoing_server_status")
        finished_score = card.select_one(".score_finished_tag")
        finished = "finished" in card.get("class", []) or finished_score is not None
        if finished_score:
            score1 = as_int(text(finished_score, ".score_team1"))
            score2 = as_int(text(finished_score, ".score_team2"))
        map_state = "finished" if finished else status
        rounds = as_int(text(body, ".rounds")) if body else None
        winner = None
        if finished and score1 is not None and score2 is not None:
            winner = result.team1.name if score1 > score2 else result.team2.name if score2 > score1 else None
        result.maps.append(MapInfo(
            key=title.get("attr-map") if title else None,
            name=text(title) or "Unknown map",
            status=map_state,
            rounds=rounds,
            score1=score1,
            score2=score2,
            winner=winner,
        ))
    if state == "unknown" and result.starts_at:
        start = parse_datetime(result.starts_at)
        if start and start > datetime.now(timezone.utc):
            result.state = "upcoming"
    return result


def read_urls() -> list[str]:
    if not URLS_FILE.exists():
        log.error("Match URL file is missing: %s", URLS_FILE)
        return []
    urls = []
    for line in URLS_FILE.read_text(encoding="utf-8").splitlines():
        candidate = line.strip()
        if candidate.startswith("https://esportligaen.dk/kampe/"):
            urls.append(candidate.rstrip("/"))
    return list(dict.fromkeys(urls))


class Store:
    def __init__(self, path: Path):
        import sqlite3
        self.sqlite3 = sqlite3
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS snapshots (match_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS poll_checks (match_id TEXT PRIMARY KEY, checked_at TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS match_messages (match_id TEXT PRIMARY KEY, channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL, content TEXT NOT NULL, updated_at TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (event_key TEXT PRIMARY KEY, match_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL)")
        self.db.commit()

    def previous(self, match_id: str) -> Match | None:
        row = self.db.execute("SELECT payload FROM snapshots WHERE match_id=?", (match_id,)).fetchone()
        if not row:
            return None
        return Match(**self._match_kwargs(json.loads(row[0])))

    @staticmethod
    def _match_kwargs(data: dict[str, Any]) -> dict[str, Any]:
        data["team1"] = Team(**data.get("team1", {}))
        data["team2"] = Team(**data.get("team2", {}))
        data["maps"] = [MapInfo(**item) for item in data.get("maps", [])]
        data["player_ratings"] = [PlayerRating(**item) for item in data.get("player_ratings", [])]
        return data

    def save_snapshot(self, match: Match) -> None:
        self.db.execute(
            "INSERT INTO snapshots VALUES (?, ?, ?) ON CONFLICT(match_id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
            (match.match_id, json.dumps(match.serializable(), ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
        )
        self.db.commit()

    def last_poll_at(self, match_id: str) -> str | None:
        row = self.db.execute("SELECT checked_at FROM poll_checks WHERE match_id=?", (match_id,)).fetchone()
        return row[0] if row else None

    def mark_polled(self, match_id: str) -> None:
        self.db.execute(
            "INSERT INTO poll_checks VALUES (?, ?) ON CONFLICT(match_id) DO UPDATE SET checked_at=excluded.checked_at",
            (match_id, datetime.now(timezone.utc).isoformat()),
        )
        self.db.commit()

    def reconcile(self, match_ids: set[str]) -> int:
        existing: set[str] = set()
        for table in ("snapshots", "poll_checks", "match_messages", "events"):
            existing.update(row[0] for row in self.db.execute(f"SELECT DISTINCT match_id FROM {table}").fetchall())
        removed = existing - match_ids
        if not removed:
            return 0
        placeholders = ",".join("?" for _ in removed)
        values = tuple(removed)
        for table in ("snapshots", "poll_checks", "match_messages", "events"):
            self.db.execute(f"DELETE FROM {table} WHERE match_id IN ({placeholders})", values)
        self.db.commit()
        return len(removed)

    def emit_once(self, key: str, match_id: str, kind: str, payload: dict[str, Any]) -> bool:
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?)",
            (key, match_id, kind, json.dumps(payload, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
        )
        self.db.commit()
        return cursor.rowcount == 1

    def results(self, match_ids: set[str], limit: int = 10) -> list[tuple[dict[str, Any], str]]:
        if not match_ids:
            return []
        placeholders = ",".join("?" for _ in match_ids)
        rows = self.db.execute(
            f"SELECT payload, updated_at FROM snapshots WHERE match_id IN ({placeholders}) AND json_extract(payload, '$.state')='finished' ORDER BY updated_at DESC LIMIT ?",
            (*match_ids, limit),
        ).fetchall()
        return [(json.loads(payload), when) for payload, when in rows]

    def message_record(self, match_id: str) -> tuple[int, int, str] | None:
        row = self.db.execute("SELECT channel_id, message_id, content FROM match_messages WHERE match_id=?", (match_id,)).fetchone()
        return (int(row[0]), int(row[1]), row[2]) if row else None

    def save_message_record(self, match_id: str, channel_id: int, message_id: int, content: str) -> None:
        self.db.execute(
            "INSERT INTO match_messages VALUES (?, ?, ?, ?, ?) ON CONFLICT(match_id) DO UPDATE SET channel_id=excluded.channel_id, message_id=excluded.message_id, content=excluded.content, updated_at=excluded.updated_at",
            (match_id, channel_id, message_id, content, datetime.now(timezone.utc).isoformat()),
        )
        self.db.commit()


def event_key(match: Match, kind: str, suffix: str = "") -> str:
    return f"{match.match_id}:{kind}:{suffix}"


def started_event(match: Match, old: Match | None) -> bool:
    return match.state in {"veto", "live"} and (old is None or old.state not in {"veto", "live"})


def halftime_event(match: Match, old: Match | None) -> bool:
    if match.state != "live":
        return False
    old_maps = {m.key or m.name: m for m in (old.maps if old else [])}
    for m in match.maps:
        prev = old_maps.get(m.key or m.name)
        # A map reaches halftime after its 12th first-half round. Detect crossing,
        # rather than equality, so one missed poll still generates the alert.
        now_total = (m.score1 or 0) + (m.score2 or 0)
        old_total = ((prev.score1 or 0) + (prev.score2 or 0)) if prev else 0
        if m.score1 is not None and m.score2 is not None and now_total >= 12 and old_total < 12:
            return True
    return False


def map_finished_events(match: Match, old: Match | None) -> list[MapInfo]:
    old_done = {
        (m.name, m.score1, m.score2) for m in (old.maps if old else [])
        if m.status == "finished" and m.score1 is not None and m.score2 is not None
    }
    completed = [m for m in match.maps if m.status == "finished"]
    if match.state == "finished":
        completed = match.maps
    return [
        m for m in completed
        if m.score1 is not None and m.score2 is not None
        and (m.name, m.score1, m.score2) not in old_done
    ]


def fmt_series(match: Match) -> str:
    if match.series1 is None or match.series2 is None:
        return "series score unavailable"
    return f"{match.team1.name} {match.series1}–{match.series2} {match.team2.name}"


def render_status_message(match: Match) -> str:
    link = match.url
    if match.state == "finished":
        map_lines = "\n".join(f"• {item.name}: {item.score1}–{item.score2}" for item in match.maps)
        mvp = ""
        if match.mvp:
            rating = f" | Rating: {match.mvp_rating}" if match.mvp_rating else ""
            mvp = f"\nMVP: **{match.mvp}**{rating}"
        details = f"\n{map_lines}" if map_lines else ""
        return f"# 🏆 Final result: {fmt_series(match)}{details}{mvp}\n{link}"

    if match.state == "veto":
        return f"# 🗳️ Veto in progress: {match.team1.name} vs {match.team2.name}\n{link}"

    if match.state == "live":
        rows: list[str] = []
        for item in match.maps:
            if item.score1 is not None and item.score2 is not None:
                score = f"{item.score1}–{item.score2}"
                if item.status == "finished":
                    rows.append(f"• ✅ **{item.name}**: {score} — map finished")
                else:
                    rounds = f" (round {item.rounds})" if item.rounds is not None else ""
                    rows.append(f"• 🔴 **{item.name}**: {score}{rounds}")
            elif item.status:
                rows.append(f"• ⏳ **{item.name}**: {item.status}")
            else:
                rows.append(f"• ⏳ **{item.name}**: waiting to start")
        maps = "\n".join(rows) if rows else "Live score is not available yet."
        return f"# 🔴 Live: {match.team1.name} vs {match.team2.name}\n{maps}\n{link}"

    start = parse_datetime(match.starts_at)
    if start:
        when = f"<t:{int(start.timestamp())}:F> (<t:{int(start.timestamp())}:R>)"
    else:
        when = "start time unavailable"
    return f"# ⏰ Match reminder: {match.team1.name} vs {match.team2.name}\nStarts {when}\n{link}"


class MatchBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.store = Store(DB_PATH)
        self.scraper_session: aiohttp.ClientSession | None = None

    async def setup_hook(self) -> None:
        timeout = aiohttp.ClientTimeout(total=20, connect=10)
        self.scraper_session = aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "CS2MatchBot/0.1 (match status monitor)"})
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced slash commands to development guild %s", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Synced global slash commands (may take time to appear)")
        self.poll_matches.start()

    async def close(self) -> None:
        self.poll_matches.cancel()
        if self.scraper_session:
            await self.scraper_session.close()
        self.store.db.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Connected as %s (%s)", self.user, self.user.id if self.user else "?")

    async def fetch_match(self, url: str) -> Match | None:
        assert self.scraper_session
        try:
            async with self.scraper_session.get(url, allow_redirects=True) as response:
                response.raise_for_status()
                html = await response.text()
            return parse_match(url, html)
        except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeError) as exc:
            log.warning("Could not fetch %s: %s", url, exc)
            return None

    async def announce(self, content: str) -> None:
        if not CHANNEL_ID:
            log.warning("DISCORD_CHANNEL_ID is unset; announcement skipped: %s", content)
            return
        channel = self.get_channel(CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.fetch_channel(CHANNEL_ID)
            except discord.HTTPException as exc:
                log.error("Unable to access announcement channel %s: %s", CHANNEL_ID, exc)
                return
        if hasattr(channel, "send"):
            await channel.send(content, suppress_embeds=True, allowed_mentions=discord.AllowedMentions.none())

    def poll_due(self, cached: Match | None, updated_at: str | None) -> bool:
        if updated_at is None:
            return True
        if cached and cached.state == "finished":
            return False
        last_checked = parse_datetime(updated_at)
        if last_checked is None:
            return True
        now = datetime.now(timezone.utc)
        if cached is None:
            return now - last_checked >= timedelta(hours=SCHEDULE_REFRESH_HOURS)
        if cached.state in {"veto", "live"}:
            return now - last_checked >= timedelta(seconds=POLL_INTERVAL)

        start = parse_datetime(cached.starts_at)
        if start:
            active_from = start - timedelta(minutes=ACTIVE_WINDOW_BEFORE_MINUTES)
            active_until = start + timedelta(hours=ACTIVE_WINDOW_AFTER_HOURS)
            if active_from <= now <= active_until:
                return now - last_checked >= timedelta(seconds=POLL_INTERVAL)
        return now - last_checked >= timedelta(hours=SCHEDULE_REFRESH_HOURS)

    async def update_status_message(self, match: Match, content: str) -> None:
        if not CHANNEL_ID:
            log.warning("DISCORD_CHANNEL_ID is unset; status message skipped for %s", match.match_id)
            return
        channel = self.get_channel(CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.fetch_channel(CHANNEL_ID)
            except discord.HTTPException as exc:
                log.error("Unable to access announcement channel %s: %s", CHANNEL_ID, exc)
                return
        if not hasattr(channel, "send"):
            log.error("Configured channel %s cannot receive messages", CHANNEL_ID)
            return

        saved = self.store.message_record(match.match_id)
        if saved and saved[0] == CHANNEL_ID:
            _, message_id, old_content = saved
            if old_content == content:
                return
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(content=content, suppress=True, allowed_mentions=discord.AllowedMentions.none())
                self.store.save_message_record(match.match_id, CHANNEL_ID, message.id, content)
                return
            except discord.NotFound:
                log.info("Stored status post for %s was deleted; creating a replacement", match.match_id)
            except discord.HTTPException as exc:
                log.error("Unable to edit status post for %s: %s", match.match_id, exc)
                return

        try:
            message = await channel.send(content, suppress_embeds=True, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            log.error("Unable to create status post for %s: %s", match.match_id, exc)
            return
        self.store.save_message_record(match.match_id, CHANNEL_ID, message.id, content)

    async def process_match(self, match: Match) -> None:
        start = parse_datetime(match.starts_at)
        now = datetime.now(timezone.utc)
        is_reminder_window = bool(start and start > now and now >= start - timedelta(minutes=30))
        has_existing_post = self.store.message_record(match.match_id) is not None
        should_post = match.state in {"veto", "live", "finished"} or (match.state == "upcoming" and (is_reminder_window or has_existing_post))
        if should_post:
            await self.update_status_message(match, render_status_message(match))
        self.store.save_snapshot(match)

    # Check often enough to honor the configured 25-second active poll cadence.
    @tasks.loop(seconds=5)
    async def poll_matches(self) -> None:
        for url in read_urls():
            match_id = match_id_from_url(url)
            cached = self.store.previous(match_id)
            # A URL with no poll-check record is fetched immediately, including
            # on the first run after this scheduler is introduced.
            last_checked = self.store.last_poll_at(match_id)
            if not self.poll_due(cached, last_checked):
                continue
            match = await self.fetch_match(url)
            self.store.mark_polled(match_id)
            if not match or match.state == "unknown":
                continue
            await self.process_match(match)

    @poll_matches.before_loop
    async def before_poll(self) -> None:
        await self.wait_until_ready()


bot = MatchBot()


@bot.tree.command(name="matches", description="Show upcoming matches from the tracked match list")
async def matches_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    upcoming: list[Match] = []
    for url in read_urls():
        match = await bot.fetch_match(url)
        if not match or match.state == "finished":
            continue
        start = parse_datetime(match.starts_at)
        # Matches without a parseable date are omitted; guessed dates are worse
        # than explaining that the page format needs a parser update.
        if start and start >= datetime.now(timezone.utc):
            upcoming.append(match)
    upcoming.sort(key=lambda m: parse_datetime(m.starts_at) or datetime.max.replace(tzinfo=timezone.utc))
    if not upcoming:
        await interaction.followup.send("No upcoming matches with a readable start time were found. The tracked pages may not expose their schedule in the current HTML format.", ephemeral=True)
        return
    lines = []
    for match in upcoming[:15]:
        start = parse_datetime(match.starts_at)
        when = f"<t:{int(start.timestamp())}:F>" if start else "time unknown"
        state = {"veto": "Veto", "live": "Live", "upcoming": "Scheduled", "unknown": "Scheduled"}.get(match.state, match.state.title())
        lines.append(f"• **{match.team1.name} vs {match.team2.name}** — {when} · {state} · {match.format or 'CS2'}\n  {match.url}")
    await interaction.followup.send("**Upcoming matches**\n" + "\n".join(lines), ephemeral=True, suppress_embeds=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="refresh", description="Force a fresh scrape of every tracked match page")
async def refresh_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    urls = read_urls()
    removed = bot.store.reconcile({match_id_from_url(url) for url in urls})
    refreshed = 0
    failed = 0
    changed: list[str] = []
    for url in urls:
        match_id = match_id_from_url(url)
        old = bot.store.previous(match_id)
        match = await bot.fetch_match(url)
        bot.store.mark_polled(match_id)
        if not match or match.state == "unknown":
            failed += 1
            continue
        if old and old.starts_at != match.starts_at and match.starts_at:
            old_start = parse_datetime(old.starts_at)
            new_start = parse_datetime(match.starts_at)
            old_text = f"<t:{int(old_start.timestamp())}:f>" if old_start else "unknown"
            new_text = f"<t:{int(new_start.timestamp())}:f>" if new_start else "unknown"
            changed.append(f"• **{match.team1.name} vs {match.team2.name}**: {old_text} → {new_text}")
        await bot.process_match(match)
        refreshed += 1

    summary = f"Refreshed {refreshed} of {len(urls)} tracked matches."
    if removed:
        summary += f" Stopped tracking and cleared stored state for {removed} removed match(es). Existing Discord posts remain as history."
    if failed:
        summary += f" {failed} page(s) could not be read."
    if changed:
        summary += "\nUpdated kickoff time(s):\n" + "\n".join(changed)
    elif refreshed:
        summary += " No kickoff-time changes were found."
    await interaction.followup.send(summary, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="results", description="Show recently completed tracked matches")
async def results_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    urls = read_urls()
    tracked_ids = {match_id_from_url(url) for url in urls}
    bot.store.reconcile(tracked_ids)
    # Refresh first so results do not depend on the background loop having run.
    for url in urls:
        match = await bot.fetch_match(url)
        if match and match.state == "finished":
            bot.store.save_snapshot(match)
    results = bot.store.results(tracked_ids, 10)
    if not results:
        await interaction.followup.send("No finished matches have been found in the tracked URL list yet.", ephemeral=True)
        return
    lines = []
    for payload, _ in results:
        maps = payload.get("maps", [])
        map_scores = " · ".join(f"{m.get('name')}: {m.get('score1')}–{m.get('score2')}" for m in maps)
        score1, score2 = payload.get("series1"), payload.get("series2")
        mvp = payload.get("mvp")
        rating = payload.get("mvp_rating")
        mvp_line = f"\n  MVP: {mvp}" if mvp else ""
        if mvp and rating:
            mvp_line = f"\n  MVP: {mvp} | Rating: {rating}"
        lines.append(f"• **{payload['team1']['name']} {score1}–{score2} {payload['team2']['name']}**\n  {map_scores or 'Map scores unavailable'}{mvp_line}\n  {payload['url']}")
    await interaction.followup.send("**Recent results**\n" + "\n".join(lines), ephemeral=True, suppress_embeds=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="scoreboard", description="Show average EsR ratings for tracked players")
async def scoreboard_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    players = read_players()
    if not players:
        await interaction.followup.send("No players were found in `data/players.txt`. Add one profile URL and display name per line.", ephemeral=True)
        return

    profile_keys = {canonical_profile_url(profile): (profile, nickname) for profile, nickname in players}
    played: dict[str, set[str]] = {key: set() for key in profile_keys}
    ratings: dict[str, list[float]] = {key: [] for key in profile_keys}
    for url in read_urls():
        match = await bot.fetch_match(url)
        if not match or match.state != "finished":
            continue
        for player in match.player_ratings:
            key = canonical_profile_url(player.profile_url)
            if key not in profile_keys:
                continue
            played[key].add(match.match_id)
            value = as_float(player.esr)
            if value is not None:
                ratings[key].append(value)

    rows = []
    for key, (_, nickname) in profile_keys.items():
        count = len(played[key])
        average = sum(ratings[key]) / len(ratings[key]) if ratings[key] else None
        average_text = f"{average:.2f}".replace(".", ",") if average is not None else "—"
        rows.append((count == 0, -(average if average is not None else 0), nickname.casefold(), nickname, average_text, count))
    rows.sort()
    lines = [
        f"{index}. **{nickname}** — EsR avg **{average}** · {count} {'match' if count == 1 else 'matches'} played"
        for index, (_, _, _, nickname, average, count) in enumerate(rows, start=1)
    ]
    await interaction.followup.send("# 🏆 Player scoreboard\n" + "\n".join(lines), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in a .env file. See .env.example and README.md.")
    bot.run(TOKEN)
