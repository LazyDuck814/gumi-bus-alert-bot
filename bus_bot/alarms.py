import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from telegram.error import TelegramError
from .tago import TagoClient

LOGGER = logging.getLogger(__name__)
ALARM_DB = Path("alarms.db")
CHECK_INTERVAL_SECONDS = 30
ALARM_DURATION_MINUTES = 30
ALERT_MINUTES = (20, 10, 5)
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class Alarm:
    chat_id: int
    route_id: str
    route_no: str
    node_id: str
    expires_at: datetime


@dataclass
class AlarmProgress:
    alarm: Alarm
    remaining_seconds: int | None = None
    sent_minutes: set[int] = field(default_factory=set)


class AlarmService:
    def __init__(self, tago: TagoClient, db_path=ALARM_DB):
        self.tago = tago
        self.db_path = db_path
        self._progress: dict[int, AlarmProgress] = {}

    def initialize(self):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS alarms (
                    chat_id INTEGER,
                    route_id TEXT,
                    route_no TEXT,
                    node_id TEXT,
                    node_nm TEXT,
                    created_at TEXT,
                    expires_at TEXT
                )
            """)

    def register(self, chat_id, route_id, route_no, node_id, stop_name):
        now = datetime.now().replace(microsecond=0)
        expires_at = now + timedelta(minutes=ALARM_DURATION_MINUTES)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("DELETE FROM alarms WHERE chat_id = ?", (chat_id,))
            db.execute("""
                INSERT INTO alarms (
                    chat_id, route_id, route_no, node_id, node_nm, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                chat_id, route_id, route_no, node_id, stop_name,
                now.strftime(TIMESTAMP_FORMAT), expires_at.strftime(TIMESTAMP_FORMAT),
            ))
        alarm = Alarm(chat_id, route_id, route_no, node_id, expires_at)
        self._progress[chat_id] = AlarmProgress(alarm)

    def remove(self, chat_id):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("DELETE FROM alarms WHERE chat_id = ?", (chat_id,))
        self._progress.pop(chat_id, None)

    def _load_active_alarms(self, now):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("DELETE FROM alarms WHERE expires_at <= ?", (now.strftime(TIMESTAMP_FORMAT),))
            rows = db.execute("""
                SELECT chat_id, route_id, route_no, node_id, expires_at FROM alarms
            """).fetchall()

        return [
            Alarm(chat_id, route_id, route_no, node_id, datetime.strptime(expires_at, TIMESTAMP_FORMAT))
            for chat_id, route_id, route_no, node_id, expires_at in rows
        ]

    async def check(self, context):
        alarms = self._load_active_alarms(datetime.now())
        active = {}
        for alarm in alarms:
            progress = self._progress.get(alarm.chat_id)
            if progress is None or progress.alarm != alarm:
                progress = AlarmProgress(alarm)
            active[alarm.chat_id] = progress
        self._progress = active.copy()

        by_stop = {}
        for chat_id, progress in active.items():
            if self._progress.get(chat_id) is not progress:
                continue
            alarm = progress.alarm
            if alarm.node_id not in by_stop:
                by_stop[alarm.node_id] = await self.tago.arrivals(alarm.node_id)

            target = next(
                (arrival for arrival in by_stop[alarm.node_id] if arrival.route_id == alarm.route_id),
                None,
            )
            if target is not None:
                await self._notify(context.bot, progress, target.remaining_seconds)

    async def _notify(self, bot, progress, current_seconds):
        alarm = progress.alarm
        previous_seconds = progress.remaining_seconds

        for minutes in ALERT_MINUTES:
            if (self._progress.get(alarm.chat_id) is not progress
                    or datetime.now() >= alarm.expires_at):
                return
            if (previous_seconds is None or minutes in progress.sent_minutes
                    or not previous_seconds > minutes * 60 >= current_seconds):
                continue

            try:
                await bot.send_message(
                    chat_id=alarm.chat_id,
                    text=f"🔔 {alarm.route_no}번 버스가 약 {minutes}분 후 도착합니다!",
                )
            except TelegramError as error:
                LOGGER.warning("텔레그램 알림 전송 실패 (%s).", type(error).__name__)
                return
            progress.sent_minutes.add(minutes)

        progress.remaining_seconds = current_seconds
