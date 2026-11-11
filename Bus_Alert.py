import logging
import os
from dotenv import load_dotenv
from telegram.ext import Application
from bus_bot.alarms import CHECK_INTERVAL_SECONDS, AlarmService
from bus_bot.handlers import BotHandlers
from bus_bot.tago import TagoClient


def build_application(token, tago):
    alarms = AlarmService(tago)
    alarms.initialize()

    application = Application.builder().token(token).build()
    BotHandlers(tago, alarms).register(application)
    application.job_queue.run_repeating(alarms.check, interval=CHECK_INTERVAL_SECONDS)
    return application


def main():
    load_dotenv("API.env")
    token = os.getenv("TELEGRAM_BOT_TOKEN_NEW")
    if not token:
        raise SystemExit("API.env에 TELEGRAM_BOT_TOKEN_NEW를 설정해주세요.")

    tago = TagoClient(
        stop_key=os.getenv("bus_stop"),
        arrival_key=os.getenv("bus_arrival"),
        route_key=os.getenv("bus_route"),
        location_key=os.getenv("bus_loc"),
    )
    build_application(token, tago).run_polling()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    main()
