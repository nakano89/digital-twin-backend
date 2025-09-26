import json
import datetime
from zoneinfo import ZoneInfo
import queue
import asyncio
import http
from websockets.asyncio.server import broadcast, serve

# 福沢先生を原点とする
origin_lat = 35.388469
origin_lon = 139.426989

# SFCでの1メートルあたりの緯度・経度
meter_per_1lat = 0.00000901325
meter_per_1lon = 0.0000110065


class Visitors:
    _visitors_hp = {}
    _existing_visitors_id = []

    @classmethod
    def update_existing_visitors(cls, ids_):
        cls._existing_visitors_id = ids_

    @classmethod
    def get_hp(cls, id_):
        if id_ not in cls._visitors_hp:
            cls._visitors_hp[id_] = 100

        return cls._visitors_hp[id_]

    @classmethod
    async def try_decrease_hp(cls, id_, try_decrease):
        if id_ not in cls._visitors_hp:
            cls._visitors_hp[id_] = 100

        if id_ not in cls._existing_visitors_id:
            return 0

        if cls._visitors_hp[id_] <= try_decrease:
            decrease = cls._visitors_hp[id_]
            cls._visitors_hp[id_] = 0

            async def revival(id_):
                await asyncio.sleep(60 * 3)
                del Visitors._visitors_hp[id_]

            await asyncio.create_task(revival(id_))
            return decrease
        else:
            cls._visitors_hp[id_] -= try_decrease
            return try_decrease


class Players:
    server = None

    @classmethod
    def get_all_players_data(cls):
        return [
            {
                "type": c.user_type,
                "name": c.user_name,
                "score": c.user_score,
                "hp": c.user_hp,
                "x": c.user_x,
                "y": c.user_y,
                "angle": c.user_angle,
                "speed": c.user_speed,
            }
            for c in cls.server.connections
        ]

    @classmethod
    def init(cls, connection, name, type_):
        assert name != ""
        for c in cls.server.connections:
            assert name != c.user_name
        connection.user_name = name
        connection.user_type = type_
        connection.user_score = 0
        connection.user_hp = 100

    @classmethod
    def get_hp(cls, connection):
        return connection.user_hp

    @classmethod
    def set_property(cls, connection, x, y, angle, speed):
        connection.user_x = x
        connection.user_y = y
        connection.user_angle = angle
        connection.user_speed = speed

    @classmethod
    async def try_decrease_hp(cls, name, try_decrease):
        for c in cls.server.connections:
            if c.user_name == name:
                if c.user_hp <= try_decrease:
                    decrease = c.user_hp
                    c.user_hp = 0

                    async def revival(name):
                        await asyncio.sleep(60 * 3)
                        for c in cls.server.connections:
                            if c.user_name == name:
                                c.user_hp = 100
                                break

                    await asyncio.create_task(revival(name))
                    return decrease
                else:
                    c.user_hp -= try_decrease
                    return try_decrease

    @classmethod
    def increase_score(cls, connection, increase):
        connection.user_score += increase


async def handler(connection):
    while True:
        try:
            received = await connection.recv()
        except:
            return

        try:
            loaded = json.loads(received)
            assert type(loaded["x"]) in (int, float)
            assert type(loaded["y"]) in (int, float)
            assert type(loaded["angle"]) in (int, float)
            assert type(loaded["speed"]) in (int, float)
            for visitor_id, try_hp_decrease in loaded["decreased_visitors_hp"].items():
                assert type(visitor_id) is str
                assert type(try_hp_decrease) is int
            for player_name, try_hp_decrease in loaded["decreased_players_hp"].items():
                assert type(player_name) is str
                assert type(try_hp_decrease) is int
        except:
            await connection.close(code=1008, reason="Invalid Message")
            return

        Players.set_property(
            connection, loaded["x"], loaded["y"], loaded["angle"], loaded["speed"]
        )

        if Players.get_hp(connection) == 0:
            return

        for visitor_id, try_hp_decrease in loaded["decreased_visitors_hp"].items():
            hp_decrease = await Visitors.try_decrease_hp(visitor_id, try_hp_decrease)
            Players.increase_score(connection, hp_decrease)
        for player_name, try_hp_decrease in loaded["decreased_players_hp"].items():
            hp_decrease = await Players.try_decrease_hp(player_name, try_hp_decrease)
            Players.increase_score(connection, hp_decrease)

        await asyncio.sleep(0.01)


async def broadcast_json(lidar2person_queue):
    while True:
        try:
            lidar2person = lidar2person_queue.get(False)
        except queue.Empty:
            await asyncio.sleep(0.01)
            continue

        loaded = json.loads(lidar2person)

        Visitors.update_existing_visitors([str(o["id"]) for o in loaded["objects"]])

        visitors = [
            {
                "id": str(o["id"]),
                "hp": Visitors.get_hp(str(o["id"])),
                "x": (o["position"]["lon"] - origin_lon) / meter_per_1lon,
                "y": (o["position"]["lat"] - origin_lat) / meter_per_1lat,
                "angle": o["heading"],
                "speed": o["horizontal_speed"],
            }
            for o in loaded["objects"]
        ]
        players = Players.get_all_players_data()
        sending_data = {
            "lidar_time": loaded["latest_timestamp"],
            "visitors": visitors,
            "players": players,
        }
        broadcast(Players.server.connections, json.dumps(sending_data))

        await asyncio.sleep(0.001)


def process_request(connection, request):
    if request.path == "/healthz":
        return connection.respond(http.HTTPStatus.OK, "OK\n")

    try:
        Players.init(
            connection,
            request.headers["digitaltwin-user-name"],
            request.headers["digitaltwin-user-type"],
        )
        Players.set_property(
            connection,
            float(request.headers["digitaltwin-user-x"]),
            float(request.headers["digitaltwin-user-y"]),
            float(request.headers["digitaltwin-user-angle"]),
            float(request.headers["digitaltwin-user-speed"]),
        )
    except:
        return connection.respond(http.HTTPStatus.BAD_REQUEST, "Invalid Request\n")


async def websocket_async(lidar2person_queue):
    async with serve(handler, port=8000, process_request=process_request) as server:
        Players.server = server
        await broadcast_json(lidar2person_queue)


def main(lidar2person_queue):
    asyncio.run(websocket_async(lidar2person_queue))
