from functools import partial
from html import escape
from secrets import token_urlsafe

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .alarms import ALARM_DURATION_MINUTES, ALERT_MINUTES, AlarmService
from .eta import estimate_arrival
from .tago import TagoClient


class BotHandlers:
    def __init__(self, tago: TagoClient, alarms: AlarmService):
        self.tago = tago
        self.alarms = alarms

    def register(self, application):
        application.add_handlers([
            CommandHandler("start", self.start),
            CommandHandler("gps", self.request_location),
            CommandHandler("unalarm", self.cancel_alarm),
            *[
                CommandHandler(mode, partial(self.search_stop, mode=mode))
                for mode in ("stop", "alarm")
            ],
            *[
                CommandHandler(mode, partial(self.search_route, mode=mode))
                for mode in ("bus", "route", "eta")
            ],
            CallbackQueryHandler(self.handle_button),
            MessageHandler(filters.LOCATION, self.handle_location),
        ])

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_text(
            "버스 도착 정보 알리미입니다.\n\n"
            "/stop [정류장] — 도착 예정 버스와 정류장 위치\n"
            "/gps — 현재 위치 주변 정류장\n"
            "/bus [번호] — 운행 중인 버스 위치\n"
            "/route [번호] — 노선의 경유 정류장과 운행 위치\n"
            "/alarm [정류장] — 도착 20·10·5분 전 알림 등록\n"
            "/unalarm — 현재 채팅의 알림 해제\n"
            "/eta [번호] — 선택한 정류장의 과거 관측 기반 도착 안내"
        )

    async def search_stop(self, update, context, *, mode):
        message = update.effective_message
        name = " ".join(context.args or [])
        if not name:
            await message.reply_text(f"정류소 이름을 입력해주세요. (예: /{mode} 금오공대)")
            return

        await message.reply_text("검색 중...", reply_markup=ReplyKeyboardRemove())
        stops = await self.tago.stops_by_name(name)
        await self.show_stop_options(message, context, stops, mode)

    async def request_location(self, update, context):
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton("내 위치 보내기", request_location=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await update.effective_message.reply_text("위치를 보내주세요.", reply_markup=keyboard)

    async def handle_location(self, update, context):
        message = update.effective_message
        location = message.location
        await message.reply_text("위치 확인 중...", reply_markup=ReplyKeyboardRemove())
        stops = await self.tago.nearby_stops(location.latitude, location.longitude)
        await self.show_stop_options(message, context, stops, "stop")

    async def show_stop_options(self, message, context, stops, mode):
        if not stops:
            await message.reply_text("정류장을 찾을 수 없습니다.")
            return
        if len(stops) == 1:
            await self.select_stop(context, message.chat_id, mode, stops[0].node_id)
            return

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(stop.name, callback_data=f"{mode.upper()}_{stop.node_id}")]
            for stop in stops
        ])
        await message.reply_text("여러 정류장이 검색되었습니다. 정류장을 선택해주세요.",
                                 reply_markup=keyboard)

    async def select_stop(self, context, chat_id, mode, node_id):
        context.user_data["selected_node_id"] = node_id
        if mode == "alarm":
            await self.show_alarm_options(context, chat_id, node_id)
            return

        arrivals = await self.tago.arrivals(node_id)
        if not arrivals:
            await context.bot.send_message(chat_id=chat_id, text="도착 정보가 없습니다.")
            return

        stop_name = arrivals[0].stop_name
        lines = [f"🚌 {stop_name} 도착 정보", ""]
        for arrival in arrivals:
            remaining = (
                "1분 미만" if arrival.remaining_seconds < 60
                else f"{arrival.remaining_seconds // 60}분 후"
            )
            lines.append(
                f"{arrival.route_no:<5} ({arrival.route_type or '유형 미상'}) - "
                f"{arrival.remaining_stops:>2}정류장 전, {remaining}"
            )

        await self.send_list(context.bot, chat_id, lines)
        stops = await self.tago.stops_by_name(stop_name)
        stop = next((stop for stop in stops if stop.node_id == node_id), None)
        if stop and stop.latitude is not None and stop.longitude is not None:
            await context.bot.send_location(
                chat_id=chat_id, latitude=stop.latitude, longitude=stop.longitude,
            )

    async def search_route(self, update, context, *, mode):
        message = update.effective_message
        if not context.args:
            await message.reply_text(f"버스 번호를 입력해주세요. (예: /{mode} 57)")
            return

        await message.reply_text("검색 중...", reply_markup=ReplyKeyboardRemove())
        routes = await self.tago.routes_by_number(context.args[0])
        if not routes:
            await message.reply_text("해당 버스를 찾을 수 없습니다.")
            return
        if len(routes) == 1:
            await self.select_route(context, message.chat_id, mode, routes[0].route_id)
            return

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{route.number} ({route.start} → {route.end})",
                                  callback_data=f"{mode.upper()}_{route.route_id}")]
            for route in routes
        ])
        await message.reply_text("여러 노선이 검색되었습니다. 노선을 선택해주세요.",
                                 reply_markup=keyboard)

    async def select_route(self, context, chat_id, mode, route_id):
        bot = context.bot
        if mode == "eta":
            node_id = context.user_data.get("selected_node_id")
            text = await estimate_arrival(node_id, route_id)
            await bot.send_message(chat_id=chat_id, text=text)
            return

        if mode == "route":
            stops = await self.tago.route_stops(route_id)
            if stops:
                lines = [f"{stop.order:>2}. {stop.name}" for stop in stops]
                await self.send_list(bot, chat_id, ["🚌 경유 정류장 전체 목록", "", *lines])
            else:
                await bot.send_message(chat_id=chat_id, text="경유 정류장이 없습니다.")

        positions = await self.tago.bus_positions(route_id)
        if not positions:
            await bot.send_message(chat_id=chat_id, text="현재 버스 위치 정보가 없습니다.")
            return

        lines = [f"[차량번호:{bus.vehicle_no}] → {bus.stop_name}" for bus in positions]
        await self.send_list(bot, chat_id, ["🚌 현재 운행 위치", "", *lines])

    async def show_alarm_options(self, context, chat_id, node_id):
        context.chat_data.pop("alarm_options", None)
        arrivals = await self.tago.arrivals(node_id)
        if not arrivals:
            await context.bot.send_message(chat_id=chat_id, text="알림을 설정할 도착 정보가 없습니다.")
            return

        options = {token_urlsafe(8): arrival for arrival in arrivals}
        context.chat_data["alarm_options"] = options
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"{arrival.route_no}번 - {arrival.remaining_stops}정류장 전, "
                f"{arrival.remaining_seconds // 60}분",
                callback_data=f"SETALARM_{token}",
            )]
            for token, arrival in options.items()
        ])
        await context.bot.send_message(
            chat_id=chat_id, text="알림을 설정할 버스를 선택하세요.", reply_markup=keyboard,
        )

    async def set_alarm(self, context, chat_id, route_id, route_no, node_id, stop_name):
        self.alarms.register(chat_id, route_id, route_no, node_id, stop_name)
        minutes = "·".join(map(str, ALERT_MINUTES))
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🔔 알람 설정 완료!\n노선: {route_no}번\n정류장: {stop_name}\n"
                f"{ALARM_DURATION_MINUTES}분 동안 유효하며, {minutes}분 전에 알림이 옵니다."
            ),
        )

    async def cancel_alarm(self, update, context):
        message = update.effective_message
        self.alarms.remove(message.chat_id)
        context.chat_data.pop("alarm_options", None)
        await message.reply_text("🔕 알림을 해제했습니다.", reply_markup=ReplyKeyboardRemove())

    async def handle_button(self, update, context):
        query = update.callback_query
        await query.answer()
        if not isinstance(query.data, str) or query.message is None:
            return

        chat_id = query.message.chat_id
        mode, _, value = query.data.partition("_")
        if not value:
            return
        if mode in ("STOP", "ALARM"):
            await self.select_stop(context, chat_id, mode.lower(), value)
        elif mode in ("BUS", "ROUTE", "ETA"):
            await self.select_route(context, chat_id, mode.lower(), value)
        elif mode == "SETALARM":
            arrival = context.chat_data.get("alarm_options", {}).get(value)
            if arrival is None:
                await context.bot.send_message(
                    chat_id=chat_id, text="선택 목록이 만료되었습니다. /alarm으로 다시 검색해주세요.",
                )
                return
            context.chat_data.pop("alarm_options", None)
            await self.set_alarm(
                context, chat_id, arrival.route_id, arrival.route_no,
                arrival.node_id, arrival.stop_name,
            )
        elif mode == "ALARMDB":
            parts = value.split("_", maxsplit=3)
            if len(parts) == 4:
                await self.set_alarm(context, chat_id, *parts)

    @staticmethod
    async def send_list(bot, chat_id, lines):
        await bot.send_message(
            chat_id=chat_id, text="<pre>" + escape("\n".join(lines)) + "</pre>", parse_mode="HTML",
        )
