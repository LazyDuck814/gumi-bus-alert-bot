from pathlib import Path

ARRIVALS_DB = Path("bus_arrivals.db")


def day_type(timestamp):
    return "weekday" if timestamp.weekday() < 5 else "weekend"
