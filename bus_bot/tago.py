import asyncio
import logging
from dataclasses import dataclass

import aiohttp

GUMI_CITY_CODE = 37050
SERVICE_ROOT = "https://apis.data.go.kr/1613000"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Stop:
    node_id: str
    name: str
    latitude: float | None
    longitude: float | None


@dataclass(frozen=True)
class Arrival:
    node_id: str
    stop_name: str
    route_id: str
    route_no: str
    route_type: str | None
    vehicle_type: str | None
    remaining_stops: int
    remaining_seconds: int


@dataclass(frozen=True)
class Route:
    route_id: str
    number: str
    start: str
    end: str


@dataclass(frozen=True)
class RouteStop:
    order: int
    name: str


@dataclass(frozen=True)
class BusPosition:
    vehicle_no: str
    stop_name: str


class TagoClient:
    def __init__(self, *, stop_key=None, arrival_key=None, route_key=None, location_key=None):
        self.stop_key = stop_key
        self.arrival_key = arrival_key
        self.route_key = route_key
        self.location_key = location_key

    async def _fetch(self, service, operation, api_key, **params):
        if not api_key:
            LOGGER.warning("TAGO 인증키가 설정되지 않았습니다 (%s).", operation)
            return []

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"{SERVICE_ROOT}/{service}/{operation}",
                    params={"serviceKey": api_key, "_type": "json", **params},
                ) as response:
                    response.raise_for_status()
                    payload = await response.json()

            envelope = payload.get("response", {})
            result_code = envelope.get("header", {}).get("resultCode")
            if result_code not in (None, "00", "0", 0):
                LOGGER.warning("TAGO가 %s 요청을 거절했습니다 (코드 %s).", operation, result_code)
                return []

            container = envelope.get("body", {}).get("items")
            items = container.get("item") if isinstance(container, dict) else None
            if isinstance(items, dict):
                return [items]
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, AttributeError) as error:
            LOGGER.warning("TAGO %s 요청 실패 (%s).", operation, type(error).__name__)

        return []

    async def stops_by_name(self, name, city_code=GUMI_CITY_CODE):
        items = await self._fetch(
            "BusSttnInfoInqireService", "getSttnNoList", self.stop_key,
            cityCode=city_code, nodeNm=name,
        )
        return _parse_stops(items)

    async def nearby_stops(self, latitude, longitude):
        items = await self._fetch(
            "BusSttnInfoInqireService", "getCrdntPrxmtSttnList", self.stop_key,
            gpsLati=latitude, gpsLong=longitude,
        )
        return _parse_stops(items)

    async def arrivals(self, node_id, city_code=GUMI_CITY_CODE):
        items = await self._fetch(
            "ArvlInfoInqireService", "getSttnAcctoArvlPrearngeInfoList", self.arrival_key,
            cityCode=city_code, nodeId=node_id,
        )
        arrivals = []
        for item in items:
            if not item.get("routeid") or item.get("routeno") is None:
                continue
            try:
                arrivals.append(Arrival(
                    node_id=str(item.get("nodeid") or node_id),
                    stop_name=item.get("nodenm") or "정류장",
                    route_id=str(item["routeid"]),
                    route_no=str(item["routeno"]),
                    route_type=item.get("routetp"),
                    vehicle_type=item.get("vehicletp"),
                    remaining_stops=int(item["arrprevstationcnt"]),
                    remaining_seconds=int(item["arrtime"]),
                ))
            except (KeyError, TypeError, ValueError):
                LOGGER.debug("필수 필드가 누락되거나 잘못된 도착 정보 행을 건너뜁니다.")
        return arrivals

    async def routes_by_number(self, number, city_code=GUMI_CITY_CODE):
        items = await self._fetch(
            "BusRouteInfoInqireService", "getRouteNoList", self.route_key,
            cityCode=city_code, routeNo=number, numOfRows=50,
        )
        return [
            Route(str(item["routeid"]), str(item["routeno"]),
                  item.get("startnodenm") or "기점 미상", item.get("endnodenm") or "종점 미상")
            for item in items if item.get("routeid") and item.get("routeno") is not None
        ]

    async def route_stops(self, route_id, city_code=GUMI_CITY_CODE):
        items = await self._fetch(
            "BusRouteInfoInqireService", "getRouteAcctoThrghSttnList", self.route_key,
            cityCode=city_code, routeId=route_id, numOfRows=100,
        )
        stops = []
        for item in items:
            try:
                stops.append(RouteStop(int(item["nodeord"]), item["nodenm"]))
            except (KeyError, TypeError, ValueError):
                LOGGER.debug("경유 순서를 알 수 없는 정류장 행을 건너뜁니다.")
        return sorted(stops, key=lambda stop: stop.order)

    async def bus_positions(self, route_id, city_code=GUMI_CITY_CODE):
        items = await self._fetch(
            "BusLcInfoInqireService", "getRouteAcctoBusLcList", self.location_key,
            cityCode=city_code, routeId=route_id, numOfRows=10,
        )
        return [
            BusPosition(item.get("vehicleno") or "미상", item["nodenm"])
            for item in items if item.get("nodenm")
        ]


def _parse_stops(items):
    stops = []
    for item in items:
        if not item.get("nodeid") or not item.get("nodenm"):
            continue
        try:
            latitude = float(item["gpslati"])
            longitude = float(item["gpslong"])
        except (KeyError, TypeError, ValueError):
            latitude = longitude = None
        stops.append(Stop(str(item["nodeid"]), item["nodenm"], latitude, longitude))
    return stops
