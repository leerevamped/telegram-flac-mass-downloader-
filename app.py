import asyncio
import json
import os
import re
import sqlite3
import subprocess
import unicodedata
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from telethon import TelegramClient
from telethon.errors import FloodWaitError, SessionPasswordNeededError

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DOWNLOAD_DIR = APP_DIR / "downloads"
SESSION_PATH = APP_DIR / "song_downloader_session"
HISTORY_DB = APP_DIR / "history.db"


# #region debug-point Z:dbg-emit
def _dbg_emit(*, hypothesis_id: str, msg: str, data: dict, location: str, run_id: str = "pre-fix"):
    try:
        import urllib.request
        env_path = APP_DIR / ".dbg" / "telegram-verification-code.env"
        url = "http://127.0.0.1:7777/event"
        session_id = "telegram-verification-code"
        try:
            content = env_path.read_text(encoding="utf-8")
            for line in content.splitlines():
                if line.startswith("DEBUG_SERVER_URL="):
                    url = line.split("=", 1)[1].strip() or url
                elif line.startswith("DEBUG_SESSION_ID="):
                    session_id = line.split("=", 1)[1].strip() or session_id
        except Exception:
            pass
        urllib.request.urlopen(
            urllib.request.Request(
                url,
                data=json.dumps(
                    {
                        "sessionId": session_id,
                        "runId": run_id,
                        "hypothesisId": hypothesis_id,
                        "location": location,
                        "msg": f"[DEBUG] {msg}",
                        "data": data or {},
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            timeout=2,
        ).read()
    except Exception:
        pass


# #endregion

SEARCH_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 180
DELAY_BETWEEN_SONGS = 5
POLL_INTERVAL = 1.0

# V3 is intentionally conservative. Weak matches are marked Not Found instead
# of being clicked and downloaded.
MIN_STRICT_SCORE = 0.72
MIN_QUERY_RECALL = 0.72
MIN_COMPONENT_SCORE = 0.60

app = FastAPI(title="Telegram Song Downloader V3 Final")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
META_WORDS = {
    "official", "audio", "video", "lyrics", "lyric", "music", "hq", "hd",
    "flac", "mp3", "aac", "m4a", "kbps", "download", "track", "song",
}
NAV_LABELS = {
    "next", "previous", "prev", "back", "cancel", "close", "menu", "search",
    "search again", "more", "refresh", "home", "help",
}
NOT_FOUND_PHRASES = (
    "not found", "no result", "no results", "nothing found", "couldn't find",
    "could not find", "can't find", "cannot find", "0 results", "zero results",
)


def strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def remove_bracketed(text: str) -> str:
    # Album/source labels in pasted lists are often parenthetical. Ignoring them
    # makes matching title + artist safer and less brittle.
    return re.sub(r"\([^)]*\)|\[[^]]*\]|\{[^}]*\}", " ", text)


def normalize(text: str, *, drop_brackets: bool = True) -> str:
    text = strip_accents(text or "").lower()
    if drop_brackets:
        text = remove_bracketed(text)
    text = text.replace("&", " and ")
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    words = [w for w in text.split() if w not in META_WORDS]
    return " ".join(words)


def tokens(text: str) -> list[str]:
    return [w for w in normalize(text).split() if len(w) >= 2]


def token_metrics(query: str, candidate: str):
    q = set(tokens(query))
    c = set(tokens(candidate))
    if not q or not c:
        return 0.0, 0.0, 0.0
    overlap = len(q & c)
    recall = overlap / len(q)
    precision = overlap / len(c)
    f1 = 0.0 if recall + precision == 0 else 2 * recall * precision / (recall + precision)
    return recall, precision, f1


def split_components(text: str) -> list[str]:
    # Song lists commonly use either "Artist - Title" or "Title - Artist".
    # Treat the two sides as unordered components so both formats work.
    cleaned = remove_bracketed(text or "")
    parts = re.split(r"\s+(?:-|–|—|\|)\s+", cleaned, maxsplit=1)
    parts = [p.strip() for p in parts if normalize(p)]
    return parts[:2]


def text_similarity(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    recall, _, f1 = token_metrics(a, b)
    seq = SequenceMatcher(None, na, nb).ratio()
    return 0.55 * recall + 0.25 * f1 + 0.20 * seq


def component_similarity(query: str, candidate: str) -> float:
    qp = split_components(query)
    cp = split_components(candidate)
    if len(qp) < 2:
        return text_similarity(query, candidate)

    # If candidate is not visibly split into two parts, compare each query side
    # against the whole label. Otherwise try both component orders.
    if len(cp) < 2:
        return (text_similarity(qp[0], candidate) + text_similarity(qp[1], candidate)) / 2

    same = (text_similarity(qp[0], cp[0]) + text_similarity(qp[1], cp[1])) / 2
    reverse = (text_similarity(qp[0], cp[1]) + text_similarity(qp[1], cp[0])) / 2
    return max(same, reverse)


def score_candidate(query: str, candidate: str) -> dict:
    recall, precision, f1 = token_metrics(query, candidate)
    component = component_similarity(query, candidate)
    seq = SequenceMatcher(None, normalize(query), normalize(candidate)).ratio() if normalize(query) and normalize(candidate) else 0.0

    # Query recall is weighted strongly: a result should contain most of what the
    # user asked for, even if the bot adds quality/size/emoji text.
    score = 0.45 * recall + 0.25 * f1 + 0.20 * component + 0.10 * seq

    qp = split_components(query)
    component_ok = True
    if len(qp) == 2:
        # Both title and artist sides must meaningfully appear somewhere.
        side_scores = [text_similarity(side, candidate) for side in qp]
        component_ok = min(side_scores) >= MIN_COMPONENT_SCORE
    else:
        side_scores = [text_similarity(query, candidate)]

    accepted = (
        score >= MIN_STRICT_SCORE
        and recall >= MIN_QUERY_RECALL
        and component_ok
    )

    # Exact normalized containment is a safe fast-pass when one side simply has
    # extra bot decoration such as file size or quality.
    nq, nc = normalize(query), normalize(candidate)
    if nq and nc and (nq in nc or nc in nq):
        accepted = recall >= MIN_QUERY_RECALL
        score = max(score, 0.90)

    return {
        "score": round(score, 4),
        "recall": round(recall, 4),
        "precision": round(precision, 4),
        "f1": round(f1, 4),
        "component": round(component, 4),
        "component_ok": component_ok,
        "side_scores": [round(x, 4) for x in side_scores],
        "accepted": accepted,
    }


def is_navigation_label(label: str) -> bool:
    n = normalize(label, drop_brackets=False)
    if not n:
        return True
    if n in NAV_LABELS:
        return True
    if re.fullmatch(r"(?:page\s*)?\d+", n):
        return True
    return False


def button_candidate(row_index: int, column_index: int, btn, query: str):
    label = (getattr(btn, "text", "") or "").strip()
    if not label or is_navigation_label(label):
        return None
    metrics = score_candidate(query, label)
    return {
        "row": row_index,
        "column": column_index,
        "label": label,
        **metrics,
    }


def first_button(buttons, query):
    """Return the first song-like inline button exactly in the bot's display order."""
    for row_index, row in enumerate(buttons or []):
        for column_index, btn in enumerate(row):
            candidate = button_candidate(row_index, column_index, btn, query)
            if candidate is not None:
                return candidate
    return None


def best_button(buttons, query):
    """Return the highest-scoring song-like inline button."""
    best = None
    for row_index, row in enumerate(buttons or []):
        for column_index, btn in enumerate(row):
            candidate = button_candidate(row_index, column_index, btn, query)
            if candidate is None:
                continue
            if best is None or candidate["score"] > best["score"]:
                best = candidate
    return best


def is_audio_message(message) -> bool:
    if not message or not getattr(message, "media", None):
        return False
    if getattr(message, "audio", None):
        return True
    file_obj = getattr(message, "file", None)
    if not file_obj:
        return False
    mime = (getattr(file_obj, "mime_type", "") or "").lower()
    name = (getattr(file_obj, "name", "") or "").lower()
    return mime.startswith("audio/") or name.endswith((".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus"))


# ---------------------------------------------------------------------------
# History database
# ---------------------------------------------------------------------------
def db_connect():
    conn = sqlite3.connect(HISTORY_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                requested_song TEXT NOT NULL,
                matched_label TEXT,
                match_score REAL,
                status TEXT NOT NULL,
                detail TEXT,
                saved_path TEXT,
                target_bot TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_status ON history(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_created ON history(created_at DESC)")


def add_history(item, job_id: str, target_bot: str):
    with db_connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO history
            (job_id, requested_song, matched_label, match_score, status, detail, saved_path, target_bot, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                item.song,
                item.matched_label,
                item.match_score,
                item.status,
                item.detail,
                item.saved_path,
                target_bot,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cur.lastrowid


def query_history(status: str = "all", q: str = "", limit: int = 500):
    sql = "SELECT * FROM history WHERE 1=1"
    params = []
    if status and status != "all":
        sql += " AND status = ?"
        params.append(status)
    if q:
        sql += " AND (requested_song LIKE ? OR matched_label LIKE ? OR detail LIKE ?)"
        like = f"%{q}%"
        params.extend([like, like, like])
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with db_connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        counts = dict(conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done,
              SUM(CASE WHEN status='not_found' THEN 1 ELSE 0 END) AS not_found,
              SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
            FROM history
            """
        ).fetchone())
    return rows, {k: int(v or 0) for k, v in counts.items()}


# ---------------------------------------------------------------------------
# Models / manager
# ---------------------------------------------------------------------------
@dataclass
class SongProgress:
    song: str
    status: str = "queued"
    detail: str = ""
    saved_path: Optional[str] = None
    matched_label: Optional[str] = None
    match_score: Optional[float] = None
    history_id: Optional[int] = None


class ConnectRequest(BaseModel):
    api_id: int
    api_hash: str
    phone: str
    target_bot: str


class VerifyRequest(BaseModel):
    code: str


class PasswordRequest(BaseModel):
    password: str


class StartRequest(BaseModel):
    songs: str
    selection_mode: str = "first_result"


class DownloaderManager:
    def __init__(self):
        self.client: Optional[TelegramClient] = None
        self.phone: Optional[str] = None
        self.target_bot: Optional[str] = None
        self.phone_code_hash: Optional[str] = None
        self.sent_code_info: dict = {}
        self.authorized = False
        self.running = False
        self.stop_requested = False
        self.items: list[SongProgress] = []
        self.downloaded = 0
        self.not_found = 0
        self.failed = 0
        self.stopped = 0
        self.current = ""
        self.last_error = ""
        self.job_id = ""
        self.selection_mode = "first_result"
        self._task: Optional[asyncio.Task] = None

    def clean_target_bot(self, value: str) -> str:
        value = (value or "").strip()
        m = re.search(r"(?:https?://)?t\.me/([A-Za-z0-9_]+)", value)
        if m:
            value = "@" + m.group(1)
        if not value:
            raise RuntimeError("Enter the Telegram bot username, for example @example_bot.")
        if " " in value:
            raise RuntimeError("Use the bot's @username, not its display name. Open the bot profile and copy the username that starts with @.")
        if not value.startswith("@"):
            value = "@" + value
        if not re.fullmatch(r"@[A-Za-z0-9_]{5,}", value):
            raise RuntimeError("That does not look like a Telegram @username.")
        return value

    async def reset_client(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.client = None
        self.authorized = False
        self.phone_code_hash = None
        self.sent_code_info = {}

    def describe_sent_code(self, sent) -> dict:
        code_type_name = type(getattr(sent, "type", None)).__name__
        next_type_name = type(getattr(sent, "next_type", None)).__name__ if getattr(sent, "next_type", None) else ""

        friendly = {
            "SentCodeTypeApp": "Telegram app on an already signed-in device",
            "SentCodeTypeSms": "SMS",
            "SentCodeTypeCall": "phone call",
            "SentCodeTypeFlashCall": "flash call",
            "SentCodeTypeMissedCall": "missed call",
            "SentCodeTypeEmailCode": "email",
            "SentCodeTypeFragmentSms": "Fragment SMS",
            "SentCodeTypeFirebaseSms": "SMS / device verification",
            "SentCodeTypeSetUpEmailRequired": "email setup required",
        }.get(code_type_name, code_type_name or "Telegram")

        next_friendly = {
            "SentCodeTypeApp": "Telegram app",
            "SentCodeTypeSms": "SMS",
            "SentCodeTypeCall": "phone call",
            "SentCodeTypeFlashCall": "flash call",
            "SentCodeTypeMissedCall": "missed call",
            "SentCodeTypeEmailCode": "email",
            "SentCodeTypeFragmentSms": "Fragment SMS",
            "SentCodeTypeFirebaseSms": "SMS / device verification",
        }.get(next_type_name, next_type_name)

        return {
            "delivery": friendly,
            "delivery_type": code_type_name,
            "next_delivery": next_friendly,
            "timeout": int(getattr(sent, "timeout", 0) or 0),
        }

    async def request_login_code(self, *, resend: bool = False):
        if not self.client or not self.phone:
            raise RuntimeError("Start the sign-in process first.")

        # send_code_request returns Telegram's actual delivery method. Keeping
        # phone_code_hash explicitly makes verification less dependent on
        # Telethon's in-memory cache and is more reliable across UI requests.
        _dbg_emit(
            hypothesis_id="A",
            msg="request_login_code entry",
            location="app.py:DownloaderManager.request_login_code",
            data={
                "resend": resend,
                "has_client": bool(self.client),
                "phone_mask": ("…" + self.phone[-2:]) if self.phone else None,
                "target_bot": self.target_bot,
            },
        )
        sent = await self.client.send_code_request(self.phone)
        self.phone_code_hash = getattr(sent, "phone_code_hash", None)
        self.sent_code_info = self.describe_sent_code(sent)
        _dbg_emit(
            hypothesis_id="B",
            msg="send_code_request ok",
            location="app.py:DownloaderManager.request_login_code",
            data={
                "delivery_type": self.sent_code_info.get("delivery_type"),
                "delivery": self.sent_code_info.get("delivery"),
                "next_delivery": self.sent_code_info.get("next_delivery"),
                "timeout": self.sent_code_info.get("timeout"),
                "phone_code_hash_len": len(self.phone_code_hash or ""),
            },
        )
        return {
            "state": "need_code",
            "target_bot": self.target_bot,
            **self.sent_code_info,
            "resend": resend,
        }

    async def validate_target_bot(self):
        if not self.client or not self.target_bot:
            raise RuntimeError("Telegram is not connected.")
        try:
            entity = await self.client.get_entity(self.target_bot)
        except Exception as exc:
            raise RuntimeError(f"Could not find {self.target_bot}. Check the bot's exact @username. ({type(exc).__name__})") from exc
        if not getattr(entity, "bot", False):
            raise RuntimeError(f"{self.target_bot} resolves to an account, but it does not appear to be a bot.")
        return entity

    async def connect(self, api_id: int, api_hash: str, phone: str, target_bot: str):
        if self.running:
            raise RuntimeError("Stop the current download job before changing Telegram accounts.")
        _dbg_emit(
            hypothesis_id="A",
            msg="connect start",
            location="app.py:DownloaderManager.connect",
            data={
                "api_id": api_id,
                "api_hash_len": len(api_hash or ""),
                "phone_mask": ("…" + phone.strip()[-2:]) if phone else None,
                "target_bot_input": (target_bot or "").strip(),
            },
        )
        await self.reset_client()
        self.phone = phone.strip()
        self.target_bot = self.clean_target_bot(target_bot)
        self.client = TelegramClient(str(SESSION_PATH), api_id, api_hash)
        await self.client.connect()
        _dbg_emit(
            hypothesis_id="D",
            msg="client connected",
            location="app.py:DownloaderManager.connect",
            data={"session_path": str(SESSION_PATH), "target_bot": self.target_bot},
        )

        if await self.client.is_user_authorized():
            await self.validate_target_bot()
            self.authorized = True
            _dbg_emit(
                hypothesis_id="D",
                msg="already authorized (no code requested)",
                location="app.py:DownloaderManager.connect",
                data={"target_bot": self.target_bot},
            )
            return {"state": "authorized", "target_bot": self.target_bot}

        return await self.request_login_code()

    async def verify_code(self, code: str):
        if not self.client or not self.phone:
            raise RuntimeError("Start the sign-in process first.")
        try:
            await self.client.sign_in(phone=self.phone, code=code.strip(), phone_code_hash=self.phone_code_hash)
            await self.validate_target_bot()
            self.authorized = True
            return {"state": "authorized", "target_bot": self.target_bot}
        except SessionPasswordNeededError:
            return {"state": "need_password"}

    async def verify_password(self, password: str):
        if not self.client:
            raise RuntimeError("Start the sign-in process first.")
        await self.client.sign_in(password=password)
        await self.validate_target_bot()
        self.authorized = True
        return {"state": "authorized", "target_bot": self.target_bot}

    async def resend_login_code(self):
        if self.authorized:
            return {"state": "authorized", "target_bot": self.target_bot}
        return await self.request_login_code(resend=True)

    async def forget_local_session(self):
        if self.running:
            raise RuntimeError("Stop the current download job first.")
        await self.reset_client()
        # Telethon appends .session to SESSION_PATH.
        candidates = [
            Path(str(SESSION_PATH) + ".session"),
            Path(str(SESSION_PATH) + ".session-journal"),
            Path(str(SESSION_PATH) + ".session-wal"),
            Path(str(SESSION_PATH) + ".session-shm"),
        ]
        removed = False
        for path in candidates:
            try:
                if path.exists():
                    path.unlink()
                    removed = True
            except OSError:
                pass
        self.phone = None
        self.target_bot = None
        return {"ok": True, "removed": removed}

    async def _recent_messages(self, limit: int = 30):
        return await self.client.get_messages(self.target_bot, limit=limit)

    async def _wait_for_buttons(self, after_message_id: int, item: SongProgress):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SEARCH_TIMEOUT
        latest_text = ""

        while loop.time() < deadline:
            if self.stop_requested:
                return None, latest_text, False

            messages = await self._recent_messages(limit=30)
            for msg in messages:
                if msg.id > after_message_id:
                    text = (getattr(msg, "message", "") or "").strip()
                    if text:
                        latest_text = text
                    if any(p in text.lower() for p in NOT_FOUND_PHRASES):
                        return None, latest_text, True
                    if getattr(msg, "buttons", None):
                        return msg, latest_text, False

            item.status = "waiting"
            item.detail = (
                f"Waiting for search results… Bot says: {latest_text[:140]}"
                if latest_text else
                "Waiting for the bot to finish searching and show results…"
            )
            await asyncio.sleep(POLL_INTERVAL)

        return None, latest_text, False

    async def _wait_for_audio(self, baseline_audio_ids: set[int], item: SongProgress):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DOWNLOAD_TIMEOUT
        latest_text = ""

        while loop.time() < deadline:
            if self.stop_requested:
                return None

            messages = await self._recent_messages(limit=40)
            for msg in messages:
                if msg.id not in baseline_audio_ids and is_audio_message(msg):
                    return msg

            for msg in messages:
                text = (getattr(msg, "message", "") or "").strip()
                if text:
                    latest_text = text
                    break

            item.status = "waiting"
            item.detail = (
                f"Match selected. Waiting for audio… Bot says: {latest_text[:140]}"
                if latest_text else
                "Match selected. Waiting for the audio file to finish loading…"
            )
            await asyncio.sleep(POLL_INTERVAL)

        return None

    async def process_song(self, item: SongProgress):
        if not self.client or not self.target_bot:
            raise RuntimeError("Telegram is not connected.")

        item.status = "searching"
        item.detail = "Sending search to bot…"
        self.current = item.song

        try:
            sent = await self.client.send_message(self.target_bot, item.song)
            response, bot_text, explicit_not_found = await self._wait_for_buttons(sent.id, item)

            if response is None:
                if self.stop_requested:
                    item.status = "stopped"
                    item.detail = "Stopped by user."
                elif explicit_not_found:
                    item.status = "not_found"
                    item.detail = f"Bot reported no result. {bot_text[:160]}".strip()
                else:
                    item.status = "failed"
                    item.detail = f"No result buttons appeared within {SEARCH_TIMEOUT} seconds."
                return item.status

            # V3 Final lets the user choose how a result is selected:
            # - first_result: trust the bot's own ranking and click its first song result
            # - best_match: click our highest-scoring text match, even if confidence is weak
            # - strict_match: click the highest-scoring result only when it passes strict checks
            if self.selection_mode == "first_result":
                selected = first_button(response.buttons, item.song)
            else:
                selected = best_button(response.buttons, item.song)

            if selected is None:
                item.status = "not_found"
                item.detail = "The bot returned buttons, but none looked like a song result."
                return item.status

            item.matched_label = selected["label"]
            item.match_score = selected["score"]

            if self.selection_mode == "strict_match" and not selected["accepted"]:
                item.status = "not_found"
                item.detail = (
                    f"Skipped weak match: '{selected['label']}' — confidence {selected['score']:.0%}, "
                    f"query coverage {selected['recall']:.0%}."
                )
                return item.status

            # Snapshot existing audio so a previous track cannot be mistaken for this one.
            before = await self._recent_messages(limit=40)
            baseline_audio_ids = {m.id for m in before if is_audio_message(m)}

            item.status = "selecting"
            if self.selection_mode == "first_result":
                item.detail = f"Using bot's first result: {selected['label']}. Clicking…"
            elif self.selection_mode == "best_match":
                item.detail = f"Using best text match: {selected['label']} ({selected['score']:.0%}). Clicking…"
            else:
                item.detail = f"Strict match accepted: {selected['label']} ({selected['score']:.0%}). Clicking…"
            await response.click(i=selected["row"], j=selected["column"])

            item.status = "waiting"
            item.detail = "Result clicked. Waiting for the bot to prepare the audio…"
            audio_msg = await self._wait_for_audio(baseline_audio_ids, item)

            if audio_msg is None:
                if self.stop_requested:
                    item.status = "stopped"
                    item.detail = "Stopped by user."
                else:
                    item.status = "failed"
                    item.detail = f"No audio file became available within {DOWNLOAD_TIMEOUT} seconds."
                return item.status

            item.status = "downloading"
            item.detail = "Audio is ready. Downloading to your Mac…"
            DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
            path = await audio_msg.download_media(file=str(DOWNLOAD_DIR) + os.sep)

            if not path:
                item.status = "failed"
                item.detail = "Telegram returned audio, but saving the file failed."
                return item.status

            item.saved_path = str(path)
            item.status = "done"
            mode_label = {
                "first_result": "bot's first result",
                "best_match": "best text match",
                "strict_match": "strict match",
            }.get(self.selection_mode, self.selection_mode)
            item.detail = f"Downloaded successfully using {mode_label}."
            return item.status

        except FloodWaitError as e:
            item.status = "waiting"
            item.detail = f"Telegram rate limit: waiting {e.seconds}s before retrying."
            await asyncio.sleep(e.seconds)
            if self.stop_requested:
                item.status = "stopped"
                item.detail = "Stopped by user."
                return item.status
            return await self.process_song(item)
        except Exception as e:
            item.status = "failed"
            item.detail = f"{type(e).__name__}: {e}"
            return item.status

    def record_finished_item(self, item: SongProgress):
        try:
            item.history_id = add_history(item, self.job_id, self.target_bot or "")
        except Exception as exc:
            self.last_error = f"History error: {type(exc).__name__}: {exc}"

    async def run(self, songs: list[str]):
        self.running = True
        self.stop_requested = False
        self.items = [SongProgress(song=s) for s in songs]
        self.downloaded = 0
        self.not_found = 0
        self.failed = 0
        self.stopped = 0
        self.current = ""
        self.last_error = ""
        self.job_id = uuid.uuid4().hex

        try:
            for item in self.items:
                if self.stop_requested:
                    item.status = "stopped"
                    item.detail = "Stopped by user."
                    self.stopped += 1
                    self.record_finished_item(item)
                    continue

                status = await self.process_song(item)
                if status == "done":
                    self.downloaded += 1
                elif status == "not_found":
                    self.not_found += 1
                elif status == "stopped":
                    self.stopped += 1
                else:
                    self.failed += 1

                self.record_finished_item(item)

                if not self.stop_requested:
                    await asyncio.sleep(DELAY_BETWEEN_SONGS)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
        finally:
            self.current = ""
            self.running = False

    def start(self, songs: list[str], selection_mode: str = "first_result"):
        if self.running:
            raise RuntimeError("A download job is already running.")
        allowed_modes = {"first_result", "best_match", "strict_match"}
        if selection_mode not in allowed_modes:
            raise RuntimeError("Unknown result selection mode.")
        self.selection_mode = selection_mode
        self._task = asyncio.create_task(self.run(songs))

    def status(self):
        total = len(self.items)
        finished = self.downloaded + self.not_found + self.failed + self.stopped
        percent = round((finished / total) * 100, 1) if total else 0
        return {
            "authorized": self.authorized,
            "target_bot": self.target_bot,
            "selection_mode": self.selection_mode,
            "running": self.running,
            "stop_requested": self.stop_requested,
            "current": self.current,
            "downloaded": self.downloaded,
            "not_found": self.not_found,
            "failed": self.failed,
            "stopped": self.stopped,
            "finished": finished,
            "total": total,
            "percent": percent,
            "last_error": self.last_error,
            "download_dir": str(DOWNLOAD_DIR),
            "items": [asdict(x) for x in self.items],
        }


init_db()
manager = DownloaderManager()


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def page(name: str):
    path = STATIC_DIR / name
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/")
async def root():
    return RedirectResponse("/downloader" if manager.authorized else "/login")


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return page("login.html")


@app.get("/downloader", response_class=HTMLResponse)
async def downloader_page():
    return page("downloader.html")


@app.get("/history", response_class=HTMLResponse)
async def history_page():
    return page("history.html")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.post("/api/connect")
async def api_connect(req: ConnectRequest):
    try:
        _dbg_emit(
            hypothesis_id="A",
            msg="/api/connect hit",
            location="app.py:/api/connect",
            data={
                "api_id": req.api_id,
                "api_hash_len": len(req.api_hash or ""),
                "phone_mask": ("…" + req.phone.strip()[-2:]) if req.phone else None,
                "target_bot": req.target_bot,
            },
        )
        return await manager.connect(req.api_id, req.api_hash, req.phone, req.target_bot)
    except Exception as e:
        _dbg_emit(
            hypothesis_id="C",
            msg="/api/connect error",
            location="app.py:/api/connect",
            data={"error_type": type(e).__name__, "error": str(e)[:240]},
        )
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")


@app.post("/api/verify")
async def api_verify(req: VerifyRequest):
    try:
        return await manager.verify_code(req.code)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")


@app.post("/api/password")
async def api_password(req: PasswordRequest):
    try:
        return await manager.verify_password(req.password)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")

@app.post("/api/resend-code")
async def api_resend_code():
    try:
        _dbg_emit(
            hypothesis_id="A",
            msg="/api/resend-code hit",
            location="app.py:/api/resend-code",
            data={"authorized": manager.authorized},
        )
        return await manager.resend_login_code()
    except Exception as e:
        _dbg_emit(
            hypothesis_id="C",
            msg="/api/resend-code error",
            location="app.py:/api/resend-code",
            data={"error_type": type(e).__name__, "error": str(e)[:240]},
        )
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")


@app.post("/api/forget-session")
async def api_forget_session():
    try:
        return await manager.forget_local_session()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")



@app.post("/api/logout")
async def api_logout():
    if manager.running:
        raise HTTPException(status_code=400, detail="Stop the current download job first.")
    await manager.reset_client()
    return {"ok": True}


@app.post("/api/start")
async def api_start(req: StartRequest):
    if not manager.authorized:
        raise HTTPException(status_code=401, detail="Sign in to Telegram first.")
    songs = [line.strip() for line in req.songs.splitlines() if line.strip()]
    if not songs:
        raise HTTPException(status_code=400, detail="Paste at least one song.")
    if len(songs) > 5000:
        raise HTTPException(status_code=400, detail="Please use batches of 5,000 songs or fewer.")
    try:
        manager.start(songs, req.selection_mode)
        return {"ok": True, "count": len(songs), "selection_mode": req.selection_mode}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/stop")
async def api_stop():
    manager.stop_requested = True
    return {"ok": True}


@app.get("/api/status")
async def api_status():
    return JSONResponse(manager.status())


@app.get("/api/current-not-found.txt")
async def current_not_found():
    lines = [item.song for item in manager.items if item.status == "not_found"]
    return PlainTextResponse("\n".join(lines) + ("\n" if lines else ""), headers={
        "Content-Disposition": 'attachment; filename="not_found_songs.txt"'
    })


@app.post("/api/open-downloads")
async def open_downloads():
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.Popen(["open", str(DOWNLOAD_DIR)])
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not open Downloads folder: {e}")


@app.get("/api/history")
async def api_history(
    status: str = Query("all", pattern="^(all|done|not_found|failed|stopped)$"),
    q: str = "",
    limit: int = Query(500, ge=1, le=2000),
):
    rows, counts = query_history(status=status, q=q.strip(), limit=limit)
    for row in rows:
        row["file_exists"] = bool(row.get("saved_path") and Path(row["saved_path"]).exists())
    return {"items": rows, "counts": counts}


@app.delete("/api/history")
async def clear_history():
    if manager.running:
        raise HTTPException(status_code=400, detail="Wait for the current job to finish before clearing history.")
    with db_connect() as conn:
        conn.execute("DELETE FROM history")
    return {"ok": True}


@app.get("/api/history/{history_id}/file")
async def history_file(history_id: int):
    with db_connect() as conn:
        row = conn.execute("SELECT saved_path FROM history WHERE id = ?", (history_id,)).fetchone()
    if not row or not row["saved_path"]:
        raise HTTPException(status_code=404, detail="No downloaded file is attached to this history item.")
    path = Path(row["saved_path"]).resolve()
    downloads = DOWNLOAD_DIR.resolve()
    try:
        path.relative_to(downloads)
    except ValueError:
        raise HTTPException(status_code=403, detail="File is outside the downloads folder.")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="The file is no longer present on disk.")
    return FileResponse(str(path), filename=path.name)


@app.get("/api/history-not-found.txt")
async def history_not_found():
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT requested_song FROM history WHERE status='not_found' ORDER BY id DESC"
        ).fetchall()
    lines = [r["requested_song"] for r in rows]
    return PlainTextResponse("\n".join(lines) + ("\n" if lines else ""), headers={
        "Content-Disposition": 'attachment; filename="history_not_found_songs.txt"'
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)
