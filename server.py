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

server = None
lastest_lidar_date = datetime.datetime.now(ZoneInfo("Asia/Tokyo"))
touched_visitors_id = set()
lastest_received_visitors_id = set()


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
            for visitor in loaded["visitors_being_touched"]:
                assert type(visitor) is int
        except:
            await connection.close(code=1008, reason="Invalid Message")
            return

        connection.user_x = loaded["x"]
        connection.user_y = loaded["y"]
        for visitor in loaded["visitors_being_touched"]:
            if visitor in (lastest_received_visitors_id - touched_visitors_id):
                connection.user_score += 1
                touched_visitors_id.add(visitor)

        await asyncio.sleep(0.01)


async def broadcast_json(lidar2person_queue):
    global lastest_lidar_date
    global touched_visitors_id
    global lastest_received_visitors_id
    while True:
        try:
            lidar2person = lidar2person_queue.get(False)
        except queue.Empty:
            await asyncio.sleep(0.01)
            continue

        loaded = json.loads(lidar2person)

        now_lidar_date = datetime.datetime.fromisoformat(loaded["latest_timestamp"])
        if now_lidar_date < lastest_lidar_date:
            touched_visitors_id = set()
        lastest_lidar_date = now_lidar_date

        lastest_received_visitors_id = {x["id"] for x in loaded["objects"]}

        visitors = [
            {
                "id": x["id"],
                "x": (x["position"]["lon"] - origin_lon) / meter_per_1lon,
                "y": (x["position"]["lat"] - origin_lat) / meter_per_1lat,
            }
            for x in loaded["objects"]
        ]
        untouched_visitors = [x for x in visitors if x["id"] not in touched_visitors_id]
        touched_visitors = [x for x in visitors if x["id"] in touched_visitors_id]
        players = [
            {"name": x.user_name, "x": x.user_x, "y": x.user_y, "score": x.user_score}
            for x in server.connections
        ]
        sending_data = {
            "lidar_time": loaded["latest_timestamp"],
            "untouched_visitors": untouched_visitors,
            "touched_visitors": touched_visitors,
            "players": players,
        }
        broadcast(server.connections, json.dumps(sending_data))

        await asyncio.sleep(0.001)


def process_request(connection, request):
    if request.path == "/healthz":
        return connection.respond(http.HTTPStatus.OK, "OK\n")

    try:
        assert request.headers["digitaltwin-user-name"] != ""
        for c in server.connections:
            assert c.user_name != request.headers["digitaltwin-user-name"]
        connection.user_name = request.headers["digitaltwin-user-name"]
        connection.user_x = float(request.headers["digitaltwin-user-x"])
        connection.user_y = float(request.headers["digitaltwin-user-y"])
    except:
        return connection.respond(http.HTTPStatus.BAD_REQUEST, "Invalid Request\n")

    connection.user_score = 0


async def websocket_async(lidar2person_queue):
    global server
    async with serve(handler, port=8000, process_request=process_request) as s:
        server = s
        await broadcast_json(lidar2person_queue)


def main(lidar2person_queue):
    asyncio.run(websocket_async(lidar2person_queue))
