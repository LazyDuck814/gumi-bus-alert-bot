import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from statistics import median

from google import genai

from .observations import ARRIVALS_DB, day_type

LOGGER = logging.getLogger(__name__)
GEMINI_MODEL = "gemini-2.5-flash"
HISTORY_DAYS = 7
WINDOW_MINUTES = 20


async def estimate_arrival(node_id, route_id, *, db_path=ARRIVALS_DB):
    if not node_id or not route_id:
        return "먼저 /stop 또는 /gps로 정류장을 선택한 뒤 /eta를 실행해주세요."

    now = datetime.now()
    try:
        rows = _recent_observations(db_path, node_id, route_id, now)
        times = [datetime.fromisoformat(row[2]) for row in rows]
    except (sqlite3.Error, ValueError) as error:
        LOGGER.warning("ETA 관측 조회 실패 (%s).", type(error).__name__)
        return "ETA 계산 중 DB 조회 오류가 발생했습니다."

    if not times:
        return "최근 1주일 동안 이 정류장과 노선의 해당 시간대 운행 기록이 없습니다."

    stop_name, route_no, _ = rows[0]
    day_label = "평일" if day_type(now) == "weekday" else "주말"
    time_list = ", ".join(timestamp.strftime("%H:%M") for timestamp in times)
    prompt = (
        f"{stop_name} 정류장, {route_no}번 버스의 최근 1주일 {day_label} 관측입니다.\n"
        f"현재 시각: {now:%H:%M}\n"
        f"도착 임박 응답을 수집한 시각 {len(times)}건: {time_list}\n"
        "관측 시각의 범위와 대표 시각(중앙값 또는 평균)을 계산해 한국어 존댓말로 안내해주세요.\n"
        "표본 수가 적거나 편차가 크면 참고용임을 설명하고, 실제 도착 시각으로 단정하지 마세요."
    )

    try:
        async with genai.Client().aio as client:
            response = await client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        if response.text and response.text.strip():
            return response.text
    except Exception as error:
        LOGGER.warning("Gemini ETA 생성 실패 (%s).", type(error).__name__)

    minutes = [timestamp.hour * 60 + timestamp.minute for timestamp in times]
    return (
        f"🚍 {route_no}번 버스 분석 ({day_label})\n\n"
        f"1. 가장 빠른 관측 시각: {_format_minutes(min(minutes))}\n"
        f"2. 가장 늦은 관측 시각: {_format_minutes(max(minutes))}\n"
        f"3. 참고용 도착 시각: 약 {_format_minutes(int(median(minutes)))} (관측 중앙값)\n\n"
        "※ Gemini 응답을 사용할 수 없어 관측 시각의 통계를 표시합니다."
    )


def _recent_observations(db_path, node_id, route_id, now):
    if not db_path.is_file():
        return []

    minute = now.hour * 60 + now.minute
    since = now - timedelta(days=HISTORY_DAYS)
    with closing(sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        return db.execute("""
            SELECT node_name, bus_no, collected_at
            FROM arrivals
            WHERE node_id = ? AND route_id = ? AND day_type = ?
                AND datetime(collected_at) BETWEEN datetime(?) AND datetime(?)
                AND (
                    CAST(strftime('%H', collected_at) AS INTEGER) * 60
                    + CAST(strftime('%M', collected_at) AS INTEGER)
                ) BETWEEN ? AND ?
            ORDER BY collected_at
        """, (
            node_id, route_id, day_type(now),
            since.isoformat(), now.isoformat(), minute, minute + WINDOW_MINUTES,
        )).fetchall()


def _format_minutes(minutes):
    hour, minute = divmod(minutes, 60)
    return f"{hour:02d}:{minute:02d}"
